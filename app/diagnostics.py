"""Diagnostics: figure out *where* the connection is cut, not just *that* it is.

The strategy sweep answers "which of our presets works". This module answers the
question before it: is the address blocked, is the name (SNI) blocked, does the
provider hand out a fake DNS answer, is a stub page returned, or is it something
on this PC (no admin, driver missing, a second VPN, a proxy)?

Borrowed in spirit from the blockcheck approach of zapret-gui: probe the same
address with the real name and with a foreign name, and compare. Everything is
plain Python — no browser, no netlog parsing.
"""
from __future__ import annotations

import csv
import io
import os
import socket
import ssl
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, i18n, paths
from .logger import get as _log

_log = _log()

# statuses
OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

# where the block happens
KIND_OK = "ok"
KIND_DNS = "dns"
KIND_IP = "ip"
KIND_SNI = "sni"
KIND_TLS = "tls"
KIND_STUB = "stub"
KIND_CERT = "cert"
KIND_NETWORK = "network"
KIND_ERROR = "error"

TARGET_HOSTS = ("discord.com", "www.youtube.com")
CONTROL_ENDPOINTS = (("1.1.1.1", "cloudflare-dns.com"), ("8.8.8.8", "dns.google"))

CONFLICT_PROCESSES = {
    "goodbyedpi.exe": "GoodbyeDPI",
    "winws2.exe": "winws2 (zapret 2)",
    "spoofdpi.exe": "SpoofDPI",
    "byedpi.exe": "ByeDPI",
    "proxifier.exe": "Proxifier",
    "clash.exe": "Clash",
    "clash-verge.exe": "Clash Verge",
    "mihomo.exe": "Mihomo",
    "v2ray.exe": "v2ray",
    "xray.exe": "Xray",
    "nekoray.exe": "NekoRay",
    "wireguard.exe": "WireGuard",
    "openvpn.exe": "OpenVPN",
    "adguard.exe": "AdGuard",
    "adguardSvc.exe": "AdGuard",
}

BLOCK_PAGE_MARKERS = (
    "заблокирован", "ограничен", "роскомнадзор", "доступ к информационному",
    "blocked", "access denied", "restricted",
)

RECOMMENDATIONS = {
    KIND_SNI: ("general (ALT2)", "general", "general (ALT3)"),
    KIND_STUB: ("general (ALT3)", "general (ALT9)", "general (FAKE TLS AUTO)"),
    KIND_TLS: ("general (ALT5)", "general (ALT7)"),
}


@dataclass
class Check:
    key: str
    status: str
    title: str
    detail: str = ""
    hint: str = ""
    group: str = "local"      # local | host
    host: str = ""
    kind: str = ""            # block kind for host checks


@dataclass
class HostProbe:
    host: str
    kind: str = KIND_ERROR
    sys_ips: list[str] = field(default_factory=list)
    doh_ips: list[str] = field(default_factory=list)
    tls_ms: float = 0.0
    note: str = ""


@dataclass
class DiagnosticReport:
    checks: list[Check]
    verdict: str
    recommended: list[str]
    at: float
    duration_s: float = 0.0

    @property
    def problems(self) -> int:
        return sum(1 for c in self.checks if c.status in (WARN, FAIL))


# ---------------------------------------------------------------- helpers

