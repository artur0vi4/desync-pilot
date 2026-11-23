"""Strategy loading and winws.exe execution.

A strategy is a JSON entry produced by strategy_parser.py — it has
sections (lists of winws.exe tokens, with `--new` as section separator),
and env var placeholders like %LISTS% / %BIN% / %GameFilterTCP% that we
substitute at runtime.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths
from .logger import get as _log
from .winws_args import clean_port_lists, is_path_flag, split_sections, strip_value_quotes

_log = _log()

REQUIRED_KEYS = ("id", "raw_args")


@dataclass
class Strategy:
    id: str
    file: str
    title: str
    group: str
    sections: list[list[str]]
    modes: list[str]
    raw_args: list[str] = field(default_factory=list)
    source: str = "upstream"   # upstream | zapret-gui
    note: str = ""

    @property
    def display_modes(self) -> str:
        return ",".join(sorted(set(self.modes))) if self.modes else "-"


def _read_strategy_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("strategies file is not a list")
    out = []
    for entry in data:
        if not isinstance(entry, dict) or any(k not in entry for k in REQUIRED_KEYS):
            continue
        out.append(entry)
    return out


def _norm_token(tok: str) -> str:
    t = (tok.replace("%BIN%", "bin/")
            .replace("%LISTS%", "lists/")
            .replace("%DATA%", "data/"))
    if "=" in t:
        flag, _, value = t.partition("=")
        if value.startswith(("bin/", "lists/", "data/")):
            value = value.lower()
        return f"{flag}={value}"
    return t


def _argv_key(strategy: "Strategy") -> tuple[str, ...]:
    return tuple(_norm_token(t) for t in strategy.raw_args if not t.startswith("--comment"))


# Which group is tried first by the quick scan.
GROUP_ORDER = {
    "base": 0,
    "alt": 1,
    "fake_tls_auto": 2,
    "simple_fake": 3,
    "community": 4,
    "game": 5,
    "experimental": 9,
}


def load_strategies() -> list[Strategy]:
    """Load bundled strategies.json (or the upstream remote copy) and the
    community preset catalog, dropping entries whose argv is already present.

    A corrupt/partial remote file must never leave the app without strategies,
    so any failure falls back to the bundled file.
    """
    data: Optional[list[dict]] = None
    remote = paths.updated_strategies_path()
    if remote.exists():
        try:
            data = _read_strategy_file(remote)
            _log.info("loaded %d strategies from remote update", len(data))
        except Exception as e:
            _log.warning("remote strategies unusable (%s) — falling back to bundle", e)
            data = None
    if not data:
        path = paths.strategies_path()
        if not path.exists():
            _log.error("strategies.json missing at %s", path)
            return []
        try:
            data = _read_strategy_file(path)
        except Exception as e:
            _log.error("strategies.json unreadable: %s", e)
            return []
        _log.info("loaded %d strategies from bundle", len(data))

    out: list[Strategy] = []
    for d in data:
        try:
            out.append(Strategy(**d))
        except TypeError as e:
            _log.warning("skipping malformed strategy %r: %s", d.get("id"), e)

    # Community catalog (converted zapret-gui winws1 presets).
    presets = paths.presets_path()
    extra = 0
    if presets.exists():
        try:
            preset_data = _read_strategy_file(presets)
        except Exception as e:
            _log.warning("preset catalog unreadable: %s", e)
            preset_data = []
        seen = {_argv_key(s) for s in out}
        for d in preset_data:
            try:
                s = Strategy(**d)
            except TypeError as e:
                _log.debug("skipping malformed preset %r: %s", d.get("id"), e)
                continue
            key = _argv_key(s)
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
            extra += 1
        _log.info("loaded %d community presets from catalog", extra)

    # Stable order: our base strategies first, then by group priority.
    out.sort(key=lambda s: (0 if s.source != "zapret-gui" else 1,
                            GROUP_ORDER.get(s.group, 8)))
    return out


def _expand_env(tokens: list[str], game_tcp: str = "", game_udp: str = "") -> list[str]:
    """Replace %BIN% / %LISTS% / %GameFilterTCP% / %GameFilterUDP% with
    real paths and values, then repair the resulting lists."""
    env = {
        "BIN": str(paths.bin_dir()) + os.sep,
        "LISTS": str(paths.lists_dir()) + os.sep,
        "DATA": str(paths.user_data_dir()) + os.sep,
        "GameFilterTCP": game_tcp,
        "GameFilterUDP": game_udp,
    }
    expanded: list[str] = []
    for tok in tokens:
        if tok == "--new":
            expanded.append(tok)
            continue
        new = tok
        for k, v in env.items():
            new = new.replace(f"%{k}%", v)
        expanded.append(new)
    # Relative asset paths from the community presets ("lists/x.txt",
    # "bin/x.bin") are resolved against the bundle, never against the CWD.
    for i, tok in enumerate(expanded):
        flag, eq, val = tok.partition("=")
        if not eq:
            continue
        if val.startswith("lists/"):
            expanded[i] = f"{flag}={paths.lists_dir()}{os.sep}{val[6:]}"
        elif val.startswith("bin/"):
            expanded[i] = f"{flag}={paths.bin_dir()}{os.sep}{val[4:]}"
    # Empty placeholders leave trailing commas ("80,443,") and quoted path
    # values; winws.exe rejects both.
    expanded = clean_port_lists(expanded)
    expanded = strip_value_quotes(expanded)
    return expanded


def build_argv(strategy: Strategy, game_tcp: str = "", game_udp: str = "") -> list[str]:
    """Turn a strategy's raw_args into the actual winws.exe argv.

    `--new` is preserved as a positional separator inside the same argv.
    Sections that are entirely dedicated to the game filter (i.e. start with
    an empty `--filter-tcp=` or `--filter-udp=` because GameFilter* is unset)
    are dropped.
    """
    expanded = _expand_env(strategy.raw_args, game_tcp, game_udp)
    cleaned: list[list[str]] = []
    for sec in split_sections(expanded):
        if not sec:
            continue
        first_filter = next(
            (t for t in sec if t.startswith(("--filter-tcp=", "--filter-udp=", "--filter-l7="))),
            None,
        )
        if first_filter and first_filter.split("=", 1)[1] == "":
            continue  # winws.exe cannot parse an empty filter list
        cleaned.append(sec)
    out = [str(paths.winws_exe())]
    for i, sec in enumerate(cleaned):
        if i > 0:
            out.append("--new")
        out.extend(sec)
    return out


def preflight_check(strategy: Strategy, game_tcp: str = "", game_udp: str = "") -> list[str]:
    """Return human-readable problems that would make winws.exe refuse to
    start this strategy, BEFORE we actually spawn it."""
    problems: list[str] = []
    exe = paths.winws_exe()
    if not exe.exists():
        problems.append(f"winws.exe not found at {exe}")
        return problems  # nothing else is meaningful without the binary
    argv = build_argv(strategy, game_tcp, game_udp)
    seen: set[str] = set()
    for tok in argv[1:]:
        if "=" not in tok:
            continue
        flag, _, val = tok.partition("=")
        if flag in ("--hostlist-auto", "--hostlist-auto-debug"):
            # winws.exe creates this file itself on the first run; requiring it
            # here would reject every preset that uses the auto hostlist.
            continue
        if not is_path_flag(flag, val) or not val:
            continue
        if val in seen:
            continue
        seen.add(val)
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        if not Path(val).exists():
            problems.append(f"{flag}: file not found: {val}")
    return problems


class StrategyRunner:
    """Owns a single winws.exe subprocess. Restart-safe and observable."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._pumps: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._current_strategy: Optional[Strategy] = None
        self._last_exit_code: Optional[int] = None

    @property
    def current(self) -> Optional[Strategy]:
        return self._current_strategy

    @property
    def last_exit_code(self) -> Optional[int]:
        return self._last_exit_code

    @property
    def is_running(self) -> bool:
        with self._lock:
            proc = self._proc
            return proc is not None and proc.poll() is None

    def start(self, strategy: Strategy, game_tcp: str = "", game_udp: str = "") -> bool:
        self.stop()
        if not paths.winws_exe().exists():
            _log.error("winws.exe not found at %s", paths.winws_exe())
            return False
        argv = build_argv(strategy, game_tcp, game_udp)
        _log.info("starting winws.exe strategy=%s args_count=%d", strategy.id, len(argv))
        _log.debug("winws argv: %s", " ".join(argv))
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                cwd=str(paths.bin_dir()),
            )
        except OSError as e:
            _log.error("failed to spawn winws.exe: %s", e)
            return False
        with self._lock:
            self._proc = proc
        self._current_strategy = strategy
        self._last_exit_code = None
        self._pumps = [
            threading.Thread(target=self._pump, args=(proc.stdout, "OUT"), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, "ERR"), daemon=True),
        ]
        for t in self._pumps:
            t.start()
        # Give it a moment to crash early (missing driver, bad args, ...)
        time.sleep(0.6)
        code = proc.poll()
        if code is not None:
            _log.error("winws.exe exited immediately with code %s", code)
            self._last_exit_code = code
            self._current_strategy = None
            with self._lock:
                self._proc = None
            return False
        return True

    def _pump(self, stream, tag: str) -> None:
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                s = self._decode(raw).rstrip()
                if s:
                    _log.info("winws[%s] %s", tag, s)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _decode(raw: bytes) -> str:
        # winws.exe is a cygwin binary; message encoding differs between
        # builds (utf-8, cp866, sometimes cp1251). Try in order, never raise.
        for enc in ("utf-8", "cp866", "cp1251"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                except Exception as e:
                    _log.warning("stop winws.exe: %s", e)
            self._last_exit_code = proc.poll()
        for t in self._pumps:
            t.join(timeout=1.0)
        self._pumps = []
        if proc is not None:
            self._current_strategy = None
            _log.info("winws.exe stopped")

    def restart(self, strategy: Strategy, game_tcp: str = "", game_udp: str = "") -> bool:
        self.stop()
        time.sleep(0.3)
        return self.start(strategy, game_tcp, game_udp)
