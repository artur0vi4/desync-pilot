"""Controller flow tests (Qt, offscreen): the accept-or-continue dialog path.

Requires PyQt6 (it is in requirements.txt and installed in CI); the offscreen
platform keeps it headless on every OS. When Qt cannot be imported here, the
whole module skips instead of failing the offline suite.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from PyQt6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:
    QApplication = None  # type: ignore[assignment]
    HAS_QT = False

if HAS_QT:
    _app = QApplication.instance() or QApplication(sys.argv)
    from app.controller import AppController  # noqa: E402
    from app.strategies import Strategy  # noqa: E402
    from app.tester import StrategyResult, TargetResult  # noqa: E402


def _result(sid: str = "ALT", score: float = 130.0):
    st = Strategy(id=sid, file=f"{sid}.bat", title=sid, group="alt",
                  sections=[["--dpi-desync=fake"]], modes=["fake"],
                  raw_args=["--dpi-desync=fake"])
    return StrategyResult(strategy=st, score=score,
                          targets=[TargetResult("https://a", True, 20.0)])


@unittest.skipUnless(HAS_QT, "PyQt6 is not importable in this environment")
class AskFoundTests(unittest.TestCase):
    def _controller(self) -> "AppController":
        return AppController()

    def test_ask_disabled_accepts_immediately(self):
        ctrl = self._controller()
        ctrl.config.ask_on_found = False
        self.assertTrue(ctrl._ask_found(_result()))

    def test_answer_keep_searching_returns_false(self):
        ctrl = self._controller()
        ctrl.config.ask_on_found = True
        ctrl.config.ask_found_timeout_seconds = 10
        ctrl._found_asked = False
        emitted: list[tuple[str, float]] = []
        ctrl.strategy_found.connect(lambda sid, score: emitted.append((sid, score)))

        answers: list[bool] = []

        def worker() -> None:
            answers.append(ctrl._ask_found(_result("ALT2")))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # the worker emits the signal (queued to this thread) and waits
        deadline = 5.0
        while not emitted and deadline > 0:
            deadline -= 0.05
            threading.Event().wait(0.05)
            _app.processEvents()
        self.assertEqual(emitted, [("ALT2", 130.0)])
        ctrl.answer_found(False)  # user pressed "keep searching"
        t.join(timeout=5)
        self.assertEqual(answers, [False])

    def test_only_one_ask_per_sweep(self):
        ctrl = self._controller()
        ctrl.config.ask_on_found = True
        ctrl._found_asked = True  # the user already answered once
        # a second pass must not ask again: keep searching, silently
        self.assertFalse(ctrl._ask_found(_result("ALT3")))


if __name__ == "__main__":
    unittest.main()