def resolve_system(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def _doh_ips(host: str) -> list[str]:
    from .dns.resolver import doh_query
    from .dns.servers import SERVERS
    for server in SERVERS[:2]:
        try:
            ips = doh_query(host, server)
            if ips:
                return ips
        except Exception:
            continue
    return []


def _tls_probe(ip: str, sni: str, timeout: float = 5.0, verify: bool = False) -> tuple[str, float, str]:
    """Return (state, ms, detail).

    state: ok | cert | rejected (server spoke TLS and refused) | reset | timeout | tcp_fail
    """
    t0 = time.perf_counter()
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
    except socket.timeout:
        return "timeout", 0.0, "tcp timeout"
    except OSError as e:
        return "tcp_fail", 0.0, type(e).__name__
    try:
        if verify:
            # A bare SSLContext(PROTOCOL_TLS_CLIENT) has no CA store at all and
            # would report "unable to get local issuer certificate" for every
            # site, so build the context the documented way.
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with raw:
            with ctx.wrap_socket(raw, server_hostname=sni):
                pass
        return "ok", (time.perf_counter() - t0) * 1000.0, ""
    except ssl.SSLCertVerificationError as e:
        return "cert", (time.perf_counter() - t0) * 1000.0, str(e.verify_message or e)
    except ssl.SSLError as e:
        ms = (time.perf_counter() - t0) * 1000.0
        reason = (getattr(e, "reason", "") or "").upper()
        text = str(e).upper()
        # A server-side TLS alert means packets flow: the server answered and
        # refused (unknown SNI, handshake failure). Only a reset/EOF means the
        # connection was cut on the way.
        if "UNRECOGNIZED_NAME" in reason + text or "HANDSHAKE_FAILURE" in reason + text:
            return "rejected", ms, reason or "server alert"
        return "reset", ms, reason or "ssl error"
    except (ConnectionResetError, ConnectionAbortedError) as e:
        return "reset", (time.perf_counter() - t0) * 1000.0, type(e).__name__
    except socket.timeout:
        return "timeout", (time.perf_counter() - t0) * 1000.0, "timeout"
    except OSError as e:
        return "reset", (time.perf_counter() - t0) * 1000.0, type(e).__name__


def _http_probe(host: str, timeout: float = 4.0) -> tuple[int, str, str]:
    """Plain HTTP request: (status, location, body head)."""
    request = (
        f"GET / HTTP/1.1\r\nHost: {host}\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
        "Accept: text/html\r\nConnection: close\r\n\r\n"
    )
    try:
        with socket.create_connection((host, 80), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(request.encode("ascii", "ignore"))
            data = s.recv(4096)
    except OSError:
        return 0, "", ""
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").splitlines()
    status = 0
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    location = ""
    for line in lines[1:]:
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
            break
    return status, location, body.decode("utf-8", "replace")


def _stub_reason(host: str) -> str:
    status, location, body = _http_probe(host)
    if status in (403, 451):
        return f"HTTP {status}"
    if status in (301, 302, 303, 307, 308) and location:
        low = location.lower()
        if low.startswith("/"):
            return ""  # relative redirect on the same host is normal
        if host.lower() not in low:
            return i18n.t("diag.stub_redirect", location[:80])
    low = body.casefold()
    for marker in BLOCK_PAGE_MARKERS:
        if marker in low:
            return i18n.t("diag.stub_page", marker)
    return ""


def _control_ok() -> bool:
    for ip, sni in CONTROL_ENDPOINTS:
        state, _ms, _detail = _tls_probe(ip, sni, timeout=4.0)
        if state in ("ok", "rejected", "cert"):
            return True
    return False


# ---------------------------------------------------------------- host probe

def probe_host(host: str) -> HostProbe:
    probe = HostProbe(host=host)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_sys = pool.submit(resolve_system, host)
        f_doh = pool.submit(_doh_ips, host)
        try:
            probe.sys_ips = f_sys.result(timeout=8.0) or []
        except FuturesTimeout:
            probe.sys_ips = []
        try:
            probe.doh_ips = f_doh.result(timeout=8.0) or []
        except FuturesTimeout:
            probe.doh_ips = []

    if not probe.sys_ips:
        probe.kind = KIND_DNS if _control_ok() else KIND_NETWORK
        probe.note = i18n.t("diag.note_dns_empty")
        return probe

    # Try the real name against a few addresses in parallel.
    results: list[tuple[str, str, float, str]] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_tls_probe, ip, host): ip for ip in probe.sys_ips[:3]}
        for fut, ip in futures.items():
            try:
                state, ms, detail = fut.result(timeout=8.0)
            except Exception:
                state, ms, detail = "timeout", 0.0, "timeout"
            results.append((ip, state, ms, detail))

    best = min(results, key=lambda r: ("ok", "rejected", "cert", "reset", "timeout", "tcp_fail").index(r[1]))
    probe.tls_ms = best[2]

    states = {r[1] for r in results}
    if "ok" in states:
        ip = next(r[0] for r in results if r[1] == "ok")
        # Handshake succeeded; now check whether we talk to the real server.
        cert_state, _ms, _detail = _tls_probe(ip, host, verify=True)
        if cert_state == "cert":
            probe.kind = KIND_CERT
            probe.note = _detail
            return probe
        stub = _stub_reason(host)
        if stub:
            probe.kind = KIND_STUB
            probe.note = stub
            return probe
        probe.kind = KIND_OK
        return probe

    if states <= {"tcp_fail", "timeout"}:
        # Nothing answered on 443 at all.
        if not _control_ok():
            probe.kind = KIND_NETWORK
            probe.note = i18n.t("diag.note_no_internet")
            return probe
        for ip in probe.doh_ips[:2]:
            state, _ms, _detail = _tls_probe(ip, host, timeout=5.0)
            if state in ("ok", "rejected"):
                probe.kind = KIND_DNS
                probe.note = i18n.t("diag.note_dns_dead", ip)
                return probe
        probe.kind = KIND_IP
        probe.note = i18n.t("diag.note_ip", best[3] or best[1])
        return probe

    # TCP works, TLS does not: is it the name or the address?
    ip = best[0]
    foreign_state, _ms, _detail = _tls_probe(ip, "example.com", timeout=5.0)
    if foreign_state in ("ok", "rejected", "cert"):
        probe.kind = KIND_SNI
        probe.note = i18n.t("diag.note_sni", host)
        return probe
    probe.kind = KIND_TLS
    probe.note = i18n.t("diag.note_tls", ip)
    return probe


