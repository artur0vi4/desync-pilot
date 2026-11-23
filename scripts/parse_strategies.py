#!/usr/bin/env python3
"""CLI wrapper around app.strategy_parser.

Usage:
    python scripts/parse_strategies.py --src path/to/zapret-discord-youtube --out strategies.json

Without arguments it looks at ./zapret-src (or $PARSE_SRC) and writes
./strategies.json (or $PARSE_OUT).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategy_parser import cli  # noqa: E402

if __name__ == "__main__":
    cli()
    sys.exit(0)
