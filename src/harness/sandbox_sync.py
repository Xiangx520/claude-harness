"""Three-way workspace synchronization; Docker is only the transport."""

import hashlib
import io
import json
import ntpath
import os
import stat
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Entry:
    content: bytes | None  # None denotes a directory, not a deleted file.
    mode: int = 0o644

    def signature(self):
        if self.content is None:
            return "directory"
        return hashlib.sha256(self.content).hexdigest() + f":{self.mode}"


def read_archive(payload, max_bytes, excluded, error):
    """Validate the entire archive before any host write, including directories."""
    entries = {}
    seen = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                        or ":" in member.name or "\x00" in member.name
                        or any(ntpath.isreserved(p) for p in path.parts)):
                    raise error("Unsafe sandbox export path")
                if not path.parts or excluded(path.parts):
                    continue
                if not member.isfile() and not member.isdir():
                    raise error("Sandbox export contains a link or special file")
                name = path.as_posix()
                key = name.casefold()
                if key in seen:
                    raise error("Duplicate sandbox export path")
                seen.add(key)
                if member.isdir():
                    entries[name] = Entry(None, 0o755)
                else:
                    total += member.size
                    if total > max_bytes:
                        raise error("Sandbox export exceeds SANDBOX_TRANSFER_MB")
                    with archive.extractfile(member) as source:
                        content = source.read()
                    mode = 0o755 if os.name != "nt" and member.mode & 0o111 else 0o644
                    entries[name] = Entry(content, mode)
    except (tarfile.TarError, OSError) as exc:
        raise error("Invalid sandbox archive") from exc
    # A case-insensitive parent alias can overwrite another tree on Windows.
    spelling = {}
    for name, entry in list(entries.items()):
        path = PurePosixPath(name)
        for prefix in [*reversed(path.parents[:-1]), path]:
            text = prefix.as_posix()
            key = text.casefold()
            if key in spelling and spelling[key] != text:
                raise error("Ambiguous sandbox path casing")
            spelling[key] = text
            if prefix != path:
                parent = entries.setdefault(text, Entry(None, 0o755))
                if parent.content is not None:
                    raise error("Sandbox path has a file as its parent")
    return entries