# ---------------------------------------------------------------- local checks

def _check_admin() -> Check:
    ok = True
    if os.name == "nt":
        try:
            import ctypes
            ok = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            ok = False
    if ok:
        return Check("local.admin", OK, i18n.t("diag.admin.ok"))
    return Check("local.admin", FAIL, i18n.t("diag.admin.fail"), hint=i18n.t("diag.admin.hint"))


def _check_files() -> Check:
    bin_dir = paths.bin_dir()
    needed = ("winws.exe", "WinDivert.dll", "WinDivert64.sys", "cygwin1.dll")
    missing = [n for n in needed if not (bin_dir / n).is_file() or (bin_dir / n).stat().st_size == 0]
    lists = paths.lists_dir()
    for name in ("list-general.txt", "ipset-all.txt"):
        if not (lists / name).is_file():
            missing.append(f"lists/{name}")
    if not missing:
        return Check("local.files", OK, i18n.t("diag.files.ok"))
    return Check("local.files", FAIL, i18n.t("diag.files.fail", ", ".join(missing)),
                 hint=i18n.t("diag.files.hint"))


def _check_driver() -> Check:
    if os.name != "nt":
        return Check("local.driver", INFO, i18n.t("diag.driver.na"))
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(["sc.exe", "query", "windivert"], capture_output=True, text=True,
                             errors="replace", timeout=8, creationflags=creationflags)
    except Exception as e:
        return Check("local.driver", INFO, i18n.t("diag.driver.unknown", str(e)))
    text = (out.stdout or "") + (out.stderr or "")
    if out.returncode != 0:
        return Check("local.driver", INFO, i18n.t("diag.driver.absent"), hint=i18n.t("diag.driver.hint"))
    if "RUNNING" in text.upper():
        return Check("local.driver", OK, i18n.t("diag.driver.running"))
    if "STOPPED" in text.upper():
        return Check("local.driver", WARN, i18n.t("diag.driver.stopped"), hint=i18n.t("diag.driver.hint"))
    last = text.strip().splitlines()[-1] if text.strip() else "?"
    return Check("local.driver", INFO, i18n.t("diag.driver.unknown", last))


