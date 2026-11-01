"""Cooperative progress leases. Never reassign while a tool is in flight."""

import threading
import time
from contextlib import contextmanager
from uuid import uuid4


class LeaseExpired(RuntimeError):
    pass


class WorkerLease:
    def __init__(self, seconds, renew):
        self.token = uuid4().hex
        self.seconds = seconds
        self.renew = renew
        self.lock = threading.RLock()
        self.valid = True
        self.deadline = time.time() + seconds

    def check(self):
        if not self.valid or time.time() >= self.deadline:
            raise LeaseExpired("Worker lease expired; further execution is fenced")

    def heartbeat(self):
        self.deadline = time.time() + self.seconds
        self.renew(self.token, self.deadline)

    @contextmanager
    def guard(self):
        with self.lock:
            self.check()
            self.heartbeat()
            try:
                yield
            finally:
                # The reaper cannot acquire this lock while effects are running.
                self.heartbeat()
