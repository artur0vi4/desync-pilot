"""Tests for the community preset converter (zapret-gui -> our JSON)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import paths  # noqa: E402
from app.preset_parser import group_of, parse_preset, title_of  # noqa: E402
from app.strategies import load_strategies  # noqa: E402

SAMPLE = """# Preset: alt1
# BuiltinVersion: 1.5
# IconColor: #60cdff
# Description: Imported from bat/alt1.txt

--wf-tcp=80,443
--wf-udp=443
--comment=Discord · UDP (обычно не нужно)
--filter-udp=443
--hostlist=lists/discord.txt
--dpi-desync=fake
--dpi-desync-fake-quic=bin/quic_5.bin

--new

--filter-tcp=443
--hostlist=lists/discord.txt
--dpi-desync=fake,split2
--dpi-desync-fake-tls=bin/tls_clienthello_3.bin
"""


class PresetParserTests(unittest.TestCase):
    def _parse(self, text: str, stem: str = "alt1"):
        return parse_preset(
            text, stem,
            bin_dir=paths.bin_dir(), lists_dir=paths.lists_dir(),
            supported_flags={"--wf-tcp", "--wf-udp", "--filter-tcp", "--filter-udp",
                             "--hostlist", "--dpi-desync", "--dpi-desync-fake-quic",
                             "--dpi-desync-fake-tls", "--comment", "--new", "--wssize",
                             "--ipset-exclude-ip", "--hostlist-auto"},
        )

    def test_basic_conversion(self):
        preset = self._parse(SAMPLE)
        self.assertIsNotNone(preset)
        self.assertEqual(preset.title, "alt1")
        self.assertEqual(preset.group, "alt")
        self.assertEqual(preset.version, "1.5")
        self.assertNotIn("--comment=Discord · UDP (обычно не нужно)", preset.raw_args)
        # service list -> our general list, unknown fake -> our google hello
        self.assertIn("--hostlist=lists/list-general.txt", preset.raw_args)
        self.assertIn("--dpi-desync-fake-quic=bin/quic_initial_www_google_com.bin", preset.raw_args)
        self.assertIn("--dpi-desync-fake-tls=bin/tls_clienthello_www_google_com.bin", preset.raw_args)
        self.assertIn("--new", preset.raw_args)

    def test_duplicate_sections_collapse(self):
        text = (
            "--filter-tcp=443\n--hostlist=lists/discord.txt\n--dpi-desync=fake\n"
            "--new\n"
            "--filter-tcp=443\n--hostlist=lists/youtube.txt\n--dpi-desync=fake\n"
        )
        preset = self._parse(text)
        self.assertEqual(len(preset.sections), 1)

    def test_multiline_value_with_comma(self):
        text = (
            "--filter-tcp=443\n--ipset-exclude-ip=1.1.1.1,\n1.0.0.1,\n212.109.195.93\n"
            "--dpi-desync=fake\n"
        )
        preset = self._parse(text)
        self.assertIn("--ipset-exclude-ip=1.1.1.1,1.0.0.1,212.109.195.93", preset.raw_args)

    def test_bare_flag_value_on_next_line(self):
        text = "--filter-tcp=443\n--wssize\n1:6\n--dpi-desync=fake\n"
        preset = self._parse(text)
        self.assertIn("--wssize=1:6", preset.raw_args)

    def test_single_dash_typo_repaired(self):
        text = "--filter-tcp=443\n-dpi-desync-fake-tls=tls_clienthello_18.bin\n"
        preset = self._parse(text)
        self.assertIn("--dpi-desync-fake-tls=bin/tls_clienthello_www_google_com.bin", preset.raw_args)

    def test_unknown_flag_drops_preset(self):
        text = "--filter-tcp=443\n--totally-unknown-flag=1\n"
        self.assertIsNone(self._parse(text))

    def test_hostlist_auto_moves_to_data_dir(self):
        text = "--filter-tcp=443\n--hostlist-auto=autohostlist.txt\n--dpi-desync=fake\n"
        preset = self._parse(text)
        self.assertIn("--hostlist-auto=%DATA%hostlist-auto.txt", preset.raw_args)

    def test_group_and_title_helpers(self):
        self.assertEqual(group_of("general ALT2 1.10.3 (game filter)"), "game")
        self.assertEqual(group_of("general (FAKE TLS AUTO)"), "fake_tls_auto")
        self.assertEqual(group_of("general (SIMPLE FAKE ALT)"), "simple_fake")
        self.assertEqual(group_of("general (ALT3)"), "alt")
        self.assertEqual(group_of("general"), "base")
        self.assertEqual(group_of("YTDisBystro_34_1"), "community")
        self.assertEqual(title_of("general 1.10.3 (game filter)"), "general 1.10.3 · game")


class CatalogTests(unittest.TestCase):
    def test_catalog_loads_and_dedupes(self):
        strategies = load_strategies()
        self.assertGreaterEqual(len(strategies), 180, "community catalog was not merged")
        self.assertEqual(strategies[0].source, "upstream")  # base strategies come first
        keys = [(s.id) for s in strategies]
        self.assertEqual(len(keys), len(set(keys)), "duplicate strategy ids")
        self.assertTrue(any(s.source == "zapret-gui" for s in strategies))
        self.assertTrue(any(s.id == "gui/alt1" for s in strategies))
        # the two duplicates from the catalog must be dropped
        raws = [tuple(s.raw_args) for s in strategies]
        self.assertEqual(len(raws), len(set(raws)))


if __name__ == "__main__":
    unittest.main()
