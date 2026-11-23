"""Local UDP DNS proxy that forwards queries to a DoH upstream.

Listens on 127.0.0.1:<port> (default 5354) so the user can set their
system DNS to 127.0.0.1 (or use the app as the system resolver via netsh).
This is a simple, dependency-free replacement for dnsproxy.exe.

Queries are handled by a small thread pool (one thread per UDP packet used to
mean hundreds of live threads during a page load) and successful answers are
cached for a short TTL so browsing does not pay the DoH round trip twice.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .servers import SERVERS, DNSServer
from .resolver import SSL_CONTEXT
from ..logger import get as _log

_log = _log()

CACHE_TTL = 60.0        # seconds; short on purpose — no TTL rewriting games
CACHE_MAX = 512
WORKERS = 16


def _question_key(query: bytes) -> Optional[tuple[bytes, int]]:
    """Return (lowercase qname, qtype) or None if the packet is unparsable."""
    if len(query) < 17:
        return None
    pos, labels = 12, []
    while pos < len(query):
        length = query[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0:  # compression pointers are not valid in questions
            return None
        labels.append(query[pos + 1:pos + 1 + length])
        pos += 1 + length
    if pos + 4 > len(query):
        return None
    qtype = struct.unpack(">H", query[pos:pos + 2])[0]
    return b".".join(labels).lower(), qtype


class LocalDoHProxy:
    """UDP DNS server that forwards each query to a DoH upstream."""

    def __init__(self, listen_addr: str = "127.0.0.1", listen_port: int = 5354,
                 upstream: DNSServer | None = None):
        self._addr = listen_addr
        self._port = listen_port
        self._upstream = upstream or SERVERS[0]
        self._fallback_idx = 0
        self._upstream_lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._stop = threading.Event()
        self._cache: dict[tuple[bytes, int], tuple[float, bytes]] = {}
        self._cache_lock = threading.Lock()
        self._queries = 0
        self._failures = 0
        self._cache_hits = 0

    # ---------- state ----------

    @property
    def upstream(self) -> DNSServer:
        with self._upstream_lock:
            return self._upstream

    def set_upstream(self, server: DNSServer) -> None:
        with self._upstream_lock:
            old = self._upstream
            self._upstream = server
        if old.name != server.name:
            _log.info("DNS upstream switched %s -> %s", old.name, server.name)

    @property
    def listen_addr(self) -> str:
        return f"{self._addr}:{self._port}"

    @property
    def stats(self) -> dict:
        return {
            "upstream": self._upstream.name,
            "queries": self._queries,
            "failures": self._failures,
            "cache_hits": self._cache_hits,
            "cache_entries": len(self._cache),
        }

    # ---------- lifecycle ----------

    def start(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self._addr, self._port))
            self._sock.settimeout(1.0)
        except OSError as e:
            _log.error("cannot bind DNS proxy on %s:%d: %s", self._addr, self._port, e)
            self._sock = None
            return False
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="doh")
        self._thread = threading.Thread(target=self._serve, name="doh-proxy", daemon=True)
        self._thread.start()
        _log.info("DNS proxy listening on %s (upstream=%s)", self.listen_addr, self._upstream.name)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
        self._sock = None
        self._thread = None
        self._pool = None
        with self._cache_lock:
            self._cache.clear()
        _log.info("DNS proxy stopped")

    # ---------- serving ----------

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12 or self._pool is None:
                continue
            self._queries += 1
            try:
                self._pool.submit(self._handle, data, addr)
            except RuntimeError:
                break  # pool already shut down

    def _handle(self, query: bytes, addr) -> None:
        key = _question_key(query)
        if key is not None:
            cached = self._cache_get(key)
            if cached is not None:
                self._cache_hits += 1
                self._send(cached, addr, txid=query[:2])
                return

        upstream = self.upstream
        resp = self._doh_query(query, upstream)
        if resp is None:
            self._failures += 1
            _log.debug("DoH upstream %s failed, trying fallback", upstream.name)
            for fb in self._fallback_candidates(upstream):
                resp = self._doh_query(query, fb, timeout=2.5)
                if resp is not None:
                    self._fallback_idx += 1
                    break
        if resp is None:
            return  # drop: the client will retry against its next server
        if key is not None:
            self._cache_put(key, resp)
        self._send(resp, addr, txid=query[:2])

    def _fallback_candidates(self, primary: DNSServer) -> list[DNSServer]:
        others = [s for s in SERVERS if s.name != primary.name]
        if not others:
            return []
        n = self._fallback_idx % len(others)
        return [others[n], others[(n + 1) % len(others)]][:1]

    def _send(self, resp: bytes, addr, txid: bytes) -> None:
        if self._sock is None:
            return
        if len(resp) >= 2 and resp[:2] != txid:
            resp = txid + resp[2:]  # cached answers carry the old transaction id
        try:
            self._sock.sendto(resp, addr)
        except OSError:
            pass

    # ---------- cache ----------

    def _cache_get(self, key: tuple[bytes, int]) -> Optional[bytes]:
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            expires, payload = entry
            if expires <= now:
                self._cache.pop(key, None)
                return None
            return payload

    def _cache_put(self, key: tuple[bytes, int], resp: bytes) -> None:
        now = time.monotonic()
        with self._cache_lock:
            if len(self._cache) >= CACHE_MAX:
                # drop the oldest third instead of growing without bound
                for k, _v in sorted(self._cache.items(), key=lambda kv: kv[1][0])[: CACHE_MAX // 3]:
                    self._cache.pop(k, None)
            self._cache[key] = (now + CACHE_TTL, resp)

    # ---------- upstream ----------

    def _doh_query(self, query: bytes, server: DNSServer, timeout: float = 4.0) -> Optional[bytes]:
        req = urllib.request.Request(
            server.doh,
            data=query,
            headers={
                "content-type": "application/dns-message",
                "accept": "application/dns-message",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
                data = r.read()
            return data if len(data) >= 12 else None
        except Exception as e:
            _log.debug("doh query failed via %s: %s", server.name, e)
            return None
