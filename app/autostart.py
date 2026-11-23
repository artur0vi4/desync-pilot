"""Run-at-login registration (HKCU\\...\\Run).

We deliberately use the per-user Run key instead of a scheduled task or a
service: no admin rights are needed to (un)register, and Windows starts the
app on logon with the ``--minimized`` flag it already understands.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .logger import get as _log

_log = _log()

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DesyncPilot"
LEGACY_VALUE_NAME = "ZapretAutopilot"


def is_supported() -> bool:
    return os.name == "nt"


def command_line() -> str:
    """Command Windows should run at logon."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    # Dev mode: python -m app.main; the module path only resolves when the
    # working directory is the project root, so pin it explicitly.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'cmd /c cd /d "{root}" && "{sys.executable}" -m app.main --minimized'


def is_enabled() -> bool:
    if not is_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _type = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as e:
        _log.debug("autostart read failed: %s", e)
        return False


def set_enabled(enabled: bool) -> bool:
    """Create or delete the Run entry. Returns True when the state matches."""
    if not is_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_line())
                _log.info("autostart enabled: %s", command_line())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                    _log.info("autostart disabled")
                except FileNotFoundError:
                    pass
            # The product was called Zapret Autopilot before 1.3.0: drop the
            # stale entry, otherwise Windows starts a copy that no longer exists.
            try:
                winreg.DeleteValue(key, LEGACY_VALUE_NAME)
                _log.info("removed legacy autostart entry")
            except FileNotFoundError:
                pass
        return True
    except OSError as e:
        _log.warning("autostart update failed: %s", e)
        return False


def sync(enabled: bool, current: Optional[bool] = None) -> None:
    """Make the registry match the config, touching it only when needed."""
    if not is_supported():
        return
    current = is_enabled() if current is None else current
    if bool(current) != bool(enabled):
        set_enabled(enabled)
    elif enabled:
        # Refresh the path: the exe may have been moved since last launch.
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, _type = winreg.QueryValueEx(key, VALUE_NAME)
        except OSError:
            value = ""
        if value != command_line():
            set_enabled(True)
