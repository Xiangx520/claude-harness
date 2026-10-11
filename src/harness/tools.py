"""Workspace tools, shell process cleanup, and session todos."""

import ast
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .sandbox import DockerSandbox, SandboxError


class BasicTools:
    """Workspace tools, shell process cleanup, and session todos."""

    def __init__(self, config):
        self.config = config
        self.CURRENT_TODOS: list[dict] = []
        self.goal = None
        self.sandbox = DockerSandbox(config) if config.SANDBOX_MODE == "docker" else None
        self._shell_processes: set[subprocess.Popen] = set()
        self._shell_process_lock = threading.RLock()

    def safe_path(self, path: str, cwd: Path | None = None) -> Path:
        base = (cwd or self.config.WORKDIR).resolve()
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    def _stop_process_group(self, process: subprocess.Popen):
        """Stop a shell process and its children (cross-platform).

        POSIX uses process-group signals (SIGTERM then SIGKILL). Windows has
        neither ``os.killpg`` nor ``signal.SIGKILL``, so it falls back to
        ``Popen.terminate()`` / ``Popen.kill()``.
        """
        if os.name == "nt":
            for stop in (process.terminate, process.kill):
                if process.poll() is not None:
                    return
                try:
                    stop()
                except OSError:
                    return
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            return
        for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            except OSError:
                return
            time.sleep(0.05)

    def _stop_all_shell_processes(self):
        with self._shell_process_lock:
            processes = list(self._shell_processes)
        for process in processes:
            self._stop_process_group(process)
        if self.sandbox:
            self.sandbox.close()

    def _handle_termination_signal(self, signum, _frame):
        self._stop_all_shell_processes()
        raise SystemExit(128 + signum)

    def _run_bash_process(self, command: str, cwd: Path | None = None) -> tuple[str, int | None]:
        if self.sandbox:
            try:
                return self.sandbox.execute(command, cwd)
            except SandboxError as exc:
                return f"Error: {exc}", None
        process = None
        try:
            # start_new_session (setsid) exists only on POSIX; skip it on Windows.
            popen_kwargs = {} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(
                command, shell=True, cwd=cwd or self.config.WORKDIR,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace",
                **popen_kwargs,
            )
            with self._shell_process_lock:
                self._shell_processes.add(process)
            stdout, stderr = process.communicate(timeout=120)
            out = (stdout + stderr).strip()
            return (out[:50000] if out else "(no output)"), process.returncode
        except subprocess.TimeoutExpired:
            return "Error: Timeout (120s)", None
        except OSError as exc:
            return f"Error: {type(exc).__name__}: {exc}", None
        finally:
            if process is not None:
                self._stop_process_group(process)
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
                with self._shell_process_lock:
                    self._shell_processes.discard(process)

    def _format_bash_result(self, output: str, exit_code: int | None) -> str:
        if self.goal is not None and self.goal.active is not None:
            code = exit_code if exit_code is not None else "unknown"
            return f"exit_code={code}\n{output}"
        if exit_code == 0:
            return output
        if exit_code is None:
            return output
        return f"Error: command exited with status {exit_code}\n{output}"

    def run_bash(self, command: str, cwd: Path | None = None,
                 run_in_background: bool = False) -> str:
        # run_in_background is consumed by the dispatcher; direct execution ignores it.
        return self._format_bash_result(*self._run_bash_process(command, cwd))

    def run_read(self, path: str, limit: int | None = None,
                 offset: int = 0, cwd: Path | None = None) -> str:
        try:
            if self.sandbox:
                # Runtime artifact references retain their host paths for
                # compaction bookkeeping; resolve them in the copied workspace.
                candidate = Path(path)
                if candidate.is_absolute() and candidate.is_relative_to(self.config.WORKDIR):
                    path = candidate.relative_to(self.config.WORKDIR).as_posix()
                return self.sandbox.file_tool("read", cwd, path=path, limit=limit, offset=offset)
            file_path = self.safe_path(path, cwd)
            lines = file_path.read_text(encoding="utf-8").splitlines()
            offset = max(int(offset or 0), 0)
            limit = int(limit) if limit is not None else None
            lines = lines[offset:]
            if limit is not None and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def run_write(self, path: str, content: str, cwd: Path | None = None) -> str:
        try:
            if self.sandbox:
                return self.sandbox.file_tool("write", cwd, path=path, content=content)
            fp = self.safe_path(path, cwd)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_edit(self, path: str, old_text: str, new_text: str,
                 cwd: Path | None = None) -> str:
        try:
            if self.sandbox:
                return self.sandbox.file_tool("edit", cwd, path=path, old_text=old_text, new_text=new_text)
            fp = self.safe_path(path, cwd)
            text = fp.read_text(encoding="utf-8")
            if old_text not in text:
                return f"Error: text not found in {path}"
            fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            return f"Edited {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_glob(self, pattern: str, cwd: Path | None = None) -> str:
        import glob as g
        try:
            if self.sandbox:
                return self.sandbox.file_tool("glob", cwd, pattern=pattern)
            base = (cwd or self.config.WORKDIR).resolve()
            matches = sorted({
                match for match in g.glob(
                    pattern, root_dir=base, recursive=True)
                if (base / match).resolve().is_relative_to(base)
            })
            shown = matches[:200]
            if len(matches) > 200:
                shown.append("... (more matches omitted; narrow the pattern)")
            return "\n".join(shown) if shown else "(no matches)"
        except Exception as e:
            return f"Error: {e}"

    def _agent_cwd(self) -> tuple[Path | None, str | None]:
        try:
            return self.tasks.assignment_cwd("agent"), None
        except (FileNotFoundError, ValueError) as exc:
            return None, f"Error: Invalid task assignment: {exc}"

    def run_agent_bash(self, command: str, run_in_background: bool = False) -> str:
        cwd, error = self._agent_cwd()
        return error or self.run_bash(command, cwd, run_in_background)

    def run_agent_read(self, path: str, limit: int | None = None,
                       offset: int = 0) -> str:
        cwd, error = self._agent_cwd()
        return error or self.run_read(path, limit, offset, cwd)

    def run_agent_write(self, path: str, content: str) -> str:
        cwd, error = self._agent_cwd()
        return error or self.run_write(path, content, cwd)

    def run_agent_edit(self, path: str, old_text: str, new_text: str) -> str:
        cwd, error = self._agent_cwd()
        return error or self.run_edit(path, old_text, new_text, cwd)

    def run_agent_glob(self, pattern: str) -> str:
        cwd, error = self._agent_cwd()
        return error or self.run_glob(pattern, cwd)

    def call_tool_handler(self, handler, args: dict, name: str) -> str:
        if not handler:
            return f"Unknown tool: {name}"
        try:
            return str(handler(**(args or {})))
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    def _normalize_todos(self, todos):
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError):
                    return None, "Error: todos must be a list or JSON array string"
        if not isinstance(todos, list):
            return None, "Error: todos must be a list"
        for i, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return None, f"Error: todos[{i}] must be an object"
            if "content" not in todo or "status" not in todo:
                return None, f"Error: todos[{i}] missing 'content' or 'status'"
            if todo["status"] not in ("pending", "in_progress", "completed"):
                return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
        return todos, None

    def run_todo_write(self, todos: list) -> str:
        todos, error = self._normalize_todos(todos)
        if error:
            return error
        self.CURRENT_TODOS = todos
        print(f"  \033[33m[todo] updated {len(self.CURRENT_TODOS)} item(s)\033[0m")
        return f"Updated {len(self.CURRENT_TODOS)} todos"

    def install_process_cleanup(self):
        def cleanup():
            try:
                self._stop_all_shell_processes()
            except SandboxError as exc:
                print(f"Sandbox cleanup warning: {exc}", file=sys.stderr)
        atexit.register(cleanup)
        signal.signal(signal.SIGTERM, self._handle_termination_signal)
