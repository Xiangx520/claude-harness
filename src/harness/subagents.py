"""Run a bounded subagent with a fresh conversation."""

from .worker import AgentWorker


class SubagentRunner:
    """Run a bounded subagent with a fresh conversation."""

    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.SUB_SYSTEM = (
            f"You are a coding subagent at {self.config.AGENT_WORKDIR}. Environment: {self.config.ENVIRONMENT_PROMPT}. "
            "Complete the task, then return a concise final summary. "
            "Do not spawn more agents."
        )
        self.SUB_TOOLS = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "limit": {"type": "integer"},
                                             "offset": {"type": "integer"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write content to a file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in a file once.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "old_text": {"type": "string"},
                                             "new_text": {"type": "string"}},
                              "required": ["path", "old_text", "new_text"]}},
            {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
             "input_schema": {"type": "object",
                              "properties": {"pattern": {"type": "string"}},
                              "required": ["pattern"]}},
        ]

    def bind_handlers(self):
        self.SUB_HANDLERS = {
            "bash": self.tools.run_bash, "read_file": self.tools.run_read,
            "write_file": self.tools.run_write, "edit_file": self.tools.run_edit,
            "glob": self.tools.run_glob,
        }

    def spawn_subagent(self, description: str) -> str:
        def execute(block):
            blocked = self.hooks.trigger_hooks("PreToolUse", block)
            if blocked is not None:
                return str(blocked)
            handler = self.SUB_HANDLERS.get(block.name)
            output = self.tools.call_tool_handler(handler, block.input, block.name)
            self.hooks.trigger_hooks("PostToolUse", block, output)
            return output

        try:
            worker = AgentWorker(
                self.config, self.client, self.context, system=self.SUB_SYSTEM,
                tools=self.SUB_TOOLS, execute_tool=execute, description=description)
            result = worker.run()
        except Exception as exc:
            return f"[Subagent error] {type(exc).__name__}: {exc}"
        if result.status == "finished":
            return result.text or "Subagent finished without a text summary."
        return f"[Subagent {result.status}] {result.reason}"
