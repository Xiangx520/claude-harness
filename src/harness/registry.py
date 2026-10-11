"""Explicit builtin tool schemas and late-bound MCP handlers."""


class ToolRegistry:
    """Explicit builtin tool schemas and late-bound MCP handlers."""

    def __init__(self, config):
        self.config = config
        self.BUILTIN_TOOLS = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"},
                                             "run_in_background": {"type": "boolean"}},
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
            {"name": "todo_write",
             "description": "Create and manage a task list for the current session.",
             "input_schema": {"type": "object",
                              "properties": {"todos": {"type": "array",
                                  "items": {"type": "object",
                                            "properties": {
                                                "content": {"type": "string"},
                                                "status": {"type": "string",
                                                           "enum": ["pending", "in_progress", "completed"]}},
                                            "required": ["content", "status"]}}},
                              "required": ["todos"]}},
            {"name": "task",
             "description": "Launch a focused subagent. Returns only its final summary.",
             "input_schema": {"type": "object",
                              "properties": {"description": {"type": "string"}},
                              "required": ["description"]}},
            {"name": "load_skill",
             "description": "Load the full content of a skill by name.",
             "input_schema": {"type": "object",
                              "properties": {"name": {"type": "string"}},
                              "required": ["name"]}},
            {"name": "compact",
             "description": "Summarize earlier conversation and continue with compacted context.",
             "input_schema": {"type": "object",
                              "properties": {"focus": {"type": "string"}},
                              "required": []}},
            {"name": "create_task",
             "description": "Create a task and return its runtime-generated ID.",
             "input_schema": {"type": "object",
                              "properties": {"subject": {"type": "string"},
                                             "description": {"type": "string"}},
                              "required": ["subject"],
                              "additionalProperties": False}},
            {"name": "update_task",
             "description": "Add dependencies using IDs returned by create_task.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "task_id": {"type": "string",
                                              "pattern": "^task_[0-9a-f]{8}$"},
                                  "addBlockedBy": {
                                      "type": "array",
                                      "items": {"type": "string",
                                                "pattern": "^task_[0-9a-f]{8}$"},
                                      "minItems": 1}},
                              "required": ["task_id", "addBlockedBy"],
                              "additionalProperties": False}},
            {"name": "list_tasks", "description": "List all tasks.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "get_task", "description": "Get full task details.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "claim_task", "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task", "description": "Complete an in-progress task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "schedule_cron",
             "description": ("Schedule a cron job. cron is 5-field: min hour dom "
                             "month dow. For one-shot reminders, compute the target "
                             "minute and set recurring=false."),
             "input_schema": {"type": "object",
                              "properties": {"cron": {"type": "string"},
                                             "prompt": {"type": "string"},
                                             "recurring": {"type": "boolean"},
                                             "durable": {"type": "boolean"}},
                              "required": ["cron", "prompt"]}},
            {"name": "list_crons", "description": "List registered cron jobs.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
             "input_schema": {"type": "object",
                              "properties": {"job_id": {"type": "string"}},
                              "required": ["job_id"]}},
            {"name": "spawn_teammate", "description": "Spawn a persistent teammate.",
             "input_schema": {"type": "object",
                              "properties": {"name": {
                                                 "type": "string",
                                                 "pattern": "^[A-Za-z0-9_-]{1,64}$",
                                             },
                                             "role": {"type": "string"},
                                             "prompt": {"type": "string"},
                                             "task_id": {
                                                 "type": "string",
                                                 "pattern": "^task_[0-9a-f]{8}$",
                                             },
                                             "require_plan": {"type": "boolean"}},
                              "required": ["name", "role", "prompt"]}},
            {"name": "list_teammates", "description": "List active teammates.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "request_shutdown",
             "description": "Request a teammate to shut down.",
             "input_schema": {"type": "object",
                              "properties": {"teammate": {"type": "string"}},
                              "required": ["teammate"]}},
            {"name": "request_plan",
             "description": "Ask a teammate to submit a plan.",
             "input_schema": {"type": "object",
                              "properties": {"teammate": {"type": "string"},
                                             "task": {"type": "string"}},
                              "required": ["teammate", "task"]}},
            {"name": "review_plan",
             "description": "Approve or reject a submitted plan.",
             "input_schema": {"type": "object",
                              "properties": {"request_id": {"type": "string"},
                                             "approve": {"type": "boolean"},
                                             "feedback": {"type": "string"}},
                              "required": ["request_id", "approve"]}},
            {"name": "create_worktree",
             "description": "Create a task-bound git worktree for a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"name": {
                                                 "type": "string",
                                                 "pattern": ("^(?!.*\\.\\.)[A-Za-z0-9]"
                                                             "[A-Za-z0-9._-]{0,63}$"),
                                                 "maxLength": 64,
                                             },
                                             "task_id": {"type": "string"}},
                              "required": ["name", "task_id"],
                              "additionalProperties": False}},
            {"name": "connect_mcp",
             "description": "Connect to an MCP server (docs, deploy) and discover tools.",
             "input_schema": {"type": "object",
                              "properties": {"name": {"type": "string"}},
                              "required": ["name"]}},
        ]

    def bind_handlers(self):
        self.BUILTIN_HANDLERS = {
            "bash": self.tools.run_agent_bash,
            "read_file": self.tools.run_agent_read,
            "write_file": self.tools.run_agent_write,
            "edit_file": self.tools.run_agent_edit,
            "glob": self.tools.run_agent_glob,
            "todo_write": self.tools.run_todo_write, "task": self.subagents.spawn_subagent,
            "load_skill": self.skills.load_skill,
            "create_task": self.tasks.run_create_task, "update_task": self.tasks.run_update_task,
            "list_tasks": self.tasks.run_list_tasks,
            "get_task": self.tasks.run_get_task,
            "claim_task": self.tasks.run_claim_task, "complete_task": self.tasks.run_complete_task,
            "schedule_cron": self.scheduler.run_schedule_cron,
            "list_crons": self.scheduler.run_list_crons,
            "cancel_cron": self.scheduler.run_cancel_cron,
            "spawn_teammate": self.team.run_spawn_teammate,
            "list_teammates": self.team.run_list_teammates,
            "send_message": self.team.run_send_message,
            "request_shutdown": self.team.run_request_shutdown,
            "request_plan": self.team.run_request_plan, "review_plan": self.team.run_review_plan,
            "create_worktree": self.worktrees.run_create_worktree,
            "connect_mcp": self.mcp.run_connect_mcp,
        }

    def assemble_tool_pool(self) -> tuple[list[dict], dict]:
        """Merge builtin tools + all MCP tools into one pool."""
        tools = list(self.BUILTIN_TOOLS)
        handlers = dict(self.BUILTIN_HANDLERS)
        policies: dict[str, str] = {}
        origins = {tool["name"]: f"built-in tool {tool['name']!r}"
                   for tool in tools}
        for server_name, mcp_client in self.mcp.mcp_clients.items():
            safe_server = self.mcp.normalize_mcp_name(server_name)
            for tool_def in mcp_client.tools:
                raw_name = tool_def["name"]
                safe_tool = self.mcp.normalize_mcp_name(raw_name)
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                if len(prefixed) > 64:
                    raise ValueError(
                        f"MCP tool name is longer than 64 characters: {prefixed}"
                    )
                origin = f"MCP tool {server_name!r}/{raw_name!r}"
                if prefixed in origins:
                    raise ValueError(
                        "MCP tool name collision after normalization: "
                        f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                    )
                schema = tool_def.get("inputSchema", {})
                if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                    raise ValueError(f"Invalid input schema for {origin}")
                origins[prefixed] = origin
                tools.append({
                    "name": prefixed,
                    "description": tool_def.get("description", ""),
                    "input_schema": schema,
                })
                handlers[prefixed] = (
                    lambda *, client=mcp_client, tool=raw_name, **kwargs:
                    client.call_tool(tool, kwargs)
                )
                policies[prefixed] = self.mcp.MCP_HOST_POLICY.get(
                    (server_name, raw_name), "confirm"
                )
        self.hooks.mcp_tool_policies = policies
        return tools, handlers
