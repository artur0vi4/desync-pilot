"""Helpers for winws.exe command lines.

Both the runtime (``app.strategies``) and the strategy parser
(``app.strategy_parser``) need to know which ``--flag=value`` options take a
filesystem path, and both need to repair lists that come out of the upstream
``.bat`` files (quoted values, empty game-filter placeholders). Keeping the
rules here means the parser and the runner can never disagree.
"""
from __future__ import annotations

# Exact flags whose value is a path that winws.exe must be able to open.
# NOTE: --wf-iface / --wf-l3 are deliberately NOT here: their values are an
# interface name and an IP version ("Ethernet", "ipv4"), and treating them as
# paths used to make preflight report "file not found: Ethernet".
PATH_FLAGS_EXACT = frozenset({
    "--ipset", "--ipset-exclude", "--ipset-list",
    "--hostlist", "--hostlist-exclude", "--hostlist-auto",
    "--hostlist-auto-exclude", "--hostlist-auto-debug",
    # --dpi-desync-split-seqovl-pattern also takes a .bin fake-packet file
    "--dpi-desync-split-seqovl-pattern",
})

PATH_FLAG_PREFIXES = (
    "--dpi-desync-fake",  # --dpi-desync-fake-tls, -http, -discord, -unknown-udp, ...
)

# ...but these members of the --dpi-desync-fake-* family take numbers/enums,
# never a file: --dpi-desync-fake-tls-mod=rnd,..., --dpi-desync-fake-len=0x...
FAKE_NON_PATH_SUFFIXES = ("-mod", "-len", "-skip", "-gen")

# Flags whose value is a comma-separated list of ports. winws.exe rejects
# "80,443," with "bad value for --wf-tcp", which is what an empty
# %GameFilterTCP% placeholder used to leave behind.
PORT_LIST_FLAGS = ("--wf-tcp", "--wf-udp", "--filter-tcp", "--filter-udp")


def is_path_flag(flag: str, val: str = "") -> bool:
    """True if `flag` (the part before `=`) takes a filesystem path.

    The --dpi-desync-fake-* family is overloaded: some members take a path to
    a .bin fake-packet file (--dpi-desync-fake-tls=<file.bin>), others take a
    numeric/enum value (--dpi-desync-fake-len=0x00000000,
    --dpi-desync-fake-skip=none, --dpi-desync-fake-tls-mod=rnd,dupsid,...).
    A --dpi-desync-fake-* flag counts as path-valued only when the value looks
    like a file.
    """
    if flag in PATH_FLAGS_EXACT:
        return True
    if any(flag.startswith(p) for p in PATH_FLAG_PREFIXES):
        if any(flag.endswith(s) for s in FAKE_NON_PATH_SUFFIXES):
            return False
        if not val:
            return False
        v = val.strip().strip('"').strip("'")
        if v.endswith(".bin"):
            return True
        if "%BIN%" in v or "%LISTS%" in v:
            return True
        if "\\" in v or "/" in v:
            return True
        if len(v) >= 2 and v[1] == ":" and v[0].isalpha():
            return True
        return False
    return False


def strip_value_quotes(tokens: list[str]) -> list[str]:
    """Drop surrounding quotes around path values.

    The .bat strategies quote paths (``--ipset-exclude="%LISTS%ipset-exclude.txt"``)
    because cmd.exe needs them; subprocess.Popen on Windows does NOT strip
    them, so winws.exe would receive ``"E:\\...\\ipset-exclude.txt"`` (quotes
    as part of the path) and fail with "cannot access ipset file".
    """
    out = list(tokens)
    for i, tok in enumerate(out):
        if "=" not in tok:
            continue
        flag, _, val = tok.partition("=")
        if not is_path_flag(flag, val):
            continue
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            out[i] = f"{flag}={val[1:-1]}"
    return out


def clean_port_lists(tokens: list[str]) -> list[str]:
    """Remove empty/duplicate commas from port lists left by empty placeholders."""
    out = list(tokens)
    for i, tok in enumerate(out):
        for flag in PORT_LIST_FLAGS:
            if tok.startswith(flag + "="):
                parts = [p for p in tok[len(flag) + 1:].split(",") if p.strip()]
                out[i] = f"{flag}={','.join(parts)}"
                break
    return out


def split_sections(tokens: list[str]) -> list[list[str]]:
    """Split a flat token list on the `--new` separator."""
    sections: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "--new":
            sections.append([])
        else:
            sections[-1].append(tok)
    return sections
