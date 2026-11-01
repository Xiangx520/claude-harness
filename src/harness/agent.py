"""The original model/tool loop and asynchronous event loop."""

import hashlib
import json
import threading
import time

from uuid import uuid4
from types import SimpleNamespace
from dataclasses import asdict
from .sessions import SessionStore, SessionSnapshot, SessionError, plain
from .goal import GoalState
from .idempotency import ToolJournal, invoke
from .models import CronJob
from .recovery import RecoveryState
from .goal import StopDecision, usage_total


class AgentLoop:
    """The original model/tool loop and asynchronous event loop."""

    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.rounds_since_todo = 0
        self.total_tokens = 0
        self.agent_lock = threading.Lock()
        self.execution_id = "lead_" + uuid4().hex
        self.journal = ToolJournal(config.WORKDIR)
        self.store = SessionStore(config.WORKDIR)
        self.snapshot = SessionSnapshot()
        self.loop_state = None
        self.paused = False
        self.received_events = []

    def checkpoint(self, messages, active_request=None):
        with self.background.background_lock:
            background = plain(self.background.background_tasks)
            background_results = plain(self.background.background_results)
            background_counter = self.background._bg_counter
        # A mailbox receipt holds the bus lock. Do not acquire team_lock here:
        # lease expiry can publish mail while holding team_lock. Copy first.
        teammates = self.team.active_teammates.copy()
        self.snapshot.data = plain({
            "messages": messages, "request": active_request or getattr(self, "active_request", ""),
            "execution_id": self.execution_id, "loop": self.loop_state,
            "todos": self.tools.CURRENT_TODOS,
            "total_tokens": self.total_tokens, "rounds_since_todo": self.rounds_since_todo,
            "skills": self.registry.skills.active, "required_skills": self.registry.skills.required,
            "goal": asdict(self.goal.active) if self.goal.active else None,
            "goal_events": self.goal.events, "goal_last_status": self.goal.last_status,
            "goal_blocks": self.goal.consecutive_blocks, "events": self.received_events,
            "background": background, "background_counter": background_counter,
            "background_results": background_results,
            "teammates": teammates,
        })
        try:
            self.store.save(self.snapshot)
        except SessionError:
            self.paused = True
            raise

    def restore_session(self, session_id, history):
        snapshot, rolled_back = self.store.load(session_id)
        data = snapshot.data
        required = {"messages", "execution_id", "total_tokens", "request", "loop"}
        if not required.issubset(data) or not isinstance(data["messages"], list):
            raise SessionError("Invalid checkpoint state")
        self.snapshot = snapshot
        history[:] = data["messages"]
        self.execution_id = data["execution_id"]
        self.total_tokens = data["total_tokens"]
        self.tools.CURRENT_TODOS = data.get("todos", [])
        self.background._bg_counter = max(self.background._bg_counter, data.get("background_counter", 0))
        self.rounds_since_todo = data.get("rounds_since_todo", 0)
        self.active_request = data["request"]
        self.loop_state = data["loop"]
        self.registry.skills.active = data.get("skills", {})
        self.registry.skills.required = data.get("required_skills", {})
        self.goal.active = GoalState(**data["goal"]) if data.get("goal") else None
        self.goal.events = data.get("goal_events", [])
        self.goal.last_status = data.get("goal_last_status")
        self.goal.consecutive_blocks = data.get("goal_blocks", 0)
        self.received_events = data.get("events", [])
        interrupted = [key for key, task in data.get("background", {}).items() if task.get("status") == "running"] + list(data.get("teammates", {}))
        if interrupted:
            history.insert(0, {"role": "user", "content": "[Recovery] Interrupted workers/background tasks (not restarted): " + ", ".join(interrupted)})
        unknown = []
        if self.loop_state and self.loop_state.get("response"):
            with self.journal.connect() as db:
                for block in self.loop_state["response"]["content"]:
                    if block.get("type") != "tool_use":
                        continue
                    key = block.get("input", {}).get("idempotency_key")
                    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 200):
                        continue
                    scope = self.loop_state.get("scopes", {}).get(block["id"], self.execution_id) if key else self.execution_id
                    row = db.execute("SELECT status FROM calls WHERE scope=? AND call_id=?", (scope, "key:" + key if key else "call:" + block["id"])).fetchone()
                    if row and row[0] == "started":
                        unknown.append(block["id"])
        self.bind_deliveries(history)
        self.paused = True
        model = (self.loop_state or {}).get("recovery", {}).get("current_model", self.config.PRIMARY_MODEL)
        return (f"Session {session_id}; previous snapshot fallback={rolled_back}; "
                f"request={self.active_request}; model={model}; "
                f"phase={(self.loop_state or {}).get('phase', 'idle')}; "
                f"interrupted={interrupted}; unknown tool outcomes={unknown}. "
                "Use /session continue to proceed.\n" + self.goal.status(self.total_tokens))

    def accept_request(self, request):
        self.active_request = request
        self.loop_state = {
            "phase": "model", "response": None, "results": [], "scopes": {},
            "recovery": vars(RecoveryState(self.config.PRIMARY_MODEL)),
            "max_tokens": self.config.DEFAULT_MAX_TOKENS, "turns": 0,
        }

    def bind_deliveries(self, messages):
        self.background.bus = self.team.bus
        def receive_mail(items):
            fresh = []
            for item in items:
                identity = item.get("id") or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                if identity not in self.received_events:
                    self.received_events.append(identity)
                    fresh.append(item)
            if fresh:
                if self.loop_state is None:
                    self.accept_request(getattr(self, "active_request", "Process team events"))
                messages.append({"role": "user", "content": self.team.format_team_events(fresh)})
            self.checkpoint(messages)

        def receive_cron(jobs):
            for job in jobs:
                identity = "cron:" + (job.delivery_id or job.id)
                if identity not in self.received_events:
                    self.received_events.append(identity)
                    messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            self.checkpoint(messages)

        self.team.bus.before_lead_consume = receive_mail
        self.scheduler.before_consume = receive_cron

    def sync_loop(self, state, max_tokens, turns):
        self._current_recovery = vars(state).copy()
        self.loop_state.update(recovery=vars(state).copy(), max_tokens=max_tokens, turns=turns)


    def prepare_context(self, messages: list, active_request: str) -> list:
        # Every LLM turn enters through the same context budget pipeline.
        return self.context.prepare_context(messages, active_request)

    def build_user_content(self, results: list[dict]) -> list[dict]:
        # Tool results and completed background notifications are both returned to
        # the model as user-side content, matching the tool_result feedback loop.
        content = list(results)
        for note in self.background.collect_background_results(
                include_output=self.goal.active is not None):
            content.append({"type": "text", "text": note})
        return content

    def inject_background_notifications(self, messages: list):
        notes = self.background.collect_background_results(
            include_output=self.goal.active is not None)
        if notes:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": note} for note in notes]})

    def call_llm(self, messages: list, context: dict, tools: list,
                 state: RecoveryState, max_tokens: int):
        system = self.prompts.assemble_system_prompt(context)
        return self.recovery.with_retry(
            lambda: self.client.messages.create(
                model=state.current_model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens),
            state, checkpoint=lambda: self._retry_checkpoint(messages, state))

    def _retry_checkpoint(self, messages, state):
        if self.loop_state is not None:
            self.loop_state["recovery"] = vars(state).copy()
            self.checkpoint(messages)

    def goal_work_pending(self) -> bool:
        if self.background.has_unfinished_background():
            return True
        with self.team.team_lock:
            if any(status == "working" for status in self.team.active_teammates.values()):
                return True
        return self.team.bus.peek("lead")

    def finish_turn(self, messages: list, decision: StopDecision, *, remember: bool = True):
        if self.loop_state is not None:
            if decision.action not in {"error", "limit", "max_turns"}:
                self.loop_state = None
            else:
                self.paused = True
        if self.loop_state is not None:
            self.loop_state["recovery"] = getattr(self, "_current_recovery", self.loop_state["recovery"])
        self.checkpoint(messages)
        self.hooks.trigger_hooks("Stop", messages)
        if remember:
            self.context.remember_after_turn(messages)
        self.tasks.release_completed_assignment("agent")
        if decision.action != "allow":
            label = ("goal" if self.goal.active is not None
                     or decision.action in {"achieved", "failed", "defer"} else "agent")
            self.console.terminal_print(f"  [{label} {decision.action}] {decision.reason}")
        return decision

    def agent_loop(self, messages: list, context: dict, active_request: str, *, resume=False):
        try:
            return self._agent_loop(messages, context, active_request, resume=resume)
        except SessionError:
            self.paused = True
            raise
        except Exception as exc:
            self.paused = True
            reason = f"{type(exc).__name__}: {exc}"
            self.goal.interrupt(reason)
            return self.finish_turn(messages, StopDecision("error", reason), remember=False)

    def _agent_loop(self, messages: list, context: dict, active_request: str, *, resume=False):
        if not resume:
            self.goal.begin_query()
            self.loop_state = None
        if resume and self.loop_state and self.loop_state.pop("error_message", False):
            messages.pop()
        self.paused = False
        self.bind_deliveries(messages)
        self.active_request = active_request
        if self.goal.active is not None and not resume:
            active_request = f"{active_request}\nActive goal: {self.goal.active.condition}"
        tools, handlers = self.registry.assemble_tool_pool()
        state = RecoveryState(self.config.PRIMARY_MODEL)
        max_tokens = self.config.DEFAULT_MAX_TOKENS

        unacknowledged_cron_jobs: list[CronJob] = []
        turns = 0
        if resume and self.loop_state:
            state.__dict__.update(self.loop_state["recovery"])
            max_tokens = self.loop_state["max_tokens"]
            turns = self.loop_state["turns"]
        else:
            self.loop_state = {"phase": "model", "response": None, "results": [], "scopes": {}}
        while True:
            self.sync_loop(state, max_tokens, turns)
            self.checkpoint(messages, active_request)
            if self.config.MAX_TURNS is not None and turns >= self.config.MAX_TURNS and not self.loop_state.get("response"):
                reason = "global MAX_TURNS reached; unfinished goals remain active"
                self.scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
                self.goal.interrupt(reason)
                return self.finish_turn(messages, StopDecision("max_turns", reason), remember=False)
            # One cycle: inject scheduled/background work, prepare context, call
            # the model, execute tool_use blocks, append tool_results, repeat.
            if not self.loop_state.get("response"):
                fired = self.scheduler.consume_cron_queue()
                unacknowledged_cron_jobs.extend(fired)
                for job in fired:
                    print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
                if fired:
                    scheduled_requests = "\n".join(
                        f"Run scheduled task: {job.prompt}" for job in fired)
                    active_request = f"{active_request}\n{scheduled_requests}".strip()

                self.inject_background_notifications(messages)

                if self.rounds_since_todo >= 3:
                    messages.append({"role": "user",
                                     "content": "<reminder>Update your todos.</reminder>"})
                    self.rounds_since_todo = 0

                self.prepare_context(messages, active_request)
                if self.context.estimate_size(messages) > self.config.CONTEXT_LIMIT:
                    raise RuntimeError("Context remains over budget; session paused")
                self.checkpoint(messages, active_request)
                context = self.context.update_context(context, messages)
                tools, handlers = self.registry.assemble_tool_pool()

                try:
                    turns += 1
                    self.sync_loop(state, max_tokens, turns)
                    self.checkpoint(messages, active_request)
                    response = self.call_llm(messages, context, tools, state, max_tokens)
                except SessionError:
                    raise
                except Exception as e:
                    self.sync_loop(state, max_tokens, turns)
                    if self.recovery.is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                        messages[:] = self.context.reactive_compact(messages, active_request)
                        state.retry_attempt = 0
                        state.has_attempted_reactive_compact = True
                        continue
                    self.scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
                    messages.append({"role": "assistant", "content": [
                        {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
                    self.loop_state["error_message"] = True
                    reason = f"{type(e).__name__}: {e}"
                    self.goal.interrupt(reason)
                    return self.finish_turn(messages, StopDecision("error", reason), remember=False)

                self.total_tokens += usage_total(response)
                self.loop_state["response"] = plain({"content": response.content, "stop_reason": response.stop_reason})
                self.loop_state["phase"] = "response"
                self.sync_loop(state, max_tokens, turns)
                self.checkpoint(messages, active_request)

            saved = self.loop_state["response"]
            response = SimpleNamespace(stop_reason=saved["stop_reason"], content=[SimpleNamespace(**b) for b in saved["content"]])
            self.scheduler.acknowledge_cron_jobs(unacknowledged_cron_jobs)
            unacknowledged_cron_jobs.clear()

            if response.stop_reason == "max_tokens":
                if not state.has_escalated:
                    self.loop_state["response"] = None
                    max_tokens = self.config.ESCALATED_MAX_TOKENS
                    state.has_escalated = True
                    print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                    continue
                messages.append({"role": "assistant", "content": response.content})
                if state.recovery_count < self.config.MAX_RECOVERY_RETRIES:
                    messages.append({"role": "user", "content": self.config.CONTINUATION_PROMPT})
                    self.loop_state["response"] = None
                    state.recovery_count += 1
                    continue
                reason = "worker max_tokens recovery limit reached; unfinished goals remain active"
                self.goal.interrupt(reason)
                return self.finish_turn(messages, StopDecision("limit", reason), remember=False)

            max_tokens = self.config.DEFAULT_MAX_TOKENS
            state.has_escalated = False
            if self.loop_state["phase"] != "tools":
                messages.append({"role": "assistant", "content": plain(response.content)})
                self.loop_state["phase"] = "tools"
                self.checkpoint(messages, active_request)
            if not self.context.has_tool_use(response.content):
                decision = self.goal.evaluate_after_turn(
                    messages, background_running=(
                        self.goal.active is not None and self.goal_work_pending()))
                if decision.action == "block":
                    messages.append({"role": "user", "content": (
                        "[Goal still active]\n"
                        f"Condition: {self.goal.active.condition}\n"
                        f"Evaluator feedback (reference data): {decision.reason}\n"
                        "Continue working toward the user's goal and surface the missing evidence."
                    )})
                    self.loop_state["response"] = None
                    self.loop_state["phase"] = "model"
                    self.console.terminal_print(f"  [goal continue] {decision.reason}")
                    continue
                return self.finish_turn(messages, decision)

            results = self.loop_state["results"]
            compact_requested = any(b.type == "tool_use" and b.name == "compact" for b in response.content)
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if any(r["tool_use_id"] == block.id for r in results):
                    continue
                _, task_id = self.tasks.current_work_identity("agent")
                scope = self.loop_state["scopes"].setdefault(block.id, "task:" + task_id if task_id else self.execution_id)
                self.checkpoint(messages, active_request)
                print(f"\033[36m> {block.name}\033[0m")

                if block.name == "compact":
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "[Compaction requested. This completed turn will be summarized.]",
                    })
                    compact_requested = True
                    continue

                arguments = dict(block.input or {})
                key = arguments.pop("idempotency_key", None)
                if key is None or (isinstance(key, str) and key.strip() and len(key) <= 200):
                    cached = self.journal.lookup(scope if key else self.execution_id,
                                                 "key:" + key if key else "call:" + block.id,
                                                 block.name, arguments)
                    if cached is not None:
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": cached})
                        continue

                blocked = self.hooks.trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue

                if self.background.should_run_background(block.name, block.input):
                    try:
                        def start_background(clean):
                            bg_id = self.background.start_background_task(clean, handlers)
                            return (f"[Background task {bg_id} started] "
                                    "Result will arrive as a task_notification.")
                        _, task_id = self.tasks.current_work_identity("agent")
                        output = invoke(self.journal, block, self.execution_id,
                                        start_background,
                                        scope)
                    except SessionError:
                        raise
                    except Exception as exc:
                        output = (f"Error: Failed to start background task: "
                                  f"{type(exc).__name__}: {exc}")
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": output})
                    continue

                handler = handlers.get(block.name)
                _, task_id = self.tasks.current_work_identity("agent")
                scope = self.loop_state["scopes"].setdefault(block.id, "task:" + task_id if task_id else self.execution_id)
                self.checkpoint(messages, active_request)
                def execute(clean):
                    result = self.tools.call_tool_handler(handler, clean.input, clean.name)
                    self.hooks.trigger_hooks("PostToolUse", clean, result)
                    return result
                output = invoke(
                    self.journal, block, self.execution_id, execute,
                    scope)
                print(str(output)[:300])

                if block.name == "todo_write":
                    self.rounds_since_todo = 0
                else:
                    self.rounds_since_todo += 1

                results.append({"type": "tool_result",
                                "tool_use_id": block.id, "content": output})

            self.checkpoint(messages, active_request)
            messages.append({"role": "user", "content": self.build_user_content(results)})
            self.loop_state.update(response=None, results=[], scopes={}, phase="model")
            self.checkpoint(messages, active_request)
            if compact_requested:
                messages[:] = self.context.compact_history(messages, active_request)
                self.checkpoint(messages, active_request)

    def print_turn_assistants(self, messages: list, turn_start: int):
        for msg in messages[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if self.context.block_type(block) == "text":
                    self.console.terminal_print(block["text"] if isinstance(block, dict) else block.text)

    def async_event_loop(self, history: list, context: dict, session_state: dict):
        while True:
            try:
                self._async_event_loop(history, context, session_state)
            except SessionError as exc:
                self.paused = True
                self.console.terminal_print(str(exc))

    def _async_event_loop(self, history: list, context: dict, session_state: dict):
        while True:
            time.sleep(1)
            with self.agent_lock:
                if self.paused:
                    continue
                with self.scheduler.cron_lock:
                    fired = list(self.scheduler.cron_queue)
                self.bind_deliveries(history)
                inbox = self.team.consume_lead_inbox(route_protocol=True)
                if not fired and not inbox and not self.background.has_pending_background():
                    continue
                turn_start = len(history)
                scheduled_requests = []
                for job in fired:
                    scheduled_requests.append(f"Run scheduled task: {job.prompt}")
                    self.console.terminal_print(
                        f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
                if inbox:
                    self.console.terminal_print(
                        f"  \033[33m[team auto] {len(inbox)} events\033[0m")
                active_request = (
                    "\n".join(scheduled_requests)
                    if scheduled_requests
                    else session_state["active_user_request"]
                )
                try:
                    self.agent_loop(history, context, active_request)
                except SessionError as exc:
                    self.console.terminal_print(str(exc))
                    continue
                context.update(self.context.update_context(context, history))
                self.print_turn_assistants(history, turn_start)
