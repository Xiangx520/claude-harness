"""Docker isolation using copied workspaces, also usable with an SSH context."""

import io
import base64
import json
import os
import stat
import subprocess
import tarfile
import threading
import uuid
from pathlib import Path, PurePosixPath

from .config import ConfigurationError
from .sandbox_sync import WorkspaceSync, read_archive


class SandboxError(ConfigurationError):
    pass


# Runtime state, local credentials and dependencies never enter the snapshot.
EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".uv-cache",
            ".idea", ".codex", ".agents", ".aws", ".ssh", ".taffy", ".memory",
            ".tasks", ".worktrees", ".mailboxes", ".transcripts", ".task_outputs",
            ".sandbox_exports", ".sandbox_conflicts", ".sandbox_history", ".sandbox_connection", ".tools",
            ".scheduled_tasks.json", ".scheduled_tasks.json.tmp"}


def excluded(parts):
    return any(p.lower() in EXCLUDED or p.lower().startswith((".env", ".taffy-sync-"))
               or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
               or p.lower() in {"id_rsa", "id_ed25519", "credentials"} for p in parts)


def workspace_archive(root: Path, max_bytes: int) -> bytes:
    """Copy regular files only; do not follow symlinks or Windows junctions."""
    output = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=output, mode="w") as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            dirs[:] = [name for name in dirs if not excluded((name,))
                       and not (base / name).is_symlink()
                       and not (base / name).is_junction()]
            if base != root:
                # Explicit directory entries preserve UID 1000 on remote
                # extraction, so the non-root agent can create new files there.
                directory_member = tarfile.TarInfo(base.relative_to(root).as_posix())
                directory_member.type = tarfile.DIRTYPE
                directory_member.mode = 0o755
                directory_member.uid = directory_member.gid = 1000
                archive.addfile(directory_member)
            for name in files:
                path = base / name
                relative = path.relative_to(root)
                if excluded(relative.parts) or path.is_symlink():
                    continue
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                with path.open("rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    total += info.st_size
                    if total > max_bytes:
                        raise SandboxError("Sandbox snapshot exceeds SANDBOX_TRANSFER_MB")
                    member = tarfile.TarInfo(relative.as_posix())
                    member.size = info.st_size
                    member.mode = 0o755 if info.st_mode & 0o111 else 0o644
                    member.uid = member.gid = 1000
                    archive.addfile(member, source)
                    if output.tell() > max_bytes + 1024 * 1024:
                        raise SandboxError("Sandbox snapshot archive exceeds transfer limit")
            if output.tell() > max_bytes + 1024 * 1024:
                raise SandboxError("Sandbox snapshot archive exceeds transfer limit")
    return output.getvalue()


FILE_TOOL = r'''
import glob, json, pathlib, sys
data = json.load(sys.stdin)
root = pathlib.Path("/workspace")
try:
    op = data["operation"]
    raw = data.get("path", "")
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path escapes workspace: " + raw)
    if op == "read":
        lines = path.read_text(encoding="utf-8").splitlines()[max(int(data.get("offset") or 0), 0):]
        limit = data.get("limit")
        if limit is not None and int(limit) < len(lines):
            limit = int(limit)
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        print("\n".join(lines), end="")
    elif op == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data["content"], encoding="utf-8")
        print(f"Wrote {len(data['content'])} bytes to {raw}", end="")
    elif op == "edit":
        text = path.read_text(encoding="utf-8")
        if data["old_text"] not in text:
            raise ValueError("text not found in " + raw)
        path.write_text(text.replace(data["old_text"], data["new_text"], 1), encoding="utf-8")
        print("Edited " + raw, end="")
    elif op == "glob":
        matches = sorted({p for p in glob.iglob(data["pattern"], root_dir=root, recursive=True)
                          if (root / p).resolve().is_relative_to(root)})
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        print("\n".join(shown) if shown else "(no matches)", end="")
    else:
        raise ValueError("unknown file operation")
except Exception as exc:
    print("Error: " + str(exc), end="")
    sys.exit(1)
'''


# Host-to-container changes have already passed archive validation. Recheck
# paths and expected hashes inside the container before applying the batch.
APPLY_TOOL = r'''
import base64, hashlib, json, pathlib, sys
data = json.load(sys.stdin)
root = pathlib.Path('/workspace')
def target(name):
    path = root / name
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Sandbox sync path is a link: ' + name)
    if not path.resolve().is_relative_to(root):
        raise ValueError('Sandbox sync path escapes workspace')
    return path
def signature(path):
    if not path.exists():
        return None
    if path.is_dir():
        return 'directory'
    if not path.is_file():
        raise ValueError('Sandbox sync path is a special file')
    mode = 420 if data['windows'] else (493 if path.stat().st_mode & 73 else 420)
    return hashlib.sha256(path.read_bytes()).hexdigest() + ':' + str(mode)
try:
    changes = data['changes']
    for name in changes:
        if signature(target(name)) != data['expected'][name]:
            raise ValueError('Sandbox changed during synchronization: ' + name)
    for name in sorted(changes, key=lambda p: -len(pathlib.PurePosixPath(p).parts)):
        path, new = target(name), changes[name]
        if path.is_file() and (new is None or new['content'] is None):
            path.unlink()
        elif path.is_dir() and (new is None or new['content'] is not None):
            path.rmdir()
    for name in sorted(changes, key=lambda p: len(pathlib.PurePosixPath(p).parts)):
        path, new = target(name), changes[name]
        if new is None:
            continue
        if new['content'] is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(new['content'], validate=True))
            path.chmod(new['mode'])
except Exception as exc:
    print('Error: ' + str(exc))
    sys.exit(1)
'''


class DockerSandbox:
    def __init__(self, config):
        self.config = config
        self._project_root = config.WORKDIR.resolve()
        self._lock = threading.RLock()
        self._containers: dict[Path, str] = {}
        self._owned: set[str] = set()
        self._syncers: dict[Path, WorkspaceSync] = {}
        self._closed = False

    def _invoke(self, args, *, payload=None, timeout=30, limit=50000):
        # An explicit context and clean Docker environment prevent ambient
        # DOCKER_HOST / DOCKER_CONTEXT from silently choosing another daemon.
        environment = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        command = [self.config.SANDBOX_DOCKER_EXECUTABLE,
                   "--context", self.config.SANDBOX_DOCKER_CONTEXT, *args]
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
        except OSError as exc:
            raise SandboxError("Docker CLI unavailable; install Docker or configure an SSH Docker context") from exc
        output = bytearray()
        truncated = False

        def drain():
            nonlocal truncated
            while chunk := process.stdout.read(8192):
                remaining = max(limit - len(output), 0)
                output.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining

        def feed():
            try:
                process.stdin.write(payload)
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        writer = None
        if payload is not None:
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise SandboxError("Docker operation timed out; command was not run on the host") from exc
        finally:
            reader.join(timeout=2)
            if writer:
                writer.join(timeout=2)
            process.stdout.close()
        return process.returncode, bytes(output), truncated

    def _checked(self, args, **kwargs):
        code, output, truncated = self._invoke(args, **kwargs)
        if code or truncated:
            raise SandboxError("Docker sandbox operation failed: " + output.decode("utf-8", "replace")[:2000])
        return output

    def prepare(self, cwd=None):
        root = (cwd or self.config.WORKDIR).resolve()
        if not root.is_relative_to(self._project_root):
            raise SandboxError("Sandbox cwd escapes workspace")
        with self._lock:
            if self._closed:
                raise SandboxError("Sandbox is closed")
            if root in self._containers:
                return self._containers[root]
            payload = workspace_archive(root, self.config.SANDBOX_TRANSFER_MB * 1024 * 1024)
            baseline = self._parse_snapshot(payload)
            name = "taffy-sandbox-" + uuid.uuid4().hex
            self._owned.add(name)
            try:
                self._checked([
                    "run", "--detach", "--pull=never", "--name", name,
                    "--label", "app=taffy-sandbox", "--init", "--read-only",
                    "--user", "1000:1000", "--cap-drop=ALL",
                    "--security-opt", "no-new-privileges=true", "--network", "none",
                    "--cpus", str(self.config.SANDBOX_CPUS),
                    "--memory", f"{self.config.SANDBOX_MEMORY_MB}m",
                    "--memory-swap", f"{self.config.SANDBOX_MEMORY_MB}m",
                    "--pids-limit", str(self.config.SANDBOX_PIDS),
                    "--ulimit", "nofile=1024:1024", "--log-driver", "none",
                    "--tmpfs", f"/workspace:rw,nosuid,nodev,uid=1000,gid=1000,size={self.config.SANDBOX_WORKSPACE_MB}m",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,uid=1000,gid=1000,size=64m",
                    "--workdir", "/workspace", "--env", "HOME=/tmp",
                    "--entrypoint", "/bin/sleep", self.config.SANDBOX_IMAGE, "infinity",
                ])
                # Docker 26 rejects docker cp writes into tmpfs when the root
                # filesystem is read-only. Extract inside the live container as
                # its unprivileged user, retaining the read-only root boundary.
                self._checked(["exec", "-i", name, "/bin/tar", "--extract", "--file=-",
                               "--directory=/workspace", "--no-same-owner"],
                              payload=payload, timeout=60)
                self._checked(["exec", name, "python", "-I", "-c", "import pathlib; assert pathlib.Path('/workspace').is_dir()"])
            except Exception:
                try:
                    self._checked(["rm", "--force", name])
                    self._owned.discard(name)
                except SandboxError:
                    pass
                raise
            self._containers[root] = name
            self._syncers[root] = WorkspaceSync(
                root, baseline, lambda: self._parse_snapshot(workspace_archive(
                    root, self.config.SANDBOX_TRANSFER_MB * 1024 * 1024)),
                lambda changes, expected: self._push_changes(name, changes, expected), SandboxError)
            return name

    def execute(self, command, cwd=None):
        # Shared workspaces are serialized across main/subagent/background tools
        # so a snapshot cannot race another Taffy command in the same container.
        with self._lock:
            self.sync(cwd)
            text, code = self._execute(command, cwd)
            try:
                self.sync(cwd)
            except SandboxError as exc:
                return text + f"\nError: {exc}", code if code else 1
            return text, code

    def _execute(self, command, cwd=None):
        name = self.prepare(cwd)
        seconds = self.config.SANDBOX_TIMEOUT_SECONDS
        try:
            code, output, truncated = self._invoke([
                "exec", name, "/usr/bin/timeout", "--signal=TERM", "--kill-after=2s",
                f"{seconds}s", "/bin/sh", "-c", command], timeout=seconds + 10)
        except SandboxError:
            # A transport timeout leaves remote execution uncertain. Remove the
            # owned container to stop descendants; never fall back to host shell.
            self.close()
            raise
        text = output.decode("utf-8", "replace").strip() or "(no output)"
        if truncated:
            text += "\n[output truncated]"
        if code in (124, 137):
            text = f"Error: Sandbox command timed out or was killed ({seconds}s limit)\n" + text
        return text, code

    def file_tool(self, operation, cwd=None, _internal=False, **data):
        with self._lock:
            if operation in {"write", "edit"} and not _internal and excluded(PurePosixPath(data.get("path", "")).parts):
                raise SandboxError("File is excluded from the project workspace")
            if not _internal:
                self.sync(cwd, pull=False)
            result = self._file_tool(operation, cwd, **data)
            if operation in {"write", "edit"} and not _internal:
                try:
                    self.sync(cwd)
                except SandboxError as exc:
                    return f"Error: {exc}\nSandbox tool output: {result}"
            return result

    def _file_tool(self, operation, cwd=None, **data):
        name = self.prepare(cwd)
        code, output, truncated = self._invoke([
            "exec", "-i", name, "/usr/bin/timeout", "--kill-after=2s", "30s",
            "python", "-I", "-c", FILE_TOOL],
            payload=json.dumps(dict(operation=operation, **data)).encode("utf-8"), timeout=40)
        text = output.decode("utf-8", "replace")
        if truncated:
            text += "\n[output truncated]"
        return text if code == 0 or text.startswith("Error:") else f"Error: Sandbox file operation exited with status {code}\n{text}"

    def _parse_snapshot(self, payload):
        return read_archive(payload, self.config.SANDBOX_TRANSFER_MB * 1024 * 1024, excluded, SandboxError)

    def _remote_snapshot(self, name):
        max_bytes = self.config.SANDBOX_TRANSFER_MB * 1024 * 1024
        patterns = [*sorted(EXCLUDED), ".env*", ".taffy-sync-*", "*.pem", "*.key", "*.p12", "*.pfx",
                    "id_rsa", "id_ed25519", "credentials"]
        payload = self._checked(["exec", name, "/bin/tar", "--create", "--file=-",
                                 "--directory=/workspace", "--ignore-case",
                                 *["--exclude=" + p for p in patterns], "."],
                                timeout=60, limit=max_bytes + 1024 * 1024)
        return self._parse_snapshot(payload)

    def _push_changes(self, name, changes, expected):
        data = {
            "windows": os.name == "nt",
            "changes": {p: None if e is None else {
                "content": None if e.content is None else base64.b64encode(e.content).decode("ascii"),
                "mode": e.mode} for p, e in changes.items()},
            "expected": {p: expected[p].signature() if p in expected else None for p in changes},
        }
        self._checked(["exec", "-i", name, "python", "-I", "-c", APPLY_TOOL],
                      payload=json.dumps(data).encode("utf-8"), timeout=60)

    def sync(self, cwd=None, *, pull=True, choice=None):
        with self._lock:
            name = self.prepare(cwd)
            root = (cwd or self.config.WORKDIR).resolve()
            syncer = self._syncers[root]
            # Reads need no network roundtrip when neither side has pending
            # changes. Every mutating operation always collects a fresh result.
            if not pull and not syncer.pending and not syncer.conflicts and syncer.snapshot() == syncer.baseline:
                return "Sandbox synced: no changes"
            syncer.pending = True
            return syncer.synchronize(self._remote_snapshot(name), choice)

    def export(self, cwd=None):
        """Optional snapshot; ordinary tool changes already update the project."""
        with self._lock:
            entries = self._remote_snapshot(self.prepare(cwd))
            return self._export_entries(entries)

    def _export_entries(self, entries):
        if self._project_root.is_symlink() or self._project_root.is_junction():
            raise SandboxError("Sandbox project root must not be a link")
        destination_root = self._project_root / ".sandbox_exports"
        if destination_root.is_symlink() or destination_root.is_junction():
            raise SandboxError("Sandbox export directory must not be a link")
        destination_root.mkdir(exist_ok=True)
        destination = destination_root / uuid.uuid4().hex
        destination.mkdir()
        for name, entry in entries.items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            if entry.content is None:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry.content)
        return destination

    def status(self):
        with self._lock:
            return (f"Sandbox: docker; context={self.config.SANDBOX_DOCKER_CONTEXT}; "
                    f"network=none; active_workspaces={len(self._containers)}; "
                    f"sync=automatic; conflicts={sum(len(s.conflicts) for s in self._syncers.values())}")

    def close(self):
        with self._lock:
            if self._closed and not self._owned:
                return
            errors = []
            # Final collection catches changes from a completed remote child;
            # conflicts are saved to disk before deleting the container.
            removable = set(self._owned) - set(self._containers.values())
            for root, name in list(self._containers.items()):
                try:
                    remote = self._remote_snapshot(name)
                    try:
                        self._syncers[root].synchronize(remote)
                    except SandboxError as exc:
                        self._syncers[root].preserve(remote, str(exc))
                        errors.append(str(exc))
                    removable.add(name)
                except SandboxError as exc:
                    errors.append(str(exc))
            self._closed = True
            for name in removable:
                try:
                    self._checked(["rm", "--force", name])
                    self._owned.discard(name)
                except SandboxError as exc:
                    errors.append(str(exc))
            self._containers = {root: name for root, name in self._containers.items() if name in self._owned}
            self._syncers = {root: syncer for root, syncer in self._syncers.items() if root in self._containers}
            if errors:
                raise SandboxError("; ".join(errors) + ("; owned containers: " + ", ".join(sorted(self._owned)) if self._owned else ""))
