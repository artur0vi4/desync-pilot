"""Strategy tester.

For each candidate strategy:
  1. Start winws.exe with that strategy's argv.
  2. Wait a brief settle period.
  3. Probe a canary subset of targets; abort early if nothing works at all.
  4. Probe the remaining targets (in parallel) and compute a weighted score.
  5. Stop winws.exe.

The caller receives a sorted list of StrategyResult. Every long step honours a
`cancel` event so the UI can abort a sweep without leaving winws.exe running.
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx

from . import paths
from .strategies import Strategy, StrategyRunner
from .logger import get as _log

_log = _log()


# Fallback when utils/targets.txt is missing. Order matters: the first four
# entries are the canaries and must cover different services.
DEFAULT_TARGETS: list[tuple[str, float]] = [
    ("https://discord.com", 1.0),
    ("https://www.youtube.com", 1.0),
    ("https://www.google.com", 0.5),
    ("https://www.cloudflare.com", 0.3),
    ("https://gateway.discord.gg", 1.2),
    ("https://cdn.discordapp.com", 1.0),
    ("https://updates.discord.com", 1.0),
    ("https://youtu.be", 0.8),
    ("https://i.ytimg.com", 0.6),
    ("https://redirector.googlevideo.com", 1.0),
    ("https://www.gstatic.com", 0.4),
    ("https://cdnjs.cloudflare.com", 1.0),
]

# A direct HTTPS request that must not inherit a system/user proxy: with a
# proxy configured in WinINET every probe would "succeed" against the proxy
# and the score would be meaningless. In httpx that is `trust_env=False`.
_HEADERS = {"User-Agent": "desync-pilot"}
_OK_HTTP_CODES = (200, 301, 302, 303, 307, 308, 401, 403, 404)

# Retries between failed attempts back off exponentially with jitter (see
# _probe_target): a CDN that just rate-limited us needs a cool-down, and
# identical retry intervals from several threads line up into new
# rate-limit windows instead of spreading out.
_RETRY_BASE_DELAY = 0.5  # seconds before the 2nd attempt
_RETRY_MAX_DELAY = 4.0   # cap for the exponential part; jitter adds up to +0.5

_CLIENT: Optional[httpx.Client] = None
_CLIENT_LOCK = threading.Lock()


def _get_client(timeout: float = 5.0) -> httpx.Client:
    """Shared httpx.Client — one connection pool for every probe thread.

    Created lazily (importing this module must not touch the network) and
    reused across strategies: keep-alive keeps the measured RTT close to the
    real round trip instead of paying TCP+TLS setup on every attempt.
    """
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                trust_env=False,  # never inherit a system/user proxy
            )
        return _CLIENT


CANARY_COUNT = 4


@dataclass
class TargetResult:
    url: str
    ok: bool
    rtt_ms: float
    error: str = ""
    weight: float = 1.0


@dataclass
class StrategyResult:
    strategy: Strategy
    score: float = 0.0
    targets: list[TargetResult] = field(default_factory=list)
    tested_at: float = 0.0
    error: str = ""

    @property
    def ok_targets(self) -> int:
        return sum(1 for t in self.targets if t.ok)

    @property
    def total_targets(self) -> int:
        return len(self.targets)

    @property
    def avg_rtt_ms(self) -> float:
        ok = [t.rtt_ms for t in self.targets if t.ok]
        return sum(ok) / len(ok) if ok else 0.0


def load_targets(path: Optional[Path] = None) -> list[tuple[str, float]]:
    """Read targets from utils/targets.txt (same file the upstream .ps1 uses).

    Unknown URLs get weight 1.0; URLs that are already in DEFAULT_TARGETS keep
    their tuned weight. PING: entries are skipped — they measure ICMP, not the
    DPI path, so they say nothing about a strategy.
    """
    path = path or paths.targets_path()
    try:
        if not path.exists():
            return list(DEFAULT_TARGETS)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return list(DEFAULT_TARGETS)

    known = {url: w for url, w in DEFAULT_TARGETS}
    out: list[tuple[str, float]] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        _key, _, val = line.partition("=")
        val = val.strip().strip(",").strip().strip('"').strip("'").strip()
        if not val.lower().startswith(("http://", "https://")):
            continue  # PING: entries and anything else
        if any(val == u for u, _w in out):
            continue
        out.append((val, known.get(val, 1.0)))
    return out or list(DEFAULT_TARGETS)


def _probe(url: str, timeout: float = 5.0) -> TargetResult:
    """HTTPS GET (small read) — measures full TCP+TLS+HTTP round trip."""
    t0 = time.perf_counter()
    try:
        client = _get_client(timeout)
        with client.stream("GET", url, headers=_HEADERS, timeout=timeout) as r:
            for _chunk in r.iter_bytes(1024):
                break  # one small chunk is enough to measure the round trip
            rtt = (time.perf_counter() - t0) * 1000.0
        if r.status_code in _OK_HTTP_CODES:
            # 4xx/5xx from our list still means the TLS path works (DPI didn't reset)
            return TargetResult(url, True, rtt)
        return TargetResult(url, False, rtt, f"HTTP {r.status_code}")
    except httpx.TimeoutException:
        # covers ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout
        return TargetResult(url, False, (time.perf_counter() - t0) * 1000.0, "timeout")
    except httpx.ConnectError:
        # DNS failure, TCP reset, TLS handshake reset — the typical DPI kill
        return TargetResult(url, False, (time.perf_counter() - t0) * 1000.0, "connection failed")
    except httpx.HTTPError as e:
        # any other transport/protocol error httpx can raise
        return TargetResult(url, False, (time.perf_counter() - t0) * 1000.0, type(e).__name__)


def _sleep(seconds: float, cancel: Optional[threading.Event]) -> bool:
    """Sleep that returns False as soon as `cancel` is set."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if cancel is not None and cancel.is_set():
            return False
        time.sleep(min(0.1, max(0.0, end - time.perf_counter())))
    return True


