"""Lease fencing and durable invocation replay without timing-dependent sleeps."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import HarnessConfig, HarnessRuntime
from harness.idempotency import ToolJournal, invoke
from harness.leases import LeaseExpired


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = HarnessRuntime(HarnessConfig(self.root, "test"), Mock())

    def reserve(self, name="worker", task=None):
        task = task or self.runtime.tasks.create_task("work")
        with patch("harness.team.threading.Thread"):
            self.runtime.team.spawn_teammate_thread(name, "tester", "work", task.id)
        return task, self.runtime.team.worker_leases[name]

    def test_expiry_releases_task_and_fences_old_generation(self):
        task, lease = self.reserve()
        lease.deadline = time.time() - 1
        self.runtime.team.reap_expired_workers()
        saved = self.runtime.tasks.load_task(task.id)
        self.assertEqual((saved.status, saved.owner), ("pending", None))
        self.assertIsNone(saved.lease_token)
        with self.assertRaises(LeaseExpired):
            with lease.guard():
                self.fail("expired worker executed")
        _, replacement = self.reserve(task=task)
        self.runtime.team._release_teammate("worker", lease.token)
        self.assertIs(self.runtime.team.worker_leases["worker"], replacement)
        self.assertEqual(self.runtime.tasks.load_task(task.id).owner, "worker")

    def test_inflight_tool_is_not_reassigned_and_renews_on_return(self):
        task, lease = self.reserve()
        started, finish = threading.Event(), threading.Event()

        def tool():
            with lease.guard():
                started.set()
                finish.wait(3)

        thread = threading.Thread(target=tool)
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            lease.deadline = time.time() - 1
            self.runtime.team.reap_expired_workers()
            self.assertEqual(self.runtime.tasks.load_task(task.id).status, "in_progress")
        finally:
            finish.set()
            thread.join(3)
        self.assertGreater(lease.deadline, time.time())

    def test_persisted_expired_assignment_recovers_after_restart(self):
        task, lease = self.reserve()
        saved = self.runtime.tasks.load_task(task.id)
        self.assertEqual(saved.lease_token, lease.token)
        saved.lease_expires_at = time.time() - 1
        self.runtime.tasks.save_task(saved)
        other = HarnessRuntime(self.runtime.config, Mock())
        other.team.reap_expired_workers()
        self.assertEqual(other.tasks.load_task(task.id).status, "pending")

    def test_late_model_response_cannot_write_or_release_replacement(self):
        task = self.runtime.tasks.create_task("write report")
        entered, resume = threading.Event(), threading.Event()
        threads = []
        real_thread = threading.Thread

        def model(**kwargs):
            entered.set()
            resume.wait(3)
            return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
                type="tool_use", id="late", name="write_file",
                input={"path": "late.txt", "content": "stale"})])

        def start(**kwargs):
            thread = real_thread(**kwargs)
            threads.append(thread)
            return thread

        self.runtime.client.messages.create.side_effect = model
        with patch("harness.team.threading.Thread", side_effect=start):
            self.runtime.team.spawn_teammate_thread("worker", "tester", "work", task.id)
        try:
            self.assertTrue(entered.wait(3))
            old = self.runtime.team.worker_leases["worker"]
            old.deadline = time.time() - 1
            self.runtime.team.reap_expired_workers()
            _, replacement = self.reserve(task=task)
        finally:
            resume.set()
            threads[0].join(3)
        self.assertFalse(threads[0].is_alive())
        self.assertFalse((self.root / "late.txt").exists())
        self.assertIs(self.runtime.team.worker_leases["worker"], replacement)
        self.assertEqual(self.runtime.tasks.load_task(task.id).owner, "worker")

    def test_completed_output_replays_across_journal_instances(self):
        operation = Mock(return_value="created")
        first = ToolJournal(self.root)
        self.assertEqual(first.execute("task:a", "key", "bash", {}, operation), "created")
        second = ToolJournal(self.root)
        self.assertEqual(second.execute("task:a", "key", "bash", {}, operation), "created")
        operation.assert_called_once()
        self.assertIn("IDEMPOTENCY_CONFLICT", second.execute(
            "task:a", "key", "bash", {"command": "different"}, operation))

    def test_interrupted_effect_is_never_blindly_reexecuted(self):
        effects = []

        def operation():
            effects.append("created")
            raise RuntimeError("response lost after effect")

        journal = ToolJournal(self.root)
        with self.assertRaises(RuntimeError):
            journal.execute("task:a", "key", "bash", {}, operation)
        output = ToolJournal(self.root).execute("task:a", "key", "bash", {}, operation)
        self.assertIn("TOOL_OUTCOME_UNKNOWN", output)
        self.assertEqual(effects, ["created"])

    def test_concurrent_duplicate_does_not_execute_twice(self):
        journal = ToolJournal(self.root)
        started, finish = threading.Event(), threading.Event()
        results = []

        def operation():
            started.set()
            finish.wait(3)
            return "done"

        thread = threading.Thread(target=lambda: results.append(
            journal.execute("task:a", "key", "bash", {}, operation)))
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertIn("TOOL_OUTCOME_UNKNOWN", journal.execute(
                "task:a", "key", "bash", {}, lambda: self.fail("duplicate effect")))
        finally:
            finish.set()
            thread.join(3)
        self.assertEqual(results, ["done"])

    def test_explicit_key_survives_worker_change_and_is_removed_from_arguments(self):
        operation = Mock(return_value="ok")
        block = SimpleNamespace(id="call1", name="write_file",
                                input={"path": "a", "idempotency_key": "create-a"})
        journal = ToolJournal(self.root)
        invoke(journal, block, "worker:1", operation, "task:a")
        block.id = "call2"
        invoke(journal, block, "worker:2", operation, "task:a")
        operation.assert_called_once()
        self.assertEqual(operation.call_args.args[0].input, {"path": "a"})
        # A new task represents different work, even with the same key.
        invoke(journal, block, "worker:2", operation, "task:b")
        self.assertEqual(operation.call_count, 2)


if __name__ == "__main__":
    unittest.main()
