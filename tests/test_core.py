"""Offline unit tests for the parts that do not need a network or Qt.

Run:  python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app import logger as app_logger  # noqa: E402
from app.dns import resolver  # noqa: E402
from app.dns.proxy import _question_key  # noqa: E402
from app.health import HealthMonitor, HealthSnapshot  # noqa: E402
from app.strategies import Strategy, build_argv, preflight_check  # noqa: E402
from app.strategy_parser import parse_bat  # noqa: E402
from app.tester import StrategyResult, StrategyTester, TargetResult, load_targets  # noqa: E402
from app.winws_args import clean_port_lists, is_path_flag, strip_value_quotes  # noqa: E402


def _strategy(raw_args: list[str]) -> Strategy:
    return Strategy(id="test", file="general.bat", title="TEST", group="base",
                    sections=[raw_args], modes=["fake"], raw_args=raw_args)


class WinwsArgsTests(unittest.TestCase):
    def test_path_flags(self):
        self.assertTrue(is_path_flag("--ipset-exclude", "%LISTS%ipset-exclude.txt"))
        self.assertTrue(is_path_flag("--dpi-desync-fake-tls", "%BIN%tls_clienthello.bin"))
        self.assertTrue(is_path_flag("--dpi-desync-fake-tls", r"C:\bin\fake.bin"))
        # numeric / enum values of the same family are not paths
        self.assertFalse(is_path_flag("--dpi-desync-fake-tls", "0x00000000"))
        self.assertFalse(is_path_flag("--dpi-desync-fake-tls", "^!"))
        self.assertFalse(is_path_flag("--dpi-desync-fake-tls-mod", "rnd,dupsid,sni=www.google.com"))
        # interface names used to be mistaken for file paths
        self.assertFalse(is_path_flag("--wf-iface", "Ethernet"))
        self.assertFalse(is_path_flag("--wf-l3", "ipv4"))

    def test_clean_port_lists(self):
        self.assertEqual(clean_port_lists(["--wf-tcp=80,443,"])[0], "--wf-tcp=80,443")
        self.assertEqual(clean_port_lists(["--wf-udp=,443,,50000-50100,"])[0], "--wf-udp=443,50000-50100")

    def test_strip_quotes(self):
        out = strip_value_quotes(['--ipset-exclude="E:\\lists\\ipset-exclude.txt"'])
        self.assertEqual(out[0], "--ipset-exclude=E:\\lists\\ipset-exclude.txt")
        # non-path values keep their quotes (they may be part of the value)
        self.assertEqual(strip_value_quotes(['--dpi-desync-fake-tls="0x00"'])[0],
                         '--dpi-desync-fake-tls="0x00"')

    def test_build_argv_drops_empty_game_sections(self):
        st = _strategy([
            "--wf-tcp=80,443,%GameFilterTCP%",
            "--wf-udp=443,%GameFilterUDP%",
            "--filter-tcp=80,443",
            "--dpi-desync=fake",
            "--new",
            "--filter-tcp=%GameFilterTCP%",
            "--filter-udp=%GameFilterUDP%",
            "--dpi-desync=fake",
        ])
        argv = build_argv(st)
        self.assertTrue(argv[0].endswith("winws.exe"))
        self.assertEqual(argv[1], "--wf-tcp=80,443")
        filters = [a for a in argv if a.startswith(("--filter-tcp=", "--filter-udp="))]
        self.assertEqual(filters, ["--filter-tcp=80,443"])  # the game-only section is gone
        self.assertNotIn("--new", argv)
        self.assertIn("--dpi-desync=fake", argv)


class PreflightTests(unittest.TestCase):
    def test_missing_files_and_interface_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            (root / "lists").mkdir()
            exe = root / "bin" / "winws.exe"
            exe.write_bytes(b"MZ")
            (root / "lists" / "list-general.txt").write_text("", encoding="utf-8")
            st = _strategy([
                "--wf-iface=Ethernet",
                "--hostlist=%LISTS%list-general.txt",
                "--dpi-desync-fake-tls=%BIN%missing.bin",
            ])
            with mock.patch("app.strategies.paths.winws_exe", return_value=exe), \
                 mock.patch("app.strategies.paths.bin_dir", return_value=root / "bin"), \
                 mock.patch("app.strategies.paths.lists_dir", return_value=root / "lists"):
                problems = preflight_check(st)
        joined = "; ".join(problems)
        self.assertIn("missing.bin", joined)
        self.assertNotIn("Ethernet", joined)  # an interface name is not a path


class TesterTests(unittest.TestCase):
    def test_score_uses_weights_and_clamps_latency(self):
        tester = StrategyTester(runner=None, targets=[("https://a", 1.0)], timeout=1, attempts=1)
        st = _strategy(["--dpi-desync=fake"])
        res = StrategyResult(strategy=st, targets=[
            TargetResult("https://discord.com", True, 50.0, weight=1.2),
            TargetResult("https://youtube.com", False, 0.0, "timeout", weight=1.0),
        ])
        score = tester._score(res)
        # weighted reach = 1.2 / 2.2 ≈ 54.5, latency bonus is clamped to 40
        self.assertAlmostEqual(score, round(100 * (1.2 / 2.2) + 40, 1), places=1)

    def test_avg_rtt_is_not_distorted_by_weight(self):
        res = StrategyResult(strategy=_strategy([]), targets=[
            TargetResult("https://a", True, 100.0, weight=0.5),
            TargetResult("https://b", True, 200.0, weight=1.5),
        ])
        self.assertAlmostEqual(res.avg_rtt_ms, 150.0)

    def test_load_targets_skips_ping_entries(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "targets.txt"
            p.write_text(
                "# comment\n"
                'Discord = "https://discord.com"\n'
                'Custom  = "https://example.org"\n'
                'CloudflareDNS = "PING:1.1.1.1"\n',
                encoding="utf-8",
            )
            targets = load_targets(p)
        urls = [u for u, _w in targets]
        self.assertIn("https://discord.com", urls)
        self.assertIn("https://example.org", urls)
        self.assertFalse(any(u.startswith("PING") for u in urls))
        weights = dict(targets)
        self.assertEqual(weights["https://discord.com"], 1.0)  # tuned weight kept
        self.assertEqual(weights["https://example.org"], 1.0)  # unknown -> default


BAT_SAMPLE = r'''@echo off
chcp 65001 > nul
start "zapret: %~n0" /min "%BIN%winws.exe" --wf-tcp=80,443,%GameFilterTCP% --wf-udp=443,%GameFilterUDP% ^
 --filter-tcp=443 --hostlist="%LISTS%list-general.txt" --dpi-desync=fake,split ^
 --dpi-desync-fake-tls="%BIN%tls_clienthello_www_google_com.bin" ^
 --new --filter-udp=443 --dpi-desync=fake --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"
'''


class ParserTests(unittest.TestCase):
    def test_parse_bat(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "general (ALT2).bat"
            p.write_text(BAT_SAMPLE, encoding="utf-8")
            entry = parse_bat(p)
        self.assertEqual(entry["id"], "general (ALT2)")
        self.assertEqual(entry["group"], "alt")
        self.assertEqual(entry["title"], "ALT2")
        self.assertEqual(len(entry["sections"]), 2)
        self.assertIn("fake,split", entry["modes"])
        self.assertTrue(any(t.startswith("--dpi-desync-fake-tls=%BIN%") for t in entry["raw_args"]))
        # quotes around path values are removed by the parser
        self.assertFalse(any('="' in t for t in entry["raw_args"]))


class ConfigTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.ini"
            cfg = app_config.Config(language="en", results_ttl_minutes=15,
                                    skip_groups=["experimental", "alt"], dns_listen_port=5444,
                                    autostart_windows=True,
                                    doh_custom_url="https://dns.example/dns-query",
                                    quick_scan=False, quick_scan_limit=12,
                                    ask_on_found=False, ask_found_timeout_seconds=60)
            app_config.save(cfg, path)
            loaded = app_config.load(path)
        self.assertEqual(loaded.language, "en")
        self.assertEqual(loaded.results_ttl_minutes, 15)
        self.assertEqual(loaded.skip_groups, ["experimental", "alt"])
        self.assertEqual(loaded.dns_listen_port, 5444)
        self.assertTrue(loaded.autostart_windows)
        self.assertEqual(loaded.doh_custom_url, "https://dns.example/dns-query")
        self.assertFalse(loaded.quick_scan)
        self.assertEqual(loaded.quick_scan_limit, 12)
        self.assertFalse(loaded.ask_on_found)
        self.assertEqual(loaded.ask_found_timeout_seconds, 60)

    def test_defaults_created_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.ini"
            cfg = app_config.load(path)
            self.assertTrue(path.exists())
        self.assertTrue(cfg.autostart_on_launch)
        self.assertEqual(cfg.results_ttl_minutes, 360)
        self.assertTrue(cfg.ask_on_found)
        self.assertEqual(cfg.ask_found_timeout_seconds, 45)


class LoggerTests(unittest.TestCase):
    def test_ring_handler_works_without_formatter(self):
        handler = app_logger._RingHandler()
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        with app_logger._lock:
            app_logger._ring.clear()
        handler.emit(record)  # must not raise AttributeError: asctime
        lines = app_logger.snapshot()
        self.assertTrue(lines and "hello world" in lines[-1])

    def test_subscribers_get_new_lines_only(self):
        seen: list[list[str]] = []
        cb = seen.append
        app_logger.subscribe(cb)
        try:
            handler = app_logger._RingHandler()
            handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "one", None, None))
            handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "two", None, None))
        finally:
            app_logger.unsubscribe(cb)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[0]), 1)


class DNSTests(unittest.TestCase):
    def test_build_query_and_parse_answers(self):
        q = resolver._build_query("example.com")
        self.assertEqual(q[12], 7)  # label length
        self.assertEqual(q[-4:], b"\x00\x01\x00\x01")  # qtype=A, qclass=IN

        # hand-built response: one A record 93.184.216.34
        header = b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
        question = b"\x07example\x03com\x00\x00\x01\x00\x01"
        answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22"
        self.assertEqual(resolver._parse_answers(header + question + answer), ["93.184.216.34"])

    def test_question_key(self):
        q = resolver._build_query("WWW.Example.COM")
        key = _question_key(q)
        self.assertIsNotNone(key)
        self.assertEqual(key[0], b"www.example.com")
        self.assertEqual(key[1], 1)
        self.assertIsNone(_question_key(b"\x00" * 5))


class I18nTests(unittest.TestCase):
    def test_literal_keys_exist_in_both_languages(self):
        import re
        from app import i18n
        used = set()
        for p in (ROOT / "app").rglob("*.py"):
            used |= set(re.findall(r'i18n\.t\("([^"]+)"', p.read_text(encoding="utf-8")))
        used = {k for k in used if not k.endswith(".")}  # prefixes built at runtime
        self.assertTrue(used)
        self.assertEqual(sorted(k for k in used if k not in i18n._RU), [])
        self.assertEqual(sorted(k for k in used if k not in i18n._EN), [])

    def test_diagnostic_kind_keys_exist(self):
        from app import diagnostics as dg, i18n
        kinds = (dg.KIND_OK, dg.KIND_DNS, dg.KIND_IP, dg.KIND_SNI, dg.KIND_TLS,
                 dg.KIND_STUB, dg.KIND_CERT, dg.KIND_NETWORK, dg.KIND_ERROR)
        for kind in kinds:
            for prefix in ("diag.verdict.", "diag.kind.", "diag.title."):
                self.assertIn(prefix + kind, i18n._RU)
                self.assertIn(prefix + kind, i18n._EN)


class HealthTests(unittest.TestCase):
    def _monitor(self, alive: bool):
        calls = []
        mon = HealthMonitor(interval_minutes=60, on_dead=lambda: calls.append(1),
                            is_alive=lambda: alive, alive_poll_s=0.02)
        return mon, calls

    def test_watchdog_calls_on_dead(self):
        mon, calls = self._monitor(False)
        snap = HealthSnapshot(1, 1, 10.0, time.time())
        with mock.patch.object(mon, "check_once", return_value=snap):
            mon.start()
            deadline = time.time() + 2.0
            while not calls and time.time() < deadline:
                time.sleep(0.02)
            mon.stop()
        self.assertTrue(calls, "watchdog did not request a restart")

    def test_watchdog_silent_when_alive(self):
        mon, calls = self._monitor(True)
        snap = HealthSnapshot(1, 1, 10.0, time.time())
        with mock.patch.object(mon, "check_once", return_value=snap):
            mon.start()
            time.sleep(0.15)
            mon.stop()
        self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()