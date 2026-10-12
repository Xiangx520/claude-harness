import importlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from harness.config import ConfigurationError, HarnessConfig


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "user"
        self.workdir = self.root / "中文 project"
        self.home.mkdir()
        self.workdir.mkdir()
        self.global_env = self.home / ".taffy" / ".env"
        self.project_env = self.workdir / ".env"
        patches = [
            patch.object(Path, "home", return_value=self.home),
            patch.object(Path, "cwd", return_value=self.workdir),
            patch.dict(os.environ, {}, clear=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def write_global(self, content):
        self.global_env.parent.mkdir(parents=True, exist_ok=True)
        self.global_env.write_text(content, encoding="utf-8")

    def test_global_configuration_uses_callers_workdir(self):
        self.write_global("MODEL_ID=global-model\nANTHROPIC_API_KEY=global-key\n"
                          "ANTHROPIC_BASE_URL=https://global.example.invalid\n"
                          "FALLBACK_MODEL_ID=fallback\n")
        config = HarnessConfig.from_env()
        self.assertEqual(config.MODEL, "global-model")
        self.assertEqual(config.FALLBACK_MODEL, "fallback")
        self.assertEqual(config.WORKDIR, self.workdir)
        self.assertEqual(config.SKILLS_DIR, self.workdir / "skills")
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "global-key")
        self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "https://global.example.invalid")

    def test_process_configuration_overrides_global_defaults(self):
        self.write_global("MODEL_ID=global-model\nANTHROPIC_API_KEY=global-key\n")
        os.environ.update(MODEL_ID="process-model", ANTHROPIC_API_KEY="process-key")
        config = HarnessConfig.from_env()
        self.assertEqual(config.MODEL, "process-model")
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "process-key")

    def test_project_configuration_overrides_process_and_global(self):
        self.write_global("MODEL_ID=global-model\nANTHROPIC_API_KEY=global-key\n"
                          "FALLBACK_MODEL_ID=global-fallback\n")
        os.environ.update(MODEL_ID="process-model", ANTHROPIC_API_KEY="process-key")
        self.project_env.write_text("MODEL_ID=project-model\nANTHROPIC_API_KEY=project-key\n",
                                    encoding="utf-8")
        config = HarnessConfig.from_env()
        self.assertEqual(config.MODEL, "project-model")
        self.assertEqual(config.FALLBACK_MODEL, "global-fallback")
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "project-key")

    def test_parent_directory_configuration_is_not_loaded(self):
        (self.workdir.parent / ".env").write_text(
            "MODEL_ID=parent-model\nANTHROPIC_API_KEY=parent-key\n", encoding="utf-8")
        with self.assertRaises(ConfigurationError) as caught:
            HarnessConfig.from_env()
        self.assertIn("MODEL_ID", str(caught.exception))
        self.assertNotIn("MODEL_ID", os.environ)

    def test_missing_configuration_reports_keys_and_paths_without_values(self):
        os.environ["ANTHROPIC_API_KEY"] = "private-test-secret"
        with self.assertRaises(ConfigurationError) as caught:
            HarnessConfig.from_env()
        message = str(caught.exception)
        self.assertIn("MODEL_ID", message)
        self.assertIn(str(self.global_env), message)
        self.assertIn(str(self.project_env), message)
        self.assertNotIn("private-test-secret", message)

    def test_empty_values_fail_validation(self):
        self.project_env.write_text("MODEL_ID='  '\nANTHROPIC_API_KEY=''\n", encoding="utf-8")
        with self.assertRaises(ConfigurationError) as caught:
            HarnessConfig.from_env()
        self.assertIn("MODEL_ID", str(caught.exception))
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))

    def test_custom_endpoint_preserves_token_cleanup(self):
        os.environ.update(MODEL_ID="model", ANTHROPIC_API_KEY="key",
                          ANTHROPIC_AUTH_TOKEN="unused-token",
                          ANTHROPIC_BASE_URL="https://api.example.invalid")
        HarnessConfig.from_env()
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", os.environ)

    def test_auth_token_without_custom_endpoint_is_supported(self):
        os.environ.update(MODEL_ID="model", ANTHROPIC_AUTH_TOKEN="auth-token")
        self.assertEqual(HarnessConfig.from_env().MODEL, "model")

    def test_utf8_bom_configuration_is_supported(self):
        self.project_env.write_text("MODEL_ID=bom-model\nANTHROPIC_API_KEY=key\n",
                                    encoding="utf-8-sig")
        self.assertEqual(HarnessConfig.from_env().MODEL, "bom-model")

    def test_worker_limits_defaults_and_project_overrides(self):
        os.environ.update(MODEL_ID="worker", ANTHROPIC_API_KEY="key")
        config = HarnessConfig.from_env()
        self.assertEqual(config.MAX_TEAMMATES, 3)
        self.assertEqual(config.WORKER_MAX_TURNS, 30)
        self.write_global("MAX_TEAMMATES=4\nWORKER_MAX_TURNS=40\n")
        self.project_env.write_text("MAX_TEAMMATES=2\nWORKER_MAX_TURNS=12\n", encoding="utf-8")
        config = HarnessConfig.from_env()
        self.assertEqual(config.MAX_TEAMMATES, 2)
        self.assertEqual(config.WORKER_MAX_TURNS, 12)

    def test_worker_limits_reject_invalid_environment_values(self):
        os.environ.update(MODEL_ID="worker", ANTHROPIC_API_KEY="key")
        for name in ["MAX_TEAMMATES", "WORKER_MAX_TURNS"]:
            for invalid in ["0", "-1", "1.5", "invalid"]:
                with self.subTest(name=name, invalid=invalid):
                    with patch.dict(os.environ, {name: invalid}):
                        with self.assertRaisesRegex(ConfigurationError, name):
                            HarnessConfig.from_env()

    def test_goal_configuration_defaults_and_project_overrides(self):
        os.environ.update(MODEL_ID="worker", ANTHROPIC_API_KEY="key")
        config = HarnessConfig.from_env()
        self.assertEqual(config.GOAL_EVALUATOR_MODEL, "worker")
        self.assertEqual(config.GOAL_STOP_HOOK_BLOCK_CAP, 8)
        self.assertIsNone(config.MAX_TURNS)
        self.write_global("GOAL_EVALUATOR_MODEL_ID=global-judge\nMAX_TURNS=10\n")
        self.project_env.write_text(
            "GOAL_EVALUATOR_MODEL_ID=project-judge\nMAX_TURNS=20\nGOAL_STOP_HOOK_BLOCK_CAP=3\n",
            encoding="utf-8")
        config = HarnessConfig.from_env()
        self.assertEqual(config.GOAL_EVALUATOR_MODEL, "project-judge")
        self.assertEqual(config.MAX_TURNS, 20)
        self.assertEqual(config.GOAL_STOP_HOOK_BLOCK_CAP, 3)

    def test_invalid_loop_limits_report_key_without_value(self):
        os.environ.update(MODEL_ID="worker", ANTHROPIC_API_KEY="key")
        for name in ("MAX_TURNS", "GOAL_STOP_HOOK_BLOCK_CAP"):
            for value in ("0", "-1", "1.5", "private-invalid-value"):
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, {name: value}):
                        with self.assertRaises(ConfigurationError) as caught:
                            HarnessConfig.from_env()
                    self.assertIn(name, str(caught.exception))
                    if value == "private-invalid-value":
                        self.assertNotIn(value, str(caught.exception))

    def test_invalid_encoding_reports_path_without_file_contents(self):
        self.project_env.write_bytes(b"\xff\xfeprivate-invalid-file")
        with self.assertRaises(ConfigurationError) as caught:
            HarnessConfig.from_env()
        self.assertIn(str(self.project_env), str(caught.exception))
        self.assertNotIn("private-invalid-file", str(caught.exception))


