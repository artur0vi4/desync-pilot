"""Flow tests for StrategyTester with a fake runner and fake probes."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategies import Strategy  # noqa: E402
from app.tester import StrategyResult, StrategyTester, TargetResult  # noqa: E402


def _strategy(sid: str) -> Strategy:
    return Strategy(id=sid, file=f"{sid}.bat", title=sid, group="base",
                    sections=[[]], modes=[], raw_args=["--dpi-desync=fake"])


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stops = 0
        self.is_running = False

    def start(self, strategy, *args) -> bool:
        self.started.append(strategy.id)
        self.is_running = True
        return True

    def stop(self) -> None:
        self.stops += 1
        self.is_running = False


TARGETS = [("https://a", 1.0), ("https://b", 1.0), ("https://c", 1.0), ("https://d", 1.0),
           ("https://e", 1.0), ("https://f", 1.0)]


class TesterFlowTests(unittest.TestCase):
    def _tester(self):
        runner = FakeRunner()
        return StrategyTester(runner, targets=TARGETS, timeout=0.1, attempts=1, concurrency=2), runner

    def test_dead_strategy_aborts_after_canaries(self):
        tester, runner = self._tester()
        calls: list[str] = []

        def fake_probe(url, timeout=5.0):
            calls.append(url)
            return TargetResult(url, False, 1.0, "timeout")

        with mock.patch("app.tester._probe", fake_probe):
            result = tester.test_one(_strategy("dead"), settle_ms=0)

        self.assertEqual(result.error, "all canary targets unreachable")
        self.assertEqual(result.score, 0.0)
        self.assertEqual(len(calls), 4)  # only the canaries were probed
        self.assertEqual(runner.stops, 1)  # winws was stopped again

    def test_partial_strategy_probes_everything_and_scores(self):
        tester, runner = self._tester()

        def fake_probe(url, timeout=5.0):
            ok = url in ("https://a", "https://b")
            return TargetResult(url, ok, 120.0 if ok else 0.0, "" if ok else "timeout")

        with mock.patch("app.tester._probe", fake_probe):
            result = tester.test_one(_strategy("half"), settle_ms=0)

        self.assertEqual(result.total_targets, 6)
        self.assertEqual(result.ok_targets, 2)
        # weighted reach 2/6 = 33.3 + latency bonus (120 ms -> ~39.1)
        self.assertGreater(result.score, 60.0)
        self.assertLess(result.score, 80.0)

    def test_cancel_before_start_does_nothing(self):
        tester, runner = self._tester()
        cancel = threading.Event()
        cancel.set()
        result = tester.test_one(_strategy("x"), settle_ms=0, cancel=cancel)
        self.assertEqual(result.error, "cancelled")
        self.assertEqual(runner.started, [])
        self.assertEqual(runner.stops, 0)

    def test_cancel_during_settle_stops_winws(self):
        tester, runner = self._tester()
        cancel = threading.Event()
        with mock.patch("app.tester._sleep", lambda seconds, c: False):
            result = tester.test_one(_strategy("x"), settle_ms=600, cancel=cancel)
        self.assertEqual(result.error, "cancelled")
        self.assertEqual(runner.stops, 1)

    def test_test_all_sorts_and_reports_progress(self):
        tester, runner = self._tester()
        scores = {"a": 10.0, "b": 90.0, "c": 0.0}

        def fake_test_one(strategy, settle_ms=600, cancel=None):
            return StrategyResult(strategy=strategy, score=scores[strategy.id])

        progress: list[tuple[int, int]] = []
        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.test_all(
                [_strategy("a"), _strategy("b"), _strategy("c")],
                progress_cb=lambda i, n, s: progress.append((i, n)),
            )

        self.assertEqual([r.strategy.id for r in results], ["b", "a", "c"])
        self.assertIn((3, 3), progress)

    def test_quick_scan_stops_after_first_pass(self):
        tester, runner = self._tester()
        seen: list[str] = []

        def fake_test_one(strategy, settle_ms=800, cancel=None, canary_only=False):
            seen.append(strategy.id)
            ok = strategy.id == "b"
            return StrategyResult(strategy=strategy, score=100.0 if ok else 0.0,
                                  targets=[TargetResult("https://a", ok, 10.0)])

        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.quick_scan([_strategy("a"), _strategy("b"), _strategy("c")], max_scan=10)

        self.assertEqual(seen, ["a", "b"])  # stopped after the first full pass
        self.assertEqual(results[0].strategy.id, "b")

    def test_quick_scan_on_pass_decides_stop_or_continue(self):
        tester, runner = self._tester()

        def fake_test_one(strategy, settle_ms=800, cancel=None, canary_only=False):
            ok = strategy.id in ("b", "c")
            return StrategyResult(strategy=strategy, score=100.0 if ok else 0.0,
                                  targets=[TargetResult("https://a", ok, 10.0)])

        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            # "keep searching" -> the scan walks the rest of the catalog
            seen: list[str] = []
            tester.quick_scan([_strategy(x) for x in "abcd"], max_scan=10,
                              stop_on_pass=False,
                              on_pass=lambda r: seen.append(r.strategy.id) or False)
            self.assertEqual(seen, ["b", "c"])  # every pass asked, scan continued

            # "accept" -> the scan stops at the first pass
            seen2: list[str] = []
            results = tester.quick_scan([_strategy(x) for x in "abcd"], max_scan=10,
                                        stop_on_pass=False,
                                        on_pass=lambda r: seen2.append(r.strategy.id) or True)
            self.assertEqual(seen2, ["b"])
            self.assertEqual(results[0].strategy.id, "b")

    def test_quick_scan_without_on_pass_and_no_stop_scans_everything(self):
        tester, runner = self._tester()

        def fake_test_one(strategy, settle_ms=800, cancel=None, canary_only=False):
            return StrategyResult(strategy=strategy, score=100.0,
                                  targets=[TargetResult("https://a", True, 10.0)])

        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.quick_scan([_strategy(x) for x in "abc"], max_scan=10,
                                        stop_on_pass=False)
        self.assertEqual(len(results), 3)

    def test_test_all_on_pass_stops_early(self):
        tester, runner = self._tester()

        def fake_test_one(strategy, settle_ms=600, cancel=None):
            ok = strategy.id == "b"
            return StrategyResult(strategy=strategy, score=100.0 if ok else 0.0,
                                  targets=[TargetResult("https://a", ok, 10.0)])

        asked: list[str] = []
        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.test_all([_strategy("a"), _strategy("b"), _strategy("c")],
                                      on_pass=lambda r: asked.append(r.strategy.id) or True)

        self.assertEqual(asked, ["b"])  # only a fully-passing strategy is offered
        self.assertEqual(results[0].strategy.id, "b")
        self.assertEqual(len(results), 2)  # "c" was never reached

    def test_test_all_on_pass_keep_searching_scans_everything(self):
        tester, runner = self._tester()

        def fake_test_one(strategy, settle_ms=600, cancel=None):
            return StrategyResult(strategy=strategy, score=100.0,
                                  targets=[TargetResult("https://a", True, 10.0)])

        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.test_all([_strategy(x) for x in "abc"],
                                      on_pass=lambda r: False)
        self.assertEqual(len(results), 3)

    def test_quick_scan_respects_max_scan(self):
        tester, runner = self._tester()
        seen: list[str] = []

        def fake_test_one(strategy, settle_ms=800, cancel=None, canary_only=False):
            seen.append(strategy.id)
            return StrategyResult(strategy=strategy, score=0.0)

        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            tester.quick_scan([_strategy(x) for x in "abcd"], max_scan=2)

        self.assertEqual(seen, ["a", "b"])

    def test_test_all_stops_when_cancelled(self):
        tester, runner = self._tester()
        cancel = threading.Event()
        tested: list[str] = []

        def fake_test_one(strategy, settle_ms=600, cancel=None):
            tested.append(strategy.id)
            return StrategyResult(strategy=strategy, score=1.0)

        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.test_all([_strategy("a"), _strategy("b"), _strategy("c")], cancel=cancel)
        self.assertEqual(len(results), 3)  # nothing cancelled yet

        cancel.set()
        tested.clear()
        with mock.patch.object(tester, "test_one", fake_test_one), \
             mock.patch("app.strategies.preflight_check", lambda s: []):
            results = tester.test_all([_strategy("a"), _strategy("b")], cancel=cancel)
        self.assertEqual(tested, [])
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
