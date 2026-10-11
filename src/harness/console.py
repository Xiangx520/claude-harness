"""Serialize stdin prompts and background terminal output."""

import threading

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False


class ConsoleBroker:
    """Serialize normal prompts and worker permission questions on one stdin."""

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.reader = None
        self.display_prompt = self.config.PROMPT
        self.readline_prompt = self.config.READLINE_PROMPT
        self.CLI_ACTIVE = False

    def set_prompt(self, display_prompt: str, readline_prompt: str):
        self.display_prompt = display_prompt
        self.readline_prompt = readline_prompt

    def ask(self, prompt: str | None = None) -> str:
        with self._lock:
            active_prompt = self.readline_prompt if prompt is None else prompt
            return (self.reader or input)(active_prompt)

    def terminal_print(self, text: str):
        if threading.current_thread() is threading.main_thread() or not self.CLI_ACTIVE:
            print(text)
            return
        line = ""
        if READLINE_AVAILABLE:
            try:
                line = readline.get_line_buffer()
            except Exception:
                line = ""
        print(f"\r\033[K{text}")
        print(self.display_prompt + line, end="", flush=True)
