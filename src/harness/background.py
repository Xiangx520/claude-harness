"""Run slow shell tools and deliver task notifications."""

import threading


class BackgroundTaskManager:
    """Run slow shell tools and deliver task notifications."""

    def __init__(self, config):
        self.config = config
        self._bg_counter = 0
        self.bus = None
        self.background_tasks: dict[str, dict] = {}
        self.background_results: dict[str, str] = {}
        self.background_lock = threading.Lock()

    def should_run_background(self, tool_name: str, tool_input: dict) -> bool:
        return (
            tool_name == "bash"
            and tool_input.get("run_in_background") is True
        )

    def start_background_task(self, block, handlers: dict) -> str:
        command = block.input.get("command", block.name)
        cwd, cwd_error = self.tools._agent_cwd()

        def worker():
            try:
                if block.name != "bash":
                    raise ValueError("only bash can run in the background")
                if cwd_error:
                    raise ValueError(cwd_error.removeprefix("Error: "))
                output, exit_code = self.tools._run_bash_process(
                    str(block.input["command"]), cwd)
                result = self.tools._format_bash_result(output, exit_code)
                status = "completed" if exit_code == 0 else "failed"
            except Exception as exc:
                result = f"Error: {type(exc).__name__}: {exc}"
                status = "failed"
            try:
                self.hooks.trigger_hooks("PostToolUse", block, result)
            except Exception as exc:
                result = (f"Error: PostToolUse hook failed: "
                          f"{type(exc).__name__}: {exc}\n{result}")
                status = "failed"
            if self.bus:
                # Durable mailbox receipt precedes in-memory completion/removal.
                self.bus.send("background", "lead", f"[Background {bg_id} {status}] {command}\n{result}", metadata={"background_id": bg_id})
            with self.background_lock:
                task = self.background_tasks.get(bg_id)
                if task is None:
                    return
                task["status"] = status
                self.background_results[bg_id] = str(result)

        with self.background_lock:
            self._bg_counter += 1
            bg_id = f"bg_{self._bg_counter:04d}"
            self.background_tasks[bg_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
                "cwd": str(cwd) if cwd else None,
            }
        thread = threading.Thread(target=worker, daemon=True)
        try:
            thread.start()
        except Exception:
            with self.background_lock:
                self.background_tasks.pop(bg_id, None)
                self.background_results.pop(bg_id, None)
            raise
        print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
        return bg_id

    def collect_background_results(self, include_output: bool = False) -> list[str]:
        with self.background_lock:
            ready = [bg_id for bg_id, task in self.background_tasks.items()
                     if task["status"] in {"completed", "failed"}]
            completed = [
                (bg_id, self.background_tasks.pop(bg_id),
                 self.background_results.pop(bg_id, ""))
                for bg_id in ready
            ]
        notifications = []
        if self.bus:
            return notifications
        for bg_id, task, output in completed:
            summary = output[:200] if len(output) > 200 else output
            evidence = f"  <output>{output}</output>\n" if include_output else ""
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{bg_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"{evidence}"
                f"</task_notification>")
        return notifications

    def has_unfinished_background(self) -> bool:
        """Running tasks and undelivered results both prevent goal evaluation."""
        with self.background_lock:
            return bool(self.background_tasks)

    def has_pending_background(self) -> bool:
        """Return whether terminal background work is waiting for delivery."""
        with self.background_lock:
            return any(task["status"] in {"completed", "failed"}
                       for task in self.background_tasks.values())
