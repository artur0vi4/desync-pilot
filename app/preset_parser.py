"""Convert community preset files (zapret-gui format) into our strategy JSON.

Their presets are plain text: one argv token per line, ``#`` comments for
metadata, ``--new`` between filter sections, ``bin/...`` and ``lists/...``
relative paths. Our runner needs absolute-ish paths and our own JSON schema,
so this module parses a preset and rewrites the referenced assets to files
that ship with Desync Pilot.

Everything here is pure: no network, no Qt, no subprocess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Files we do not ship are replaced by the closest asset of the same protocol.
# The desync structure (split positions, TTL, fooling, repeats) is preserved;
# only the fake packet payload changes.
BIN_SUBSTITUTES = {
    "--dpi-desync-fake-tls": "tls_clienthello_www_google_com.bin",
    "--dpi-desync-fake-http": "tls_clienthello_www_sferum_ru.bin",
    "--dpi-desync-fake-quic": "quic_initial_www_google_com.bin",
    "--dpi-desync-fake-unknown-udp": "quic_initial_www_google_com.bin",
    "--dpi-desync-fake-discord": "ACTIVE_DISCORD_UDP.bin",
    "--dpi-desync-fake-stun": "ACTIVE_DISCORD_UDP.bin",
    "--dpi-desync-fake-syndata": "stun.bin",
    "--dpi-desync-split-seqovl-pattern": "tls_clienthello_www_google_com.bin",
    "--dpi-desync-fakedsplit-pattern": "tls_clienthello_www_google_com.bin",
}
BIN_DEFAULT = "tls_clienthello_www_google_com.bin"

# Service lists from their bundle map onto the lists we ship. A preset that
# separates "Discord" and "YouTube" sections therefore applies the same desync
# to all blocked domains — the per-service tuning is lost, the mechanism is not.
LIST_SUBSTITUTES = {
    "--hostlist": "list-general.txt",
    "--hostlist-exclude": "list-exclude.txt",
    "--ipset": "ipset-all.txt",
    "--ipset-exclude": "ipset-exclude.txt",
}
LIST_DEFAULT = "list-general.txt"

META_RE = re.compile(r"^#\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")


@dataclass
class Preset:
    stem: str
    title: str
    group: str
    version: str = ""
    description: str = ""
    sections: list[list[str]] = field(default_factory=list)
    substitutions: list[str] = field(default_factory=list)

    @property
    def raw_args(self) -> list[str]:
        out: list[str] = []
        for i, sec in enumerate(self.sections):
            if i:
                out.append("--new")
            out.extend(sec)
        return out

    @property
    def modes(self) -> list[str]:
        modes: list[str] = []
        for sec in self.sections:
            mode = next((t.split("=", 1)[1] for t in sec if t.startswith("--dpi-desync=")), None)
            if mode:
                modes.append(mode)
        return modes

    @property
    def id(self) -> str:
        return f"gui/{self.stem}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file": f"{self.stem}.txt",
            "title": self.title,
            "group": self.group,
            "sections": self.sections,
            "modes": self.modes,
            "raw_args": self.raw_args,
            "source": "zapret-gui",
            "note": self.description or f"zapret-gui preset {self.version}".strip(),
        }


def group_of(stem: str) -> str:
    s = stem.casefold()
    if any(k in s for k in ("game filter", "valorant", "riot", "dead by daylight", " lol")):
        return "game"
    if "general" in s or s.startswith(("alt", "general")):
        if "fake tls auto" in s or "faketls" in s:
            return "fake_tls_auto"
        if "simple fake" in s or "simplefake" in s:
            return "simple_fake"
        if "exp" in s:
            return "experimental"
        if "alt" in s:
            return "alt"
        return "base"
    return "community"


def title_of(stem: str) -> str:
    t = re.sub(r"\s+", " ", stem).strip()
    t = t.replace(" (game filter)", " · game")
    return t


def _read_meta(line: str) -> Optional[tuple[str, str]]:
    m = META_RE.match(line)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _map_bin(value: str, flag: str, available: set[str]) -> tuple[str, Optional[str]]:
    name = value[4:]  # strip "bin/"
    if name.lower() in available:
        return value, None
    replacement = BIN_SUBSTITUTES.get(flag, BIN_DEFAULT)
    return f"bin/{replacement}", f"{name} -> {replacement}"


def _map_list(value: str, flag: str, available: set[str]) -> tuple[str, Optional[str]]:
    name = value[6:]  # strip "lists/"
    if name.lower() in available:
        return value, None
    if flag == "--hostlist-auto":
        # winws creates this file itself; keep it in the writable data dir.
        return "%DATA%hostlist-auto.txt", f"{name} -> %DATA%hostlist-auto.txt"
    replacement = LIST_SUBSTITUTES.get(flag, LIST_DEFAULT)
    return f"lists/{replacement}", f"{name} -> {replacement}"


def convert_tokens(tokens: Iterable[str], bin_dir: Optional[Path] = None,
                   lists_dir: Optional[Path] = None,
                   supported_flags: Optional[set[str]] = None) -> tuple[list[str], list[str], list[str]]:
    """Rewrite asset paths and drop annotations.

    Returns (tokens, substitutions, unsupported_flags).
    """
    available_bin = {p.name.lower() for p in bin_dir.iterdir()} if bin_dir and bin_dir.is_dir() else set()
    available_lists = {p.name.lower() for p in lists_dir.iterdir()} if lists_dir and lists_dir.is_dir() else set()
    out: list[str] = []
    substitutions: list[str] = []
    unsupported: list[str] = []
    for tok in tokens:
        if tok.startswith("--comment="):
            continue  # purely informational, bloats the JSON
        flag, eq, value = tok.partition("=")
        if supported_flags is not None and flag not in supported_flags:
            unsupported.append(flag)
            continue
        if flag == "--hostlist-auto":
            # winws creates this file itself; keep it in the writable data dir.
            out.append("--hostlist-auto=%DATA%hostlist-auto.txt")
            substitutions.append(f"{value} -> %DATA%hostlist-auto.txt")
            continue
        if eq and value.startswith("bin/"):
            value, sub = _map_bin(value, flag, available_bin)
            if sub:
                substitutions.append(sub)
        elif eq and value.startswith("lists/"):
            value, sub = _map_list(value, flag, available_lists)
            if sub:
                substitutions.append(sub)
        out.append(f"{flag}={value}" if eq else tok)
    return out, substitutions, unsupported


def _tokenize(lines: list[str], supported_flags: Optional[set[str]]) -> list[str]:
    """Turn preset lines into argv tokens.

    Handles two quirks of the community catalog:
      * a value may continue on the following lines (``--ipset-exclude-ip=``
        followed by a list of addresses, ``--wssize=`` followed by ``1:6``);
      * one file has a single-dash typo (``-dpi-desync-fake-tls=...``) and a
        bare .bin name without the ``bin/`` prefix.
    """
    tokens: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i]
        # A bare flag followed by its value on the next line (--wssize / 1:6).
        if s.startswith("--") and "=" not in s and i + 1 < len(lines) \
                and not lines[i + 1].startswith("--"):
            s = f"{s}={lines[i + 1].strip()}"
            i += 1
        # A value that continues on the next lines (ends with "=" or a comma).
        while "=" in s and (s.endswith("=") or s.endswith(",")) and i + 1 < len(lines) \
                and not lines[i + 1].startswith("--"):
            nxt = lines[i + 1].strip()
            s = s + ("" if s.endswith(("=", ",")) else ",") + nxt
            i += 1
        if s.startswith("-") and not s.startswith("--"):
            candidate = "-" + s
            flag = candidate.split("=", 1)[0]
            if supported_flags and flag in supported_flags:
                s = candidate
        flag, eq, value = s.partition("=")
        if eq and value.endswith(".bin") and "/" not in value and flag.startswith("--dpi-desync-fake"):
            s = f"{flag}=bin/{value}"
        tokens.append(s)
        i += 1
    return tokens


def parse_preset(text: str, stem: str, *, bin_dir: Optional[Path] = None,
                 lists_dir: Optional[Path] = None,
                 supported_flags: Optional[set[str]] = None) -> Optional[Preset]:
    """Parse one preset file. Returns None when it has no usable sections."""
    meta: dict[str, str] = {}
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            parsed = _read_meta(s)
            if parsed:
                meta[parsed[0].casefold()] = parsed[1]
            continue
        lines.append(s)

    tokens = _tokenize(lines, supported_flags)
    tokens, substitutions, unsupported = convert_tokens(tokens, bin_dir, lists_dir, supported_flags)
    if unsupported or not tokens:
        return None

    # Split into sections, drop duplicates that appear after list remapping.
    sections: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "--new":
            sections.append([])
        else:
            sections[-1].append(tok)
    cleaned: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for sec in sections:
        if not sec:
            continue
        key = tuple(sec)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(sec)
    if not cleaned:
        return None

    return Preset(
        stem=stem,
        title=meta.get("preset") or title_of(stem),
        group=group_of(stem),
        version=meta.get("builtinversion", ""),
        description=meta.get("description", ""),
        sections=cleaned,
        substitutions=substitutions,
    )


def parse_preset_file(path: Path, **kwargs) -> Optional[Preset]:
    return parse_preset(path.read_text(encoding="utf-8", errors="replace"), path.stem, **kwargs)
