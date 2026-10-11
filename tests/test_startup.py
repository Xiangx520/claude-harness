import io
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness import HarnessConfig, HarnessRuntime
from harness import startup


class TerminalOutput(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.output = TerminalOutput()
        self.environment = {}
        patches = [
            patch.object(startup.sys, "stdout", self.output),
            patch.object(startup, "os", SimpleNamespace(
                name="posix", environ=self.environment, getenv=self.environment.get)),
            patch.object(startup.shutil, "get_terminal_size", return_value=(80, 24)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_default_terminal_displays_supplied_avatar_and_resets_colors(self):
        startup.show_startup_animation()
        lines = self.output.getvalue().splitlines()
        self.assertEqual(len(lines), 19)
        for line in lines[:-1]:
            self.assertTrue(line.startswith("  "))
            self.assertTrue(line.endswith("\033[0m"))
            self.assertEqual(len(re.sub(r"\033\[[0-9;]*m", "", line)), 38)
        self.assertIn("\033[38;2;", self.output.getvalue())

    def test_largest_variant_that_fits_is_selected(self):
        with patch.object(startup.shutil, "get_terminal_size", return_value=(100, 50)):
            startup.show_startup_animation()
        self.assertEqual(len(self.output.getvalue().splitlines()), 41)

    def test_redirected_output_is_skipped(self):
        with patch.object(startup.sys, "stdout", io.StringIO()) as redirected:
            startup.show_startup_animation()
        self.assertEqual(redirected.getvalue(), "")

    def test_color_disabled_and_small_terminals_are_skipped(self):
        for environment in ({"NO_COLOR": ""}, {"TERM": "dumb"}):
            self.environment.clear()
            self.environment.update(environment)
            startup.show_startup_animation()
            self.assertEqual(self.output.getvalue(), "")
        self.environment.clear()
        with patch.object(startup.shutil, "get_terminal_size", return_value=(20, 10)):
            startup.show_startup_animation()
        self.assertEqual(self.output.getvalue(), "")

    def test_unsupported_encoding_is_skipped(self):
        class AsciiOutput(TerminalOutput):
            encoding = "ascii"

        with patch.object(startup.sys, "stdout", AsciiOutput()) as output:
            startup.show_startup_animation()
        self.assertEqual(output.getvalue(), "")

    def test_missing_and_malformed_asset_do_not_prevent_startup(self):
        with patch.object(Path, "read_text", side_effect=OSError):
            startup.show_startup_animation()
        for content in ("invalid JSON", "null", '[{"width": "bad", "lines": []}]'):
            with patch.object(Path, "read_text", return_value=content):
                startup.show_startup_animation()
        self.assertEqual(self.output.getvalue(), "")

    def test_windows_console_enables_virtual_terminal_processing(self):
        kernel32 = Mock()
        kernel32.GetStdHandle.return_value = 123

        def get_mode(handle, mode):
            mode._obj.value = 1
            return 1

        kernel32.GetConsoleMode.side_effect = get_mode
        windows = SimpleNamespace(name="nt", environ={}, getenv=lambda key: None)
        with patch.object(startup, "os", windows), \
                patch("ctypes.windll", SimpleNamespace(kernel32=kernel32), create=True):
            startup.show_startup_animation()
        self.assertEqual(kernel32.SetConsoleMode.call_args.args[1], 5)
        self.assertTrue(self.output.getvalue())

    def test_cli_displays_avatar_before_starting_workers(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = HarnessRuntime(HarnessConfig(Path(temporary.name), "test-model"), Mock())
        runtime.console.reader = Mock(return_value="q")
        events = []
        with patch("harness.cli.show_startup_animation", side_effect=lambda: events.append("avatar")) as show, \
                patch.object(runtime.scheduler, "start_runtime_services", side_effect=lambda: events.append("scheduler")), \
                patch("harness.cli.threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: events.append("agent")
            runtime.cli.run()
        self.assertEqual(events, ["avatar", "scheduler", "agent"])
        show.assert_called_once_with()
        runtime.console.reader.assert_called_once_with(runtime.config.READLINE_PROMPT)


if __name__ == "__main__":
    unittest.main()
