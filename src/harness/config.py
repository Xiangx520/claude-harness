"""Environment configuration and the original runtime limits."""

import os
import math
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """A startup configuration error that can be shown without a traceback."""


class HarnessConfig:
    def __init__(self, workdir: Path, model: str,
                 fallback_model: str | None = None, *,
                 goal_evaluator_model: str | None = None,
                 max_turns: int | None = None, stop_hook_block_cap: int = 8,
                 sandbox_mode: str = "disabled", sandbox_image: str = "taffy-sandbox:1",
                 sandbox_docker_context: str = "default", sandbox_cpus: float = 1,
                 sandbox_memory_mb: int = 1024, sandbox_pids: int = 128,
                 sandbox_timeout_seconds: int = 120, sandbox_workspace_mb: int = 512,
                 sandbox_transfer_mb: int = 128, sandbox_docker_executable: str = "docker"):
        if max_turns is not None and max_turns < 1:
            raise ConfigurationError("MAX_TURNS 必须是大于 0 的整数。")
        if stop_hook_block_cap < 1:
            raise ConfigurationError("GOAL_STOP_HOOK_BLOCK_CAP 必须是大于 0 的整数。")
        if sandbox_mode not in {"disabled", "docker"}:
            raise ConfigurationError("SANDBOX_MODE 必须为 docker 或 disabled。")
        if not math.isfinite(sandbox_cpus) or sandbox_cpus <= 0:
            raise ConfigurationError("SANDBOX_CPUS 必须是大于 0 的有限数字。")
        for name, value in (("MEMORY_MB", sandbox_memory_mb), ("PIDS", sandbox_pids),
                            ("TIMEOUT_SECONDS", sandbox_timeout_seconds),
                            ("WORKSPACE_MB", sandbox_workspace_mb), ("TRANSFER_MB", sandbox_transfer_mb)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigurationError(f"SANDBOX_{name} 必须是大于 0 的整数。")
        for name, value in (("IMAGE", sandbox_image), ("DOCKER_CONTEXT", sandbox_docker_context)):
            if not value or value.startswith("-") or any(c.isspace() for c in value):
                raise ConfigurationError(f"SANDBOX_{name} 配置无效。")
        if (not sandbox_docker_executable or sandbox_docker_executable.startswith("-")
                or any(c in sandbox_docker_executable for c in ("\n", "\r", "\x00"))):
            raise ConfigurationError("SANDBOX_DOCKER_EXECUTABLE 配置无效。")
        self.SANDBOX_MODE = sandbox_mode
        self.SANDBOX_IMAGE = sandbox_image
        self.SANDBOX_DOCKER_CONTEXT = sandbox_docker_context
        self.SANDBOX_DOCKER_EXECUTABLE = sandbox_docker_executable
        self.SANDBOX_CPUS = sandbox_cpus
        self.SANDBOX_MEMORY_MB = sandbox_memory_mb
        self.SANDBOX_PIDS = sandbox_pids
        self.SANDBOX_TIMEOUT_SECONDS = sandbox_timeout_seconds
        self.SANDBOX_WORKSPACE_MB = sandbox_workspace_mb
        self.SANDBOX_TRANSFER_MB = sandbox_transfer_mb
        self.WORKDIR = workdir
        self.MODEL = model
        self.PRIMARY_MODEL = self.MODEL
        self.FALLBACK_MODEL = fallback_model
        self.GOAL_EVALUATOR_MODEL = goal_evaluator_model or model
        self.GOAL_EVALUATOR_MAX_TOKENS = 512
        self.GOAL_STOP_HOOK_BLOCK_CAP = stop_hook_block_cap
        self.MAX_TURNS = max_turns
        self.SKILLS_DIR = self.WORKDIR / "skills"
        self.TRANSCRIPT_DIR = self.WORKDIR / ".transcripts"
        self.TOOL_RESULTS_DIR = self.WORKDIR / ".task_outputs" / "tool-results"
        self.DEFAULT_MAX_TOKENS = 8000
        self.ESCALATED_MAX_TOKENS = 16000
        self.MAX_RETRIES = 3
        self.MAX_CONSECUTIVE_529 = 2
        self.MAX_RECOVERY_RETRIES = 2
        self.BASE_DELAY_MS = 500
        self.CONTEXT_LIMIT = 50000
        self.KEEP_RECENT_TOOL_RESULTS = 3
        self.PERSIST_THRESHOLD = 30000
        self.CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
        self.PROMPT = "\033[38;2;255;105;180m>>Taffy \033[0m"
        self.READLINE_PROMPT = "\001\033[38;2;255;105;180m\002>>Taffy \001\033[0m\002"
        self.ENVIRONMENT_PROMPT = (
            "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
            "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
            if os.name == "nt"
            else "Unix-like: the bash tool runs the system shell"
        )
        self.AGENT_WORKDIR = "/workspace" if sandbox_mode == "docker" else str(workdir)
        if sandbox_mode == "docker":
            self.ENVIRONMENT_PROMPT = (
                "Linux Docker sandbox: bash runs /bin/sh, use POSIX shell syntax. "
                "File tools share the copied /workspace. Network is disabled. "
                "Host secrets, Git history and runtime state are excluded. "
                "Project changes automatically sync to the local project after file writes and shell commands. "
                "Local edits sync into the sandbox before the next tool call. "
                "Concurrent conflicting edits stop synchronization and preserve both versions; "
                "report the conflict to the user instead of claiming success. "
                "Credentials and runtime state cannot be synced. /sandbox export is only an optional snapshot. "
                "Use relative workspace paths. Host worktree operations are unavailable"
            )

    @classmethod
    def from_env(cls):
        workdir = Path.cwd()
        global_env = Path.home() / ".taffy" / ".env"
        project_env = workdir / ".env"
        # Global defaults yield to process variables; project values retain
        # the original override=True behavior. Never search beside the source.
        for path, override in ((global_env, False), (project_env, True)):
            try:
                load_dotenv(path, override=override, encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise ConfigurationError(
                    f"无法读取配置文件: {path}。请检查文件权限和 UTF-8 编码。"
                ) from exc
        if os.getenv("ANTHROPIC_BASE_URL"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

        model = os.getenv("MODEL_ID", "").strip()
        missing = []
        if not model:
            missing.append("MODEL_ID")
        if not (os.getenv("ANTHROPIC_API_KEY", "").strip()
                or os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()):
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise ConfigurationError(
                f"缺少必要配置: {', '.join(missing)}\n"
                f"请在全局配置 {global_env}\n"
                f"或当前项目配置 {project_env}\n"
                "中填写这些配置项，也可以设置进程环境变量。"
            )
        def positive_integer(name, default=None):
            raw = os.getenv(name, "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} 必须是大于 0 的整数。") from exc
            if value < 1:
                raise ConfigurationError(f"{name} 必须是大于 0 的整数。")
            return value

        try:
            sandbox_cpus = float(os.getenv("SANDBOX_CPUS", "1"))
        except ValueError as exc:
            raise ConfigurationError("SANDBOX_CPUS 必须是大于 0 的有限数字。") from exc
        return cls(workdir, model, os.getenv("FALLBACK_MODEL_ID"),
                   goal_evaluator_model=os.getenv("GOAL_EVALUATOR_MODEL_ID", "").strip() or None,
                   max_turns=positive_integer("MAX_TURNS"),
                   stop_hook_block_cap=positive_integer("GOAL_STOP_HOOK_BLOCK_CAP", 8),
                   sandbox_mode=os.getenv("SANDBOX_MODE", "docker").strip(),
                   sandbox_image=os.getenv("SANDBOX_IMAGE", "taffy-sandbox:1").strip(),
                   sandbox_docker_context=os.getenv("SANDBOX_DOCKER_CONTEXT", "default").strip(),
                   sandbox_cpus=sandbox_cpus,
                   sandbox_memory_mb=positive_integer("SANDBOX_MEMORY_MB", 1024),
                   sandbox_pids=positive_integer("SANDBOX_PIDS", 128),
                   sandbox_timeout_seconds=positive_integer("SANDBOX_TIMEOUT_SECONDS", 120),
                   sandbox_workspace_mb=positive_integer("SANDBOX_WORKSPACE_MB", 512),
                   sandbox_transfer_mb=positive_integer("SANDBOX_TRANSFER_MB", 128),
                   sandbox_docker_executable=os.getenv("SANDBOX_DOCKER_EXECUTABLE", "docker").strip())
