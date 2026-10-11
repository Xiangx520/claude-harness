"""Display Taffy's pixel avatar before the interactive session starts."""

import json
import os
import shutil
import sys
from pathlib import Path


def show_startup_animation():
    """Display the completed avatar once, before any worker can print."""
    output = sys.stdout
    if not output.isatty() or os.getenv("TERM") == "dumb" or "NO_COLOR" in os.environ:
        return
    try:
        "▀▄".encode(output.encoding or "utf-8")
    except UnicodeEncodeError:
        return

    # Windows consoles need virtual terminal processing for ANSI colors/cursors.
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(kernel32.GetStdHandle(-11))
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return

    columns, rows = shutil.get_terminal_size(fallback=(80, 24))
    asset = Path(__file__).resolve().parent / "assets" / "startup_avatar.ansi.json"
    try:
        variants = json.loads(asset.read_text(encoding="utf-8"))
        if not isinstance(variants, list):
            return
        portrait = next((item for item in reversed(variants)
                         if isinstance(item, dict)
                         and isinstance(item.get("width"), int)
                         and isinstance(item.get("lines"), list)
                         and all(isinstance(line, str) for line in item["lines"])
                         and item["width"] + 4 <= columns
                         and len(item["lines"]) + 5 <= rows), None)
    except (OSError, ValueError):
        return
    if portrait is None:
        return

    output.write("\n".join(f"  {line}" for line in portrait["lines"]) + "\n\n")
    output.flush()
