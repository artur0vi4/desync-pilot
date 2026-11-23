"""Centralised logging with rotating files and an in-memory ring buffer
that the UI can read for the live log view."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Deque

from . import paths

_lock = threading.Lock()
_ring: Deque[str] = deque(maxlen=2000)
_started = False
_subscribers: list[Callable[[list[str]], None]] = []


def _format_ring_line(record: logging.LogRecord) -> str:
    """Format a record for the ring buffer.

    NOTE: we must not touch ``record.asctime`` — that attribute only exists
    after a ``logging.Formatter`` has formatted the record, so a handler that
    runs first (e.g. when the file handler could not be created) would crash
    with AttributeError and take ``log.info(...)`` down with it.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
    except Exception:  # pragma: no cover - defensive
        stamp = "-"
    try:
        msg = record.getMessage()
    except Exception:  # pragma: no cover - broken %-args
        msg = str(record.msg)
    level = record.levelname[:1] if record.levelname else "?"
    return f"{level} {stamp} | {msg}"


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        line = _format_ring_line(record)
        with _lock:
            _ring.append(line)
            subscribers = list(_subscribers)
        # Subscribers receive only the new lines; the UI appends them instead
        # of re-rendering the whole buffer on every record.
        for cb in subscribers:
            try:
                cb([line])
            except Exception:  # pragma: no cover - subscriber must not crash logging
                pass


def start(level: int = logging.INFO) -> logging.Logger:
    global _started
    log = logging.getLogger("desyncpilot")
    if _started:
        return log
    log.setLevel(level)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        fh = RotatingFileHandler(paths.log_file(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass  # filesystem not writable — still keep in-memory log

    ring = _RingHandler()
    ring.setFormatter(fmt)
    log.addHandler(ring)

    # console mirror in dev
    import sys
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    _started = True
    return log


def get() -> logging.Logger:
    return logging.getLogger("desyncpilot")


def snapshot() -> list[str]:
    with _lock:
        return list(_ring)


def subscribe(cb: Callable[[list[str]], None]) -> None:
    with _lock:
        if cb not in _subscribers:
            _subscribers.append(cb)


def unsubscribe(cb: Callable[[list[str]], None]) -> None:
    with _lock:
        try:
            _subscribers.remove(cb)
        except ValueError:
            pass


def export_to(target: Path, append: bool = False) -> Path:
    """Write the in-memory ring to `target` (truncating by default)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8") as f:
        f.write(f"===== export {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        for line in snapshot():
            f.write(line + "\n")
    return target
