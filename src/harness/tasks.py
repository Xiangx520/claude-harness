"""Durable task records, dependency edges, and assignment leases."""

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import Task

try:
    import fcntl
except ImportError:  # Windows has no fcntl module.
    fcntl = None


class TaskSystem:
    """Durable task records, dependency edges, and assignment leases."""

    def __init__(self, config):
        self.config = config
        self.TASKS_DIR = self.config.WORKDIR / ".tasks"
        self.TASKS_ROOT = self.TASKS_DIR.resolve()
        self.TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
        self.task_lock = threading.RLock()
        self.TASK_LOCK_PATH = self.TASKS_DIR / ".lock"
        self._task_store_state = threading.local()
        self.teammate_assignments: dict[str, dict[str, object]] = {}
        self.assignment_versions: dict[str, int] = {}

    @contextmanager
    def task_store_lock(self):
        """Serialize task mutations across threads and host processes."""
        with self.task_lock:
            depth = getattr(self._task_store_state, "depth", 0)
            if depth == 0:
                self.TASKS_DIR.mkdir(parents=True, exist_ok=True)
                handle = self.TASK_LOCK_PATH.open("a+", encoding="utf-8")
                # fcntl.flock gives cross-process locking on POSIX. Windows has no
                # fcntl, so it falls back to the in-process threading lock only.
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._task_store_state.handle = handle
            self._task_store_state.depth = depth + 1
            try:
                yield
            finally:
                self._task_store_state.depth -= 1
                if self._task_store_state.depth == 0:
                    handle = self._task_store_state.handle
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    del self._task_store_state.handle

    def advance_assignment_version(self, owner: str):
        """Invalidate old approvals without clearing an explicit plan requirement."""
        with self.task_lock:
            self.assignment_versions[owner] = self.assignment_versions.get(owner, 0) + 1
            gates = self.team.plan_gates
            request_ids = self.team.plan_request_ids
            team = self.team.team_lock
            if team is not None:
                team.acquire()
            try:
                if (isinstance(gates, dict) and owner in gates
                        and gates[owner] != "not_required"):
                    gates[owner] = "required"
                if isinstance(request_ids, dict):
                    request_ids.pop(owner, None)
            finally:
                if team is not None:
                    team.release()

    def _task_path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not self.TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        path = (self.TASKS_DIR / f"{task_id}.json").resolve()
        if (not self.TASKS_ROOT.is_relative_to(self.config.WORKDIR.resolve())
                or not path.is_relative_to(self.TASKS_ROOT)):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def create_task(self, subject: str, description: str = "") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")
        with self.task_store_lock():
            for _ in range(100):
                task = Task(
                    id=f"task_{secrets.token_hex(4)}",
                    subject=subject,
                    description=description,
                    status="pending",
                    owner=None,
                    blockedBy=[],
                )
                try:
                    with self._task_path(task.id).open("x", encoding="utf-8") as handle:
                        json.dump(asdict(task), handle, indent=2)
                    return task
                except FileExistsError:
                    continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _task_depends_on(self, task_id: str, target_id: str) -> bool:
        """Return whether task_id transitively depends on target_id."""
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load_task(current).blockedBy)
        return False

    def update_task(self, task_id: str, addBlockedBy: list[str]) -> Task:
        """Add dependency edges after create_task has returned real task IDs."""
        if not isinstance(addBlockedBy, list):
            raise ValueError("addBlockedBy must be a list of task IDs")

        with self.task_store_lock():
            task = self.load_task(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    f"Task {task_id} dependencies can only be updated while "
                    "pending and unowned"
                )

            dependencies = list(dict.fromkeys(addBlockedBy))
            for dependency in dependencies:
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself")
                if not self._task_path(dependency).is_file():
                    raise ValueError(f"Dependency not found: {dependency}")
                if dependency not in task.blockedBy and self._task_depends_on(
                    dependency, task_id
                ):
                    raise ValueError(
                        f"Dependency cycle detected: {task_id} -> {dependency}"
                    )

            task.blockedBy.extend(
                dependency for dependency in dependencies
                if dependency not in task.blockedBy
            )
            self.save_task(task)
            return task

    def save_task(self, task: Task):
        with self.task_store_lock():
            path = self._task_path(task.id)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(asdict(task), indent=2), encoding="utf-8"
                )
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def load_task(self, task_id: str) -> Task:
        with self.task_lock:
            data = json.loads(self._task_path(task_id).read_text(encoding="utf-8"))
            task = Task(**data)
            if task.id != task_id:
                raise ValueError(f"Task file ID does not match {task_id}")
            if task.status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"Invalid task status: {task.status}")
            return task

    def list_tasks(self) -> list[Task]:
        with self.task_lock:
            if not self.TASKS_DIR.exists():
                return []
            if not self.TASKS_ROOT.is_relative_to(self.config.WORKDIR.resolve()):
                raise ValueError("Tasks directory escapes workspace")
            return [self.load_task(path.stem)
                    for path in sorted(self.TASKS_DIR.glob("task_*.json"))]

    def get_task_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2)

    def can_start(self, task_id: str) -> bool:
        # Dependencies are intentionally simple: every blocker must exist and be
        # completed before the task can be claimed.
        task = self.load_task(task_id)
        for dep_id in task.blockedBy:
            try:
                dep_path = self._task_path(dep_id)
            except ValueError:
                return False
            if not dep_path.exists():
                return False
            if self.load_task(dep_id).status != "completed":
                return False
        return True

    def _owner_in_progress(self, owner: str) -> Task | None:
        return next((task for task in self.list_tasks()
                     if task.status == "in_progress" and task.owner == owner), None)

    def _incomplete_dependencies(self, task: Task) -> list[str]:
        incomplete = []
        for dep_id in task.blockedBy:
            try:
                dep_path = self._task_path(dep_id)
            except ValueError:
                incomplete.append(dep_id)
                continue
            if not dep_path.exists() or self.load_task(dep_id).status != "completed":
                incomplete.append(dep_id)
        return incomplete

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        """Atomically claim one task and bind the owner's filesystem cwd."""
        with self.task_store_lock():
            task = self.load_task(task_id)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"
            if task.owner:
                return f"Task {task_id} is already owned by {task.owner}"
            assignment = self.teammate_assignments.get(owner)
            if assignment:
                return (f"Owner {owner} must finish the current work turn for "
                        f"{assignment['task_id']} before claiming another task")
            current = self._owner_in_progress(owner)
            if current:
                return (f"Owner {owner} must complete {current.id} before "
                        "claiming another task")
            if not self.can_start(task_id):
                return f"Blocked by: {self._incomplete_dependencies(task)}"
            cwd, error = self.worktrees.task_worktree_cwd(task)
            if error:
                return f"Cannot claim {task_id}: {error}"
            task.owner = owner
            task.status = "in_progress"
            self.save_task(task)
            self.teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
            self.advance_assignment_version(owner)
        print(f"  \033[36m[claim] {task.subject} -> in_progress (owner: {owner})\033[0m")
        return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str, owner: str = "agent") -> str:
        """Complete an assignment only when the caller owns it."""
        with self.task_store_lock():
            task = self.load_task(task_id)
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            if task.owner != owner:
                return (f"Task {task_id} is owned by {task.owner}, "
                        f"not {owner}; cannot complete")
            gate = self.team.plan_gates.get(owner, "not_required")
            if gate in {"required", "pending", "rejected"}:
                return f"Task {task_id} cannot complete while plan status is {gate}"
            assignment = self.teammate_assignments.get(owner)
            if not assignment or assignment.get("task_id") != task.id:
                cwd, error = self.worktrees.task_worktree_cwd(task)
                if error:
                    return f"Task {task_id} cannot complete: {error}"
                self.teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
            task.status = "completed"
            self.save_task(task)
            unblocked = [t.subject for t in self.list_tasks()
                         if t.status == "pending" and t.blockedBy and self.can_start(t.id)]
        print(f"  \033[32m[complete] {task.subject}\033[0m")
        msg = f"Completed {task.id} ({task.subject})"
        if unblocked:
            msg += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
        return msg

    def assignment_cwd(self, owner: str) -> Path:
        with self.task_lock:
            assignment = self.teammate_assignments.get(owner)
            task = self._owner_in_progress(owner)
            if task and (not assignment or assignment.get("task_id") != task.id):
                cwd, error = self.worktrees.task_worktree_cwd(task)
                if error:
                    raise ValueError(error)
                assignment = {"task_id": task.id, "cwd": cwd}
                self.teammate_assignments[owner] = assignment
            elif not assignment:
                return self.config.WORKDIR
            task = self.load_task(str(assignment["task_id"]))
            if task.status not in {"in_progress", "completed"} or task.owner != owner:
                raise ValueError(f"Assignment for {owner} is no longer active")
            cwd, error = self.worktrees.task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            if cwd.resolve() != Path(assignment["cwd"]).resolve():
                raise ValueError(f"Assignment cwd changed for task {task.id}")
            return cwd

    def release_completed_assignment(self, owner: str) -> bool:
        """Release a completed cwd lease only at a model turn boundary."""
        with self.task_lock:
            assignment = self.teammate_assignments.get(owner)
            if not assignment:
                return False
            task = self.load_task(str(assignment["task_id"]))
            if task.status != "completed" or task.owner != owner:
                return False
            self.teammate_assignments.pop(owner, None)
            self.advance_assignment_version(owner)
            if owner in self.team.plan_gates:
                self.team.plan_gates[owner] = "not_required"
            return True

    def release_teammate_assignment(self, owner: str):
        """Return abandoned teammate work to the task board on thread exit."""
        with self.task_lock:
            try:
                task = self._owner_in_progress(owner)
                if task:
                    task.status = "pending"
                    task.owner = None
                    self.save_task(task)
            finally:
                self.teammate_assignments.pop(owner, None)
                self.advance_assignment_version(owner)
                if owner in self.team.plan_gates:
                    self.team.plan_gates[owner] = "not_required"

    def scan_unclaimed_tasks(self) -> list[Task]:
        """Return ready tasks whose optional worktree binding is usable."""
        with self.task_lock:
            ready = []
            for task in self.list_tasks():
                if (task.status != "pending" or task.owner is not None
                        or not self.can_start(task.id)):
                    continue
                _, error = self.worktrees.task_worktree_cwd(task)
                if not error:
                    ready.append(task)
            return ready

    def claim_next_task(self, name: str) -> Task | None:
        """Claim the first still-available task, never a second assignment."""
        with self.task_lock:
            if self.teammate_assignments.get(name) or self._owner_in_progress(name):
                return None
        for task in self.scan_unclaimed_tasks():
            result = self.claim_task(task.id, owner=name)
            if result.startswith("Claimed "):
                return self.load_task(task.id)
        return None

    def current_work_identity(self, owner: str) -> tuple[int, str | None]:
        with self.task_lock:
            assignment = self.teammate_assignments.get(owner)
            task_id = str(assignment["task_id"]) if assignment else None
            return self.assignment_versions.get(owner, 0), task_id

    def run_create_task(self, subject: str, description: str = "") -> str:
        task = self.create_task(subject, description)
        print(f"  \033[34m[create] {task.subject}\033[0m")
        return f"Created {task.id}: {task.subject}"

    def run_update_task(self, task_id: str, addBlockedBy: list[str]) -> str:
        try:
            task = self.update_task(task_id, addBlockedBy)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"
        dependencies = ", ".join(task.blockedBy) or "(none)"
        print(f"  \033[34m[update] {task.subject} blockedBy: {dependencies}\033[0m")
        return f"Updated {task.id} blockedBy: {dependencies}"

    def run_list_tasks(self) -> str:
        tasks = self.list_tasks()
        if not tasks:
            return "No tasks."
        return "\n".join(
            f"  {t.id}: {t.subject} [{t.status}]"
            + (f" (wt:{t.worktree})" if t.worktree else "")
            for t in tasks)

    def run_get_task(self, task_id: str) -> str:
        try:
            return self.get_task_json(task_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def run_claim_task(self, task_id: str) -> str:
        try:
            return self.claim_task(task_id, owner="agent")
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def run_complete_task(self, task_id: str) -> str:
        try:
            return self.complete_task(task_id, owner="agent")
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