def _process_names() -> list[str]:
    if os.name != "nt":
        return []
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             errors="replace", timeout=10, creationflags=creationflags)
    except Exception:
        return []
    names: list[str] = []
    for row in csv.reader(io.StringIO(out.stdout or "")):
        if row:
            names.append(row[0].strip().lower())
    return names


def _check_conflicts() -> Check:
    names = _process_names()
    if not names:
        return Check("local.conflicts", INFO, i18n.t("diag.conflicts.unknown"))
    found = sorted({label for exe, label in CONFLICT_PROCESSES.items() if exe.lower() in names})
    winws = sum(1 for n in names if n == "winws.exe")
    if winws > 1:
        found.append(f"winws.exe Г—{winws}")
    if not found:
        return Check("local.conflicts", OK, i18n.t("diag.conflicts.ok"))
    return Check("local.conflicts", WARN, i18n.t("diag.conflicts.fail", ", ".join(found)),
                 hint=i18n.t("diag.conflicts.hint"))


def _check_proxy() -> Check:
    if os.name != "nt":
        return Check("local.proxy", INFO, i18n.t("diag.proxy.na"))
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = 0
            server = ""
            pac = ""
            try:
                enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except OSError:
                pass
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
            except OSError:
                pass
            try:
                pac = str(winreg.QueryValueEx(key, "AutoConfigURL")[0])
            except OSError:
                pass
    except OSError as e:
        return Check("local.proxy", INFO, i18n.t("diag.proxy.unknown", str(e)))
    if enabled and server:
        return Check("local.proxy", WARN, i18n.t("diag.proxy.on", server), hint=i18n.t("diag.proxy.hint"))
    if pac:
        return Check("local.proxy", WARN, i18n.t("diag.proxy.pac", pac), hint=i18n.t("diag.proxy.hint"))
    return Check("local.proxy", OK, i18n.t("diag.proxy.off"))


def _check_hosts() -> Check:
    host_file = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    try:
        lines = host_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return Check("local.hosts", INFO, i18n.t("diag.hosts.unknown"))
    hits = [
        ln.strip() for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
        and any(h in ln.lower() for h in ("discord", "youtube", "youtu.be"))
    ]
    if hits:
        return Check("local.hosts", WARN, i18n.t("diag.hosts.found", hits[0][:100]),
                     hint=i18n.t("diag.hosts.hint"))
    return Check("local.hosts", OK, i18n.t("diag.hosts.ok"))


def _check_port(port: int) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", port))
        return Check("local.port", OK, i18n.t("diag.port.ok", port))
    except OSError as e:
        return Check("local.port", WARN, i18n.t("diag.port.busy", port, str(e)),
                     hint=i18n.t("diag.port.hint"))
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- report

_KIND_KEYS = {
    KIND_OK: "diag.kind.ok",
    KIND_DNS: "diag.kind.dns",
    KIND_IP: "diag.kind.ip",
    KIND_SNI: "diag.kind.sni",
    KIND_TLS: "diag.kind.tls",
    KIND_STUB: "diag.kind.stub",
    KIND_CERT: "diag.kind.cert",
    KIND_NETWORK: "diag.kind.network",
    KIND_ERROR: "diag.kind.error",
}

_KIND_TITLES = {
    KIND_OK: "diag.title.ok",
    KIND_DNS: "diag.title.dns",
    KIND_IP: "diag.title.ip",
    KIND_SNI: "diag.title.sni",
    KIND_TLS: "diag.title.tls",
    KIND_STUB: "diag.title.stub",
    KIND_CERT: "diag.title.cert",
    KIND_NETWORK: "diag.title.network",
    KIND_ERROR: "diag.title.error",
}

