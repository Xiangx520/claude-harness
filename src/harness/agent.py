"""The original model/tool loop and asynchronous event loop."""

import threading
import time

from uuid import uuid4
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
            state)

    def goal_work_pending(self) -> bool:
        if self.background.has_unfinished_background():
            return True
        with self.team.team_lock:
            if any(status == "working" for status in self.team.active_teammates.values()):
                return True
        return self.team.bus.peek("lead")

    def finish_turn(self, messages: list, decision: StopDecision, *, remember: bool = True):
        self.hooks.trigger_hooks("Stop", messages)
        if remember:
            self.context.remember_after_turn(messages)
        self.tasks.release_completed_assignment("agent")
        if decision.action != "allow":
            label = ("goal" if self.goal.active is not None
                     or decision.action in {"achieved", "failed", "defer"} else "agent")
            self.console.terminal_print(f"  [{label} {decision.action}] {decision.reason}")
        return decision

    def agent_loop(self, messages: list, context: dict, active_request: str):
        self.goal.begin_query()
        if self.goal.active is not None:
            active_request = f"{active_request}\nActive goal: {self.goal.active.condition}"
        tools, handlers = self.registry.assemble_tool_pool()
        state = RecoveryState(self.config.PRIMARY_MODEL)
        max_tokens = self.config.DEFAULT_MAX_TOKENS

        unacknowledged_cron_jobs: list[CronJob] = []
        turns = 0
        while True:
            if self.config.MAX_TURNS is not None and turns >= self.config.MAX_TURNS:
                reason = "global MAX_TURNS reached; unfinished goals remain active"
                self.scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
                self.goal.interrupt(reason)
                return self.finish_turn(messages, StopDecision("max_turns", reason), remember=False)
            # One cycle: inject scheduled/background work, prepare context, call
            # the model, execute tool_use blocks, append tool_results, repeat.
            fired = self.scheduler.consume_cron_queue()
            unacknowledged_cron_jobs.extend(fired)
            for job in fired:
                messages.append({"role": "user",
                                 "content": f"[Scheduled] {job.prompt}"})
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
            context = self.context.update_context(context, messages)
            tools, handlers = self.registry.assemble_tool_pool()

            try:
                turns += 1
                response = self.call_llm(messages, context, tools, state, max_tokens)
            except Exception as e:
                if self.recovery.is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                    messages[:] = self.context.reactive_compact(messages, active_request)
                    state.has_attempted_reactive_compact = True
                    continue
                self.scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
                reason = f"{type(e).__name__}: {e}"
                self.goal.interrupt(reason)
                return self.finish_turn(messages, StopDecision("error", reason), remember=False)

            self.total_tokens += usage_total(response)

            self.scheduler.acknowledge_cron_jobs(unacknowledged_cron_jobs)
            unacknowledged_cron_jobs.clear()

            if response.stop_reason == "max_tokens":
                if not state.has_escalated:
                    max_tokens = self.config.ESCALATED_MAX_TOKENS
                    state.has_escalated = True
                    print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                    continue
                messages.append({"role": "assistant", "content": response.content})
                if state.recovery_count < self.config.MAX_RECOVERY_RETRIES:
                    messages.append({"role": "user", "content": self.config.CONTINUATION_PROMPT})
                    state.recovery_count += 1
                    continue
                reason = "worker max_tokens recovery limit reached; unfinished goals remain active"
                self.goal.interrupt(reason)
                return self.finish_turn(messages, StopDecision("limit", reason), remember=False)

            max_tokens = self.config.DEFAULT_MAX_TOKENS
            state.has_escalated = False
            messages.append({"role": "assistant", "content": response.content})
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
                    self.console.terminal_print(f"  [goal continue] {decision.reason}")
                    continue
                return self.finish_turn(messages, decision)

            results = []
            compact_requested = False
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"\033[36m> {block.name}\033[0m")

                if block.name == "compact":
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "[Compaction requested. This completed turn will be summarized.]",
                    })
                    compact_requested = True
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
                                        "task:" + task_id if task_id else self.execution_id)
                    except Exception as exc:
                        output = (f"Error: Failed to start background task: "
                                  f"{type(exc).__name__}: {exc}")
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": output})
                    continue

                handler = handlers.get(block.name)
                _, task_id = self.tasks.current_work_identity("agent")
                def execute(clean):
                    result = self.tools.call_tool_handler(handler, clean.input, clean.name)
                    self.hooks.trigger_hooks("PostToolUse", clean, result)
                    return result
                output = invoke(
                    self.journal, block, self.execution_id, execute,
                    "task:" + task_id if task_id else self.execution_id)
                print(str(output)[:300])

                if block.name == "todo_write":
                    self.rounds_since_todo = 0
                else:
                    self.rounds_since_todo += 1

                results.append({"type": "tool_result",
                                "tool_use_id": block.id, "content": output})

            messages.append({"role": "user", "content": self.build_user_content(results)})
            if compact_requested:
                messages[:] = self.context.compact_history(messages, active_request)

    def print_turn_assistants(self, messages: list, turn_start: int):
        for msg in messages[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if self.context.block_type(block) == "text":
                    self.console.terminal_print(block["text"] if isinstance(block, dict) else block.text)

    def async_event_loop(self, history: list, context: dict, session_state: dict):
        while True:
            time.sleep(1)
            with self.agent_lock:
                with self.scheduler.cron_lock:
                    fired = list(self.scheduler.cron_queue)
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
                    history.append({"role": "user",
                                    "content": self.team.format_team_events(inbox)})
                    self.console.terminal_print(
                        f"  \033[33m[team auto] {len(inbox)} events\033[0m")
                active_request = (
                    "\n".join(scheduled_requests)
                    if scheduled_requests
                    else session_state["active_user_request"]
                )
                self.agent_loop(history, context, active_request)
                context.update(self.context.update_context(context, history))
                self.print_turn_assistants(history, turn_start)
