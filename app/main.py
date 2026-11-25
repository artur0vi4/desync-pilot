"""Entry point for Desync Pilot.

Run from source:
    python -m app.main            # GUI
    python -m app.main --minimized

PyInstaller bundle is built as one-dir; the bundled exe simply runs this.
"""
from __future__ import annotations

import os
import sys
import traceback

MUTEX_NAME = "Local\\DesyncPilot.SingleInstance"
_mutex_handle = None  # keep the handle alive for the process lifetime


def _bootstrap_logging() -> None:
    from app import logger
    logger.start()
    log = logger.get()
    from app import __version__
    log.info("===== Desync Pilot %s start =====", __version__)
    log.info("python %s on %s", sys.version.split()[0], sys.platform)
    log.info("is_frozen=%s argv=%s", getattr(sys, "frozen", False), sys.argv[1:])


def _is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _message_box(text: str, title: str = "Desync Pilot", flags: int = 0x40) -> None:
    """Native message box — usable before QApplication exists."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def _instance_already_running() -> bool:
    """True if another copy already owns the single-instance mutex.

    Used before elevation: the elevated child will create the mutex itself,
    so the non-elevated parent must not hold it.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _single_instance() -> bool:
    """False if another copy is already running (per Windows session)."""
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _escape_arg(arg: str) -> str:
    """Escape one argument for a Windows command line.

    ShellExecuteW re-splits lpParameters with the usual CRT rules, so an
    argument containing spaces or quotes must be wrapped in quotes with its
    inner quotes escaped. The naive `" ".join(f'"{p}"' for p in params)`
    corrupts any argument that itself contains a quote character.
    """
    if not arg:
        return '""'
    if " " not in arg and '"' not in arg:
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def _relaunch_as_admin(argv: list[str]) -> int:
    """On Windows: relaunch self elevated via ShellExecuteW runas verb.

    `argv` is the full list of original CLI args to forward (including
    --minimized so the elevated process honours it).
    """
    if os.name != "nt":
        return 1
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            # For a frozen PyInstaller build, sys.executable IS the .exe
            exe, params = sys.executable, list(argv)
        else:
            # Dev mode: `python -m app.main` — elevating python.exe without
            # the -m flag would just open a bare interpreter.
            exe, params = sys.executable, ["-m", "app.main", *argv]
        cmdline = " ".join(_escape_arg(p) for p in params)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, cmdline, os.getcwd(), 1)
        if int(rc) <= 32:
            # 5 = access denied, i.e. the user dismissed the UAC prompt
            log = __import__("app.logger", fromlist=["get"]).get()
            log.warning("elevation failed (ShellExecuteW rc=%s)", rc)
            return 1
        return 0
    except Exception as e:
        print(f"elevation failed: {e}", file=sys.stderr)
        return 1


def main() -> int:
    minimized = "--minimized" in sys.argv
    argv = [a for a in sys.argv[1:]]

    _bootstrap_logging()
    log = __import__("app.logger", fromlist=["get"]).get()

    # winws.exe needs admin (WinDivert driver). On Windows, escalate.
    if os.name == "nt" and not _is_admin():
        if _instance_already_running():
            _message_box("Desync Pilot уже запущен.\nИконка программы — в системном трее.")
            return 0
        log.warning("not admin — requesting elevation")
        if _relaunch_as_admin(argv) == 0:
            return 0  # elevated copy is starting
        _message_box(
            "Без прав администратора обход не сможет перехватывать трафик.\n"
            "Запустите программу ещё раз и подтвердите запрос UAC.",
            flags=0x30,  # warning
        )
        return 1

    if not _single_instance():
        log.info("another instance is already running — exiting")
        _message_box("Desync Pilot уже запущен.\nИконка программы — в системном трее.")
        return 0

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QFont
    from app.config import load as load_config
    from app import i18n
    cfg = load_config()
    i18n.set_lang(cfg.language)

    app = QApplication(sys.argv)
    app.setApplicationName("Desync Pilot")
    app.setQuitOnLastWindowClosed(False)  # tray keeps the app alive
    app.setFont(QFont("Segoe UI", 9))

    from app.controller import AppController
    from app.ui.main_window import MainWindow
    ctrl = AppController()
    win = MainWindow(ctrl)
    if not (minimized or cfg.start_minimized):
        win.show()

    # initialize the controller (loads strategies, may autostart)
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(100, ctrl.initialize)

    try:
        return app.exec()
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.error("fatal: %s", traceback.format_exc())
        return 1
    finally:
        try:
            ctrl.shutdown()
        except Exception:
            log.error("shutdown error: %s", traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main())
