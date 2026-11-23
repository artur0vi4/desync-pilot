"""List of candidate DNS servers with both plain (53) and DoH endpoints."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DNSServer:
    name: str
    plain: str        # 1.1.1.1
    doh: str          # https://cloudflare-dns.com/dns-query
    country: str = ""


SERVERS: list[DNSServer] = [
    DNSServer("Cloudflare", "1.1.1.1", "https://cloudflare-dns.com/dns-query", "global"),
    DNSServer("Cloudflare Secondary", "1.0.0.1", "https://cloudflare-dns.com/dns-query", "global"),
    DNSServer("Google", "8.8.8.8", "https://dns.google/dns-query", "global"),
    DNSServer("Google Secondary", "8.8.4.4", "https://dns.google/dns-query", "global"),
    DNSServer("Quad9", "9.9.9.9", "https://dns.quad9.net/dns-query", "ch"),
    DNSServer("Yandex", "77.88.8.8", "https://common.dot.dns.yandex.net/dns-query", "ru"),
    DNSServer("Yandex Safe", "77.88.8.1", "https://family.dot.dns.yandex.net/dns-query", "ru"),
    DNSServer("AdGuard", "94.140.14.14", "https://dns.adguard-dns.com/dns-query", "cy"),
    DNSServer("AdGuard Family", "94.140.15.15", "https://dns-family.adguard-dns.com/dns-query", "cy"),
    DNSServer("OpenDNS", "208.67.222.222", "https://doh.opendns.com/dns-query", "us"),
    DNSServer("NextDNS", "45.90.28.0", "https://dns.nextdns.io/dns-query", "us"),
    DNSServer("ControlD", "76.76.2.0", "https://freedns.controld.com/p0/dns-query", "ca"),
    DNSServer("Mullvad", "194.242.2.2", "https://doh.mullvad.net/dns-query", "se"),
]


def find(name: str) -> DNSServer | None:
    for s in SERVERS:
        if s.name == name:
            return s
    return None
