"""Toast notifications — Windows 10/11 toast via PowerShell, fallback to
console log on other platforms."""
from __future__ import annotations

import os
import platform
import subprocess
import threading

from .logger import get as _log

_log = _log()

# PowerShell variables and environment variables are different things: the
# script must read $env:ZA_TITLE, not $title. Passing the strings through the
# environment also keeps them out of the command line, so titles like
# "Стратегия O'Brien" cannot break the script.
_TOAST_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
    "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    # InnerText (not AppendChild on a live NodeList) — the latter silently
    # drops the body text of ToastText02.
    "$n=$t.GetElementsByTagName('text');"
    "$n.Item(0).InnerText=$env:ZA_TITLE;"
    "$n.Item(1).InnerText=$env:ZA_BODY;"
    "$toast=[Windows.UI.Notifications.ToastNotification]::new($t);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Desync Pilot').Show($toast)"
)


def _powershell_exe() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.exists(candidate):
        return candidate
    return "powershell"


def _windows_toast(title: str, body: str) -> None:
    """Show a Windows toast without extra dependencies."""
    try:
        env = dict(os.environ)
        env["ZA_TITLE"] = title
        env["ZA_BODY"] = body
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST_PS],
            env=env,
            timeout=10,
            check=False,
            creationflags=creationflags,
        )
    except Exception as e:
        _log.debug("toast failed: %s", e)


def notify(title: str, body: str, enabled: bool = True) -> None:
    if not enabled:
        _log.info("toast (suppressed): %s — %s", title, body)
        return
    threading.Thread(target=_dispatch, args=(title, body), daemon=True).start()


def _dispatch(title: str, body: str) -> None:
    if platform.system() == "Windows":
        _windows_toast(title, body)
    else:
        _log.info("toast: %s — %s", title, body)
