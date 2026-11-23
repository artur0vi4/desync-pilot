"""High-level DNS manager: picks the best DoH server and runs the local proxy."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .servers import SERVERS, DNSServer, find
from .resolver import test_server, test_servers, DNSResult
from .proxy import LocalDoHProxy
from ..logger import get as _log

_log = _log()


@dataclass
class DNSChoice:
    name: str
    plain: str
    doh: str
    rtt_ms: float


class DNSManager:
    def __init__(self, mode: str = "hybrid", listen_port: int = 5354, preferred: str = "",
                 custom_url: str = ""):
        self._mode = mode
        self._listen_port = listen_port
        self._preferred = preferred
        self._custom_url = custom_url
        self._proxy: Optional[LocalDoHProxy] = None
        self._current: Optional[DNSChoice] = None
        self._lock = threading.RLock()
        self._on_change: list[Callable[[DNSChoice], None]] = []

    # ---------- configuration ----------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def listen_port(self) -> int:
        return self._listen_port

    def configure(self, mode: Optional[str] = None, listen_port: Optional[int] = None,
                  preferred: Optional[str] = None, custom_url: Optional[str] = None) -> bool:
        """Apply new settings. Returns True when a restart is required."""
        with self._lock:
            restart = False
            if mode is not None and mode != self._mode:
                self._mode = mode
                restart = True
            if listen_port is not None and listen_port != self._listen_port:
                self._listen_port = listen_port
                restart = True
            if preferred is not None and preferred != self._preferred:
                self._preferred = preferred
                restart = True
            if custom_url is not None and custom_url != self._custom_url:
                self._custom_url = custom_url
                restart = True
            return restart

    def _custom_server(self) -> Optional[DNSServer]:
        url = (self._custom_url or "").strip()
        if not url.lower().startswith("https://"):
            return None
        return DNSServer("Custom", "", url, "custom")

    def subscribe(self, cb: Callable[[DNSChoice], None]) -> None:
        self._on_change.append(cb)

    def _emit(self) -> None:
        if self._current:
            for cb in list(self._on_change):
                try:
                    cb(self._current)
                except Exception:
                    pass

    @property
    def current(self) -> Optional[DNSChoice]:
        return self._current

    @property
    def is_running(self) -> bool:
        return self._proxy is not None

    # ---------- selection ----------

    def pick_best(self) -> Optional[DNSChoice]:
        """Speed-test the servers and pick the best (concurrently, ~2-4 s)."""
        custom = self._custom_server()
        first = custom or (find(self._preferred) if self._preferred else None)
        if first is not None:
            r = test_server(first)
            if r.ok:
                self._current = _to_choice(r)
                _log.info("DNS: %s ok (%.0fms)", first.name, r.rtt_ms)
                self._emit()
                return self._current
        candidates = ([custom] if custom else []) + list(SERVERS)
        results = test_servers(candidates)
        _log.info(
            "DNS ranking: %s",
            ", ".join(f"{r.server.name}={r.rtt_ms:.0f}ms({'ok' if r.ok else 'FAIL'})" for r in results),
        )
        for r in results:
            if r.ok:
                self._current = _to_choice(r)
                _log.info("DNS: picked %s (%.0fms)", r.server.name, r.rtt_ms)
                self._emit()
                return self._current
        _log.warning("DNS: no server responded; using Cloudflare as default")
        s = SERVERS[0]
        self._current = DNSChoice(s.name, s.plain, s.doh, 9999.0)
        self._emit()
        return self._current

    # ---------- lifecycle ----------

    def start(self) -> bool:
        """Start the proxy immediately and rank servers in the background.

        Blocking here used to freeze the caller (and the UI) for ~25 s while
        13 DoH servers were probed one by one. The proxy now comes up with the
        preferred/known server and switches to the fastest one when the
        ranking finishes.
        """
        if self._mode == "system":
            _log.info("DNS mode=system; skipping local proxy")
            threading.Thread(target=self._rank, daemon=True).start()
            return True
        srv = self._custom_server()
        if srv is None and self._preferred:
            srv = find(self._preferred)
        if srv is None and self._current:
            srv = find(self._current.name)
        self._proxy = LocalDoHProxy("127.0.0.1", self._listen_port, srv or SERVERS[0])
        ok = self._proxy.start()
        if not ok:
            self._proxy = None
            return False
        _log.info("DNS proxy active on 127.0.0.1:%d", self._listen_port)
        threading.Thread(target=self._rank, daemon=True).start()
        return True

    def _rank(self) -> None:
        """Pick the fastest DoH server, then point the running proxy at it."""
        proxy = self._proxy
        best = self.pick_best()
        if best is None or proxy is None:
            return
        if self._proxy is not proxy:
            return  # stopped/replaced while we were probing
        srv = self._custom_server() if best.name == "Custom" else find(best.name)
        if srv:
            proxy.set_upstream(srv)

    def stop(self) -> None:
        with self._lock:
            proxy, self._proxy = self._proxy, None
        if proxy:
            proxy.stop()

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def switch_upstream(self, server_name: str) -> bool:
        srv = self._custom_server() if server_name == "Custom" else find(server_name)
        if not srv:
            return False
        result = test_server(srv)
        rtt = result.rtt_ms if result.ok else (self._current.rtt_ms if self._current else 0.0)
        if self._proxy:
            self._proxy.set_upstream(srv)
        self._current = DNSChoice(srv.name, srv.plain, srv.doh, rtt)
        _log.info("DNS: manual switch to %s (%.0fms, ok=%s)", srv.name, rtt, result.ok)
        self._emit()
        return True

    @property
    def stats(self) -> dict:
        if self._proxy:
            return self._proxy.stats
        return {"upstream": self._current.name if self._current else "-", "queries": 0, "failures": 0}


def _to_choice(r: DNSResult) -> DNSChoice:
    return DNSChoice(r.server.name, r.server.plain, r.server.doh, r.rtt_ms)
