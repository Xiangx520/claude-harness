"""Compose the functional classes into one integrated harness."""

import os
import sys

from anthropic import Anthropic

from .config import HarnessConfig
from .console import ConsoleBroker
from .tasks import TaskSystem
from .worktrees import WorktreeManager
from .skills import SkillLoader
from .prompts import PromptBuilder
from .tools import BasicTools
from .bus import MessageBus
from .team import TeamManager
from .hooks import HookManager
from .subagents import SubagentRunner
from .context import ContextManager
from .recovery import RecoveryManager
from .background import BackgroundTaskManager
from .scheduler import CronScheduler
from .mcp import MCPManager
from .registry import ToolRegistry
from .agent import AgentLoop
from .memory import MemorySystem
from .goal import GoalController, PromptGoalEvaluator
from .cli import HarnessCLI


class HarnessRuntime:
    def __init__(self, config: HarnessConfig | None = None, client=None, *, goal_evaluator=None):
        self.config = config if config is not None else HarnessConfig.from_env()
        self.client = client if client is not None else Anthropic(
            base_url=os.getenv("ANTHROPIC_BASE_URL"))
        self.console = ConsoleBroker(self.config)
        self.tasks = TaskSystem(self.config)
        self.worktrees = WorktreeManager(self.config)
        self.skills = SkillLoader(self.config)
        self.prompts = PromptBuilder(self.config)
        self.tools = BasicTools(self.config)
        self.bus = MessageBus(self.config)
        self.team = TeamManager(self.config, self.client)
        self.hooks = HookManager(self.config)
        self.subagents = SubagentRunner(self.config, self.client)
        self.context = ContextManager(self.config, self.client)
        self.recovery = RecoveryManager(self.config)
        self.background = BackgroundTaskManager(self.config)
        self.scheduler = CronScheduler(self.config)
        self.mcp = MCPManager(self.config)
        self.registry = ToolRegistry(self.config)
        self.agent = AgentLoop(self.config, self.client)
        self.memory = MemorySystem(self.config, self.client)
        evaluator = goal_evaluator if goal_evaluator is not None else PromptGoalEvaluator(
            self.client, self.config.GOAL_EVALUATOR_MODEL,
            self.config.GOAL_EVALUATOR_MAX_TOKENS)
        self.goal = GoalController(evaluator, self.config.GOAL_STOP_HOOK_BLOCK_CAP)

        self.tasks.team = self.team
        self.tasks.worktrees = self.worktrees
        self.worktrees.background = self.background
        self.worktrees.tasks = self.tasks
        self.prompts.mcp = self.mcp
        self.prompts.skills = self.skills
        self.prompts.goal = self.goal
        self.tools.tasks = self.tasks
        self.tools.goal = self.goal
        self.team.bus = self.bus
        self.team.hooks = self.hooks
        self.team.tasks = self.tasks
        self.team.tools = self.tools
        self.team.context = self.context
        self.bus.context = self.context
        self.hooks.console = self.console
        self.subagents.skills = self.skills
        self.team.skills = self.skills
        self.subagents.context = self.context
        self.subagents.hooks = self.hooks
        self.subagents.tools = self.tools
        self.context.mcp = self.mcp
        self.context.memory = self.memory
        self.context.team = self.team
        self.context.sandbox = self.tools.sandbox
        self.background.hooks = self.hooks
        self.background.tools = self.tools
        self.registry.hooks = self.hooks
        self.registry.mcp = self.mcp
        self.registry.scheduler = self.scheduler
        self.registry.skills = self.skills
        self.registry.subagents = self.subagents
        self.registry.tasks = self.tasks
        self.registry.team = self.team
        self.registry.tools = self.tools
        self.registry.worktrees = self.worktrees
        self.agent.background = self.background
        self.agent.console = self.console
        self.agent.context = self.context
        self.agent.hooks = self.hooks
        self.agent.prompts = self.prompts
        self.agent.recovery = self.recovery
        self.agent.registry = self.registry
        self.agent.scheduler = self.scheduler
        self.agent.tasks = self.tasks
        self.agent.team = self.team
        self.agent.tools = self.tools
        self.agent.goal = self.goal
        self.subagents.bind_handlers()
        self.registry.bind_handlers()
        self.cli = HarnessCLI(self)

    def run(self):
        self.tools.install_process_cleanup()
        try:
            if self.tools.sandbox:
                self.tools.sandbox.prepare()
            self.team.start_lease_monitor()
            self.cli.run()
        finally:
            self.team.stop_lease_monitor()
            failing = sys.exc_info()[0] is not None
            try:
                self.tools._stop_all_shell_processes()
            except Exception as exc:
                if not failing:
                    raise
                print(f"Sandbox cleanup warning: {exc}", file=sys.stderr)
