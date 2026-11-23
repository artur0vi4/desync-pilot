"""DoH resolver + speed test for picking the fastest DNS server.

We keep it pure-Python (no external dnsproxy binary dependency) so that
the app works out of the box even if dnsproxy.exe is not bundled.
"""
from __future__ import annotations

import socket
import ssl
import struct
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .servers import SERVERS, DNSServer
from ..logger import get as _log

_log = _log()

# Creating a default SSL context reloads the CA store (~10-50 ms). One context
# for the whole process is enough — this used to be built per DNS query.
SSL_CONTEXT = ssl.create_default_context()


@dataclass
class DNSResult:
    server: DNSServer
    rtt_ms: float
    ok: bool


def _build_query(name: str, qtype: int = 1) -> bytes:
    """Build a DNS wire-format query (header + question)."""
    header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    q = b""
    for label in name.rstrip(".").split("."):
        q += bytes([len(label)]) + label.encode()
    q += b"\x00"
    q += struct.pack(">HH", qtype, 1)
    return header + q


def _doh_resolve(server: DNSServer, name: str, timeout: float = 3.0) -> tuple[bool, float]:
    """Do a single DoH query, return (ok, rtt_ms)."""
    q = _build_query(name)
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            server.doh,
            data=q,
            headers={"content-type": "application/dns-message", "accept": "application/dns-message"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
            data = r.read()
        ok = len(data) >= 12 and data[:2] == q[:2]
        return ok, (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        _log.debug("doh resolve %s via %s failed: %s", name, server.name, e)
        return False, (time.perf_counter() - t0) * 1000.0


def _tcp53_resolve(server_ip: str, name: str, timeout: float = 2.0) -> tuple[bool, float]:
    """Plain TCP DNS query (with length prefix) as a fallback test."""
    q = _build_query(name)
    t0 = time.perf_counter()
    try:
        with socket.create_connection((server_ip, 53), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(struct.pack(">H", len(q)) + q)
            resp = s.recv(4096)
        ok = len(resp) >= 14
        return ok, (time.perf_counter() - t0) * 1000.0
    except Exception:
        return False, (time.perf_counter() - t0) * 1000.0


def test_server(server: DNSServer, name: str = "www.youtube.com") -> DNSResult:
    """Try DoH first, then plain TCP 53. Return the faster of the two."""
    ok, rtt = _doh_resolve(server, name)
    if ok:
        return DNSResult(server, rtt, True)
    ok2, rtt2 = _tcp53_resolve(server.plain, name)
    return DNSResult(server, rtt2 if ok2 else max(rtt, rtt2), ok2)


def test_servers(servers: list[DNSServer], timeout: float = 3.0, workers: int = 8) -> list[DNSResult]:
    """Test many servers concurrently (13 servers sequentially took ~25 s)."""
    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(servers))) as pool:
        results = list(pool.map(test_server, servers))
    results.sort(key=lambda r: (not r.ok, r.rtt_ms))
    return results


def pick_best(servers: list[DNSServer] | None = None) -> Optional[DNSResult]:
    results = test_servers(servers or SERVERS)
    best = results[0] if results else None
    _log.info(
        "DNS ranking: %s",
        ", ".join(f"{r.server.name}={r.rtt_ms:.0f}ms({'ok' if r.ok else 'FAIL'})" for r in results),
    )
    return best


def doh_query(name: str, server: DNSServer | None = None, qtype: int = 1) -> list[str]:
    """Resolve a name via DoH, return list of IPv4 strings."""
    server = server or SERVERS[0]
    q = _build_query(name, qtype)
    req = urllib.request.Request(
        server.doh,
        data=q,
        headers={"content-type": "application/dns-message", "accept": "application/dns-message"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5, context=SSL_CONTEXT) as r:
        data = r.read()
    return _parse_answers(data)


def _skip_name(msg: bytes, pos: int) -> int:
    """Skip a (possibly compressed) DNS name, return the next offset."""
    while pos < len(msg):
        length = msg[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:  # pointer, 2 bytes
            return pos + 2
        pos += length + 1
    return pos


def _parse_answers(msg: bytes) -> list[str]:
    """Extract A records from a DNS response."""
    if len(msg) < 12:
        return []
    ancount = struct.unpack(">H", msg[6:8])[0]
    qdcount = struct.unpack(">H", msg[4:6])[0]
    pos = 12
    for _ in range(qdcount):
        pos = _skip_name(msg, pos) + 4  # type, class
    ips: list[str] = []
    for _ in range(ancount):
        if pos >= len(msg):
            break
        pos = _skip_name(msg, pos)
        if pos + 10 > len(msg):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", msg[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4 and pos + 4 <= len(msg):
            ips.append(".".join(str(b) for b in msg[pos:pos + 4]))
        pos += rdlen
    return ips