class WorkspaceSync:
    def __init__(self, root, baseline, snapshot, push, error):
        self.root = root
        self.baseline = baseline
        self.snapshot = snapshot
        self.push = push
        self.error = error
        self.session = uuid.uuid4().hex
        self.conflicts = []
        self.recovery = None
        self._recovery_signature = None
        self.applied = 0
        self.pending = False

    def safe_target(self, name):
        target = self.root
        for part in ("", *PurePosixPath(name).parts):
            target = target / part
            if target.is_symlink() or target.is_junction():
                raise self.error("Local sync path is a link: " + name)
            if target.exists():
                mode = target.lstat().st_mode
                if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                    raise self.error("Local sync path is a special file: " + name)
        return target

    def _store(self, directory, entries):
        directory.mkdir(parents=True, exist_ok=True)
        for name, entry in entries.items():
            if entry is None or entry.content is None:
                continue
            target = directory.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry.content)

    def preserve(self, remote, reason):
        changes = {p: remote.get(p) for p in self.baseline.keys() | remote.keys()
                   if self.baseline.get(p) != remote.get(p)}
        signature = tuple(sorted((p, e.signature() if e else None) for p, e in changes.items()))
        if signature != self._recovery_signature or self.recovery is None:
            folder = self.safe_target(".sandbox_conflicts/" + self.session + "/" + uuid.uuid4().hex)
            # Create a fresh tree; pre-existing links cannot redirect the writes.
            folder.mkdir(parents=True, exist_ok=False)
            self._store(folder / "remote", changes)
            (folder / "changes.json").write_text(json.dumps({
                "reason": reason, "workspace": str(self.root),
                "conflicts": self.conflicts,
                "deleted": [p for p, e in changes.items() if e is None],
                "directories": [p for p, e in changes.items() if e and e.content is None],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            self.recovery = folder
            self._recovery_signature = signature
        return self.recovery

    def synchronize(self, remote, choice=None):
        self.pending = True
        self.safe_target(".")
        local = self.snapshot()
        merged = {}
        conflicts = []
        for name in self.baseline.keys() | local.keys() | remote.keys():
            before, here, there = self.baseline.get(name), local.get(name), remote.get(name)
            if here == there or there == before:
                result = here
            elif here == before:
                result = there
            else:
                conflicts.append(name)
                result = here if choice == "local" else there
            if result is not None:
                merged[name] = result
        # A directory deletion/type replacement conflicts with edits below it,
        # even when those edits touch a previously nonexistent child.
        names = self.baseline.keys() | local.keys() | remote.keys()
        local_changes = {p for p in names if local.get(p) != self.baseline.get(p)}
        remote_changes = {p for p in names if remote.get(p) != self.baseline.get(p)}
        for side, other, other_changes in ((local, remote, remote_changes), (remote, local, local_changes)):
            for name in names:
                old, new = self.baseline.get(name), side.get(name)
                old_directory = old is not None and old.content is None
                new_directory = new is not None and new.content is None
                if (old_directory != new_directory and new != other.get(name)
                        and any(p.startswith(name + "/") for p in other_changes)):
                    conflicts.append(name)
        self.conflicts = sorted(set(conflicts))
        if choice in {"local", "remote"}:
            selected = local if choice == "local" else remote
            for name in self.conflicts:
                if any(name.startswith(parent + "/") for parent in self.conflicts):
                    continue
                for child in names:
                    if child == name or child.startswith(name + "/"):
                        merged.pop(child, None)
                        if child in selected:
                            merged[child] = selected[child]
        if conflicts and choice not in {"local", "remote"}:
            recovery = self.preserve(remote, "Concurrent local and sandbox edits")
            raise self.error("Sandbox sync conflict: " + ", ".join(self.conflicts[:10])
                             + f". Local files preserved; remote changes saved at {recovery}. "
                             "Use /sandbox resolve local or /sandbox resolve remote.")
        to_local = {p: merged.get(p) for p in local.keys() | merged.keys()
                    if local.get(p) != merged.get(p)}
        to_remote = {p: merged.get(p) for p in remote.keys() | merged.keys()
                     if remote.get(p) != merged.get(p)}
        try:
            for name in merged:
                for parent in PurePosixPath(name).parents:
                    entry = merged.get(parent.as_posix())
                    if entry and entry.content is not None:
                        raise self.error("Conflicting file and directory changes: " + name)
            # Check upload deletions too: a locally replaced symlink must not be
            # mistaken for a deliberate file deletion.
            for name in to_local.keys() | to_remote.keys():
                self.safe_target(name)
            if self.snapshot() != local:
                raise self.error("Local project changed during synchronization; retry /sandbox sync")
            if conflicts:
                self.preserve(remote, "Conflict resolved using " + choice)
            if to_local:
                self._apply_local(to_local, local)
            if to_remote:
                self.push(to_remote, remote)
        except (OSError, ValueError) as exc:
            recovery = self.preserve(remote, str(exc))
            raise self.error(f"Sandbox sync failed: {exc}. Remote changes saved at {recovery}") from exc
        self.baseline = merged
        self.conflicts = []
        self.pending = False
        self.applied += len(to_local)
        return f"Sandbox synced: {len(to_local)} local changes, {len(to_remote)} sandbox changes"

    def _apply_local(self, changes, local):
        # Save only prior versions, not another full project or normal outputs.
        history = self.safe_target(".sandbox_history/" + self.session + "/" + uuid.uuid4().hex)
        history.mkdir(parents=True, exist_ok=False)
        self._store(history / "before", {p: local.get(p) for p in changes})
        (history / "changes.json").write_text(json.dumps({
            "workspace": str(self.root), "paths": sorted(changes),
            "created": [p for p in changes if p not in local],
            "deleted": [p for p, e in changes.items() if e is None],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # Remove old entries deepest first, then create directories and files.
        for name in sorted(changes, key=lambda p: (-len(PurePosixPath(p).parts), p)):
            old, new = local.get(name), changes[name]
            if old is None:
                continue
            self._check_current(name, old)
            if new is not None and (old.content is None) == (new.content is None):
                continue
            target = self.safe_target(name)
            if old.content is None:
                # Never recursively delete: excluded credentials/state can be
                # present in a directory that disappeared from the sandbox.
                if target.is_dir() and not any(target.iterdir()):
                    target.rmdir()
                elif new is not None:
                    raise self.error("Cannot replace nonempty local directory: " + name)
            else:
                target.unlink()
        for name, new in sorted(changes.items(), key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
            if new is None:
                continue
            target = self.safe_target(name)
            old = local.get(name)
            removed = old is not None and (old.content is None) != (new.content is None)
            self._check_current(name, None if removed else old)
            if new.content is None:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".taffy-sync-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(new.content)
                os.chmod(temporary, new.mode)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _check_current(self, name, expected):
        path = self.safe_target(name)
        current = None
        if path.is_dir():
            current = Entry(None, 0o755)
        elif path.is_file():
            mode = 0o755 if os.name != "nt" and path.stat().st_mode & 0o111 else 0o644
            current = Entry(path.read_bytes(), mode)
        if current != expected:
            raise self.error("Local project changed during synchronization: " + name)
