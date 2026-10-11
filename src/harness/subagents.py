"""Run a bounded subagent with a fresh conversation."""


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
        messages = [{"role": "user", "content": description}]
        for _ in range(30):
            response = self.client.messages.create(
                model=self.config.MODEL, system=self.SUB_SYSTEM, messages=messages,
                tools=self.SUB_TOOLS, max_tokens=8000)
            messages.append({"role": "assistant", "content": response.content})
            if not self.context.has_tool_use(response.content):
                break
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                blocked = self.hooks.trigger_hooks("PreToolUse", block)
                if blocked:
                    output = str(blocked)
                else:
                    handler = self.SUB_HANDLERS.get(block.name)
                    output = self.tools.call_tool_handler(handler, block.input, block.name)
                    self.hooks.trigger_hooks("PostToolUse", block, output)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(output)})
            messages.append({"role": "user", "content": results})
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                text = self.context.extract_text(msg["content"])
                if text:
                    return text
        return "Subagent finished without a text summary."
