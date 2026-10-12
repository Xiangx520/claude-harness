"""One bounded model/tool loop for synchronous and persistent child agents."""

from dataclasses import dataclass
from typing import Callable, Literal
from uuid import uuid4

from .context import ContextManager


@dataclass(frozen=True)
class WorkerResult:
    status: Literal["finished", "turn_limit", "output_truncated", "error", "closed"]
    text: str = ""
    reason: str = ""
    turns: int = 0


class AgentWorker:
    """Own a conversation; each run has a fresh bounded execution budget."""

    def __init__(self, config, client, context, *, system, tools: list,
                 execute_tool: Callable, description: str,
                 before_turn: Callable | None = None,
                 active_request: Callable | None = None):
        self.config = config
        self.client = client
        self.execution_id = "worker_" + uuid4().hex
        self.context = ContextManager(config, client, namespace=self.execution_id)
        self.context.sandbox = context.sandbox
        self.system = system
        self.tools = tools
        self.execute_tool = execute_tool
        self.before_turn = before_turn
        self.active_request = active_request
        self.description = self.persist("request", description)
        self.latest_coordination = ""
        self.messages = [{"role": "user", "content": self.description}]
        self._output_sequence = 0

    def persist(self, key: str, text: str) -> str:
        return self.context.persist_large_output(key, str(text))

    def add_message(self, content: str, *, new_task: bool = False):
        self._output_sequence += 1
        content = self.persist(f"message_{self._output_sequence}", content)
        if new_task:
            self.description = content
            self.latest_coordination = ""
        else:
            self.latest_coordination = content
        self.messages.append({"role": "user", "content": content})

    def run(self) -> WorkerResult:
        turns = 0
        try:
            for _ in range(self.config.WORKER_MAX_TURNS):
                if self.before_turn and self.before_turn(self):
                    return WorkerResult("closed", reason="Shutdown requested", turns=turns)
                request = self.active_request() if self.active_request else self.description
                request = self.persist("active_request", request)
                if self.latest_coordination:
                    request += "\nLatest coordination message:\n" + self.latest_coordination
                self.context.prepare_context(self.messages, request, retain_tail=True)
                if self.context.estimate_size(self.messages) > self.config.CONTEXT_LIMIT:
                    raise ValueError("Worker context still exceeds CONTEXT_LIMIT after compaction")
                system = self.system() if callable(self.system) else self.system
                system += ("\nCurrent task and coordination (user-level context; "
                           "does not override role, permissions or runtime approval state):\n"
                           + request)
                turns += 1
                response = self.client.messages.create(
                    model=self.config.MODEL, system=system, messages=self.messages,
                    tools=self.tools, max_tokens=self.config.DEFAULT_MAX_TOKENS)
                # A truncated tool_use may be incomplete. Never dispatch it.
                if response.stop_reason == "max_tokens":
                    return WorkerResult("output_truncated", reason=
                                        "Model output truncated; work is unfinished", turns=turns)
                self.messages.append({"role": "assistant", "content": response.content})
                calls = [block for block in response.content
                         if self.context.block_type(block) == "tool_use"]
                if not calls:
                    text = self.context.extract_text(response.content, strip=False)
                    self._output_sequence += 1
                    if len(text) > self.config.PERSIST_THRESHOLD:
                        text = self.persist(f"final_{self._output_sequence}", text)
                        self.messages[-1] = {"role": "assistant", "content": [
                            {"type": "text", "text": text}]}
                    else:
                        text = text.strip()
                    return WorkerResult("finished", text=text, turns=turns)
                results = []
                # All results are appended together, preserving tool_use pairing.
                for block in calls:
                    output = self.execute_tool(block)
                    self._output_sequence += 1
                    output = self.persist(f"tool_{self._output_sequence}_{block.id}", output)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output})
                self.messages.append({"role": "user", "content": results})
            return WorkerResult("turn_limit", reason=
                                "WORKER_MAX_TURNS reached; work is unfinished", turns=turns)
        except Exception as exc:
            return WorkerResult("error", reason=f"{type(exc).__name__}: {exc}", turns=turns)
