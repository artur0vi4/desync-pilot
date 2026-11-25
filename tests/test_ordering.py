"""Tests for the diversity-first scan order and strategy signatures."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategies import Strategy, diverse_order, strategy_signature  # noqa: E402


def _strategy(sid: str, sections: list[list[str]]) -> Strategy:
    return Strategy(id=sid, file=f"{sid}.bat", title=sid, group="community",
                    sections=sections, modes=[], raw_args=[t for sec in sections for t in sec])


def _sec(methods: str, link: str = "tcp") -> list[str]:
    return [f"--filter-{link}=443", f"--dpi-desync={methods}",
            "--dpi-desync-fake-tls=bin/tls.bin"]


class SignatureTests(unittest.TestCase):
    def test_same_methods_same_signature_regardless_of_payload(self):
        a = _strategy("a", [_sec("fake,split")])
        b = _strategy("b", [_sec("fake,split"),
                            ["--filter-tcp=443", "--dpi-desync=fake,split",
                             "--dpi-desync-fake-tls=bin/other.bin"]])
        self.assertEqual(strategy_signature(a), strategy_signature(b))

    def test_different_methods_different_signature(self):
        a = _strategy("a", [_sec("fake,split")])
        b = _strategy("b", [_sec("fake,disorder")])
        self.assertNotEqual(strategy_signature(a), strategy_signature(b))

    def test_link_type_is_part_of_the_signature(self):
        a = _strategy("a", [_sec("fake", link="tcp")])
        b = _strategy("b", [_sec("fake", link="udp")])
        self.assertNotEqual(strategy_signature(a), strategy_signature(b))

    def test_signature_ignores_hostlists_and_ports(self):
        a = _strategy("a", [["--wf-tcp=80,443", "--hostlist=lists/one.txt", "--dpi-desync=fake"]])
        b = _strategy("b", [["--wf-tcp=443,5443", "--hostlist=lists/two.txt", "--dpi-desync=fake"]])
        self.assertEqual(strategy_signature(a), strategy_signature(b))


class DiverseOrderTests(unittest.TestCase):
    def _catalog(self) -> list[Strategy]:
        return [
            _strategy("f1", [_sec("fake")]),
            _strategy("f2", [_sec("fake")]),            # variant of f1
            _strategy("f3", [_sec("fake")]),            # another variant
            _strategy("s1", [_sec("split,split2")]),
            _strategy("d1", [_sec("disorder")]),
            _strategy("u1", [_sec("fake", link="udp")]),
        ]

    def test_first_pass_covers_every_signature(self):
        catalog = self._catalog()
        sigs = {strategy_signature(s) for s in catalog}
        self.assertEqual(len(sigs), 4)  # f1..f3 share one signature
        ordered = diverse_order(catalog)
        seen: set = set()
        first_cover = 0
        for i, s in enumerate(ordered, 1):
            seen.add(strategy_signature(s))
            if len(seen) == len(sigs):
                first_cover = i
                break
        # A diversity-first order reaches the last signature in 4 tries; the
        # plain catalog order would need all 6 (f1->f2->f3 variants first).
        self.assertEqual(first_cover, 4)
        self.assertEqual(len(ordered), len(catalog))

    def test_no_duplicates_in_order(self):
        ordered = diverse_order(self._catalog())
        ids = [s.id for s in ordered]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cached_scores_are_boosted_to_front(self):
        catalog = self._catalog()
        ordered = diverse_order(catalog, scores={"d1": 80.0, "f2": 95.0})
        self.assertEqual([s.id for s in ordered[:2]], ["f2", "d1"])  # score desc
        # the boost must not drop anyone
        self.assertEqual(sorted(s.id for s in ordered), sorted(s.id for s in catalog))

    def test_zero_scores_are_not_boosted(self):
        ordered = diverse_order(self._catalog(), scores={"s1": 0.0})
        self.assertNotEqual(ordered[0].id, "s1")

    def test_empty_catalog(self):
        self.assertEqual(diverse_order([]), [])


if __name__ == "__main__":
    unittest.main()
