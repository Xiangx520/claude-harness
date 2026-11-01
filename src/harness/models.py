"""The original durable task, team protocol, and cron records."""

import time
from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None
    lease_token: str | None = None
    lease_expires_at: float | None = None


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    delivery_id: str = ""