_KIND_HINTS = {
    KIND_DNS: "diag.hint.dns",
    KIND_IP: "diag.hint.ip",
    KIND_SNI: "diag.hint.sni",
    KIND_TLS: "diag.hint.tls",
    KIND_STUB: "diag.hint.stub",
    KIND_CERT: "diag.hint.cert",
    KIND_NETWORK: "diag.hint.network",
}


def _host_check(probe: HostProbe) -> Check:
    if probe.kind == KIND_OK:
        status = OK
    elif probe.kind in (KIND_NETWORK, KIND_IP, KIND_CERT):
        status = FAIL
    elif probe.kind in (KIND_ERROR,):
        status = INFO
    else:
        status = WARN
    return Check(
        key=f"host.{probe.host}",
        status=status,
        title=i18n.t(_KIND_TITLES.get(probe.kind, "diag.title.error"), probe.host),
        detail=probe.note or i18n.t(_KIND_KEYS.get(probe.kind, "diag.kind.error")),
        hint=i18n.t(_KIND_HINTS[probe.kind]) if probe.kind in _KIND_HINTS else "",
        group="host",
        host=probe.host,
        kind=probe.kind,
    )


def _verdict(probes: list[HostProbe]) -> tuple[str, list[str]]:
    kinds = [p.kind for p in probes if p.kind != KIND_OK]
    for kind in (KIND_NETWORK, KIND_DNS, KIND_IP, KIND_TLS, KIND_SNI, KIND_STUB, KIND_CERT):
        if kind in kinds:
            return kind, list(RECOMMENDATIONS.get(kind, ()))
    if probes and all(p.kind == KIND_OK for p in probes):
        return KIND_OK, []
    return KIND_ERROR, []


def run_all(strategy_ids: tuple[str, ...] = (), port: int = 5354) -> DiagnosticReport:
    t0 = time.perf_counter()
    checks: list[Check] = [
        _check_admin(),
        _check_files(),
        _check_driver(),
        _check_conflicts(),
        _check_proxy(),
        _check_hosts(),
        _check_port(port),
    ]

    probes: list[HostProbe] = []
    with ThreadPoolExecutor(max_workers=len(TARGET_HOSTS)) as pool:
        futures = [pool.submit(probe_host, host) for host in TARGET_HOSTS]
        for fut in futures:
            try:
                probes.append(fut.result())
            except Exception as e:
                _log.warning("host probe failed: %s", e)
                probes.append(HostProbe(host="?", kind=KIND_ERROR))

    checks.extend(_host_check(p) for p in probes)
    verdict, recommended = _verdict(probes)
    if strategy_ids:
        recommended = [s for s in recommended if s in strategy_ids] or recommended[:1]
    report = DiagnosticReport(
        checks=checks,
        verdict=verdict,
        recommended=recommended,
        at=time.time(),
        duration_s=time.perf_counter() - t0,
    )
    _log.info("diagnostics done in %.1fs: verdict=%s, problems=%d",
              report.duration_s, report.verdict, report.problems)
    for c in checks:
        _log.info("  [%s] %s — %s", c.status, c.title, c.detail)
    return report


def report_as_text(report: DiagnosticReport) -> str:
    lines = [
        f"Desync Pilot v{__version__} — {i18n.t('diag.report_title')}",
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
    ]
    for c in report.checks:
        lines.append(f"[{c.status.upper():4}] {c.title}")
        if c.detail:
            lines.append(f"       {c.detail}")
        if c.hint:
            lines.append(f"       в†’ {c.hint}")
    lines.append("")
    lines.append(f"{i18n.t('diag.verdict')}: {i18n.t('diag.verdict.' + report.verdict)}")
    if report.recommended:
        lines.append(f"{i18n.t('diag.recommended')}: {', '.join(report.recommended)}")
    return "\n".join(lines)
