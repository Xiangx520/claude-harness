#!/usr/bin/env python3
"""
s15: Integrated Harness - combine the course mechanisms in one runtime.

Run:  python src/code.py
Need: uv sync + .env with ANTHROPIC_API_KEY and MODEL_ID
"""

from harness.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
