"""Background health monitor.

Every N minutes it probes the active strategy's targets. If they degrade it
asks the controller for a fresh strategy sweep; if winws.exe died it asks for a
restart. Probing is parallel and short-circuits, so a dead network does not
freeze the monitor for a minute.
"""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from .tester import _probe, DEFAULT_TARGETS
from .logger import get as _log

_log = _log()

# Plain TCP endpoints used to tell "the DPI bypass broke" apart from
# "there is no internet at all". Raw TCP is not affected by DPI filtering of
# TLS handshakes, so it is a decent internet canary.
_INTERNET_CANARIES = (("1.1.1.1", 443), ("8.8.8.8", 53))


@dataclass
class HealthSnapshot:
    reachable: int
    total: int
    avg_rtt_ms: float
    at: float
    internet_ok: bool = True


class HealthMonitor:
    def __init__(self, probe_targets: list[tuple[str, float]] | None = None,
                 interval_minutes: int = 30,
                 on_degraded: Optional[Callable[[], None]] = None,
                 on_snapshot: Optional[Callable[[HealthSnapshot], None]] = None,
                 on_dead: Optional[Callable[[], None]] = None,
                 is_alive: Optional[Callable[[], bool]] = None,
                 alive_poll_s: float = 5.0):
        self._targets = probe_targets or DEFAULT_TARGETS
        self._interval = max(1, interval_minutes) * 60
        self._on_degraded = on_degraded
        self._on_snapshot = on_snapshot
        self._on_dead = on_dead
        self._is_alive = is_alive
        self._alive_poll = max(0.01, alive_poll_s)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = self._stop
        self._last: Optional[HealthSnapshot] = None
        self._consecutive_bad = 0

    @property
    def last(self) -> Optional[HealthSnapshot]:
        return self._last

    def start(self) -> None:
        # A fresh event per run: a monitor that was stopped without joining
        # must not be revived by the next start().
        ev = threading.Event()
        self._stop = ev
        self._stop_event = ev
        self._thread = threading.Thread(target=self._loop, args=(ev,), name="health", daemon=True)
        self._thread.start()
        _log.info("health monitor started (interval=%ds)", self._interval)

    def stop(self, wait: bool = True) -> None:
        self._stop_event.set()
        if self._thread and wait:
            self._thread.join(timeout=2.0)
        self._thread = None

    # ---------- checks ----------

    @staticmethod
    def _internet_ok(timeout: float = 1.5) -> bool:
        for host, port in _INTERNET_CANARIES:
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    return True
            except OSError:
                continue
        return False

    def check_once(self) -> HealthSnapshot:
        pairs = list(self._targets)
        results = []
        workers = min(4, max(1, len(pairs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_probe, url, 6.0) for url, _w in pairs]
            for fut in futures:
                try:
                    results.append(fut.result())
                except Exception:
                    results.append(None)
        ok = sum(1 for r in results if r is not None and r.ok)
        rtts = [r.rtt_ms for r in results if r is not None and r.ok]
        failed = [f"{r.url} ({r.error or 'unknown'})" for r in results if r is not None and not r.ok]
        snap = HealthSnapshot(
            reachable=ok,
            total=len(pairs),
            avg_rtt_ms=sum(rtts) / len(rtts) if rtts else 0.0,
            at=time.time(),
            internet_ok=self._internet_ok() if ok < len(pairs) else True,
        )
        self._last = snap
        if self._on_snapshot:
            try:
                self._on_snapshot(snap)
            except Exception:
                pass
        if failed:
            _log.warning("health snapshot: ok=%d/%d avg=%.0fms internet=%s FAILED: %s",
                         ok, len(pairs), snap.avg_rtt_ms, snap.internet_ok, "; ".join(failed))
        else:
            _log.info("health snapshot: ok=%d/%d avg=%.0fms", ok, len(pairs), snap.avg_rtt_ms)
        return snap

    def _loop(self, ev: threading.Event) -> None:
        while not ev.is_set():
            try:
                snap = self.check_once()
                ratio = snap.reachable / max(1, snap.total)
                # Zero reachable targets is only a strategy problem when plain
                # TCP still reaches the internet; otherwise the user's
                # connection is down and a re-test cannot help.
                if snap.reachable == 0:
                    degraded = snap.internet_ok
                    if not snap.internet_ok:
                        _log.warning("health: no internet at all — not auto-retesting")
                else:
                    degraded = ratio < 0.6 or snap.avg_rtt_ms > 3000.0
                if degraded:
                    self._consecutive_bad += 1
                    if self._consecutive_bad >= 2 and self._on_degraded:
                        _log.warning("health degraded x%d — requesting re-test", self._consecutive_bad)
                        try:
                            self._on_degraded()
                        except Exception:
                            _log.exception("on_degraded handler failed")
                        self._consecutive_bad = 0
                else:
                    self._consecutive_bad = 0
                if snap.reachable == 0:
                    _log.warning("health: 0 targets reachable — skipping auto-retest")
            except Exception as e:
                _log.warning("health check error: %s", e)

            # Wait for the next full check, but watch winws.exe every 5 s:
            # a crashed process should be restarted in seconds, not in half
            # an hour when the next network probe would happen.
            deadline = time.monotonic() + self._interval
            while not ev.is_set() and time.monotonic() < deadline:
                if self._is_alive is not None and not self._is_alive():
                    _log.warning("health: winws.exe is not running — requesting restart")
                    if self._on_dead:
                        try:
                            self._on_dead()
                        except Exception:
                            _log.exception("on_dead handler failed")
                    deadline = time.monotonic() + self._alive_poll * 2  # give the restart a chance
                ev.wait(self._alive_poll)
