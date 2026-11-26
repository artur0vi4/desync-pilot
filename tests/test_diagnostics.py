"""Offline tests for the diagnostics classifier.

Network calls and TLS probes are mocked: the goal is to verify the decision
tree (DNS vs IP vs SNI vs stub vs cert), not to touch the network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import autostart, diagnostics as dg  # noqa: E402


class ProbeClassificationTests(unittest.TestCase):
    def _run(self, sys_ips, doh_ips, tls, control=True, stub="", verify="ok"):
        """tls(ip, sni, verify) -> state string."""

        def fake_tls(ip, sni, timeout=5.0, verify=False):
            return tls(ip, sni, verify), 10.0, ""

        with mock.patch.object(dg, "resolve_system", return_value=sys_ips), \
             mock.patch.object(dg, "_doh_ips", return_value=doh_ips), \
             mock.patch.object(dg, "_tls_probe", fake_tls), \
             mock.patch.object(dg, "_control_ok", return_value=control), \
             mock.patch.object(dg, "_stub_reason", return_value=stub):
            return dg.probe_host("discord.com")

    def test_ok(self):
        probe = self._run(["1.2.3.4"], ["1.2.3.4"], lambda ip, sni, verify: "ok")
        self.assertEqual(probe.kind, dg.KIND_OK)

    def test_dns_spoofing(self):
        def tls(ip, sni, verify):
            return "ok" if ip == "5.6.7.8" else "tcp_fail"
        probe = self._run(["10.0.0.1"], ["5.6.7.8"], tls)
        self.assertEqual(probe.kind, dg.KIND_DNS)

    def test_sni_block(self):
        def tls(ip, sni, verify):
            return "reset" if sni == "discord.com" else "ok"
        probe = self._run(["1.2.3.4"], ["1.2.3.4"], tls)
        self.assertEqual(probe.kind, dg.KIND_SNI)

    def test_tls_block_for_any_name(self):
        def tls(ip, sni, verify):
            return "reset"
        probe = self._run(["1.2.3.4"], ["1.2.3.4"], tls)
        self.assertEqual(probe.kind, dg.KIND_TLS)

    def test_ip_block(self):
        probe = self._run(["1.2.3.4"], ["5.6.7.8"], lambda ip, sni, verify: "tcp_fail", control=True)
        self.assertEqual(probe.kind, dg.KIND_IP)

    def test_no_internet(self):
        probe = self._run([], [], lambda ip, sni, verify: "tcp_fail", control=False)
        self.assertEqual(probe.kind, dg.KIND_NETWORK)

    def test_cert(self):
        def tls(ip, sni, verify):
            return "cert" if verify else "ok"
        probe = self._run(["1.2.3.4"], ["1.2.3.4"], tls)
        self.assertEqual(probe.kind, dg.KIND_CERT)

    def test_stub_page(self):
        probe = self._run(["1.2.3.4"], ["1.2.3.4"], lambda ip, sni, verify: "ok", stub="HTTP 403")
        self.assertEqual(probe.kind, dg.KIND_STUB)
        self.assertIn("403", probe.note)


class VerdictTests(unittest.TestCase):
    def _probe(self, kind):
        return dg.HostProbe(host="x", kind=kind)

    def test_priority(self):
        verdict, rec = dg._verdict([self._probe(dg.KIND_SNI), self._probe(dg.KIND_IP)])
        self.assertEqual(verdict, dg.KIND_IP)
        self.assertEqual(rec, [])

    def test_sni_recommends_strategies(self):
        verdict, rec = dg._verdict([self._probe(dg.KIND_SNI), self._probe(dg.KIND_OK)])
        self.assertEqual(verdict, dg.KIND_SNI)
        self.assertIn("general (ALT2)", rec)

    def test_all_ok(self):
        verdict, rec = dg._verdict([self._probe(dg.KIND_OK), self._probe(dg.KIND_OK)])
        self.assertEqual(verdict, dg.KIND_OK)
        self.assertEqual(rec, [])


class CheckStatusTests(unittest.TestCase):
    def test_host_check_statuses(self):
        cases = {
            dg.KIND_OK: dg.OK,
            dg.KIND_SNI: dg.WARN,
            dg.KIND_IP: dg.FAIL,
            dg.KIND_NETWORK: dg.FAIL,
            dg.KIND_CERT: dg.FAIL,
            dg.KIND_ERROR: dg.INFO,
        }
        for kind, expected in cases.items():
            check = dg._host_check(dg.HostProbe(host="h", kind=kind, note="note"))
            self.assertEqual(check.status, expected, kind)

    def test_report_text_contains_checks(self):
        report = dg.DiagnosticReport(
            checks=[dg.Check("k", dg.OK, "title", "detail", "hint"), dg.Check("k2", dg.WARN, "t2")],
            verdict=dg.KIND_SNI, recommended=["general"], at=0.0,
        )
        text = dg.report_as_text(report)
        self.assertIn("title", text)
        self.assertIn("general", text)


class AutostartTests(unittest.TestCase):
    def test_command_line_has_minimized(self):
        cmd = autostart.command_line()
        self.assertIn("--minimized", cmd)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_toggle_is_idempotent(self):
        # Read the current state, flip it, flip it back: never leave the
        # machine with an unexpected Run entry. Hosted CI images forbid
        # HKCU Run writes, so a failed write means "cannot test here",
        # not a product bug; the toggle keeps full coverage on desktops.
        before = autostart.is_enabled()
        if not autostart.set_enabled(True):
            self.skipTest("HKCU Run writes are not available on this machine")
        try:
            self.assertTrue(autostart.is_enabled())
            self.assertTrue(autostart.set_enabled(False))
            self.assertFalse(autostart.is_enabled())
        finally:
            autostart.set_enabled(before)


if __name__ == "__main__":
    unittest.main()
