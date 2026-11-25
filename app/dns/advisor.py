"""DNS advisor: background speed-test of public resolvers + one recommendation.

The app used to manage DNS itself (local DoH proxy with a picker in the
status tab), which cluttered the UI. Now DNS is purely informational: once
per session (cached for 6 h) we measure the known resolvers concurrently and
show the fastest one as a hint. Nothing is switched automatically — the user
decides what to do with the recommendation.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .. import paths
from ..logger import get as _log
from .resolver import test_servers
from .servers import DNSServer, SERVERS

_log = _log()

CACHE_TTL_SECONDS = 6 * 3600.0


@dataclass
class DNSRecommendation:
    name: str
    rtt_ms: float
    at: float


class DNSAdvisor:
    """Rank the known resolvers once, cache the winner, expose it to the UI."""

    def __init__(self, on_update: Optional[Callable[[DNSRecommendation], None]] = None,
                 servers: Optional[list[DNSServer]] = None):
        self._on_update = on_update
        self._servers = servers if servers is not None else SERVERS
        self._rec: Optional[DNSRecommendation] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def recommendation(self) -> Optional[DNSRecommendation]:
        with self._lock:
            return self._rec

    def load_cached(self) -> Optional[DNSRecommendation]:
        """Read the cached recommendation; None if missing or stale."""
        try:
            data = json.loads(paths.dns_recommend_file().read_text(encoding="utf-8"))
            rec = DNSRecommendation(str(data["name"]), float(data["rtt_ms"]), float(data["at"]))
        except Exception:
            return None
        if time.time() - rec.at > CACHE_TTL_SECONDS:
            return None
        with self._lock:
            self._rec = rec
        return rec

    def start(self) -> None:
        """Show the cached recommendation immediately and re-measure in the
        background when the cache is stale or missing."""
        cached = self.load_cached()
        if cached is not None:
            self._notify(cached)
            self._thread = threading.Thread(target=self._run, name="dns-advisor", daemon=True)
            self._thread.start()
        else:
            self._thread = threading.Thread(target=self._run, name="dns-advisor", daemon=True)
            self._thread.start()

    def refresh(self) -> None:
        """Force a re-measure (ignores the cache)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="dns-advisor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            results = test_servers(list(self._servers))
        except Exception as e:
            _log.warning("dns advisor failed: %s", e)
            return
        best = results[0] if results else None
        if best is None or not best.ok:
            _log.warning("dns advisor: no resolver responded — no recommendation")
            return
        rec = DNSRecommendation(best.server.name, best.rtt_ms, time.time())
        with self._lock:
            self._rec = rec
        self._save(rec)
        _log.info("dns advisor: recommend %s (%.0f ms)", rec.name, rec.rtt_ms)
        self._notify(rec)

    def _notify(self, rec: DNSRecommendation) -> None:
        if self._on_update:
            try:
                self._on_update(rec)
            except Exception:
                pass

    @staticmethod
    def _save(rec: DNSRecommendation) -> None:
        try:
            path = paths.dns_recommend_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"name": rec.name, "rtt_ms": rec.rtt_ms, "at": rec.at}),
                encoding="utf-8",
            )
        except Exception as e:
            _log.warning("dns advisor cache write failed: %s", e)
