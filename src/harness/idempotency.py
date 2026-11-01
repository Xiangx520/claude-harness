"""Durable tool-call deduplication, not an exactly-once external transaction."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace


def journal_tools(tools):
    tools = deepcopy(tools)
    for tool in tools:
        schema = tool.setdefault("input_schema", {"type": "object"})
        schema.setdefault("properties", {})["idempotency_key"] = {
            "type": "string",
            "description": "Stable ID for one logical operation. Reuse only when retrying "
                           "that operation in the same task; use a new ID for new work."
        }
    return tools


def invoke(journal, block, scope, operation, shared_scope=None):
    arguments = dict(block.input or {})
    key = arguments.pop("idempotency_key", None)
    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 200):
        return "Error: idempotency_key must be a nonempty string of at most 200 characters"
    clean = SimpleNamespace(type="tool_use", id=block.id, name=block.name, input=arguments)
    return journal.execute(
        shared_scope if key and shared_scope else scope,
        "key:" + key if key else "call:" + block.id,
        block.name, arguments, lambda: operation(clean))


class ToolJournal:
    def __init__(self, workdir):
        self.workdir = workdir.resolve()
        self.path = workdir / ".tasks" / "tool_calls.sqlite3"

    @contextmanager
    def connect(self):
        if not self.path.resolve().is_relative_to(self.workdir):
            raise ValueError("Tool journal escapes workspace")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS calls (
                scope TEXT, call_id TEXT, fingerprint TEXT, status TEXT, output TEXT,
                PRIMARY KEY (scope, call_id))""")
            with connection:
                yield connection
        finally:
            connection.close()

    def lookup(self, scope, call_id, name, arguments):
        """Read an existing outcome without reserving or executing a call."""
        fingerprint = hashlib.sha256(json.dumps(
            [name, arguments], sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT fingerprint,status,output FROM calls WHERE scope=? AND call_id=?",
                             (scope, call_id)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            return "Error: IDEMPOTENCY_CONFLICT: call ID reused with different arguments"
        if row[1] == "completed":
            return row[2]
        return "Error: TOOL_OUTCOME_UNKNOWN: invocation was interrupted; inspect its effects before issuing a new invocation"

    def execute(self, scope, call_id, name, arguments, operation):
        fingerprint = hashlib.sha256(json.dumps(
            [name, arguments], sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint, status, output FROM calls "
                             "WHERE scope=? AND call_id=?", (scope, call_id)).fetchone()
            if row:
                if row[0] != fingerprint:
                    return "Error: IDEMPOTENCY_CONFLICT: call ID reused with different arguments"
                if row[1] == "completed":
                    return row[2]
                return ("Error: TOOL_OUTCOME_UNKNOWN: invocation is running or was interrupted; "
                        "inspect its effects before issuing a new invocation")
            db.execute("INSERT INTO calls VALUES (?, ?, ?, 'started', NULL)",
                       (scope, call_id, fingerprint))
        # Commit intent BEFORE effects; a crash leaves a durable unknown outcome.
        output = str(operation())
        with self.connect() as db:
            db.execute("UPDATE calls SET status='completed', output=? "
                       "WHERE scope=? AND call_id=?", (output, scope, call_id))
        return output
