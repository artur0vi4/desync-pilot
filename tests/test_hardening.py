"""Offline tests for the hardening changes:

- httpx probe: status/exception mapping and retry backoff with jitter;
- config validation bounds (dns_listen_port etc.);
- safe Windows argument escaping in app/main.py.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.main import _escape_arg  # noqa: E402
from app.tester import StrategyTester, _probe  # noqa: E402


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler),
                        follow_redirects=True, trust_env=False)


class ProbeHttpxTests(unittest.TestCase):
    """_probe maps httpx results and exceptions onto TargetResult."""

    def test_ok_200(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 32)

        with mock.patch("app.tester._get_client", return_value=_client(handler)):
            tr = _probe("https://example.org")
        self.assertTrue(tr.ok)
        self.assertEqual(tr.error, "")
        self.assertGreater(tr.rtt_ms, 0.0)

    def test_status_500_is_failure_with_clear_message(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"")

        with mock.patch("app.tester._get_client", return_value=_client(handler)):
            tr = _probe("https://example.org")
        self.assertFalse(tr.ok)
        self.assertEqual(tr.error, "HTTP 500")

    def test_status_403_still_means_tls_path_works(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b"")

        with mock.patch("app.tester._get_client", return_value=_client(handler)):
            tr = _probe("https://example.org")
        self.assertTrue(tr.ok)

    def test_redirect_is_followed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/moved":
                return httpx.Response(302, headers={"Location": "https://example.org/final"})
            return httpx.Response(200, content=b"ok")

        with mock.patch("app.tester._get_client", return_value=_client(handler)):
            tr = _probe("https://example.org/moved")
        self.assertTrue(tr.ok)

    def test_timeout_maps_to_readable_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        with mock.patch("app.tester._get_client", return_value=_client(handler)):
            tr = _probe("https://example.org")
        self.assertFalse(tr.ok)
        self.assertEqual(tr.error, "timeout")

    def test_connect_error_maps_to_readable_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection reset", request=request)

        with mock.patch("app.tester._get_client", return_value=_client(handler)):
            tr = _probe("https://example.org")
        self.assertFalse(tr.ok)
        self.assertEqual(tr.error, "connection failed")


class RetryBackoffTests(unittest.TestCase):
    """_probe_target retries failed targets with an exponential+jitter delay."""

    def _tester(self, attempts: int = 2) -> StrategyTester:
        return StrategyTester(runner=None, targets=[("https://a", 1.0)],
                              timeout=1.0, attempts=attempts, concurrency=1)

    def test_retry_succeeds_after_backoff(self):
        tester = self._tester(attempts=2)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, content=b"")
            return httpx.Response(200, content=b"")

        sleeps: list[float] = []
        with mock.patch("app.tester._get_client", return_value=_client(handler)), \
             mock.patch("app.tester._sleep",
                        side_effect=lambda s, c: sleeps.append(round(s, 2)) or True):
            tr = tester._probe_target("https://a", 1.0, None)
        self.assertTrue(tr.ok)
        self.assertEqual(len(sleeps), 1)          # one pause between two attempts
        self.assertTrue(0.5 <= sleeps[0] <= 1.0)  # 0.5 * 2**0 + jitter(0..0.5)

    def test_single_attempt_does_not_sleep(self):
        tester = self._tester(attempts=1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"")

        sleeps: list[float] = []
        with mock.patch("app.tester._get_client", return_value=_client(handler)), \
             mock.patch("app.tester._sleep",
                        side_effect=lambda s, c: sleeps.append(s) or True):
            tr = tester._probe_target("https://a", 1.0, None)
        self.assertFalse(tr.ok)
        self.assertEqual(sleeps, [])

    def test_backoff_is_exponential(self):
        tester = self._tester(attempts=4)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"")

        sleeps: list[float] = []
        with mock.patch("app.tester._get_client", return_value=_client(handler)), \
             mock.patch("app.tester._sleep",
                        side_effect=lambda s, c: sleeps.append(round(s, 2)) or True):
            tr = tester._probe_target("https://a", 1.0, None)
        self.assertFalse(tr.ok)
        # bases 0.5 -> 1.0 -> 2.0 (+ up to 0.5 jitter each, never below the base)
        self.assertTrue(0.5 <= sleeps[0] <= 1.0)
        self.assertTrue(1.0 <= sleeps[1] <= 1.5)
        self.assertTrue(2.0 <= sleeps[2] <= 2.5)

    def test_cancel_during_backoff_reports_cancelled(self):
        tester = self._tester(attempts=2)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"")

        with mock.patch("app.tester._get_client", return_value=_client(handler)), \
             mock.patch("app.tester._sleep", return_value=False):
            tr = tester._probe_target("https://a", 1.0, None)
        self.assertFalse(tr.ok)
        self.assertEqual(tr.error, "cancelled")


class ConfigValidationTests(unittest.TestCase):
    def test_dns_listen_port_bounds(self):
        for bad in (0, 80, 1023, 65536, 70000, True, "abc"):
            cfg = app_config.Config.from_dict({"dns_listen_port": bad})
            self.assertEqual(cfg.dns_listen_port, 5354, f"{bad!r} -> default")
        for good in (1024, 5354, 5444, 65535):
            cfg = app_config.Config.from_dict({"dns_listen_port": good})
            self.assertEqual(cfg.dns_listen_port, good)

    def test_positive_int_fields(self):
        cases = {
            "test_timeout_seconds": (8, (0, -5, 121, 10_000)),
            "test_attempts": (2, (0, -1, 11, 999)),
            "quick_scan_limit": (40, (0, -1, 1001, 10**6)),
        }
        for key, (default, bad_values) in cases.items():
            for bad in bad_values:
                cfg = app_config.Config.from_dict({key: bad})
                self.assertEqual(getattr(cfg, key), default, f"{key}={bad!r} -> default")
            cfg = app_config.Config.from_dict({key: 1})
            self.assertEqual(getattr(cfg, key), 1)

    def test_valid_roundtrip_is_untouched(self):
        cfg = app_config.Config.from_dict({
            "dns_listen_port": 5444, "test_timeout_seconds": 30,
            "test_attempts": 5, "quick_scan_limit": 120,
        })
        self.assertEqual((cfg.dns_listen_port, cfg.test_timeout_seconds,
                          cfg.test_attempts, cfg.quick_scan_limit),
                         (5444, 30, 5, 120))

    def test_load_broken_ini_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.ini"
            path.write_text("[desyncpilot]\ndns_listen_port = 70000\n"
                            "test_attempts = 0\nlanguage = en\n", encoding="utf-8")
            cfg = app_config.load(path)
        self.assertEqual(cfg.dns_listen_port, 5354)
        self.assertEqual(cfg.test_attempts, 2)
        self.assertEqual(cfg.language, "en")  # untouched fields keep their values


class EscapeArgTests(unittest.TestCase):
    def test_plain_argument_untouched(self):
        self.assertEqual(_escape_arg("--minimized"), "--minimized")
        self.assertEqual(_escape_arg(r"C:\tools\app.exe"), r"C:\tools\app.exe")

    def test_empty_argument(self):
        self.assertEqual(_escape_arg(""), '""')

    def test_spaces_are_quoted(self):
        self.assertEqual(_escape_arg(r"C:\Program Files\Desync Pilot\app.exe"),
                         '"C:\\Program Files\\Desync Pilot\\app.exe"')

    def test_inner_quotes_are_escaped(self):
        self.assertEqual(_escape_arg('a"b'), '"a\\"b"')
        # quote + space: quoting and escaping combine
        self.assertEqual(_escape_arg('he said "hi" ok'), '"he said \\"hi\\" ok"')

    def test_cmdline_survives_space_and_quote_args(self):
        args = [r"C:\my app\app.exe", "--minimized", 'we"ird "value']
        cmdline = " ".join(_escape_arg(a) for a in args)
        # every multi-word / quoted arg ends up wrapped in escaped quotes
        for frag in args:
            if " " in frag or '"' in frag:
                self.assertIn('"' + frag.replace('"', '\\"') + '"', cmdline)


if __name__ == "__main__":
    unittest.main()
