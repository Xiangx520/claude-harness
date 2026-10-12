"""Child worker integration tests with deterministic model and thread controls."""

import contextlib
import copy
import io
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
from harness.config import ConfigurationError
from harness.worker import AgentWorker


def response(text="done", *, calls=None, stop_reason="end_turn"):
    return SimpleNamespace(
        content=calls if calls is not None else [SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason)


def tool(name="glob", args=None, tool_id="tool_same"):
    return SimpleNamespace(type="tool_use", name=name,
                           input=args or {"pattern": "**/*"}, id=tool_id)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.client = Mock()
        self.client.messages.create.return_value = response()
        self.runtime = HarnessRuntime(HarnessConfig(self.root, "test-model"), self.client)
        self.threads = []
        self.events = []
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.addCleanup(self.close_threads)

    def close_threads(self):
        for name in list(self.runtime.team.active_teammates):
            self.runtime.team.run_request_shutdown(name)
        for thread in self.threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "teammate thread did not exit")

    def worker(self, **kwargs):
        options = dict(system="worker role", tools=[], execute_tool=lambda block: "ok",
                       description="current task")
        options.update(kwargs)
        return AgentWorker(self.runtime.config, self.client, self.runtime.context, **options)

    def spawn(self, name="worker", task_id=None, require_plan=False):
        real_thread = threading.Thread

        def record_thread(**kwargs):
            thread = real_thread(**kwargs)
            self.threads.append(thread)
            return thread

        with patch("harness.team.threading.Thread", side_effect=record_thread):
            result = self.runtime.team.spawn_teammate_thread(
                name, "tester", "do assigned work", task_id, require_plan)
        self.assertIn("spawned", result)
        return self.threads[-1]

    def wait_event(self, event_type, sender="worker"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for index, event in enumerate(self.events):
                if event["type"] == event_type and event["from"] == sender:
                    return self.events.pop(index)
            self.events.extend(self.runtime.bus.wait_for_messages("lead", timeout=0.1))
        self.fail(f"No {event_type} event from {sender}")

    def test_sync_adapter_uses_worker_with_fresh_history_and_hooks(self):
        requests = []
        write = tool("write_file", {"path": "sub.txt", "content": "ok"})
        replies = iter([response(calls=[write]), response("summary"), response("second")])

        def model(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            return next(replies)

        self.client.messages.create.side_effect = model
        with patch.object(AgentWorker, "run", autospec=True, side_effect=AgentWorker.run) as run:
            self.assertEqual(self.runtime.subagents.spawn_subagent("write file"), "summary")
            self.assertEqual(self.runtime.subagents.spawn_subagent("another task"), "second")
            self.assertEqual(run.call_count, 2)
        self.assertEqual((self.root / "sub.txt").read_text(), "ok")
        self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "write file"}])
        self.assertEqual(requests[2]["messages"], [{"role": "user", "content": "another task"}])
        names = {entry["name"] for entry in requests[0]["tools"]}
        self.assertNotIn("task", names)
        self.assertNotIn("spawn_teammate", names)

    def test_before_turn_can_close_without_model_request(self):
        result = self.worker(before_turn=lambda worker: True).run()
        self.assertEqual(result.status, "closed")
        self.assertEqual(result.turns, 0)
        self.client.messages.create.assert_not_called()

    def test_turn_limit_resets_for_each_run_and_does_not_return_intermediate_text(self):
        self.runtime.config.WORKER_MAX_TURNS = 2
        self.client.messages.create.return_value = response(calls=[
            SimpleNamespace(type="text", text="almost done"), tool()])
        worker = self.worker()
        for _ in range(2):
            result = worker.run()
            self.assertEqual((result.status, result.turns, result.text), ("turn_limit", 2, ""))
        self.assertEqual(self.client.messages.create.call_count, 4)
        output = self.runtime.subagents.spawn_subagent("inspect")
        self.assertIn("turn_limit", output)
        self.assertNotIn("almost done", output)

    def test_truncated_response_never_dispatches_tools(self):
        execute = Mock()
        self.client.messages.create.return_value = response(
            calls=[tool("write_file", {"path": "bad.txt", "content": "bad"})],
            stop_reason="max_tokens")
        result = self.worker(execute_tool=execute).run()
        self.assertEqual(result.status, "output_truncated")
        execute.assert_not_called()
        self.assertIn("output_truncated", self.runtime.subagents.spawn_subagent("write"))
        self.assertFalse((self.root / "bad.txt").exists())

    def test_worker_reports_model_and_callback_failures(self):
        self.client.messages.create.side_effect = RuntimeError("API unavailable")
        result = self.worker().run()
        self.assertEqual(result.status, "error")
        self.assertIn("API unavailable", result.reason)
        self.client.messages.create.side_effect = None
        self.client.messages.create.return_value = response(calls=[tool()])
        result = self.worker(execute_tool=Mock(side_effect=ValueError("broken handler"))).run()
        self.assertEqual(result.status, "error")
        self.assertIn("broken handler", result.reason)

    def test_large_tool_results_and_final_text_are_persisted_across_workers(self):
        large = "complete output\n" * 3000
        workers = [self.worker(execute_tool=lambda block: large) for _ in range(2)]
        paths = []
        for worker in workers:
            self.client.messages.create.side_effect = [response(calls=[tool()]), response(large)]
            result = worker.run()
            self.assertEqual(result.status, "finished")
            tool_result = worker.messages[2]["content"][0]
            self.assertEqual(tool_result["tool_use_id"], "tool_same")
            for output in [tool_result["content"], result.text]:
                path = self.runtime.context.persisted_output_path(output)
                self.assertIsNotNone(path)
                self.assertEqual(Path(path).read_text(encoding="utf-8"), large)
                self.assertIn(worker.execution_id, Path(path).name)
                paths.append(path)
        self.assertEqual(len(set(paths)), 4)

    def test_repeated_ids_and_changed_requests_do_not_overwrite_artifacts(self):
        worker = self.worker()
        first = worker.persist("same_id", "a" * 40000)
        second = worker.persist("same_id", "b" * 40000)
        first_path = self.runtime.context.persisted_output_path(first)
        second_path = self.runtime.context.persisted_output_path(second)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(Path(first_path).read_text(), "a" * 40000)

    def test_large_messages_keep_protocol_metadata_and_full_body(self):
        content = "large plan\n" * 4000
        metadata = {"request_id": "req_test", "task_id": "task_12345678"}
        self.runtime.bus.send("worker", "lead", content, "plan_approval_request", metadata)
        event = self.runtime.bus.read_inbox("lead")[0]
        self.assertEqual(event["metadata"], metadata)
        self.assertEqual(event["type"], "plan_approval_request")
        path = self.runtime.context.persisted_output_path(event["content"])
        self.assertEqual(Path(path).read_text(), content)

    def test_artifacts_are_published_through_existing_sandbox(self):
        sandbox = Mock()
        sandbox.file_tool.return_value = "ok"
        self.runtime.context.sandbox = sandbox
        worker = self.worker()
        self.client.messages.create.return_value = response("x" * 40000)
        self.assertEqual(worker.run().status, "finished")
        sandbox.file_tool.assert_called_once()
        self.assertTrue(sandbox.file_tool.call_args.kwargs["_internal"])

    def test_compaction_preserves_task_latest_coordination_and_tool_pairs(self):
        self.runtime.memory.extract_memories = Mock()
        self.runtime.config.CONTEXT_LIMIT = 5000
        self.runtime.config.KEEP_RECENT_TOOL_RESULTS = 0
        worker = self.worker(description="implement login")
        for index in range(8):
            worker.messages.extend([
                {"role": "assistant", "content": [tool(tool_id=f"t{index}")]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{index}",
                                                "content": "old output " * 500}]},
            ])
        worker.add_message("latest: preserve public API")
        requests = []

        def model(**kwargs):
            if "tools" not in kwargs:
                return response("old history summary")
            requests.append(copy.deepcopy(kwargs))
            return response("done")

        self.client.messages.create.side_effect = model
        self.assertEqual(worker.run().status, "finished")
        request = requests[0]
        self.assertIn("implement login", request["system"])
        self.assertIn("latest: preserve public API", request["system"])
        self.assertLessEqual(worker.context.estimate_size(request["messages"]), 5000)
        for index, message in enumerate(request["messages"]):
            if worker.context.is_tool_result_message(message):
                self.assertTrue(worker.context.message_has_tool_use(request["messages"][index - 1]))
        self.runtime.memory.extract_memories.assert_not_called()

    def test_archiving_messages_does_not_lose_current_task(self):
        worker = self.worker()
        worker.add_message("new assigned task", new_task=True)
        worker.add_message("keep backwards compatibility")
        for _ in range(55):
            worker.messages.extend([{"role": "assistant", "content": []},
                                    {"role": "user", "content": "small history"}])
        observed = []
        self.client.messages.create.side_effect = lambda **kwargs: (
            observed.append(copy.deepcopy(kwargs)) or response())
        self.assertEqual(worker.run().status, "finished")
        self.assertIn("new assigned task", observed[0]["system"])
        self.assertIn("keep backwards compatibility", observed[0]["system"])
        self.assertTrue(any(worker.context.is_archive_marker(msg)
                            for msg in observed[0]["messages"]))

    def test_full_compaction_keeps_latest_unseen_exchange(self):
        self.runtime.config.CONTEXT_LIMIT = 3500
        worker = self.worker()
        worker.messages.extend([
            {"role": "assistant", "content": [SimpleNamespace(type="text", text="x" * 5000)]},
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": [tool(tool_id="latest")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "latest",
                                            "content": "new evidence"}]},
        ])
        worker.add_message("first coordination message")
        worker.add_message("second coordination message")
        observed = []

        def model(**kwargs):
            if "tools" not in kwargs:
                return response("history summary")
            observed.append(copy.deepcopy(kwargs))
            return response()

        self.client.messages.create.side_effect = model
        self.assertEqual(worker.run().status, "finished")
        results = worker.context.collect_tool_results(observed[0]["messages"])
        self.assertEqual(results[-1][2]["content"], "new evidence")
        self.assertTrue(worker.context.message_has_tool_use(
            observed[0]["messages"][results[-1][0] - 1]))

    def test_shutdown_callback_runs_between_tool_rounds(self):
        self.client.messages.create.return_value = response(calls=[tool()])
        before = Mock(side_effect=[False, True])
        worker = self.worker(before_turn=before)
        result = worker.run()
        self.assertEqual((result.status, result.turns), ("closed", 1))
        self.client.messages.create.assert_called_once()
        self.assertTrue(worker.context.is_tool_result_message(worker.messages[-1]))

    def test_sync_tool_hooks_preserve_denials_and_post_execution(self):
        before, after = Mock(return_value=None), Mock(return_value=None)
        self.runtime.hooks.register_hook("PreToolUse", before)
        self.runtime.hooks.register_hook("PostToolUse", after)
        self.client.messages.create.side_effect = [
            response(calls=[tool("write_file", {"path": "ok.txt", "content": "ok"})]),
            response("done"),
        ]
        self.assertEqual(self.runtime.subagents.spawn_subagent("write"), "done")
        before.assert_called_once()
        after.assert_called_once()
        self.assertTrue((self.root / "ok.txt").exists())
        before.reset_mock()
        after.reset_mock()
        before.return_value = "Blocked by custom hook"
        self.client.messages.create.side_effect = [
            response(calls=[tool("write_file", {"path": "blocked.txt", "content": "bad"})]),
            response("denied"),
        ]
        self.runtime.subagents.spawn_subagent("write")
        self.assertFalse((self.root / "blocked.txt").exists())
        after.assert_not_called()

    def test_capacity_counts_all_states_and_refuses_before_claiming(self):
        team = self.runtime.team
        team.active_teammates.update(a="idle", b="waiting_approval", c="stopping")
        task = self.runtime.tasks.create_task("unclaimed")
        with patch("harness.team.threading.Thread") as thread:
            result = team.spawn_teammate_thread("d", "tester", "work", task.id)
        self.assertIn("MAX_TEAMMATES", result)
        self.assertIn("Reuse", result)
        thread.assert_not_called()
        self.assertEqual(self.runtime.tasks.load_task(task.id).status, "pending")
        self.assertNotIn("d", self.runtime.tasks.teammate_assignments)
        team.active_teammates.clear()

    def test_concurrent_spawns_reserve_capacity_atomically(self):
        real_thread = threading.Thread
        barrier = threading.Barrier(9)
        results = []

        def spawn(index):
            barrier.wait()
            results.append(self.runtime.team.spawn_teammate_thread(str(index), "tester", "work"))

        with patch("harness.team.threading.Thread") as start_thread:
            callers = [real_thread(target=spawn, args=(index,)) for index in range(8)]
            for caller in callers:
                caller.start()
            barrier.wait()
            for caller in callers:
                caller.join(timeout=3)
                self.assertFalse(caller.is_alive())
            self.assertEqual(start_thread.call_count, 3)
        self.assertEqual(sum("spawned" in result for result in results), 3)
        self.assertEqual(len(self.runtime.team.active_teammates), 3)
        for name in list(self.runtime.team.active_teammates):
            self.runtime.team._release_teammate(name)

    def test_start_failure_rolls_back_task_and_slot(self):
        task = self.runtime.tasks.create_task("recover")
        with patch("harness.team.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start thread")
            result = self.runtime.team.spawn_teammate_thread("worker", "tester", "work", task.id)
        self.assertIn("cannot start thread", result)
        self.assertEqual(self.runtime.team.active_teammates, {})
        self.assertEqual(self.runtime.team.plan_gates, {})
        self.assertEqual(self.runtime.tasks.teammate_assignments, {})
        restored = self.runtime.tasks.load_task(task.id)
        self.assertEqual((restored.status, restored.owner), ("pending", None))

    def test_claim_failure_releases_slot(self):
        with patch.object(self.runtime.tasks, "claim_task", side_effect=OSError("claim failed")):
            result = self.runtime.team.spawn_teammate_thread("worker", "tester", "work", "task_12345678")
        self.assertIn("claim failed", result)
        self.assertEqual(self.runtime.team.active_teammates, {})

    def test_claim_exception_after_persisting_assignment_rolls_back_work(self):
        task = self.runtime.tasks.create_task("partially claimed")
        claim = self.runtime.tasks.claim_task

        def fail_after_claim(task_id, owner):
            claim(task_id, owner)
            raise OSError("after claim")

        with patch.object(self.runtime.tasks, "claim_task", side_effect=fail_after_claim):
            result = self.runtime.team.spawn_teammate_thread("worker", "tester", "work", task.id)
        self.assertIn("after claim", result)
        self.assertEqual(self.runtime.team.active_teammates, {})
        self.assertEqual(self.runtime.tasks.load_task(task.id).status, "pending")
        self.assertEqual(self.runtime.tasks.teammate_assignments, {})

    def test_teammate_uses_worker_runs_in_background_and_wakes_with_history(self):
        entered, release = threading.Event(), threading.Event()
        requests = []

        def model(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return response("result " + str(len(requests)))

        self.addCleanup(release.set)
        self.client.messages.create.side_effect = model
        with patch.object(AgentWorker, "run", autospec=True, side_effect=AgentWorker.run) as run:
            thread = self.spawn()
            self.assertTrue(entered.wait(3))
            self.assertTrue(thread.is_alive())
            release.set()
            self.wait_event("idle_notification")
            self.runtime.team.run_send_message("worker", "next request")
            self.wait_event("idle_notification")
            self.assertEqual(run.call_count, 2)
        self.assertGreater(len(requests[1]["messages"]), len(requests[0]["messages"]))
        self.assertIn("next request", requests[1]["system"])

    def test_teammate_limit_and_model_error_restore_assignment(self):
        self.runtime.config.WORKER_MAX_TURNS = 1
        for failure in [response(calls=[tool()]), RuntimeError("API failed")]:
            task = self.runtime.tasks.create_task("recover abandoned work")
            if isinstance(failure, Exception):
                self.client.messages.create.side_effect = failure
            else:
                self.client.messages.create.return_value = failure
            thread = self.spawn(task_id=task.id)
            event = self.wait_event("error")
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertIn(event["metadata"]["status"], {"turn_limit", "error"})
            self.assertEqual(event["metadata"]["task_id"], task.id)
            restored = self.runtime.tasks.load_task(task.id)
            self.assertEqual((restored.status, restored.owner), ("pending", None))
            self.assertNotIn("worker", self.runtime.team.active_teammates)
            # Prevent the next worker from auto-claiming the abandoned task.
            self.runtime.tasks.claim_task(task.id)
            self.runtime.tasks.complete_task(task.id)
            self.runtime.tasks.release_completed_assignment("agent")

    def test_teammate_truncated_output_cannot_write_file(self):
        task = self.runtime.tasks.create_task("write")
        self.client.messages.create.return_value = response(
            calls=[tool("write_file", {"path": "bad.txt", "content": "bad"})],
            stop_reason="max_tokens")
        thread = self.spawn(task_id=task.id)
        event = self.wait_event("error")
        thread.join(timeout=3)
        self.assertEqual(event["metadata"]["status"], "output_truncated")
        self.assertFalse((self.root / "bad.txt").exists())
        self.assertEqual(self.runtime.tasks.load_task(task.id).status, "pending")

    def test_teammate_shell_keeps_existing_asynchronous_approval_restriction(self):
        task = self.runtime.tasks.create_task("shell task")
        observed = []
        replies = iter([response(calls=[tool("bash", {"command": "echo test"})]),
                        response("shell denied")])

        def model(**kwargs):
            observed.append(copy.deepcopy(kwargs))
            return next(replies)

        self.client.messages.create.side_effect = model
        with patch.object(self.runtime.tools, "run_bash") as bash:
            self.spawn(task_id=task.id)
            self.wait_event("idle_notification")
            bash.assert_not_called()
        self.assertIn("interactive shell approval is unavailable",
                      observed[1]["messages"][-1]["content"][0]["content"])

    def test_teammate_plan_gate_and_shutdown_survive_worker_refactor(self):
        task = self.runtime.tasks.create_task("planned")
        snapshots = []
        calls = iter([
            response(calls=[tool("write_file", {"path": "blocked.txt", "content": "bad"})]),
            response(calls=[tool("submit_plan", {"plan": "write approved.txt"})]),
            response("waiting"),
            response(calls=[tool("write_file", {"path": "approved.txt", "content": "ok"})]),
            response(calls=[tool("complete_task", {"task_id": task.id})]),
            response("finished"),
        ])
        waiting = threading.Event()
        wait_for_messages = self.runtime.bus.wait_for_messages

        def observe_wait(agent, timeout=None):
            if agent == "worker" and self.runtime.team.active_teammates.get(agent) == "waiting_approval":
                waiting.set()
            return wait_for_messages(agent, timeout)

        def model(**kwargs):
            snapshots.append(copy.deepcopy(kwargs))
            return next(calls)

        self.client.messages.create.side_effect = model
        with patch.object(self.runtime.bus, "wait_for_messages", side_effect=observe_wait):
            thread = self.spawn(task_id=task.id, require_plan=True)
            event = self.wait_event("plan_approval_request")
            self.assertTrue(waiting.wait(3))
            self.assertFalse((self.root / "blocked.txt").exists())
            self.runtime.team.run_review_plan(event["metadata"]["request_id"], True)
            self.wait_event("idle_notification")
        self.assertEqual((self.root / "approved.txt").read_text(), "ok")
        self.assertEqual(self.runtime.tasks.load_task(task.id).status, "completed")
        self.assertIn("Plan status: approved", snapshots[3]["system"])
        self.runtime.team.run_request_shutdown("worker")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.wait_event("shutdown_response")["metadata"]["approve"], True)

    def test_idle_teammate_auto_claims_task_with_fresh_execution_budget(self):
        self.runtime.config.WORKER_MAX_TURNS = 2
        self.runtime.team.IDLE_SCAN_INTERVAL = 0.01
        self.client.messages.create.return_value = response("ready")
        thread = self.spawn()
        self.wait_event("idle_notification")
        observed = []
        new_task = threading.Event()
        task_created = threading.Event()
        self.addCleanup(task_created.set)

        def model(**kwargs):
            self.assertTrue(task_created.wait(3))
            observed.append(copy.deepcopy(kwargs))
            if len(observed) == 1:
                return response(calls=[tool("complete_task", {"task_id": task.id})])
            new_task.set()
            return response("completed new task")

        self.client.messages.create.side_effect = model
        task = self.runtime.tasks.create_task("next job", "specific task instructions")
        task_created.set()
        self.assertTrue(new_task.wait(3))
        self.wait_event("idle_notification")
        self.assertEqual(self.runtime.tasks.load_task(task.id).status, "completed")
        self.assertIn("specific task instructions", observed[0]["system"])
        self.assertTrue(thread.is_alive())

    def test_teammate_large_final_result_is_readable_and_preserves_task_identity(self):
        task = self.runtime.tasks.create_task("produce report")
        text = "report line\n" * 4000
        self.client.messages.create.return_value = response(text)
        self.spawn(task_id=task.id)
        event = self.wait_event("result")
        path = self.runtime.context.persisted_output_path(event["content"])
        self.assertEqual(Path(path).read_text(), text)
        self.assertEqual(event["metadata"]["task_id"], task.id)
        self.assertIn(event["metadata"]["execution_id"], Path(path).name)
        # Model ending an execution must not auto-complete the task board.
        self.assertEqual(self.runtime.tasks.load_task(task.id).status, "in_progress")

    def test_next_assignment_replaces_previous_task_in_current_context(self):
        first = self.runtime.tasks.create_task("first job", "obsolete instructions")
        second = self.runtime.tasks.create_task("second job", "current instructions")
        self.runtime.team.IDLE_SCAN_INTERVAL = 0.01
        self.runtime.config.WORKER_MAX_TURNS = 2
        observed = []
        replies = iter([
            response(calls=[tool("complete_task", {"task_id": first.id})]), response("first done"),
            response(calls=[tool("complete_task", {"task_id": second.id})]), response("second done"),
        ])

        def model(**kwargs):
            observed.append(copy.deepcopy(kwargs))
            return next(replies)

        self.client.messages.create.side_effect = model
        self.spawn(task_id=first.id)
        self.wait_event("idle_notification")
        self.wait_event("idle_notification")
        self.assertEqual(self.runtime.tasks.load_task(second.id).status, "completed")
        self.assertIn("current instructions", observed[2]["system"])
        self.assertNotIn("obsolete instructions", observed[2]["system"])

    def test_limits_validate_constructor_values(self):
        for name in ["max_teammates", "worker_max_turns"]:
            for invalid in [0, -1, True, 1.5, "3"]:
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ConfigurationError):
                        HarnessConfig(self.root, "test", **{name: invalid})


if __name__ == "__main__":
    unittest.main()
