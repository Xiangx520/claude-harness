"""Permission, logging, and lifecycle hooks around tool calls."""

import threading


class HookManager:
    """Permission, logging, and lifecycle hooks around tool calls."""

    def __init__(self, config):
        self.config = config
        self.HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
                 "PostToolUse": [], "Stop": []}
        self.DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
        self.mcp_tool_policies: dict[str, str] = {}
        self.register_hook("UserPromptSubmit", self.user_prompt_hook)
        self.register_hook("PreToolUse", self.permission_hook)
        self.register_hook("PreToolUse", self.log_hook)
        self.register_hook("PostToolUse", self.large_output_hook)
        self.register_hook("Stop", self.stop_hook)

    def register_hook(self, event: str, callback):
        self.HOOKS[event].append(callback)

    def trigger_hooks(self, event: str, *args):
        for callback in self.HOOKS[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None

    def permission_hook(self, block):
        # The permission layer sees the raw tool_use before dispatch. It can deny,
        # ask the user, or allow execution to continue.
        if block.name == "bash":
            command = block.input.get("command", "")
            if not isinstance(command, str):
                return "Permission denied: shell command must be a string"
            for pattern in self.DENY_LIST:
                if pattern in command:
                    return f"Permission denied: '{pattern}' is on the deny list"
            if threading.current_thread() is not threading.main_thread():
                return ("Permission denied: interactive shell approval is unavailable "
                        "during an asynchronous turn")
            self.console.terminal_print("\n\033[33m[permission] shell command\033[0m")
            self.console.terminal_print(f"  {command}")
            choice = self.console.ask("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
        if block.name in ("read_file", "write_file", "edit_file"):
            path = block.input.get("path", "")
            if not isinstance(path, str):
                return "Permission denied: path must be a string"
            if self.config.SANDBOX_MODE != "docker" and not (self.config.WORKDIR / path).resolve().is_relative_to(self.config.WORKDIR):
                return "Permission denied: path is outside the workspace"
        if (block.name.startswith("mcp__")
                and self.mcp_tool_policies.get(block.name, "confirm") != "allow"):
            if threading.current_thread() is not threading.main_thread():
                return ("Permission denied: interactive MCP approval is unavailable "
                        "during an asynchronous turn")
            self.console.terminal_print(f"\n\033[33m[permission] MCP tool: {block.name}\033[0m")
            choice = self.console.ask("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
        return None

    def log_hook(self, block):
        print(f"\033[90m[HOOK] {block.name}\033[0m")
        return None

    def large_output_hook(self, block, output):
        if len(str(output)) > 100000:
            print(f"\033[33m[HOOK] large output from {block.name}: "
                  f"{len(str(output))} chars\033[0m")
        return None

    def user_prompt_hook(self, query: str):
        print(f"\033[90m[HOOK] UserPromptSubmit: {self.config.WORKDIR}\033[0m")
        return None

    def stop_hook(self, messages: list):
        tool_count = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                tool_count += sum(1 for item in content
                                  if isinstance(item, dict)
                                  and item.get("type") == "tool_result")
        print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
        return None
