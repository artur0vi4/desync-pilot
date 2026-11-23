"""Parse upstream ``general*.bat`` strategy files into ``strategies.json``.

The parser is importable (``app.strategy_parser``) so the updater can turn a
downloaded upstream release into strategies in-process — spawning
``sys.executable`` from a frozen build would just relaunch the app.

Source and output locations, in order of precedence:
  1. explicit argument (CLI ``--src`` / ``--out``, or function argument)
  2. environment variables ``PARSE_SRC`` / ``PARSE_OUT``
  3. defaults: ``<project>/zapret-src`` and ``<project>/strategies.json``
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from .winws_args import strip_value_quotes, split_sections

DEFAULT_SRC_DIRNAME = "zapret-src"
DEFAULT_OUT_NAME = "strategies.json"


def project_root() -> Path:
    """Repo root in dev, bundle root when frozen."""
    try:
        from . import paths
        return paths.bundle_root()
    except Exception:  # pragma: no cover - defensive
        return Path(__file__).resolve().parent.parent


def _env_path(name: str) -> Optional[Path]:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def resolve_paths(src: Optional[Path] = None, out: Optional[Path] = None) -> tuple[Path, Path]:
    root = project_root()
    src = src or _env_path("PARSE_SRC") or (root / DEFAULT_SRC_DIRNAME)
    out = out or _env_path("PARSE_OUT") or (root / DEFAULT_OUT_NAME)
    return Path(src), Path(out)


def _read_text(path: Path) -> str:
    """Upstream .bat files are UTF-8 nowadays but older drops were cp866."""
    for enc in ("utf-8-sig", "cp866"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _tokenize(body: str) -> list[str]:
    """Split a winws.exe command line into tokens, honouring double quotes."""
    tokens: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 1
            tokens.append(body[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not body[j].isspace():
                j += 1
            tokens.append(body[i:j])
            i = j
    return tokens


def _winws_command(raw: str) -> Optional[str]:
    """Extract the multi-line `start "zapret: ..." ... winws.exe ...` command."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    joined: list[str] = []
    in_cmd = False
    for ln in lines:
        s = ln.rstrip()
        if not in_cmd:
            if s.lower().startswith('start "zapret:'):
                in_cmd = True
                joined.append(s)
                if not s.endswith("^"):
                    break
        else:
            joined.append(s)
            if not s.endswith("^"):
                break
    if not joined:
        return None
    joined = [ln[:-1].rstrip() if ln.endswith("^") else ln for ln in joined]
    return " ".join(joined)


def _group_of(fname: str) -> str:
    upper = fname.upper()
    if "EXP" in upper:
        return "experimental"
    if "FAKE TLS AUTO" in upper:
        return "fake_tls_auto"
    if "SIMPLE FAKE" in upper:
        return "simple_fake"
    if "ALT" in upper:
        return "alt"
    return "base"


def _modes_of(sections: Iterable[list[str]]) -> list[str]:
    modes: list[str] = []
    for sec in sections:
        mode = None
        for tok in sec:
            if tok.startswith("--dpi-desync="):
                mode = tok.split("=", 1)[1]
        if mode:
            modes.append(mode)
    return modes


def parse_bat(path: Path) -> dict:
    """Parse one .bat file into a strategy dict (empty dict when not a strategy)."""
    cmd = _winws_command(_read_text(path))
    if not cmd:
        return {}
    m = re.search(r'winws\.exe["\']?\s+(.*)', cmd, re.IGNORECASE)
    if not m:
        return {}
    tokens = strip_value_quotes(_tokenize(m.group(1).strip()))
    if not tokens:
        return {}
    sections = split_sections(tokens)
    fname = path.stem.strip()
    return {
        "id": fname,
        "file": path.name,
        "title": fname.replace("general", "").strip(" ()") or "BASE",
        "group": _group_of(fname),
        "sections": sections,
        "modes": _modes_of(sections),
        "raw_args": tokens,
    }


def _natural_key(p: Path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", p.stem)]


def parse_dir(src: Path) -> list[dict]:
    """Parse every ``general*.bat`` under `src` (recursively)."""
    src = Path(src)
    if not src.exists():
        return []
    strategies: list[dict] = []
    seen: set[str] = set()
    for bat in sorted(src.rglob("general*.bat"), key=_natural_key):
        try:
            entry = parse_bat(bat)
        except Exception as e:  # a broken .bat must not kill the sweep
            print(f"skip {bat.name}: {e}", file=sys.stderr)
            continue
        if not entry:
            continue
        sid = entry["id"]
        if sid in seen:  # nested folders can repeat file names
            continue
        seen.add(sid)
        strategies.append(entry)
    return strategies


def write_json(strategies: list[dict], out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(strategies, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def regenerate(src: Optional[Path] = None, out: Optional[Path] = None) -> int:
    """Parse `src` and write `out`. Returns the number of strategies written."""
    src_path, out_path = resolve_paths(src, out)
    strategies = parse_dir(src_path)
    if not strategies:
        return 0
    write_json(strategies, out_path)
    return len(strategies)


def cli(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Parse zapret .bat strategies into strategies.json")
    ap.add_argument("--src", help=f"folder with general*.bat (default: ./{DEFAULT_SRC_DIRNAME} or $PARSE_SRC)")
    ap.add_argument("--out", help=f"output json (default: ./{DEFAULT_OUT_NAME} or $PARSE_OUT)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    src, out = resolve_paths(Path(args.src) if args.src else None, Path(args.out) if args.out else None)
    strategies = parse_dir(src)
    if not strategies:
        print(
            f"No general*.bat found in {src} — nothing to do.\n"
            f"Download the upstream release and put its .bat files there, "
            f"or pass --src / set PARSE_SRC.",
            file=sys.stderr,
        )
        return 0
    write_json(strategies, out)
    print(f"Parsed {len(strategies)} strategies -> {out}")
    if not args.quiet:
        for s in strategies:
            print(f"  {s['id']:42s} group={s['group']:14s} modes={','.join(s['modes'])}")
    return len(strategies)
