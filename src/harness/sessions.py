"""Versioned, transactional main-session checkpoints and host exclusion."""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4


class SessionError(RuntimeError):
    pass


def plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    raise SessionError(f"Unsupported checkpoint value: {type(value).__name__}")


@dataclass
class SessionSnapshot:
    session_id: str = field(default_factory=lambda: uuid4().hex)
    version: int = 1
    workspace: str = ""
    data: dict = field(default_factory=dict)


class SessionStore:
    def __init__(self, workdir):
        self.root = workdir.resolve()
        self.directory = self.root / ".sessions"

    @contextmanager
    def connect(self):
        if not self.directory.resolve().is_relative_to(self.root):
            raise SessionError("Session directory escapes workspace")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.directory / "sessions.sqlite3", timeout=30)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (id TEXT, generation INTEGER, payload TEXT, digest TEXT, updated REAL, PRIMARY KEY(id,generation))")
            try:
                with db:
                    yield db
            finally:
                db.close()
        except (OSError, sqlite3.Error) as exc:
            raise SessionError(f"Checkpoint unavailable; execution paused: {exc}") from exc

    @staticmethod
    def valid_data(data):
        if not isinstance(data, dict):
            return False
        if "messages" not in data:
            return True
        if not isinstance(data["messages"], list) or not isinstance(data.get("execution_id"), str):
            return False
        if not isinstance(data.get("request"), str) or not isinstance(data.get("total_tokens"), int):
            return False
        for message in data["messages"]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"} or not isinstance(message.get("content"), (str, list)):
                return False
        loop = data.get("loop")
        if loop is not None and (not isinstance(loop, dict) or not {"phase", "response", "results", "scopes", "recovery", "turns", "max_tokens"}.issubset(loop)):
            return False
        goal = data.get("goal")
        if goal is not None and (not isinstance(goal, dict) or not isinstance(goal.get("condition"), str) or not goal["condition"].strip()):
            return False
        return True

    def save(self, snapshot):
        snapshot.workspace = str(self.root)
        payload = json.dumps(plain(vars(snapshot)), ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for gen, old_payload, old_digest in db.execute("SELECT generation,payload,digest FROM snapshots WHERE id=?", (snapshot.session_id,)).fetchall():
                if hashlib.sha256(old_payload.encode()).hexdigest() != old_digest:
                    db.execute("DELETE FROM snapshots WHERE id=? AND generation=?", (snapshot.session_id, gen))
            generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM snapshots WHERE id=?", (snapshot.session_id,)).fetchone()[0]
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)", (snapshot.session_id, generation, payload, digest, time.time()))
            db.execute("DELETE FROM snapshots WHERE id=? AND generation<?", (snapshot.session_id, generation-1))

    def load(self, session_id):
        with self.connect() as db:
            rows = db.execute("SELECT payload,digest FROM snapshots WHERE id=? ORDER BY generation DESC", (session_id,)).fetchall()
        for index, (payload, digest) in enumerate(rows):
            try:
                if hashlib.sha256(payload.encode()).hexdigest() != digest:
                    continue
                obj = json.loads(payload)
                if obj.get("version") != 1 or obj.get("workspace") != str(self.root):
                    raise SessionError("Incompatible checkpoint version or workspace")
                snapshot = SessionSnapshot(**obj)
                if snapshot.session_id != session_id or not self.valid_data(snapshot.data):
                    continue
                return snapshot, bool(index)
            except (ValueError, TypeError):
                continue
        raise SessionError("No valid checkpoint; original data preserved")

    def list(self):
        with self.connect() as db:
            return db.execute("SELECT id,MAX(updated) FROM snapshots GROUP BY id ORDER BY MAX(updated) DESC").fetchall()


class WorkspaceLock:
    def __init__(self, root):
        self.store = SessionStore(root)
        self.handle = None

    def __enter__(self):
        # Validate the directory before creating the lock file.
        with self.store.connect():
            pass
        self.handle = (self.store.directory / "host.lock").open("a+b")
        try:
            self.handle.seek(0)
            if self.handle.read(1) == b"":
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise SessionError("Workspace is already owned by another host") from exc
        return self

    def __exit__(self, *args):
        self.handle.close()
