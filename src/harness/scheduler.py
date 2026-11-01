"""Durable cron jobs, delivery acknowledgement, and recovery."""

import json
import os
import random
import threading
import time
from uuid import uuid4
from dataclasses import asdict
from datetime import datetime

from .models import CronJob


class CronScheduler:
    """Durable cron jobs, delivery acknowledgement, and recovery."""

    def __init__(self, config):
        self.config = config
        self.DURABLE_PATH = self.config.WORKDIR / ".scheduled_tasks.json"
        self.scheduled_jobs: dict[str, CronJob] = {}
        self.cron_queue: list[CronJob] = []
        self.before_consume = None
        self.cron_lock = threading.RLock()
        self._last_fired: dict[str, str] = {}
        self._runtime_services_started = False
        self._runtime_services_lock = threading.Lock()

    def _cron_field_matches(self, field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return step > 0 and value % step == 0
        if "," in field:
            return any(self._cron_field_matches(part.strip(), value)
                       for part in field.split(","))
        if "-" in field:
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        return value == int(field)

    def cron_matches(self, cron_expr: str, dt: datetime) -> bool:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        dow_val = (dt.weekday() + 1) % 7
        m = self._cron_field_matches(minute, dt.minute)
        h = self._cron_field_matches(hour, dt.hour)
        dom_ok = self._cron_field_matches(dom, dt.day)
        month_ok = self._cron_field_matches(month, dt.month)
        dow_ok = self._cron_field_matches(dow, dow_val)
        if not (m and h and month_ok):
            return False
        if dom == "*" and dow == "*":
            return True
        if dom == "*":
            return dow_ok
        if dow == "*":
            return dom_ok
        return dom_ok or dow_ok

    def _validate_cron_field(self, field: str, lo: int, hi: int) -> str | None:
        if field == "*":
            return None
        if field.startswith("*/"):
            step = field[2:]
            if not step.isdigit() or int(step) <= 0:
                return f"Invalid step: {field}"
            return None
        if "," in field:
            for part in field.split(","):
                err = self._validate_cron_field(part.strip(), lo, hi)
                if err:
                    return err
            return None
        if "-" in field:
            left, right = field.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                return f"Invalid range: {field}"
            a, b = int(left), int(right)
            if a < lo or a > hi or b < lo or b > hi:
                return f"Range {field} out of bounds [{lo}-{hi}]"
            if a > b:
                return f"Range start > end: {field}"
            return None
        if not field.isdigit():
            return f"Invalid field: {field}"
        value = int(field)
        if value < lo or value > hi:
            return f"Value {value} out of bounds [{lo}-{hi}]"
        return None

    def validate_cron(self, cron_expr: str) -> str | None:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
        for field, (lo, hi), name in zip(fields, bounds, names):
            err = self._validate_cron_field(field, lo, hi)
            if err:
                return f"{name}: {err}"
        return None

    def save_durable_jobs(self):
        with self.cron_lock:
            durable = [asdict(job) for job in self.scheduled_jobs.values() if job.durable]
            temporary = self.DURABLE_PATH.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(durable, indent=2), encoding="utf-8")
            os.replace(temporary, self.DURABLE_PATH)

    def load_durable_jobs(self):
        if not self.DURABLE_PATH.exists():
            return
        try:
            for item in json.loads(self.DURABLE_PATH.read_text(encoding="utf-8")):
                job = CronJob(**item)
                if not self.validate_cron(job.cron):
                    self.scheduled_jobs[job.id] = job
                    if job.pending_delivery:
                        if not job.delivery_id:
                            job.delivery_id = uuid4().hex
                            self.save_durable_jobs()
                        self.cron_queue.append(job)
        except Exception:
            pass

    def schedule_job(self, cron: str, prompt: str,
                     recurring: bool = True, durable: bool = True) -> CronJob | str:
        err = self.validate_cron(cron)
        if err:
            return err
        job = CronJob(
            id=f"cron_{random.randint(0, 999999):06d}",
            cron=cron, prompt=prompt,
            recurring=recurring, durable=durable)
        with self.cron_lock:
            self.scheduled_jobs[job.id] = job
            if durable:
                self.save_durable_jobs()
        return job

    def cancel_job(self, job_id: str) -> str:
        with self.cron_lock:
            job = self.scheduled_jobs.pop(job_id, None)
            self.cron_queue[:] = [queued for queued in self.cron_queue if queued.id != job_id]
            if job and job.durable:
                self.save_durable_jobs()
        if not job:
            return f"Job {job_id} not found"
        return f"Cancelled {job_id}"

    def _enqueue_due_job(self, job: CronJob):
        """Persist a delivery before exposing it through the queue."""
        job.delivery_id = uuid4().hex
        job.pending_delivery = True
        try:
            if job.durable:
                self.save_durable_jobs()
        except Exception:
            job.pending_delivery = False
            raise
        self.cron_queue.append(job)

    def cron_scheduler_loop(self):
        while True:
            time.sleep(1)
            now = datetime.now()
            marker = now.strftime("%Y-%m-%d %H:%M")
            with self.cron_lock:
                for job in list(self.scheduled_jobs.values()):
                    try:
                        if job.pending_delivery:
                            continue
                        if self.cron_matches(job.cron, now) and self._last_fired.get(job.id) != marker:
                            self._enqueue_due_job(job)
                            self._last_fired[job.id] = marker
                    except Exception as e:
                        print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")

    def consume_cron_queue(self) -> list[CronJob]:
        with self.cron_lock:
            fired = list(self.cron_queue)
            if self.before_consume:
                self.before_consume(fired)
            self.cron_queue.clear()
        return fired

    def acknowledge_cron_jobs(self, jobs: list[CronJob]):
        """Remove one-shot jobs after a model call accepts their prompts."""
        durable_changed = False
        with self.cron_lock:
            for job in jobs:
                current = self.scheduled_jobs.get(job.id)
                if current and current.pending_delivery:
                    if current.recurring:
                        current.pending_delivery = False
                    else:
                        self.scheduled_jobs.pop(job.id, None)
                    durable_changed = durable_changed or current.durable
            if durable_changed:
                self.save_durable_jobs()

    def restore_cron_jobs(self, jobs: list[CronJob]):
        """Put unacknowledged deliveries back after a failed model call."""
        with self.cron_lock:
            queued_ids = {job.id for job in self.cron_queue}
            for job in jobs:
                current = self.scheduled_jobs.get(job.id)
                if current and current.id not in queued_ids:
                    self.cron_queue.append(current)
                    queued_ids.add(current.id)

    def run_schedule_cron(self, cron: str, prompt: str,
                          recurring: bool = True, durable: bool = True) -> str:
        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' -> {prompt}"

    def run_list_crons(self) -> str:
        with self.cron_lock:
            jobs = list(self.scheduled_jobs.values())
        if not jobs:
            return "No cron jobs."
        return "\n".join(
            f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
            f"[{'recurring' if job.recurring else 'one-shot'}, "
            f"{'durable' if job.durable else 'session'}]"
            for job in jobs)

    def run_cancel_cron(self, job_id: str) -> str:
        return self.cancel_job(job_id)

    def start_runtime_services(self):
        """Start durable scheduling once when a CLI host becomes active."""
        with self._runtime_services_lock:
            if self._runtime_services_started:
                return
            self.load_durable_jobs()
            threading.Thread(target=self.cron_scheduler_loop, daemon=True).start()
            self._runtime_services_started = True