class StrategyTester:
    def __init__(self, runner: StrategyRunner, targets: list[tuple[str, float]] | None = None,
                 timeout: float = 5.0, attempts: int = 2, concurrency: int = 4):
        self._runner = runner
        self._targets = targets if targets is not None else load_targets()
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._concurrency = max(1, concurrency)

    # ---------- configuration ----------

    def set_params(self, timeout: Optional[float] = None, attempts: Optional[int] = None,
                   concurrency: Optional[int] = None) -> None:
        if timeout:
            self._timeout = timeout
        if attempts:
            self._attempts = max(1, attempts)
        if concurrency:
            self._concurrency = max(1, concurrency)

    @property
    def targets(self) -> list[tuple[str, float]]:
        return list(self._targets)

    def reload_targets(self) -> None:
        self._targets = load_targets()

    # ---------- probing ----------

    def _probe_target(self, url: str, weight: float, cancel: Optional[threading.Event],
                      attempts: Optional[int] = None) -> TargetResult:
        last: TargetResult = TargetResult(url, False, 0.0, "no attempts", weight)
        total = attempts if attempts is not None else self._attempts
        for i in range(total):
            if cancel is not None and cancel.is_set():
                last.error = "cancelled"
                return last
            tr = _probe(url, timeout=self._timeout)
            tr.weight = weight
            last = tr
            if tr.ok:
                return tr
            if i < total - 1:
                # Exponential backoff with jitter before the next attempt: an
                # immediate retry often lands inside the same CDN rate-limit
                # window (Cloudflare) and fails again, and without jitter all
                # probe threads retry in lockstep. Honours `cancel`.
                delay = min(_RETRY_BASE_DELAY * (2 ** i), _RETRY_MAX_DELAY)
                if not _sleep(delay + random.uniform(0.0, _RETRY_BASE_DELAY), cancel):
                    last.error = "cancelled"
                    return last
        return last

    def _probe_many(self, pairs: Iterable[tuple[str, float]], cancel: Optional[threading.Event],
                    attempts: Optional[int] = None) -> list[TargetResult]:
        pairs = list(pairs)
        if not pairs:
            return []
        workers = min(self._concurrency, len(pairs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._probe_target, url, weight, cancel, attempts) for url, weight in pairs]
            out: list[TargetResult] = []
            for fut in futures:
                try:
                    out.append(fut.result())
                except Exception as e:  # pragma: no cover - defensive
                    out.append(TargetResult("", False, 0.0, type(e).__name__))
            return out

    # ---------- main entry points ----------

    def test_one(self, strategy: Strategy, settle_ms: float = 800,
                 cancel: Optional[threading.Event] = None,
                 canary_only: bool = False) -> StrategyResult:
        result = StrategyResult(strategy=strategy, tested_at=time.time())
        if cancel is not None and cancel.is_set():
            result.error = "cancelled"
            return result
        if not self._runner.start(strategy):
            result.error = "winws failed to start"
            return result
        # let WinDivert hook and the new filters stabilise
        if not _sleep(settle_ms / 1000.0, cancel):
            self._runner.stop()
            result.error = "cancelled"
            return result

        # Phase 1: a canary subset, one attempt per target — a dead strategy
        # is detected at least twice as fast. If every canary fails, the
        # strategy is dead — no point spending another two minutes on the rest.
        canary = self._targets[: min(CANARY_COUNT, len(self._targets))]
        result.targets = self._probe_many(canary, cancel, attempts=1)
        if cancel is not None and cancel.is_set():
            self._runner.stop()
            result.error = "cancelled"
            return result
        if not any(t.ok for t in result.targets):
            result.error = "all canary targets unreachable"
            self._runner.stop()
            _log.warning("tested %s: dead (0/%d canaries) — skipped remaining targets",
                         strategy.id, len(canary))
            return result
        if canary_only:
            self._runner.stop()
            result.score = self._score(result)
            return result

        # Phase 2: everything else.
        rest = self._targets[len(canary):]
        result.targets.extend(self._probe_many(rest, cancel))
        self._runner.stop()
        result.score = self._score(result)
        _log.info(
            "tested %s: ok=%d/%d avg=%.0fms score=%.1f err=%s",
            strategy.id, result.ok_targets, result.total_targets, result.avg_rtt_ms,
            result.score, result.error or "-",
        )
        # Log per-target results so "which sites don't open" is answerable
        # from the log alone. FAIL entries go at WARNING so they stand out.
        for t in result.targets:
            if t.ok:
                _log.info("  OK   %s  %.0fms", t.url, t.rtt_ms)
            else:
                _log.warning("  FAIL %s  err=%s", t.url, t.error or "unknown")
        return result

    def _score(self, result: StrategyResult) -> float:
        """Higher = better. Weighted reachability plus a latency bonus."""
        if not result.targets:
            return 0.0
        total_w = sum(t.weight for t in result.targets) or 1.0
        ok_w = sum(t.weight for t in result.targets if t.ok)
        reach = ok_w / total_w
        avg = result.avg_rtt_ms or 5000.0
        base = 100.0 * reach
        # bonus: low latency (1s = 0, 100ms = +40), clamped
        lat_bonus = 0.0
        if reach > 0:
            lat_bonus = max(0.0, min(40.0, 40.0 * (1.0 - (avg - 100.0) / 900.0)))
        # bonus: full coverage
        if all(t.ok for t in result.targets):
            base += 20.0
        return round(base + lat_bonus, 1)

    def quick_scan(self, strategies: list[Strategy],
                   progress_cb: Callable[[int, int, Strategy | None], None] | None = None,
                   cancel: Optional[threading.Event] = None,
                   stop_on_pass: bool = True,
                   max_scan: int = 40,
                   on_pass: Callable[[StrategyResult], bool] | None = None) -> list[StrategyResult]:
        """Cheap pass over the catalog: canaries only, stop at the first hit.

        With ~190 presets a honest full sweep takes minutes; most networks are
        covered by one of the first few strategies, so the autopilot tries them
        in order and stops as soon as one passes every canary. `max_scan`
        bounds the worst case when nothing passes.

        `on_pass` is consulted when a strategy passes every canary: returning
        True stops the scan, False keeps searching for a better one (the
        callback is how the app asks "accept or keep searching?"). It takes
        precedence over `stop_on_pass`.
        """
        from .strategies import preflight_check

        results: list[StrategyResult] = []
        limit = min(len(strategies), max_scan) if max_scan > 0 else len(strategies)
        for i, s in enumerate(strategies[:limit]):
            if cancel is not None and cancel.is_set():
                break
            if progress_cb:
                progress_cb(i, limit, s)
            problems = preflight_check(s)
            if problems:
                _log.warning("preflight FAIL %s: %s", s.id, "; ".join(problems))
                continue
            result = self.test_one(s, settle_ms=500, cancel=cancel, canary_only=True)
            results.append(result)
            if progress_cb:
                progress_cb(i + 1, limit, s)
            if result.ok_targets == result.total_targets and result.ok_targets > 0:
                if on_pass is not None:
                    if on_pass(result):
                        _log.info("quick scan: %s accepted after %d/%d canaries — stopping early",
                                  s.id, result.ok_targets, result.total_targets)
                        break
                elif stop_on_pass:
                    _log.info("quick scan: %s passed %d/%d canaries — stopping early",
                              s.id, result.ok_targets, result.total_targets)
                    break
        if progress_cb:
            progress_cb(limit, limit, None)
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def test_all(self, strategies: list[Strategy],
                 progress_cb: Callable[[int, int, Strategy | None], None] | None = None,
                 cancel: Optional[threading.Event] = None,
                 on_pass: Callable[[StrategyResult], bool] | None = None) -> list[StrategyResult]:
        from .strategies import preflight_check

        results: list[StrategyResult] = []
        total = len(strategies)
        env_failed = 0  # strategies skipped due to missing files / winws.exe
        for i, s in enumerate(strategies):
            if cancel is not None and cancel.is_set():
                _log.info("sweep cancelled after %d/%d strategies", i, total)
                break
            if progress_cb:
                progress_cb(i, total, s)
            # Pre-flight: if winws.exe can't even start this strategy (missing
            # ipset / hostlist file, no winws.exe), don't waste a second
            # spawning it. If three strategies in a row fail purely on
            # environment (not on connectivity), abort the sweep: the problem
            # is systemic (e.g. the bundle wasn't packaged correctly).
            problems = preflight_check(s)
            if problems:
                r = StrategyResult(strategy=s, score=0.0, targets=[], error="preflight: " + "; ".join(problems))
                results.append(r)
                _log.warning("preflight FAIL %s: %s", s.id, "; ".join(problems))
                env_failed += 1
                if env_failed >= 3:
                    _log.error("3 strategies failed preflight — aborting sweep (systemic env problem)")
                    break
                if progress_cb:
                    progress_cb(i + 1, total, s)
                continue
            r = self.test_one(s, cancel=cancel)
            results.append(r)
            if progress_cb:
                progress_cb(i + 1, total, s)
            # Early exit: a strategy that reaches every target is good enough
            # to offer to the user; if they accept, no reason to sit through
            # the remaining ~180 presets.
            if (on_pass is not None and r.error == "" and r.total_targets > 0
                    and r.ok_targets == r.total_targets):
                if on_pass(r):
                    _log.info("sweep: %s accepted early — skipping the remaining %d strategies",
                              s.id, total - i - 1)
                    break
        if progress_cb:
            progress_cb(total, total, None)
        results.sort(key=lambda r: r.score, reverse=True)
        return results
