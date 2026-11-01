import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness import HarnessConfig, HarnessRuntime
from harness.sessions import SessionStore, SessionSnapshot, SessionError, WorkspaceLock, plain
from harness.recovery import RecoveryState


def response(*blocks):
    return SimpleNamespace(content=list(blocks) or [SimpleNamespace(type="text", text="done")], stop_reason="end_turn")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = Mock()
        self.client.messages.create.return_value = response()
        self.runtime = HarnessRuntime(HarnessConfig(self.root, "primary", "fallback"), self.client)
        self.runtime.context.remember_after_turn = Mock()

    def test_roundtrip_goal_skills_usage(self):
        a = self.runtime.agent
        self.runtime.goal.set_goal("verify", 14)
        self.runtime.goal.active.iterations = 3
        a.total_tokens = 42
        self.runtime.skills.active = {"test": "original"}
        history = [{"role": "user", "content": "request"}]
        a.checkpoint(history, "request")
        other = HarnessRuntime(self.runtime.config, self.client)
        other.context.remember_after_turn = Mock()
        restored = []
        other.agent.restore_session(a.snapshot.session_id, restored)
        self.assertEqual(restored, history)
        self.assertEqual(other.agent.execution_id, a.execution_id)
        self.assertEqual(other.agent.total_tokens, 42)
        self.assertEqual(other.goal.active.tokens_at_start, 14)
        self.assertEqual(other.goal.active.iterations, 3)
        self.assertEqual(other.skills.active, {"test": "original"})
        self.assertTrue(other.agent.paused)

    def test_corrupt_latest_falls_back_and_preserves_previous(self):
        store = SessionStore(self.root)
        snap = SessionSnapshot(data={"value": 1})
        store.save(snap)
        snap.data = {"value": 2}
        store.save(snap)
        with store.connect() as db:
            db.execute("UPDATE snapshots SET payload='broken' WHERE generation=2")
        loaded, fallback = store.load(snap.session_id)
        self.assertTrue(fallback)
        self.assertEqual(loaded.data, {"value": 1})
        store.save(loaded)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)

    def test_incompatible_version_refused(self):
        store = SessionStore(self.root)
        snap = SessionSnapshot(version=9)
        store.save(snap)
        with self.assertRaises(SessionError):
            store.load(snap.session_id)

    def test_host_lock_excludes_other_host(self):
        with WorkspaceLock(self.root):
            with self.assertRaises(SessionError):
                with WorkspaceLock(self.root):
                    self.fail("second host acquired lock")
        with WorkspaceLock(self.root):
            pass

    def test_crash_after_effect_replays_journal_not_operation(self):
        a = self.runtime.agent
        block = SimpleNamespace(type="tool_use", name="write_file", id="write", input={"path": "a", "content": "one"})
        self.client.messages.create.side_effect = [response(block), response()]
        original = a.checkpoint
        crashed = False
        def checkpoint(messages, active_request=None):
            nonlocal crashed
            if a.loop_state and a.loop_state.get("results") and not crashed:
                crashed = True
                raise KeyboardInterrupt("crash before result checkpoint")
            return original(messages, active_request)
        history = [{"role": "user", "content": "write"}]
        with patch.object(a, "checkpoint", side_effect=checkpoint):
            with self.assertRaises(KeyboardInterrupt):
                a.agent_loop(history, {}, "write")
        other = HarnessRuntime(self.runtime.config, self.client)
        other.context.remember_after_turn = Mock()
        restored = []
        other.agent.restore_session(a.snapshot.session_id, restored)
        with patch.object(other.tools, "call_tool_handler", side_effect=AssertionError("effect replayed")):
            other.agent.agent_loop(restored, {}, "write", resume=True)
        self.assertEqual((self.root / "a").read_text(), "one")
        self.assertEqual(len([m for m in restored if m["role"] == "assistant" and any(b.get("type") == "tool_use" for b in m["content"])]), 1)

    def test_unknown_outcome_never_reexecutes(self):
        a = self.runtime.agent
        block = SimpleNamespace(type="tool_use", name="write_file", id="unknown", input={"path": "a", "content": "one"})
        with self.assertRaises(RuntimeError):
            a.journal.execute(a.execution_id, "call:unknown", block.name, block.input, lambda: (_ for _ in ()).throw(RuntimeError("lost")))
        self.client.messages.create.side_effect = [response(block), response()]
        history = [{"role": "user", "content": "write"}]
        with patch.object(self.runtime.tools, "call_tool_handler", side_effect=AssertionError("effect replayed")):
            a.agent_loop(history, {}, "write")
        self.assertIn("TOOL_OUTCOME_UNKNOWN", history[-2]["content"][0]["content"])

    def test_mail_checkpoint_before_consumption_and_dedup(self):
        a = self.runtime.agent
        history = []
        a.bind_deliveries(history)
        self.runtime.bus.send("worker", "lead", "result")
        with patch.object(a, "checkpoint", side_effect=SessionError("disk full")):
            with self.assertRaises(SessionError):
                self.runtime.bus.read_inbox("lead")
        self.assertTrue(self.runtime.bus.peek("lead"))
        self.runtime.bus.read_inbox("lead")
        self.assertEqual(len(history), 1)

    def test_checkpoint_failure_stops_before_model(self):
        with patch.object(self.runtime.agent.store, "save", side_effect=SessionError("disk full")):
            with self.assertRaises(SessionError):
                self.runtime.agent.agent_loop([], {}, "request")
        self.client.messages.create.assert_not_called()
        self.assertTrue(self.runtime.agent.paused)

    def test_transient_fallback_bounded_and_fatal_no_retry(self):
        state = RecoveryState("primary")
        fn = Mock(side_effect=TimeoutError("timeout"))
        with patch("harness.recovery.time.sleep"):
            with self.assertRaises(RuntimeError):
                self.runtime.recovery.with_retry(fn, state)
        self.assertEqual(fn.call_count, self.runtime.config.MAX_RETRIES * 2)
        self.assertEqual(state.current_model, "fallback")
        fn = Mock(side_effect=SimpleError("bad", 401))
        with self.assertRaises(SimpleError):
            self.runtime.recovery.with_retry(fn, RecoveryState("primary"))
        self.assertEqual(fn.call_count, 1)

    def test_compaction_service_failure_keeps_complete_pair(self):
        self.client.messages.create.side_effect = TimeoutError()
        messages = [{"role": "user", "content": "old"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "bash", "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "result"}]}]
        compacted = self.runtime.context.compact_history(messages, "current")
        self.assertIn("current", compacted[0]["content"])
        self.assertEqual(compacted[-2:], messages[-2:])

    def test_partial_multi_tool_response_resumes_remaining_tool(self):
        a = self.runtime.agent
        blocks = [SimpleNamespace(type="tool_use", name="write_file", id=f"call{i}", input={"path": f"file{i}", "content": str(i)}) for i in range(2)]
        self.client.messages.create.side_effect = [response(*blocks), response()]
        original = a.checkpoint
        def checkpoint(messages, active_request=None):
            if (a.loop_state or {}).get("results") and len(a.loop_state["results"]) == 1:
                original(messages, active_request)
                raise KeyboardInterrupt()
            return original(messages, active_request)
        with patch.object(a, "checkpoint", side_effect=checkpoint):
            with self.assertRaises(KeyboardInterrupt):
                a.agent_loop([{"role": "user", "content": "write two"}], {}, "write two")
        other = HarnessRuntime(self.runtime.config, self.client)
        other.context.remember_after_turn = Mock()
        history = []
        other.agent.restore_session(a.snapshot.session_id, history)
        with patch.object(other.tools, "call_tool_handler", wraps=other.tools.call_tool_handler) as execute:
            other.agent.agent_loop(history, {}, "write two", resume=True)
        self.assertEqual(execute.call_count, 1)
        self.assertTrue((self.root / "file1").exists())

    def test_paused_async_loop_does_not_consume_or_call_model(self):
        a = self.runtime.agent
        a.paused = True
        self.runtime.bus.send("worker", "lead", "queued")
        with patch("harness.agent.time.sleep", side_effect=[None, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                a.async_event_loop([], {}, {"active_user_request": "request"})
        self.client.messages.create.assert_not_called()
        self.assertTrue(self.runtime.bus.peek("lead"))

    def test_accepted_request_restores_before_first_model_call(self):
        state = {"active_user_request": "request"}
        a = self.runtime.agent
        with patch.object(a, "agent_loop", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.cli.submit_query("request", [], {}, state)
        other = HarnessRuntime(self.runtime.config, self.client)
        other.context.remember_after_turn = Mock()
        history = []
        other.agent.restore_session(a.snapshot.session_id, history)
        self.assertIsNotNone(other.agent.loop_state)
        other.cli.submit_query("/session continue", history, {}, state)
        self.assertEqual(self.client.messages.create.call_count, 1)

    def test_cron_receipt_checkpoint_failure_leaves_queue(self):
        a = self.runtime.agent
        history = []
        a.bind_deliveries(history)
        job = self.runtime.scheduler.schedule_job("* * * * *", "scheduled", False, True)
        self.runtime.scheduler._enqueue_due_job(job)
        with patch.object(a, "checkpoint", side_effect=SessionError("disk full")):
            with self.assertRaises(SessionError):
                self.runtime.scheduler.consume_cron_queue()
        self.assertEqual(len(self.runtime.scheduler.cron_queue), 1)
        self.runtime.scheduler.consume_cron_queue()
        self.assertEqual(len(history), 1)

    def test_both_generations_corrupt_refused(self):
        store = SessionStore(self.root)
        snap = SessionSnapshot()
        store.save(snap)
        store.save(snap)
        with store.connect() as db:
            db.execute("UPDATE snapshots SET digest='bad'")
        with self.assertRaises(SessionError):
            store.load(snap.session_id)

    def test_new_query_returns_to_primary_after_fallback(self):
        a = self.runtime.agent
        self.client.messages.create.side_effect = [TimeoutError()] * self.runtime.config.MAX_RETRIES + [response(), response()]
        with patch("harness.recovery.time.sleep"):
            a.agent_loop([{"role": "user", "content": "first"}], {}, "first")
            a.agent_loop([{"role": "user", "content": "second"}], {}, "second")
        self.assertEqual(self.client.messages.create.call_args_list[-2].kwargs["model"], "fallback")
        self.assertEqual(self.client.messages.create.call_args_list[-1].kwargs["model"], "primary")

    def test_saved_turn_budget_not_reset(self):
        a = self.runtime.agent
        a.config.MAX_TURNS = 1
        a.accept_request("request")
        a.loop_state["turns"] = 1
        a.checkpoint([], "request")
        other = HarnessRuntime(self.runtime.config, self.client)
        other.context.remember_after_turn = Mock()
        history = []
        other.agent.restore_session(a.snapshot.session_id, history)
        decision = other.agent.agent_loop(history, {}, "request", resume=True)
        self.assertEqual(decision.action, "max_turns")
        self.client.messages.create.assert_not_called()


class SimpleError(Exception):
    def __init__(self, text, status):
        super().__init__(text)
        self.status_code = status
