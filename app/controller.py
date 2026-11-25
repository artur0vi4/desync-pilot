"""Central application controller.

Owns the StrategyRunner, StrategyTester, DNSManager and HealthMonitor,
and exposes a single signal-based API to the UI. The UI never touches
the subprocesses directly — it talks to AppController.

Threading model: every long operation (DNS probe, strategy sweep, winws
restart) runs on a daemon thread. Each such run gets a monotonically
increasing `run_id`; anything that finishes after a newer run started (or
after Stop) is discarded instead of fighting over the process.
"""
from __future__ import annotations

import datetime
import json
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import i18n, paths, toasts
from .config import Config, load as load_config, save as save_config
from .strategies import Strategy, StrategyRunner, diverse_order, load_strategies
from .tester import StrategyTester, StrategyResult
from .dns.advisor import DNSAdvisor, DNSRecommendation
from .dns.manager import DNSManager
from .health import HealthMonitor, HealthSnapshot
from .logger import get as _log
from .logger import export_to

_log = _log()

CACHE_VERSION = 1
RETEST_THROTTLE_SECONDS = 30.0


class State(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    TESTING = "testing"
    DIAGNOSING = "diagnosing"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class AppSnapshot:
    state: State = State.IDLE
    strategy_id: str = ""
    strategy_display: str = ""
    strategy_modes: str = ""
    dns_recommend_name: str = ""
    dns_recommend_rtt_ms: float = 0.0
    health_ok: int = 0
    health_total: int = 0
    health_rtt_ms: float = 0.0
    last_test_at: float = 0.0
    last_test_best: str = ""
    testing_progress: tuple[int, int] = (0, 0)
    testing_current: str = ""
    error: str = ""


class AppController(QObject):
    state_changed = pyqtSignal(object)  # AppSnapshot (immutable copy)
    log_appended = pyqtSignal(list)     # new lines only
    strategies_loaded = pyqtSignal(list)  # list[Strategy]
    test_progress = pyqtSignal(int, int, str)
    strategy_found = pyqtSignal(str, float)  # strategy id, score of the first pass
    toast_request = pyqtSignal(str, str)  # title, body
    update_finished = pyqtSignal(bool, str)  # ok, human message
    diagnostics_ready = pyqtSignal(object)  # DiagnosticReport

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self.config: Config = load_config()
        i18n.set_lang(self.config.language)
        self._runner = StrategyRunner()
        self._tester = StrategyTester(
            self._runner,
            timeout=self.config.test_timeout_seconds,
            attempts=self.config.test_attempts,
        )
        self._dns = DNSManager(
            mode=self.config.dns_mode,
            listen_port=self.config.dns_listen_port,
            preferred=self.config.doh_provider if self.config.doh_provider != "auto" else "",
            custom_url=self.config.doh_custom_url,
        )
        self._dns_advisor = DNSAdvisor(on_update=self._on_dns_recommendation)
        self._health: Optional[HealthMonitor] = None
        self._state = State.IDLE
        self._strategies: list[Strategy] = []
        self._last_test: list[StrategyResult] = []
        self._last_test_at: float = 0.0
        self._snapshot = AppSnapshot()
        self._run_id = 0
        self._cancel = threading.Event()
        self._last_retest_at = 0.0
        self._diag_running = False
        # "accept or keep searching" dialog state (worker thread <-> UI)
        self._found_event = threading.Event()
        self._found_accept = True
        self._found_asked = False

        # Log lines are produced from worker threads (winws.exe stdout);
        # buffer them and hand the UI one batch per tick instead of one
        # signal per line.
        self._log_buf: list[str] = []
        self._log_lock = threading.Lock()
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(200)
        self._log_timer.timeout.connect(self._flush_log)
        self._log_timer.start()

        from . import logger
        logger.subscribe(self._on_log)

    # ---------- public API ----------

    def initialize(self) -> None:
        try:
            self._strategies = load_strategies()
            if self.config.skip_groups:
                self._strategies = [s for s in self._strategies if s.group not in self.config.skip_groups]
        except Exception:
            _log.exception("failed to load strategies")
            self._strategies = []
        _log.info("app initialised with %d usable strategies", len(self._strategies))
        self._load_cache()
        # DNS is informational only: measure the resolvers once in the
        # background and surface a recommendation.
        self._dns_advisor.start()
        # Keep the Run key in sync with the config (and with the exe path,
        # which changes when the folder is moved).
        from . import autostart
        autostart.sync(self.config.autostart_windows)
        self.strategies_loaded.emit(list(self._strategies))
        self._emit_snapshot()
        if self.config.auto_update_strategies:
            self.check_updates(silent=True)
        if self.config.autostart_on_launch:
            self.start()

    def strategies(self) -> list[Strategy]:
        return list(self._strategies)

    def last_results(self) -> list[StrategyResult]:
        return list(self._last_test)

    def last_test_at(self) -> float:
        return self._last_test_at

    @property
    def state(self) -> State:
        return self._state

    def start(self) -> None:
        """Start zapret. If auto_mode and no fresh cached result exists, run a
        full test first; otherwise start the preferred/best known strategy."""
        with self._lock:
            if self._state in (State.RUNNING, State.STARTING, State.TESTING, State.DIAGNOSING, State.STOPPING):
                return
        if not self._strategies:
            self._set_error(i18n.t("msg.no_strategies"))
            return
        if not paths.winws_exe().exists():
            self._set_error(i18n.t("msg.winws_missing"))
            return
        cancel = threading.Event()
        self._cancel = cancel
        run_id = self._next_run()
        threading.Thread(target=self._start_thread, args=(run_id, cancel), daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            if self._state in (State.IDLE, State.STOPPING):
                return
        self._cancel.set()
        run_id = self._next_run()  # invalidate anything still running
        self._stop_health(wait=False)
        self._set_state(State.STOPPING)
        threading.Thread(target=self._stop_thread, args=(run_id,), daemon=True).start()

    def retest(self) -> None:
        """Re-test all strategies, then restore the previous run state."""
        with self._lock:
            if self._state in (State.TESTING, State.STARTING, State.DIAGNOSING, State.STOPPING):
                return
            now = time.time()
            if now - self._last_retest_at < RETEST_THROTTLE_SECONDS:
                _log.info("retest throttled (%.0fs since last), skipping", now - self._last_retest_at)
                return
            self._last_retest_at = now
        cancel = threading.Event()
        self._cancel = cancel
        run_id = self._next_run()
        threading.Thread(target=self._retest_thread, args=(run_id, cancel), daemon=True).start()

    def switch_strategy(self, strategy_id: str) -> None:
        s = next((x for x in self._strategies if x.id == strategy_id), None)
        if not s:
            return
        with self._lock:
            if self._state in (State.STARTING, State.TESTING, State.DIAGNOSING, State.STOPPING):
                return
        cancel = threading.Event()
        self._cancel = cancel
        run_id = self._next_run()
        threading.Thread(target=self._switch_thread, args=(run_id, cancel, s), daemon=True).start()

    def answer_found(self, accept: bool) -> None:
        """UI → worker: the user answered the "accept or keep searching" dialog."""
        self._found_accept = bool(accept)
        self._found_event.set()

    def export_log(self, target: Optional[Path] = None) -> Path:
        target = target or (
            Path(self.config.log_export_dir or str(paths.user_data_dir()))
            / f"zapret-log-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        )
        return export_to(target)

    def update_config(self, cfg: Config) -> None:
        old = self.config
        self.config = cfg
        save_config(cfg)
        i18n.set_lang(cfg.language)

        # Apply to live components instead of waiting for a restart.
        self._tester.set_params(cfg.test_timeout_seconds, cfg.test_attempts)
        from . import autostart
        autostart.sync(cfg.autostart_windows)
        if cfg.dns_mode != old.dns_mode or cfg.dns_listen_port != old.dns_listen_port \
                or cfg.doh_provider != old.doh_provider or cfg.doh_custom_url != old.doh_custom_url:
            needs_restart = self._dns.configure(
                mode=cfg.dns_mode,
                listen_port=cfg.dns_listen_port,
                preferred=cfg.doh_provider if cfg.doh_provider != "auto" else "",
                custom_url=cfg.doh_custom_url,
            )
            if needs_restart and (self._state == State.RUNNING or self._dns.is_running):
                threading.Thread(target=self._restart_dns, daemon=True).start()
        if self._health is not None and cfg.recheck_every_minutes != old.recheck_every_minutes:
            self._start_health()
        self._update_snapshot()

    # ---------- updater ----------

    def check_updates(self, silent: bool = False) -> None:
        threading.Thread(target=self._check_updates_thread, args=(silent,), daemon=True).start()

    def apply_update(self) -> None:
        threading.Thread(target=self._apply_update_thread, daemon=True).start()

    def run_diagnostics(self) -> None:
        """Probe the network and this PC, then restore the bypass.

        The probes must run without the bypass, otherwise every site looks
        reachable and the report is meaningless — so zapret is paused for the
        duration and restarted afterwards if it was running.
        """
        with self._lock:
            if self._diag_running or self._state in (State.STARTING, State.STOPPING):
                return
            self._diag_running = True
        cancel = threading.Event()
        self._cancel = cancel
        run_id = self._next_run()
        threading.Thread(target=self._diagnostics_thread, args=(run_id, cancel), daemon=True).start()

    # ---------- background workers ----------

    def _start_thread(self, run_id: int, cancel: threading.Event) -> None:
        if not self._is_current(run_id):
            return
        self._set_state(State.STARTING)
        try:
            # DNS is a background service: start it here, never on the UI thread.
            self._dns.start()
            if not self._is_current(run_id):
                self._dns.stop()  # Stop was pressed while we were probing DNS
                return
            strategy = self._pick_strategy()
            if strategy is None:
                self._set_state(State.TESTING)
                if self.config.quick_scan:
                    found = self._run_quick_scan(run_id, cancel)
                else:
                    full_results = self._run_full_test(run_id, cancel)
                    found = full_results[0] if full_results and full_results[0].score > 0 else None
                if not self._is_current(run_id):
                    self._dns.stop()
                    return
                if found is None:
                    self._set_error(i18n.t("msg.no_working_strategy"))
                    self._dns.stop()
                    return
                strategy = found.strategy
            if not self._is_current(run_id):
                self._dns.stop()
                return
            ok = self._runner.start(strategy)
            if not ok:
                self._set_error(i18n.t("msg.winws_failed"))
                self._dns.stop()
                return
            self._set_state(State.RUNNING)
            self._update_snapshot(strategy=strategy)
            toasts.notify(i18n.t("app.title"), i18n.t("toast.started", strategy.id), self.config.show_toasts)
            self._start_health()
        except Exception as e:
            _log.exception("start failed")
            self._set_error(str(e))

    def _stop_thread(self, run_id: int) -> None:
        try:
            self._runner.stop()
            self._dns.stop()
        except Exception:
            _log.exception("stop failed")
        if self._run_id != run_id:
            return  # a newer run took over
        self._set_state(State.IDLE)
        self._update_snapshot(strategy=None)
        toasts.notify(i18n.t("app.title"), i18n.t("toast.stopped"), self.config.show_toasts)

    def _retest_thread(self, run_id: int, cancel: threading.Event) -> None:
        was_running = self._state == State.RUNNING
        if was_running:
            self._runner.stop()
            self._stop_health()
        results = self._run_full_test(run_id, cancel)
        if not self._is_current(run_id):
            return
        if results and results[0].score > 0:
            if was_running:
                ok = self._runner.start(results[0].strategy)
                if ok:
                    self._set_state(State.RUNNING)
                    self._update_snapshot(strategy=results[0].strategy)
                    self._start_health()
                else:
                    self._set_error(i18n.t("msg.winws_failed"))
            else:
                self._set_state(State.IDLE)
                self._update_snapshot()
        else:
            self._set_error(i18n.t("msg.no_working_strategy"))

    def _switch_thread(self, run_id: int, cancel: threading.Event, s: Strategy) -> None:
        self._stop_health()
        self._set_state(State.STARTING)
        if not self._is_current(run_id):
            return
        if not self._dns.is_running:
            self._dns.start()  # user picked a strategy while the app was idle
        ok = self._runner.restart(s)
        if not self._is_current(run_id):
            return
        if ok:
            self._set_state(State.RUNNING)
            self._update_snapshot(strategy=s)
            toasts.notify(i18n.t("app.title"), i18n.t("toast.switched", s.id), self.config.show_toasts)
            self._start_health()
        else:
            self._set_error(i18n.t("msg.winws_failed"))

    def _restart_dns(self) -> None:
        self._dns.stop()
        self._dns.start()
        self._update_snapshot()

    def _check_updates_thread(self, silent: bool) -> None:
        from . import updater
        info = updater.fetch_latest_release()
        if info is None:
            if not silent:
                self.update_finished.emit(False, i18n.t("msg.update_check_failed"))
            return
        if info.has_update:
            msg = i18n.t("msg.update_available", info.latest_tag)
            if silent:
                toasts.notify(i18n.t("app.title"), msg, self.config.show_toasts)
                return
            self.update_finished.emit(True, msg)
        elif not silent:
            self.update_finished.emit(True, i18n.t("msg.update_none", info.current_tag))

    def _apply_update_thread(self) -> None:
        from . import updater
        info = updater.fetch_latest_release()
        if info is None:
            self.update_finished.emit(False, i18n.t("msg.update_check_failed"))
            return
        if not info.has_update:
            self.update_finished.emit(True, i18n.t("msg.update_none", info.current_tag))
            return
        ok, msg = updater.update_strategies(info)
        if ok:
            msg = i18n.t("msg.update_done", info.latest_tag)
        else:
            msg = i18n.t("msg.update_failed", msg or "?")
        toasts.notify(i18n.t("app.title"), msg, self.config.show_toasts)
        self.update_finished.emit(ok, msg)

    def _diagnostics_thread(self, run_id: int, cancel: threading.Event) -> None:
        from .diagnostics import run_all

        was_running = self._state == State.RUNNING
        strategy = self._runner.current
        self._stop_health(wait=False)
        if was_running:
            self._runner.stop()
        self._set_state(State.DIAGNOSING)
        report = None
        try:
            report = run_all(
                strategy_ids=tuple(s.id for s in self._strategies),
                port=self.config.dns_listen_port,
            )
        except Exception as e:
            _log.exception("diagnostics failed")
            if self._is_current(run_id):
                self._set_error(str(e))
        finally:
            if report is not None and self._is_current(run_id):
                self.diagnostics_ready.emit(report)
            if was_running and strategy and self._is_current(run_id):
                if self._runner.start(strategy):
                    self._set_state(State.RUNNING)
                    self._update_snapshot(strategy=strategy)
                    self._start_health()
                else:
                    self._set_error(i18n.t("msg.winws_failed"))
            elif self._is_current(run_id) and self._state != State.ERROR:
                self._set_state(State.IDLE)
                self._update_snapshot()
            with self._lock:
                self._diag_running = False

    # ---------- internal: selection ----------

    def _pick_strategy(self) -> Optional[Strategy]:
        if not self.config.auto_mode and self.config.preferred_strategy:
            s = next((x for x in self._strategies if x.id == self.config.preferred_strategy), None)
            if s:
                return s
        best = self._fresh_best()
        if best is not None:
            _log.info("reusing cached best strategy %s (score %.1f, %s old)",
                      best.strategy.id, best.score, _fmt_age(time.time() - self._last_test_at))
            return best.strategy
        return None

    def _fresh_best(self) -> Optional[StrategyResult]:
        ttl = max(0, self.config.results_ttl_minutes) * 60
        if not self._last_test or not self._last_test_at:
            return None
        if ttl <= 0:
            return None  # cache disabled
        if (time.time() - self._last_test_at) > ttl:
            _log.info("cached test results are older than %d min — re-testing", self.config.results_ttl_minutes)
            return None
        best = max(self._last_test, key=lambda r: r.score)
        return best if best.score > 0 else None

    # ---------- internal: testing ----------

    def _cached_scores(self) -> dict[str, float]:
        return {r.strategy.id: r.score for r in self._last_test if r.score > 0}

    def _ask_found(self, result: StrategyResult) -> bool:
        """Decide whether to stop the sweep at the first working strategy.

        Returns True to stop and accept, False to keep searching. When the
        user dialog is enabled we ask exactly once per sweep; no answer
        within the timeout auto-accepts, so an unattended autopilot in the
        tray is never stuck.
        """
        if not self.config.ask_on_found:
            return True  # old behaviour: silently take the first pass
        if self._found_asked:
            return False  # the user already chose "keep searching" once
        self._found_asked = True
        timeout = max(5, int(self.config.ask_found_timeout_seconds))
        with self._lock:
            self._found_accept = True  # default answer
            self._found_event = threading.Event()
        self.strategy_found.emit(result.strategy.id, result.score)
        ev = self._found_event
        deadline = time.time() + timeout
        while time.time() < deadline and not ev.wait(0.2):
            if self._cancel.is_set():
                return False  # Stop was pressed — the sweep will abort anyway
        accepted = self._found_accept and not self._cancel.is_set()
        _log.info("first pass %s (score %.1f): user chose %s",
                  result.strategy.id, result.score,
                  "accept" if accepted else "keep searching")
        return accepted

    def _on_dns_recommendation(self, rec: DNSRecommendation) -> None:
        with self._lock:
            self._snapshot.dns_recommend_name = rec.name
            self._snapshot.dns_recommend_rtt_ms = rec.rtt_ms
        self._emit_snapshot()

    def _progress_cb(self):
        def progress(i: int, n: int, s) -> None:
            label = s.id if isinstance(s, Strategy) else ""
            self.test_progress.emit(i, n, label)
            with self._lock:
                self._snapshot.testing_progress = (i, n)
                self._snapshot.testing_current = label
            self._emit_snapshot()
        return progress

    def _run_quick_scan(self, run_id: int, cancel: threading.Event) -> Optional[StrategyResult]:
        """Two-stage pick: canaries over the first N strategies, then a full
        verification of the winner."""
        self._tester.reload_targets()
        self._found_asked = False
        ordered = diverse_order(self._strategies, self._cached_scores())
        _log.info("quick scan: first candidate is %s (diversity order)", ordered[0].id if ordered else "-")
        results = self._tester.quick_scan(
            ordered,
            progress_cb=self._progress_cb(),
            cancel=cancel,
            stop_on_pass=False,
            on_pass=self._ask_found,
            max_scan=max(1, self.config.quick_scan_limit),
        )
        if cancel.is_set() or not self._is_current(run_id):
            return None
        if not results:
            return None
        best = results[0]
        if best.score <= 0:
            return None
        # Confirm the winner against every target before trusting it.
        full = self._tester.test_one(best.strategy, cancel=cancel)
        if cancel.is_set() or not self._is_current(run_id):
            return None
        if full.score > 0:
            best = full
        self._last_test = [best]
        self._last_test_at = time.time()
        self._save_cache([best])
        with self._lock:
            self._snapshot.last_test_at = self._last_test_at
            self._snapshot.last_test_best = best.strategy.id
        _log.info("quick scan winner: %s (score %.1f, %d/%d targets)",
                  best.strategy.id, best.score, best.ok_targets, best.total_targets)
        return best

    def _run_full_test(self, run_id: int, cancel: threading.Event) -> list[StrategyResult]:
        self._set_state(State.TESTING)
        self._tester.reload_targets()  # pick up edits to utils/targets.txt
        self._found_asked = False
        ordered = diverse_order(self._strategies, self._cached_scores())
        _log.info("full sweep: %d strategies, first candidate is %s (diversity order)",
                  len(ordered), ordered[0].id if ordered else "-")

        results = self._tester.test_all(
            ordered, progress_cb=self._progress_cb(), cancel=cancel, on_pass=self._ask_found)
        if not self._is_current(run_id):
            return results
        with self._lock:
            self._last_test = results
            self._last_test_at = time.time() if results else 0.0
            self._snapshot.last_test_at = self._last_test_at
            if results and results[0].score > 0:
                self._snapshot.last_test_best = results[0].strategy.id
        if results:
            self._save_cache(results)
            if results[0].score > 0:
                toasts.notify(i18n.t("app.title"), i18n.t("toast.test_done", results[0].strategy.id),
                              self.config.show_toasts)
        return results

    # ---------- internal: state cache ----------

    def _load_cache(self) -> None:
        path = paths.state_file()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != CACHE_VERSION:
                return
            at = float(data.get("at") or 0)
            by_id = {s.id: s for s in self._strategies}
            results: list[StrategyResult] = []
            for item in data.get("results") or []:
                s = by_id.get(item.get("id"))
                if not s:
                    continue
                results.append(StrategyResult(strategy=s, score=float(item.get("score") or 0.0),
                                              tested_at=at))
            if not results:
                return
            results.sort(key=lambda r: r.score, reverse=True)
            self._last_test = results
            self._last_test_at = at
            self._snapshot.last_test_at = at
            self._snapshot.last_test_best = results[0].strategy.id
            _log.info("cached test results from %s (%d strategies, best %s)",
                      datetime.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M"),
                      len(results), results[0].strategy.id)
        except Exception as e:
            _log.warning("could not read state cache: %s", e)

    def _save_cache(self, results: list[StrategyResult]) -> None:
        payload = {
            "version": CACHE_VERSION,
            "at": self._last_test_at,
            "results": [{"id": r.strategy.id, "score": r.score} for r in results],
        }
        try:
            path = paths.state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            _log.warning("could not write state cache: %s", e)

    # ---------- internal: health ----------

    def _start_health(self) -> None:
        self._stop_health()
        self._health = HealthMonitor(
            interval_minutes=self.config.recheck_every_minutes,
            on_degraded=self._on_degraded,
            on_snapshot=self._on_health,
            on_dead=self._on_winws_dead,
            is_alive=lambda: self._runner.is_running,  # property, so wrap it
        )
        self._health.start()

    def _stop_health(self, wait: bool = False) -> None:
        if self._health:
            self._health.stop(wait=wait)
            self._health = None

    def _on_winws_dead(self) -> None:
        strategy = self._runner.current
        if not strategy or self._state != State.RUNNING:
            return
        _log.warning("winws.exe died — restarting with %s", strategy.id)
        if not self._runner.restart(strategy):
            self._on_degraded()

    def _on_degraded(self) -> None:
        _log.info("health degraded — scheduling auto re-test")
        self.retest()

    def _on_health(self, snap: HealthSnapshot) -> None:
        with self._lock:
            self._snapshot.health_ok = snap.reachable
            self._snapshot.health_total = snap.total
            self._snapshot.health_rtt_ms = snap.avg_rtt_ms
        self._emit_snapshot()

    # ---------- internal: plumbing ----------

    def _next_run(self) -> int:
        with self._lock:
            self._run_id += 1
            return self._run_id

    def _is_current(self, run_id: int) -> bool:
        return run_id == self._run_id

    def _flush_log(self) -> None:
        with self._log_lock:
            if not self._log_buf:
                return
            chunk, self._log_buf = self._log_buf, []
        self.log_appended.emit(chunk)

    def _on_log(self, lines: list[str]) -> None:
        with self._log_lock:
            self._log_buf.extend(lines)

    def _set_state(self, state: State) -> None:
        with self._lock:
            self._state = state
            self._snapshot.state = state
        self._emit_snapshot()

    def _set_error(self, msg: str) -> None:
        with self._lock:
            self._snapshot.error = msg
            self._state = State.ERROR
            self._snapshot.state = State.ERROR
        toasts.notify(i18n.t("app.title"), i18n.t("toast.error", msg), self.config.show_toasts)
        self._emit_snapshot()

    def _update_snapshot(self, strategy: Optional[Strategy] = None) -> None:
        s = self._runner.current or strategy
        with self._lock:
            if s:
                self._snapshot.strategy_id = s.id
                self._snapshot.strategy_display = s.title
                self._snapshot.strategy_modes = s.display_modes
            else:
                self._snapshot.strategy_id = ""
                self._snapshot.strategy_display = ""
                self._snapshot.strategy_modes = ""
            rec = self._dns_advisor.recommendation
            if rec:
                self._snapshot.dns_recommend_name = rec.name
                self._snapshot.dns_recommend_rtt_ms = rec.rtt_ms
        self._emit_snapshot()

    def _emit_snapshot(self) -> None:
        with self._lock:
            snap = replace(self._snapshot)  # never hand the same mutable object out
        self.state_changed.emit(snap)

    def shutdown(self) -> None:
        self._cancel.set()
        self._next_run()
        try:
            self._stop_health()
            self._runner.stop()
            self._dns.stop()
        except Exception:
            _log.exception("shutdown error")
        from . import logger
        logger.unsubscribe(self._on_log)


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}min"
    return f"{seconds / 3600:.1f}h"
