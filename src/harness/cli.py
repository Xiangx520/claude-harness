"""The original interactive CLI entry point."""

import threading

from .startup import show_startup_animation
from .goal import CLEAR_ALIASES, GoalError
from .sandbox import SandboxError
from .sessions import SessionError


class HarnessCLI:
    def __init__(self, runtime):
        self.runtime = runtime

    def submit_query(self, query: str, history: list, context: dict, session_state: dict):
        """Handle session commands locally, then enter the existing model/tool loop."""
        parts = query.strip().split(maxsplit=1)
        agent = self.runtime.agent
        if parts and parts[0] == "/session":
            args = parts[1].split() if len(parts) > 1 else ["status"]
            try:
                if args == ["list"]:
                    message = "\n".join(f"{sid} {updated:.0f}" for sid, updated in agent.store.list()) or "No saved sessions"
                elif args == ["status"]:
                    message = f"Session {agent.snapshot.session_id}; paused={agent.paused}; phase={(agent.loop_state or {}).get('phase', 'idle')}"
                elif len(args) == 2 and args[0] == "resume":
                    if self.runtime.background.has_unfinished_background() or self.runtime.team.active_teammates:
                        raise SessionError("Wait for background tasks and teammates before switching sessions")
                    message = agent.restore_session(args[1], history)
                    session_state["active_user_request"] = agent.active_request
                    context.update(self.runtime.context.update_context({}, history))
                elif args == ["continue"]:
                    if not agent.paused:
                        raise SessionError("Session is not paused")
                    if agent.loop_state is None:
                        agent.paused = False
                        message = "Session ready for a new request"
                    else:
                        start = len(history)
                        decision = agent.agent_loop(history, context, agent.active_request, resume=True)
                        context.update(self.runtime.context.update_context(context, history))
                        agent.print_turn_assistants(history, start)
                        return decision
                else:
                    message = "Usage: /session [list|status|resume <id>|continue]"
            except SessionError as exc:
                message = str(exc)
            self.runtime.console.terminal_print(message)
            return
        if agent.paused:
            self.runtime.console.terminal_print("Session paused. Use /session continue before submitting work.")
            return
        if parts and parts[0] == "/sandbox":
            sandbox = self.runtime.tools.sandbox
            argument = parts[1].strip() if len(parts) > 1 else "status"
            try:
                if not sandbox:
                    message = "Sandbox disabled: commands run on the host. Set SANDBOX_MODE=docker to enable isolation."
                elif argument == "status":
                    message = sandbox.status()
                elif argument in {"export", "sync", "resolve local", "resolve remote"}:
                    with self.runtime.team.team_lock:
                        teammates_busy = bool(self.runtime.team.active_teammates)
                    if self.runtime.background.has_unfinished_background() or teammates_busy:
                        message = "Wait for background tasks and teammates to finish before managing sandbox files."
                    else:
                        cwd, error = self.runtime.tools._agent_cwd()
                        if error:
                            message = error
                        elif argument == "export":
                            message = f"Optional sandbox snapshot saved: {sandbox.export(cwd)}"
                        else:
                            choice = argument.split()[1] if argument.startswith("resolve ") else None
                            message = sandbox.sync(cwd, choice=choice)
                else:
                    message = "Usage: /sandbox [status|sync|resolve local|resolve remote|export]"
            except (SandboxError, OSError, ValueError) as exc:
                message = f"Sandbox error: {exc}"
            self.runtime.console.terminal_print(message)
            return
        if parts and parts[0] == "/goal":
            if len(parts) == 1:
                self.runtime.console.terminal_print(
                    self.runtime.goal.status(self.runtime.agent.total_tokens))
                return
            argument = parts[1].strip()
            if argument.lower() in CLEAR_ALIASES:
                self.runtime.console.terminal_print(self.runtime.goal.clear())
                agent.checkpoint(history, session_state["active_user_request"])
                return
            try:
                self.runtime.goal.set_goal(argument, self.runtime.agent.total_tokens)
            except GoalError as exc:
                self.runtime.console.terminal_print(f"[goal error] {exc}")
                return
            query = argument
            self.runtime.console.terminal_print(f"Goal set: {argument}")
        self.runtime.hooks.trigger_hooks("UserPromptSubmit", query)
        turn_start = len(history)
        session_state["active_user_request"] = query
        history.append({"role": "user", "content": query})
        agent.accept_request(query)
        agent.checkpoint(history, query)
        decision = self.runtime.agent.agent_loop(history, context, query)
        context.update(self.runtime.context.update_context(context, history))
        self.runtime.agent.print_turn_assistants(history, turn_start)
        return decision

    def run(self):
        show_startup_animation()
        self.runtime.console.CLI_ACTIVE = True
        self.runtime.scheduler.start_runtime_services()
        print("Taffy Harness: an agent for coding")
        print("Enter a question, press Enter to send. Type q to quit.\n")
        history = []
        context = self.runtime.context.update_context({}, [])
        session_state = {"active_user_request": "(no active user request)"}
        threading.Thread(target=self.runtime.agent.async_event_loop,
                         args=(history, context, session_state), daemon=True).start()
        while True:
            try:
                query = self.runtime.console.ask()
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            with self.runtime.agent.agent_lock:
                try:
                    self.submit_query(query, history, context, session_state)
                except SessionError as exc:
                    self.runtime.console.terminal_print(str(exc))
            print()
