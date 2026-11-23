"""Path resolution.

Works both in dev (running from source) and in a PyInstaller-frozen build.
All paths are resolved relative to the bundle root, never to the CWD.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "desyncpilot"
LEGACY_DIR_NAME = "zapret-autopilot"  # pre-1.3 data dir, migrated on first run


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """Directory that contains bin/, lists/, strategies.json."""
    if is_frozen():
        # One-file: _MEIPASS is the temp extraction dir.
        # One-dir: exe lives next to the data.
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        exe_dir = Path(sys.executable).resolve().parent
        # Prefer the exe directory (one-dir build keeps data next to exe).
        if (exe_dir / "bin").is_dir():
            return exe_dir
        if (meipass / "bin").is_dir():
            return meipass
        return meipass
    # Dev: project root is two levels up from this file (app/paths.py).
    return Path(__file__).resolve().parent.parent


def bin_dir() -> Path:
    return bundle_root() / "bin"


def lists_dir() -> Path:
    return bundle_root() / "lists"


def strategies_path() -> Path:
    return bundle_root() / "strategies.json"


def presets_path() -> Path:
    """Community preset catalog converted from zapret-gui (winws1)."""
    return bundle_root() / "strategies_gui.json"


def targets_path() -> Path:
    return bundle_root() / "utils" / "targets.txt"


def winws_exe() -> Path:
    return bin_dir() / "winws.exe"


def config_path() -> Path:
    """User-editable config — kept next to the executable if frozen,
    otherwise in the project root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent / "config.ini"
    return bundle_root() / "config.ini"


def user_data_dir() -> Path:
    """Where logs and cached state live."""
    base = Path(os.environ.get("APPDATA") or bundle_root())
    d = base / APP_DIR_NAME
    if not d.exists():
        legacy = base / LEGACY_DIR_NAME
        if legacy.is_dir():
            # Keep the user's config/cache when the product was renamed.
            try:
                legacy.rename(d)
            except OSError:
                try:
                    shutil.copytree(legacy, d, dirs_exist_ok=True)
                except OSError:
                    pass
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    d = user_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    return log_dir() / "desyncpilot.log"


def state_file() -> Path:
    """Cached auto-test results and the currently chosen strategy."""
    return user_data_dir() / "state.json"


def updated_strategies_path() -> Path:
    """Where strategies downloaded by the updater are stored."""
    return user_data_dir() / "strategies_remote.json"
