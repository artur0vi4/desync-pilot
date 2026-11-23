#!/usr/bin/env python3
"""Build strategies_gui.json from the zapret-gui community preset catalog.

Two modes:

    python scripts/import_presets.py --dir path/to/winws1_presets
    python scripts/import_presets.py --base-url https://<forgejo>/<owner>/<repo> --cache-dir .presets-cache

The first mode converts files already on disk (e.g. downloaded once). The
second lists ``src/presets/builtin/winws1/*.txt`` through the Gitea API and
downloads them, caching the text files so the next run is offline.

The script never touches the network at runtime of the app — it produces a
static JSON that ships with the build.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import paths  # noqa: E402
from app.preset_parser import parse_preset  # noqa: E402

PRESET_DIR_IN_REPO = "src/presets/builtin/winws1"
DEFAULT_BASE_URL = "https://iianajhlkh.zapret.moe/zapretdiscordyoutube/zapretgui"
USER_AGENT = "desync-pilot-import"


def supported_flags() -> set[str]:
    """Read the flag names compiled into the bundled winws.exe."""
    exe = paths.winws_exe()
    if not exe.exists():
        return set()
    blob = exe.read_bytes()
    return {m.decode() for m in re.findall(rb"--[a-z][a-z0-9-]{2,40}", blob)}


def _get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_remote(base_url: str, cache_dir: Path) -> list[str]:
    """Return preset file names (without directory) from the Gitea tree API."""
    entries: list[str] = []
    page = 1
    while True:
        url = (f"{base_url}/api/v1/repos/"
               f"{base_url.rstrip('/').split('/')[-2]}/{base_url.rstrip('/').split('/')[-1]}"
               f"/git/trees/main?recursive=1&per_page=1000&page={page}")
        data = json.loads(_get(url))
        for item in data.get("tree") or []:
            path = item.get("path", "")
            if item.get("type") == "blob" and path.startswith(PRESET_DIR_IN_REPO + "/") and path.endswith(".txt"):
                entries.append(Path(path).name)
        if not data.get("truncated"):
            break
        page += 1
    return sorted(set(entries))


def fetch_presets(base_url: str, cache_dir: Path, names: list[str]) -> list[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for name in names:
        dest = cache_dir / name
        if not dest.exists() or dest.stat().st_size == 0:
            url = f"{base_url}/raw/branch/main/{PRESET_DIR_IN_REPO}/{urllib.parse.quote(name)}"
            for attempt in range(3):
                try:
                    dest.write_bytes(_get(url))
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  FAIL {name}: {e}", file=sys.stderr)
                    time.sleep(1.0)
        if dest.exists():
            out.append(dest)
    return out


def load_base_argv(path: Path) -> set[tuple[str, ...]]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {tuple(item.get("raw_args") or ()) for item in data if isinstance(item, dict)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", help="folder with preset .txt files")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Forgejo base URL")
    ap.add_argument("--cache-dir", default=str(ROOT / ".presets-cache"))
    ap.add_argument("--out", default=str(paths.presets_path()))
    args = ap.parse_args()

    flags = supported_flags()
    if not flags:
        print("warning: bin/winws.exe not found — flag validation disabled", file=sys.stderr)

    if args.dir:
        files = sorted(Path(args.dir).glob("*.txt"))
    else:
        try:
            names = list_remote(args.base_url, Path(args.cache_dir))
        except Exception as e:
            print(
                f"could not list remote presets: {e}\n"
                f"Download src/presets/builtin/winws1 once and rerun with --dir <folder>.",
                file=sys.stderr,
            )
            return 1
        print(f"remote presets: {len(names)}")
        files = fetch_presets(args.base_url, Path(args.cache_dir), names)

    base_argv = load_base_argv(paths.strategies_path())
    seen: set[tuple[str, ...]] = set(base_argv)
    result = []
    skipped_dup = skipped_bad = 0
    substitutions = 0

    for path in files:
        preset = parse_preset(
            path.read_text(encoding="utf-8", errors="replace"),
            path.stem,
            bin_dir=paths.bin_dir(),
            lists_dir=paths.lists_dir(),
            supported_flags=flags or None,
        )
        if preset is None:
            skipped_bad += 1
            continue
        key = tuple(preset.raw_args)
        if key in seen:
            skipped_dup += 1
            continue
        seen.add(key)
        substitutions += len(preset.substitutions)
        result.append(preset.to_dict())

    out = Path(args.out)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    groups: dict[str, int] = {}
    for item in result:
        groups[item["group"]] = groups.get(item["group"], 0) + 1
    print(f"converted {len(result)} presets -> {out}")
    print(f"skipped: {skipped_dup} duplicates, {skipped_bad} unusable")
    print(f"asset substitutions: {substitutions}")
    print("groups:", ", ".join(f"{k}={v}" for k, v in sorted(groups.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
