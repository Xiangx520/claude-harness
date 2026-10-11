import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.config import ConfigurationError, HarnessConfig
from harness.runtime import HarnessRuntime
from harness.sandbox import DockerSandbox, FILE_TOOL, SandboxError, workspace_archive
from harness.tools import BasicTools


def make_archive(name="result.txt", content=b"new", kind=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(name)
        if kind:
            member.type = kind
            member.linkname = "../../outside"
            archive.addfile(member)
        else:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


class SandboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = HarnessConfig(self.root, "model", sandbox_mode="docker")
        self.sandbox = DockerSandbox(self.config)

    def test_snapshot_excludes_credentials_state_and_dependencies(self):
        for name in (".env", ".env.local", "private.pem", "private.KEY", "id_rsa",
                     ".git/config", ".aws/credentials", ".tasks/state.json",
                     "node_modules/lib.js", ".sandbox_exports/old.txt",
                     ".sandbox_connection/known_hosts", ".tools/docker.exe", "src/app.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("secret" if name != "src/app.py" else "print(1)", encoding="utf-8")
        with tarfile.open(fileobj=io.BytesIO(workspace_archive(self.root, 1024))) as archive:
            self.assertEqual(archive.getnames(), ["src", "src/app.py"])
            for member in archive.getmembers():
                self.assertEqual(member.uid, 1000)
                self.assertEqual(member.gid, 1000)

    def test_snapshot_enforces_transfer_limit(self):
        (self.root / "large").write_bytes(b"12345")
        with self.assertRaises(SandboxError):
            workspace_archive(self.root, 4)

    def test_snapshot_ignores_links_to_external_files(self):
        try:
            (self.root / "link").symlink_to(self.root.parent)
        except OSError:
            self.skipTest("symlinks unavailable")
        with tarfile.open(fileobj=io.BytesIO(workspace_archive(self.root, 1024))) as archive:
            self.assertEqual(archive.getnames(), [])

    def test_prepare_uses_isolation_and_remote_copy_without_host_mounts(self):
        self.config.SANDBOX_DOCKER_CONTEXT = "aliyun"
        with patch.object(self.sandbox, "_checked", return_value=b"ok") as call:
            first = self.sandbox.prepare()
            self.assertEqual(self.sandbox.prepare(), first)
        args = call.call_args_list[0].args[0]
        for flag in ("--read-only", "--cap-drop=ALL", "--init", "--pids-limit"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--user") + 1], "1000:1000")
        self.assertNotIn("--mount", args)
        self.assertNotIn("--volume", args)
        self.assertNotIn("ANTHROPIC_API_KEY", " ".join(args))
        self.assertEqual(call.call_args_list[1].args[0][:3], ["exec", "-i", first])
        self.assertIn("--no-same-owner", call.call_args_list[1].args[0])
        self.assertIsInstance(call.call_args_list[1].kwargs["payload"], bytes)
        self.assertEqual(call.call_count, 3)

    def test_prepare_rejects_cwd_outside_project(self):
        with patch.object(self.sandbox, "_invoke") as invoke:
            with self.assertRaises(SandboxError):
                self.sandbox.prepare(self.root.parent)
            invoke.assert_not_called()

    def test_failed_setup_cleans_owned_container_and_does_not_cache(self):
        with patch.object(self.sandbox, "_checked", side_effect=[b"ok", SandboxError("copy failed"), b"ok"]) as call:
            with self.assertRaisesRegex(SandboxError, "copy failed"):
                self.sandbox.prepare()
        self.assertEqual(call.call_args.args[0][:2], ["rm", "--force"])
        self.assertFalse(self.sandbox._containers)
        self.assertFalse(self.sandbox._owned)

    def test_shell_failure_never_falls_back_to_host(self):
        tools = BasicTools(self.config)
        with patch.object(tools.sandbox, "execute", side_effect=SandboxError("offline")), patch("harness.tools.subprocess.Popen") as process:
            self.assertEqual(tools._run_bash_process("echo unsafe"), ("Error: offline", None))
            process.assert_not_called()

    def test_all_file_tools_delegate_to_same_sandbox_backend(self):
        tools = BasicTools(self.config)
        with patch.object(tools.sandbox, "file_tool", return_value="sandbox result") as call:
            self.assertEqual(tools.run_write("new.txt", "x"), "sandbox result")
            tools.run_edit("new.txt", "x", "y")
            tools.run_read("new.txt")
            tools.run_glob("**/*")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["write", "edit", "read", "glob"])
        self.assertFalse((self.root / "new.txt").exists())

    def test_shell_timeout_is_enforced_inside_remote_container(self):
        with patch.object(self.sandbox, "sync"), patch.object(self.sandbox, "prepare", return_value="owned"), patch.object(self.sandbox, "_invoke", return_value=(124, b"", False)) as invoke:
            output, code = self.sandbox.execute("sleep 300")
        self.assertEqual(code, 124)
        self.assertIn("timed out", output)
        self.assertIn("/usr/bin/timeout", invoke.call_args.args[0])
        self.assertEqual(invoke.call_args.args[0][-1], "sleep 300")

    def test_transport_timeout_removes_container(self):
        with patch.object(self.sandbox, "sync"), patch.object(self.sandbox, "prepare", return_value="owned"), patch.object(self.sandbox, "_invoke", side_effect=SandboxError("timeout")), patch.object(self.sandbox, "close") as close:
            with self.assertRaises(SandboxError):
                self.sandbox.execute("sleep 300")
        close.assert_called_once()

    def test_export_creates_review_copy_and_preserves_original(self):
        (self.root / "result.txt").write_text("original", encoding="utf-8")
        with patch.object(self.sandbox, "prepare", return_value="owned"), patch.object(self.sandbox, "_checked", return_value=make_archive()):
            destination = self.sandbox.export()
        self.assertEqual((destination / "result.txt").read_text(), "new")
        self.assertEqual((self.root / "result.txt").read_text(), "original")

    def test_export_rejects_traversal_links_devices_and_windows_paths(self):
        bad = [("../../outside", None), ("/outside", None), ("C:/outside", None),
               ("folder\\outside", None), ("NUL", None), ("file.txt:stream", None),
               ("link", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE), ("fifo", tarfile.FIFOTYPE)]
        for name, kind in bad:
            with self.subTest(name=name), patch.object(self.sandbox, "prepare", return_value="owned"), patch.object(self.sandbox, "_checked", return_value=make_archive(name, kind=kind)):
                with self.assertRaises(SandboxError):
                    self.sandbox.export()
        self.assertFalse((self.root / ".sandbox_exports").exists())

    def test_file_rpc_rejects_traversal(self):
        # Exercise the actual container file handler in a temporary filesystem.
        script = FILE_TOOL.replace('pathlib.Path("/workspace")', f"pathlib.Path({str(self.root)!r})")
        for operation in ("read", "write", "edit"):
            result = subprocess.run([sys.executable, "-I", "-c", script],
                                    input=json.dumps({"operation": operation, "path": "../outside", "content": "bad"}),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Path escapes workspace", result.stdout)

    def test_invoke_uses_explicit_context_scrubs_docker_env_and_bounds_output(self):
        original_popen = subprocess.Popen
        captured = {}

        def spawn(command, **kwargs):
            captured.update(command=command, environment=kwargs["env"])
            return original_popen([sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"], **kwargs)

        self.config.SANDBOX_DOCKER_CONTEXT = "aliyun"
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://wrong-host:2375", "DOCKER_CONTEXT": "wrong"}), patch("harness.sandbox.subprocess.Popen", side_effect=spawn):
            code, output, truncated = self.sandbox._invoke(["info"], limit=100)
        self.assertEqual(code, 0)
        self.assertEqual(len(output), 100)
        self.assertTrue(truncated)
        self.assertEqual(captured["command"], ["docker", "--context", "aliyun", "info"])
        self.assertNotIn("DOCKER_HOST", captured["environment"])
        self.assertNotIn("DOCKER_CONTEXT", captured["environment"])

    def test_invoke_streams_large_stdin_without_pipe_deadlock(self):
        original_popen = subprocess.Popen

        def spawn(command, **kwargs):
            return original_popen([sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"], **kwargs)

        with patch("harness.sandbox.subprocess.Popen", side_effect=spawn):
            code, output, truncated = self.sandbox._invoke(["cp"], payload=b"x" * 200000, timeout=5)
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), b"200000")
        self.assertFalse(truncated)

    def test_invoke_supports_cli_path_with_spaces_without_shell_interpolation(self):
        original_popen = subprocess.Popen
        commands = []

        def spawn(command, **kwargs):
            commands.append(command)
            return original_popen([sys.executable, "-c", "print('ok')"], **kwargs)

        self.config.SANDBOX_DOCKER_EXECUTABLE = "C:/Program Files/Docker/docker.exe"
        with patch("harness.sandbox.subprocess.Popen", side_effect=spawn):
            code, output, truncated = self.sandbox._invoke(["info"])
        self.assertEqual(code, 0)
        self.assertEqual(commands[0][0], "C:/Program Files/Docker/docker.exe")
        self.assertEqual(output.strip(), b"ok")

    def test_invoke_timeout_stops_client_process(self):
        original_popen = subprocess.Popen
        processes = []

        def spawn(command, **kwargs):
            process = original_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
            processes.append(process)
            return process

        with patch("harness.sandbox.subprocess.Popen", side_effect=spawn):
            with self.assertRaisesRegex(SandboxError, "timed out"):
                self.sandbox._invoke(["info"], timeout=0.1)
        self.assertIsNotNone(processes[0].poll())

    def test_config_rejects_invalid_limits_and_mode(self):
        for args in ({"sandbox_mode": "bad"}, {"sandbox_cpus": float("nan")},
                     {"sandbox_cpus": 0}, {"sandbox_pids": -1}, {"sandbox_image": "--privileged"},
                     {"sandbox_docker_context": "bad context"}, {"sandbox_transfer_mb": 0}):
            with self.subTest(args=args), self.assertRaises(ConfigurationError):
                HarnessConfig(self.root, "model", **args)

    def test_env_defaults_to_docker_and_allows_explicit_host_mode(self):
        with patch.dict(os.environ, {"MODEL_ID": "model", "ANTHROPIC_API_KEY": "test"}, clear=True), patch.object(Path, "cwd", return_value=self.root), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(HarnessConfig.from_env().SANDBOX_MODE, "docker")
            os.environ["SANDBOX_MODE"] = "disabled"
            self.assertEqual(HarnessConfig.from_env().SANDBOX_MODE, "disabled")

    def test_cli_export_is_local_and_waits_for_background(self):
        runtime = HarnessRuntime(self.config, Mock())
        runtime.console.terminal_print = Mock()
        with patch.object(runtime.tools.sandbox, "export", return_value=self.root / "review") as export:
            runtime.cli.submit_query("/sandbox export", [], {}, {})
            export.assert_called_once()
            runtime.background.background_tasks["bg"] = {"status": "running"}
            runtime.cli.submit_query("/sandbox export", [], {}, {})
            self.assertEqual(export.call_count, 1)
        runtime.client.messages.create.assert_not_called()

    def test_cli_sync_and_conflict_resolution_do_not_call_model(self):
        runtime = HarnessRuntime(self.config, Mock())
        runtime.console.terminal_print = Mock()
        with patch.object(runtime.tools.sandbox, "sync", return_value="synced") as sync:
            for command, choice in (("/sandbox sync", None), ("/sandbox resolve local", "local"),
                                    ("/sandbox resolve remote", "remote")):
                runtime.cli.submit_query(command, [], {}, {})
                self.assertEqual(sync.call_args.kwargs, {"choice": choice})
            runtime.background.background_tasks["bg"] = {"status": "running"}
            runtime.cli.submit_query("/sandbox resolve remote", [], {}, {})
            self.assertEqual(sync.call_count, 3)
        runtime.client.messages.create.assert_not_called()

    def test_compacted_artifacts_are_copied_into_sandbox(self):
        runtime = HarnessRuntime(self.config, Mock())
        with patch.object(runtime.tools.sandbox, "file_tool", return_value="Wrote artifact") as call:
            path = runtime.context.save_output("tool", "full result")
            self.assertEqual(call.call_args.kwargs["path"], ".task_outputs/tool-results/tool.txt")
            runtime.tools.run_read(str(path))
            self.assertEqual(call.call_args.kwargs["path"], ".task_outputs/tool-results/tool.txt")

    def test_sandbox_blocks_host_worktree_mutations(self):
        runtime = HarnessRuntime(self.config, Mock())
        with patch("harness.worktrees.subprocess.run") as run:
            self.assertIn("unavailable", runtime.worktrees.create_worktree("test", "task"))
            self.assertIn("unavailable", runtime.worktrees.remove_worktree("test"))
            run.assert_not_called()


@unittest.skipUnless(os.getenv("TAFFY_DOCKER_INTEGRATION") == "1", "requires Docker and built sandbox image")
class DockerIntegrationTests(unittest.TestCase):
    def test_real_container_isolation_files_and_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".env").write_text("DO_NOT_COPY=private", encoding="utf-8")
            (root / "source.txt").write_text("original", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "source.txt").write_text("nested", encoding="utf-8")
            config = HarnessConfig(root, "unused", sandbox_mode="docker", sandbox_timeout_seconds=1,
                                   sandbox_docker_context=os.getenv("SANDBOX_DOCKER_CONTEXT", "default"),
                                   sandbox_docker_executable=os.getenv("SANDBOX_DOCKER_EXECUTABLE", "docker"),
                                   sandbox_memory_mb=512, sandbox_workspace_mb=128,
                                   sandbox_transfer_mb=64, sandbox_cpus=0.5, sandbox_pids=64)
            tools = BasicTools(config)
            name = None
            try:
                name = tools.sandbox.prepare()
                host_config = json.loads(tools.sandbox._checked(
                    ["inspect", "--format", "{{json .HostConfig}}", name]))
                self.assertTrue(host_config["ReadonlyRootfs"])
                self.assertEqual(host_config["NetworkMode"], "none")
                self.assertEqual(host_config["CapDrop"], ["ALL"])
                self.assertEqual(host_config["Memory"], 512 * 1024 * 1024)
                self.assertEqual(host_config["MemorySwap"], 512 * 1024 * 1024)
                self.assertEqual(host_config["PidsLimit"], 64)
                self.assertEqual(host_config["NanoCpus"], 500000000)
                self.assertFalse(host_config["Binds"])
                self.assertEqual(tools.run_read("source.txt"), "original")
                self.assertIn("Error:", tools.run_read(".env"))
                self.assertIn("Error:", tools.run_write("../outside", "bad"))
                self.assertIn("Wrote", tools.run_write("nested/new.txt", "new"))
                tools.run_write("source.txt", "modified")
                self.assertEqual(tools.run_bash("cat source.txt"), "modified")
                self.assertEqual((root / "source.txt").read_text(), "modified")
                self.assertEqual((root / "nested/new.txt").read_text(), "new")
                self.assertFalse((root / ".sandbox_exports").exists())
                # External editor changes become visible on the next tool call.
                (root / "source.txt").write_text("editor", encoding="utf-8")
                self.assertEqual(tools.run_read("source.txt"), "editor")
                self.assertEqual(tools.run_bash("printf '\\000\\377' > binary.bin; mv nested/new.txt renamed.txt; mkdir empty; rm nested/source.txt"), "(no output)")
                self.assertEqual((root / "binary.bin").read_bytes(), b"\x00\xff")
                self.assertEqual((root / "renamed.txt").read_text(), "new")
                self.assertFalse((root / "nested/source.txt").exists())
                self.assertTrue((root / "empty").is_dir())
                # Excluded caches and credentials created inside the container
                # never block the snapshot or overwrite host state.
                tools.run_bash("mkdir node_modules; ln -s /tmp node_modules/link; printf private > .env; printf ignored > node_modules/cache")
                self.assertEqual((root / ".env").read_text(), "DO_NOT_COPY=private")
                self.assertFalse((root / "node_modules").exists())
                self.assertEqual(tools.run_bash("id -u"), "1000")
                self.assertIn("status", tools.run_bash("touch /root/blocked"))
                self.assertEqual(tools.run_bash("test ! -S /var/run/docker.sock && test -z \"$ANTHROPIC_API_KEY\" && echo isolated"), "isolated")
                self.assertIn("status", tools.run_bash("python -c 'import socket; socket.create_connection((\"1.1.1.1\", 443), .5)'"))
                self.assertIn("timed out", tools.run_bash("sleep 10"))
                # Simulate simultaneous editor/remote edits without the tool lock.
                tools.sandbox._checked(["exec", name, "sh", "-c", "printf remote > source.txt"])
                (root / "source.txt").write_text("local", encoding="utf-8")
                self.assertIn("sync conflict", tools.run_read("source.txt"))
                self.assertEqual((root / "source.txt").read_text(), "local")
                self.assertTrue(list((root / ".sandbox_conflicts").rglob("source.txt")))
                tools.sandbox.sync(choice="remote")
                self.assertEqual((root / "source.txt").read_text(), "remote")
                destination = tools.sandbox.export()
                self.assertEqual((destination / "source.txt").read_text(), "remote")
            finally:
                tools.sandbox.close()
            self.assertIsNotNone(name)
            code, _, _ = tools.sandbox._invoke(["inspect", name])
            self.assertNotEqual(code, 0, "sandbox container must be removed after cleanup")


if __name__ == "__main__":
    unittest.main()
