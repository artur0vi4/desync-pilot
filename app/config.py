"""User configuration persisted to config.ini next to the executable."""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import paths
from .logger import get as _log

_log = _log()

SECTION = "desyncpilot"
LEGACY_SECTION = "zapret"  # configs written before the rename


@dataclass
class Config:
    # general
    language: str = "ru"
    start_minimized: bool = False
    show_toasts: bool = True
    autostart_on_launch: bool = True  # app starts zapret immediately on launch
    autostart_windows: bool = False   # register in HKCU\...\Run at logon
    recheck_every_minutes: int = 30
    # dpi bypass
    auto_mode: bool = True
    preferred_strategy: str = ""  # "" = auto
    quick_scan: bool = True  # cheap canary pass instead of a full sweep on start
    quick_scan_limit: int = 40  # how many presets the quick pass may try
    skip_groups: list = field(default_factory=lambda: ["experimental"])
    test_timeout_seconds: int = 8
    test_attempts: int = 2
    # how long a cached sweep result may be reused on the next launch
    # (0 = always re-test on start)
    results_ttl_minutes: int = 360
    # dns
    dns_mode: str = "hybrid"  # hybrid | doh_only | system
    dns_listen_port: int = 5354
    doh_provider: str = "cloudflare"  # auto from servers.py if "auto"
    doh_custom_url: str = ""          # https://... custom DoH endpoint
    system_dns_fallback: str = "1.1.1.1"
    # updater
    auto_update_strategies: bool = True
    upstream_release_url: str = "https://github.com/Flowseal/zapret-discord-youtube/releases/latest"
    # ui
    theme: str = "dark"
    log_export_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if isinstance(v, bool):
                out[k] = "true" if v else "false"
            elif isinstance(v, list):
                out[k] = ",".join(v)
            else:
                out[k] = v
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Config":
        c = cls()
        for k, v in d.items():
            if k == "skip_groups" and isinstance(v, str):
                c.skip_groups = [s for s in v.split(",") if s]
            elif hasattr(c, k):
                # coerce to the type of the default value
                default = getattr(c, k)
                if isinstance(default, bool):
                    c.__dict__[k] = v in (True, "true", "True", "1", 1)
                elif isinstance(default, int) and not isinstance(default, bool):
                    try:
                        c.__dict__[k] = int(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    c.__dict__[k] = v
        return c


def _default_path() -> Path:
    return paths.config_path()


def load(path: Path | None = None) -> Config:
    path = path or _default_path()
    if not path.exists():
        cfg = Config()
        save(cfg, path)
        return cfg
    p = configparser.ConfigParser()
    try:
        p.read(path, encoding="utf-8")
    except Exception as e:
        _log.warning("config read failed: %s, using defaults", e)
        return Config()
    section = p[SECTION] if SECTION in p else (p[LEGACY_SECTION] if LEGACY_SECTION in p else {})
    d: dict[str, Any] = {}
    for k, v in section.items():
        if v in ("true", "false"):
            d[k] = v == "true"
        elif v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            d[k] = int(v)
        else:
            d[k] = v
    return Config.from_dict(d)


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or _default_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        p = configparser.ConfigParser()
        p[SECTION] = cfg.to_dict()
        with path.open("w", encoding="utf-8") as f:
            p.write(f)
    except Exception as e:
        _log.warning("config save failed: %s", e)
