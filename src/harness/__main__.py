"""Shared entry point for taffy, python -m harness, and src/code.py."""

import sys

from .config import ConfigurationError
from .sessions import SessionError
from .runtime import HarnessRuntime


def main() -> int:
    try:
        HarnessRuntime().run()
    except (ConfigurationError, SessionError) as exc:
        print(f"Taffy 配置错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