class EntryPointTests(unittest.TestCase):
    def test_import_does_not_start_runtime(self):
        with patch("harness.runtime.HarnessRuntime") as runtime:
            entry = importlib.import_module("harness.__main__")
            importlib.reload(entry)
            runtime.assert_not_called()
        # Restore the real binding for other tests after the patched reload.
        importlib.reload(entry)

    def test_main_runs_runtime_and_returns_zero(self):
        from harness.__main__ import main
        with patch("harness.__main__.HarnessRuntime") as runtime:
            self.assertEqual(main(), 0)
            runtime.return_value.run.assert_called_once_with()

    def test_main_reports_configuration_error_without_traceback(self):
        from harness.__main__ import main
        errors = io.StringIO()
        with patch("harness.__main__.HarnessRuntime", side_effect=ConfigurationError("缺少 MODEL_ID")), \
                redirect_stderr(errors):
            self.assertEqual(main(), 1)
        self.assertIn("MODEL_ID", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertNotIn("KeyError", errors.getvalue())

    def test_legacy_script_uses_same_main(self):
        from harness.__main__ import main
        spec = importlib.util.spec_from_file_location("taffy_legacy", ROOT / "src/code.py")
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        self.assertIs(legacy.main, main)

    def test_module_launch_reports_missing_config_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, workdir = root / "home", root / "中文 project"
            home.mkdir()
            workdir.mkdir()
            environment = os.environ.copy()
            for key in ("MODEL_ID", "FALLBACK_MODEL_ID", "ANTHROPIC_API_KEY",
                        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
                environment.pop(key, None)
            environment.update(USERPROFILE=str(home), HOME=str(home),
                               PYTHONIOENCODING="utf-8")
            result = subprocess.run([sys.executable, "-m", "harness"], cwd=workdir,
                                    env=environment, input="q\n", capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("MODEL_ID", result.stderr)
        self.assertIn("ANTHROPIC_API_KEY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
