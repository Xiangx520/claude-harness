import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import HarnessConfig, HarnessRuntime
from harness.mcp import MCPClient
from harness.recovery import RecoveryState


def response(text="[]", blocks=None, stop_reason="end_turn"):
    return SimpleNamespace(
        content=blocks if blocks is not None else [
            SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
    )


def tool(name, args, tool_id="tool_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=tool_id)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.client = Mock()
        self.client.messages.create.return_value = response()
        self.runtime = HarnessRuntime(
            HarnessConfig(self.root, "primary", "fallback"), self.client)
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_runtime_instances_do_not_share_mutable_state(self):
        other = HarnessRuntime(self.runtime.config, self.client)
        self.runtime.tools.run_todo_write([
            {"content": "inspect", "status": "pending"}])
        self.runtime.team.active_teammates["worker"] = "idle"
        self.runtime.mcp.connect_mcp("docs")
        self.assertEqual(other.tools.CURRENT_TODOS, [])
        self.assertEqual(other.team.active_teammates, {})
        self.assertEqual(other.mcp.mcp_clients, {})
        self.assertIsNot(other.tasks.task_lock, self.runtime.tasks.task_lock)

    def test_task_dependencies_and_assignment_lease(self):
        tasks = self.runtime.tasks
        first = tasks.create_task("  inspect  ", "details")
        second = tasks.create_task("implement")
        tasks.update_task(second.id, [first.id, first.id])
        self.assertEqual(tasks.load_task(second.id).blockedBy, [first.id])
        self.assertTrue(tasks.claim_task(second.id).startswith("Blocked by:"))
        self.assertTrue(tasks.claim_task(first.id).startswith("Claimed "))
        self.assertTrue(tasks.complete_task(first.id).startswith("Completed "))
        self.assertIn("finish the current work turn", tasks.claim_task(second.id))
        self.assertTrue(tasks.release_completed_assignment("agent"))
        self.assertTrue(tasks.claim_task(second.id).startswith("Claimed "))
        self.assertEqual(tasks.assignment_cwd("agent"), self.root)
        self.assertEqual(tasks.load_task(first.id).subject, "inspect")

    def test_task_cycles_and_invalid_ids_are_rejected(self):
        tasks = self.runtime.tasks
        first = tasks.create_task("first")
        second = tasks.create_task("second")
        tasks.update_task(first.id, [second.id])
        with self.assertRaisesRegex(ValueError, "cycle"):
            tasks.update_task(second.id, [first.id])
        with self.assertRaisesRegex(ValueError, "Invalid task ID"):
            tasks.load_task("../outside")
        with self.assertRaisesRegex(ValueError, "itself"):
            tasks.update_task(second.id, [second.id])

    def test_only_one_thread_claims_a_task(self):
        tasks = self.runtime.tasks
        task = tasks.create_task("exclusive")
        barrier = threading.Barrier(5)
        results = []

        def claim(index):
            barrier.wait()
            results.append(tasks.claim_task(task.id, f"worker_{index}"))

        workers = [threading.Thread(target=claim, args=(i,)) for i in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(result.startswith("Claimed ") for result in results), 1)

    def test_abandoned_teammate_task_returns_to_board(self):
        tasks = self.runtime.tasks
        task = tasks.create_task("abandoned")
        tasks.claim_task(task.id, "worker")
        tasks.release_teammate_assignment("worker")
        restored = tasks.load_task(task.id)
        self.assertEqual(restored.status, "pending")
        self.assertIsNone(restored.owner)
        self.assertNotIn("worker", tasks.teammate_assignments)

    def test_plan_approval_blocks_completion_and_rejects_stale_responses(self):
        tasks, team = self.runtime.tasks, self.runtime.team
        task = tasks.create_task("planned")
        team.active_teammates["worker"] = "working"
        team.plan_gates["worker"] = "required"
        tasks.claim_task(task.id, "worker")
        self.assertIn("plan status is required", tasks.complete_task(task.id, "worker"))
        team._teammate_submit_plan("worker", "Inspect, then edit.")
        request_id = team.plan_request_ids["worker"]
        self.assertIn("approved", team.run_review_plan(request_id, True))
        message = self.runtime.bus.read_inbox("worker")[0]
        accepted, _ = team.apply_plan_response("worker", message)
        self.assertTrue(accepted)
        self.assertTrue(tasks.complete_task(task.id, "worker").startswith("Completed "))
        tasks.release_completed_assignment("worker")
        accepted, _ = team.apply_plan_response("worker", message)
        self.assertFalse(accepted)

    def test_shutdown_protocol_matches_sender_and_recipient(self):
        team = self.runtime.team
        team.active_teammates["worker"] = "idle"
        team.run_request_shutdown("worker")
        message = self.runtime.bus.read_inbox("worker")[0]
        accepted, request_id = team.apply_shutdown_request("worker", message)
        self.assertTrue(accepted)
        self.assertFalse(team.match_response(
            "shutdown_response", request_id, True, "impostor", "lead"))
        self.assertTrue(team.match_response(
            "shutdown_response", request_id, True, "worker", "lead"))

    def test_teammate_names_and_missing_task_fail_before_thread_start(self):
        team = self.runtime.team
        for name in ["lead", "AGENT", "../worker"]:
            self.assertIn("name", team.spawn_teammate_thread(name, "tester", "check"))
        self.assertIn("Cannot spawn", team.spawn_teammate_thread(
            "worker", "tester", "check", "task_00000000"))
        self.assertEqual(team.active_teammates, {})

    def test_teammate_thread_completes_assignment_and_accepts_shutdown(self):
        tasks, team, bus = self.runtime.tasks, self.runtime.team, self.runtime.bus
        task = tasks.create_task("teammate work")
        self.client.messages.create.side_effect = [
            response(blocks=[tool("complete_task", {"task_id": task.id})]),
            response("teammate finished"),
        ]
        self.assertIn("spawned", team.spawn_teammate_thread(
            "worker", "tester", "complete assigned work", task.id))
        deadline = time.monotonic() + 3
        events = []
        while time.monotonic() < deadline:
            events.extend(bus.wait_for_messages("lead", timeout=0.1))
            if any(event["type"] == "idle_notification" for event in events):
                break
        self.assertTrue(any(event["type"] == "result" and event["content"] == "teammate finished"
                            for event in events))
        self.assertEqual(tasks.load_task(task.id).status, "completed")
        self.assertNotIn("worker", tasks.teammate_assignments)
        team.run_request_shutdown("worker")
        while "worker" in team.active_teammates and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn("worker", team.active_teammates)
        events.extend(team.consume_lead_inbox())
        self.assertTrue(any(event["type"] == "shutdown_response" for event in events))

    def test_mailboxes_deliver_once_and_validate_paths(self):
        bus = self.runtime.bus
        bus.send("lead", "worker", "hello", metadata={"request_id": "req_1"})
        self.assertTrue(bus.peek("worker"))
        messages = bus.wait_for_messages("worker", timeout=0.1)
        self.assertEqual(messages[0]["content"], "hello")
        self.assertEqual(messages[0]["metadata"]["request_id"], "req_1")
        self.assertEqual(bus.read_inbox("worker"), [])
        with self.assertRaises(ValueError):
            bus.send("lead", "../outside", "invalid")

    def test_filesystem_tools_keep_original_outputs_and_boundaries(self):
        tools = self.runtime.tools
        self.assertEqual(tools.run_write("nested/file.txt", "one\ntwo\none"),
                         "Wrote 11 bytes to nested/file.txt")
        self.assertEqual(tools.run_edit("nested/file.txt", "one", "changed"),
                         "Edited nested/file.txt")
        self.assertEqual(tools.run_read("nested/file.txt", limit=1, offset=1),
                         "two\n... (1 more lines)")
        self.assertEqual(tools.run_glob("**/*.txt"), str(Path("nested/file.txt")))
        self.assertIn("text not found", tools.run_edit("nested/file.txt", "missing", "x"))
        self.assertIn("Path escapes workspace", tools.run_write("../escape.txt", "x"))

    def test_shell_success_and_nonzero_exit_are_reported(self):
        tools = self.runtime.tools
        self.assertEqual(tools.run_bash("echo harness-smoke"), "harness-smoke")
        self.assertIn("status 7", tools.run_bash("exit 7"))

    def test_todos_accept_json_and_reject_invalid_status(self):
        tools = self.runtime.tools
        self.assertEqual(tools.run_todo_write(
            '[{"content": "review", "status": "pending"}]'), "Updated 1 todos")
        self.assertIn("invalid status", tools.run_todo_write([
            {"content": "review", "status": "invalid"}]))
        self.assertEqual(tools.CURRENT_TODOS[0]["status"], "pending")

    def test_hooks_keep_permissions_and_dispatch_order(self):
        hooks = self.runtime.hooks
        self.assertIn("deny list", hooks.permission_hook(
            tool("bash", {"command": "sudo something"})))
        self.runtime.console.reader = lambda prompt: "n"
        self.assertEqual(hooks.permission_hook(tool("bash", {"command": "echo check"})),
                         "Permission denied by user")
        self.runtime.console.reader = lambda prompt: "y"
        self.assertIsNone(hooks.permission_hook(tool("bash", {"command": "echo check"})))
        self.assertIn("outside the workspace", hooks.permission_hook(
            tool("read_file", {"path": "../escape.txt"})))
        self.runtime.mcp.connect_mcp("docs")
        self.runtime.registry.assemble_tool_pool()
        self.assertIsNone(hooks.permission_hook(tool("mcp__docs__search", {"query": "x"})))

    def test_skill_frontmatter_and_catalog(self):
        directory = self.root / "skills" / "example"
        directory.mkdir(parents=True)
        manifest = '---\nname: sample\ndescription: Read examples\n---\n# Example\n'
        (directory / "SKILL.md").write_text(manifest, encoding="utf-8")
        self.runtime.skills.scan_skills()
        self.assertEqual(self.runtime.skills.load_skill("sample"), manifest)
        self.assertIn("sample: Read examples", self.runtime.skills.list_skills())
        self.assertIn("Available: sample", self.runtime.skills.load_skill("unknown"))

    def test_mcp_tools_use_host_policy_and_keep_bound_handlers(self):
        self.runtime.mcp.connect_mcp("docs")
        self.runtime.mcp.connect_mcp("deploy")
        definitions, handlers = self.runtime.registry.assemble_tool_pool()
        self.assertEqual(len(definitions), 30)
        self.assertEqual(handlers["mcp__docs__get_version"](), "[docs] API v2.1.0")
        self.assertEqual(handlers["mcp__deploy__status"](service="api"),
                         "[deploy] api: running (v1.4.2)")
        self.assertEqual(self.runtime.hooks.mcp_tool_policies["mcp__docs__search"], "allow")
        self.assertEqual(self.runtime.hooks.mcp_tool_policies["mcp__deploy__trigger"], "confirm")

    def test_mcp_name_collisions_are_rejected(self):
        for name in ["a.b", "a_b"]:
            client = MCPClient(name)
            client.register([{"name": "read", "inputSchema": {"type": "object"}}],
                            {"read": lambda: "ok"})
            self.runtime.mcp.mcp_clients[name] = client
        with self.assertRaisesRegex(ValueError, "collision"):
            self.runtime.registry.assemble_tool_pool()

    def test_memory_persistence_and_keyword_fallback(self):
        memory = self.runtime.memory
        path = memory.write_memory_file("Python preference", "user", "Python formatting", "Use spaces.")
        self.assertIn("Python preference", memory.read_memory_index())
        self.assertEqual(memory.list_memory_files()[0]["filename"], path.name)
        self.client.messages.create.side_effect = RuntimeError("offline")
        loaded = json.loads(memory.load_memories([
            {"role": "user", "content": "Python formatting"}]))
        self.assertEqual(loaded[0]["source"], path.name)
        self.assertIn("Use spaces.", loaded[0]["content"])
        self.assertIsNone(memory.read_memory_file("../outside.md"))
        self.assertIsNone(memory.read_memory_file("MEMORY.md"))

    def test_memory_extraction_ignores_temporary_and_duplicate_records(self):
        persistent = {"name": "Formatting", "type": "user", "scope": "persistent",
                      "description": "Use spaces", "body": "Indent Python with spaces."}
        temporary = dict(persistent, name="Temporary", body="Use tabs this session.")
        self.client.messages.create.return_value = response(text=json.dumps([
            persistent, temporary, persistent]))
        self.assertEqual(self.runtime.memory.extract_memories([
            {"role": "user", "content": "Remember my formatting preference."}]), 1)
        self.assertEqual(len(self.runtime.memory.list_memory_files()), 1)

    def test_memory_consolidation_restores_snapshot_after_write_failure(self):
        memory = self.runtime.memory
        for index in range(10):
            memory.write_memory_file(f"Record {index}", "project", f"Fact {index}", f"Body {index}")
        before = {path.name: path.read_text(encoding="utf-8")
                  for path in memory.MEMORY_DIR.glob("*.md")}
        self.client.messages.create.return_value = response(text=json.dumps([
            {"name": "Merged", "type": "project", "description": "Facts", "body": "Merged facts."}]))
        original_write = Path.write_text

        def fail_merged(path, *args, **kwargs):
            if path.name == "merged.md":
                raise OSError("injected write failure")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", fail_merged):
            self.assertEqual(memory.consolidate_memories(), 0)
        after = {path.name: path.read_text(encoding="utf-8")
                 for path in memory.MEMORY_DIR.glob("*.md")}
        self.assertEqual(after, before)

    def test_compaction_preserves_unseen_results_and_tool_pairs(self):
        context = self.runtime.context
        messages = []
        for index in range(7):
            messages.extend([
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": str(index), "name": "read_file", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": str(index), "content": "x" * 500}]},
            ])
        messages = context.micro_compact(copy.deepcopy(messages))
        self.assertIn("Earlier tool result saved at", messages[1]["content"][0]["content"])
        self.assertEqual(messages[-1]["content"][0]["content"], "x" * 500)
        snipped = context.snip_compact(messages, max_messages=8)
        self.assertTrue(any(context.is_archive_marker(message) for message in snipped))
        for index, message in enumerate(snipped):
            if context.is_tool_result_message(message):
                self.assertTrue(context.message_has_tool_use(snipped[index - 1]))

    def test_oversized_outputs_are_persisted_once(self):
        context = self.runtime.context
        large = "x" * 40000
        preview = context.persist_large_output("tool/unsafe", large)
        path = context.persisted_output_path(preview)
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), large)
        self.assertEqual(context.persisted_output_path(
            context.persisted_preview("tool/unsafe", preview)), path)
        self.assertEqual(len(list(self.runtime.config.TOOL_RESULTS_DIR.glob("*.txt"))), 1)

    def test_retry_switches_model_after_two_overloads(self):
        state = RecoveryState("primary")
        call = Mock(side_effect=[RuntimeError("529 overloaded"), RuntimeError("529 overloaded"), "ok"])
        with patch("harness.recovery.time.sleep"):
            self.assertEqual(self.runtime.recovery.with_retry(call, state), "ok")
        self.assertEqual(state.current_model, "fallback")
        self.assertEqual(state.consecutive_529, 0)

    def test_cron_validation_and_day_of_month_or_week(self):
        scheduler = self.runtime.scheduler
        self.assertIsNone(scheduler.validate_cron("*/5 9-17 * * 1,3,5"))
        self.assertIn("minute", scheduler.validate_cron("60 * * * *"))
        self.assertTrue(scheduler.cron_matches("0 9 1 * 1", datetime(2026, 10, 5, 9)))
        self.assertTrue(scheduler.cron_matches("0 9 1 * 1", datetime(2026, 10, 1, 9)))
        self.assertFalse(scheduler.cron_matches("0 9 1 * 1", datetime(2026, 10, 2, 9)))

    def test_one_shot_cron_survives_reload_until_acknowledged(self):
        scheduler = self.runtime.scheduler
        job = scheduler.schedule_job("* * * * *", "reminder", recurring=False)
        scheduler._enqueue_due_job(job)
        fired = scheduler.consume_cron_queue()
        other = HarnessRuntime(self.runtime.config, self.client).scheduler
        other.load_durable_jobs()
        self.assertEqual(other.cron_queue[0].id, job.id)
        scheduler.restore_cron_jobs(fired)
        scheduler.restore_cron_jobs(fired)
        self.assertEqual(len(scheduler.cron_queue), 1)
        scheduler.acknowledge_cron_jobs(scheduler.consume_cron_queue())
        self.assertEqual(scheduler.scheduled_jobs, {})
        self.assertEqual(json.loads(scheduler.DURABLE_PATH.read_text(encoding="utf-8")), [])

    def test_background_failure_is_delivered_once(self):
        background = self.runtime.background
        with patch.object(self.runtime.tools, "_run_bash_process", return_value=("failed", 2)):
            bg_id = background.start_background_task(tool("bash", {"command": "fake"}), {})
            deadline = time.monotonic() + 3
            while not background.has_pending_background() and time.monotonic() < deadline:
                time.sleep(0.01)
            notes = background.collect_background_results()
        self.assertEqual(len(notes), 1)
        self.assertIn(bg_id, notes[0])
        self.assertIn("<status>failed</status>", notes[0])
        self.assertEqual(background.collect_background_results(), [])

    def test_agent_tool_feedback_then_final_text(self):
        self.client.messages.create.side_effect = [
            response(blocks=[tool("write_file", {"path": "result.txt", "content": "done"})], stop_reason="tool_use"),
            response("finished"), response(),
        ]
        messages = [{"role": "user", "content": "write result"}]
        self.runtime.agent.agent_loop(messages, {}, "write result")
        self.assertEqual((self.root / "result.txt").read_text(encoding="utf-8"), "done")
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], "tool_1")
        self.assertEqual(messages[-1]["content"][0].text, "finished")

    def test_agent_escalates_tokens_and_continues_without_first_partial(self):
        self.client.messages.create.side_effect = [
            response("discarded partial", stop_reason="max_tokens"),
            response("retained partial", stop_reason="max_tokens"),
            response("finished"), response(),
        ]
        messages = [{"role": "user", "content": "finish task"}]
        self.runtime.agent.agent_loop(messages, {}, "finish task")
        calls = self.client.messages.create.call_args_list
        self.assertEqual([call.kwargs["max_tokens"] for call in calls[:3]], [8000, 16000, 16000])
        self.assertFalse(any("discarded partial" in str(message) for message in messages))
        self.assertIn(self.runtime.config.CONTINUATION_PROMPT, [message["content"] for message in messages
                                                              if isinstance(message["content"], str)])

    def test_failed_model_call_restores_scheduled_delivery(self):
        scheduler = self.runtime.scheduler
        job = scheduler.schedule_job("* * * * *", "recover me", recurring=False)
        scheduler._enqueue_due_job(job)
        self.client.messages.create.side_effect = ValueError("model unavailable")
        messages = [{"role": "user", "content": "start"}]
        self.runtime.agent.agent_loop(messages, {}, "start")
        self.assertEqual([queued.id for queued in scheduler.cron_queue], [job.id])
        self.assertIn("model unavailable", messages[-1]["content"][0]["text"])

    def test_agent_reactively_compacts_only_once_after_prompt_too_long(self):
        self.client.messages.create.side_effect = [
            ValueError("prompt too long"), response("old state summary"),
            ValueError("prompt too long"),
        ]
        messages = [{"role": "user", "content": "finish task"}]
        self.runtime.agent.agent_loop(messages, {}, "finish task")
        self.assertEqual(self.client.messages.create.call_count, 3)
        self.assertIn("Authoritative request:\nfinish task", messages[0]["content"])
        self.assertIn("prompt too long", messages[-1]["content"][0]["text"])

    def test_subagent_uses_fresh_history_and_builtin_tools(self):
        self.client.messages.create.side_effect = [
            response(blocks=[tool("write_file", {"path": "sub.txt", "content": "ok"})]),
            response("subagent summary"),
        ]
        self.assertEqual(self.runtime.subagents.spawn_subagent("write a file"), "subagent summary")
        self.assertEqual((self.root / "sub.txt").read_text(encoding="utf-8"), "ok")
        self.assertEqual(self.client.messages.create.call_args.kwargs["model"], "primary")

    def test_cli_retains_prompt_and_quit_behavior(self):
        self.runtime.console.reader = Mock(return_value="q")
        with patch.object(self.runtime.scheduler, "start_runtime_services"), patch("harness.cli.threading.Thread"):
            self.runtime.cli.run()
        self.runtime.console.reader.assert_called_once_with(self.runtime.config.READLINE_PROMPT)
        self.assertTrue(self.runtime.console.CLI_ACTIVE)
        self.client.messages.create.assert_not_called()

    def test_worktree_creation_routes_tools_and_preserves_branch_on_removal(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True,
                                  capture_output=True, text=True).stdout.strip()

        git("init")
        (self.root / "initial.txt").write_text("initial", encoding="utf-8")
        git("add", "initial.txt")
        git("-c", "user.name=Harness Test", "-c", "user.email=harness-test@example.invalid",
            "commit", "-m", "initial")
        task = self.runtime.tasks.create_task("isolated")
        result = self.runtime.worktrees.create_worktree("isolated", task.id)
        self.assertIn("created at", result)
        self.runtime.tasks.claim_task(task.id)
        self.runtime.tools.run_agent_write("isolated.txt", "only in worktree")
        checkout = self.root / ".worktrees" / "isolated"
        self.assertTrue((checkout / "isolated.txt").is_file())
        self.assertFalse((self.root / "isolated.txt").exists())
        self.runtime.tasks.complete_task(task.id)
        self.assertIn("still in use", self.runtime.worktrees.remove_worktree("isolated"))
        self.runtime.tasks.release_completed_assignment("agent")
        self.assertIn("uncommitted", self.runtime.worktrees.remove_worktree("isolated"))
        (checkout / "isolated.txt").unlink()
        self.assertIn("branch 'wt/isolated'", self.runtime.worktrees.remove_worktree("isolated"))
        self.assertFalse(checkout.exists())
        self.assertIn("wt/isolated", git("branch", "--list"))
        self.assertIsNone(self.runtime.tasks.load_task(task.id).worktree)


if __name__ == "__main__":
    unittest.main()
