"""File-backed mailboxes and condition-based message delivery."""

import json
import re
import threading
import time
from pathlib import Path


class MessageBus:
    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self.MAILBOX_DIR = self.config.WORKDIR / ".mailboxes"
        self.MAILBOX_ROOT = self.MAILBOX_DIR.resolve()
        self.VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
        self.RESERVED_TEAMMATE_NAMES = {"lead", "agent"}

    def _path(self, agent: str) -> Path:
        if not self.is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (self.MAILBOX_DIR / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(self.MAILBOX_ROOT):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with self._changed:
            self.MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        print(f"  \033[33m[bus] {from_agent} -> {to_agent}: "
              f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str,
                          timeout: float | None = None) -> list[dict]:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = (None if deadline is None
                             else deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)

    def is_valid_agent_name(self, name: str) -> bool:
        return bool(self.VALID_AGENT_NAME.fullmatch(name))
