"""Check for new zapret releases on GitHub, download them and regenerate
strategies.json from the upstream .bat files.

Nothing is applied behind the user's back: the generated file is staged in the
user data dir and picked up on the next start.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __upstream_tag__, paths
from .logger import get as _log
from .strategy_parser import parse_dir, write_json

_log = _log()

GITHUB_API_LATEST = "https://api.github.com/repos/Flowseal/zapret-discord-youtube/releases/latest"
GITHUB_REPO = "Flowseal/zapret-discord-youtube"
USER_AGENT = "desync-pilot"
MAX_ZIP_BYTES = 200 * 1024 * 1024


@dataclass
class UpdateInfo:
    latest_tag: str
    current_tag: str
    has_update: bool
    zip_url: Optional[str] = None
    notes: str = ""
    asset_name: str = ""


def _tag_file() -> Path:
    return paths.user_data_dir() / "upstream_tag.txt"


def current_tag() -> str:
    """Tag of the strategies currently in use: the staged one if present."""
    try:
        staged = _tag_file().read_text(encoding="utf-8").strip()
        if staged:
            return staged
    except OSError:
        pass
    return __upstream_tag__


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})


def _latest_from_api() -> Optional[UpdateInfo]:
    with urllib.request.urlopen(_request(GITHUB_API_LATEST), timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    asset = next((a for a in data.get("assets") or [] if str(a.get("name", "")).lower().endswith(".zip")), None)
    return UpdateInfo(
        latest_tag=tag,
        current_tag=current_tag(),
        has_update=tag != current_tag(),
        zip_url=(asset or {}).get("browser_download_url"),
        notes=(data.get("body") or "")[:4000],
        asset_name=(asset or {}).get("name", ""),
    )


def _latest_from_redirect() -> Optional[UpdateInfo]:
    """Fallback when the API is rate-limited or blocked."""
    url = f"https://github.com/{GITHUB_REPO}/releases/latest"
    with urllib.request.urlopen(_request(url), timeout=15) as r:
        final = r.geturl()
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    if not tag or tag == "latest":
        return None
    return UpdateInfo(tag, current_tag(), tag != current_tag())


def fetch_latest_release() -> Optional[UpdateInfo]:
    """Resolve the latest release tag (and zip asset) without auth."""
    for attempt in (_latest_from_api, _latest_from_redirect):
        try:
            info = attempt()
            if info:
                return info
        except Exception as e:
            _log.debug("update check via %s failed: %s", attempt.__name__, e)
    _log.warning("update check failed")
    return None


def download_release_zip(info: UpdateInfo, dest: Path) -> Optional[Path]:
    """Download the upstream release zip, streaming to disk."""
    if not info.zip_url:
        info.zip_url = (f"https://github.com/{GITHUB_REPO}/releases/download/"
                        f"{info.latest_tag}/zapret-discord-youtube-{info.latest_tag}.zip")
    try:
        req = urllib.request.Request(info.zip_url, headers={"User-Agent": USER_AGENT})
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as fh:
            size = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ZIP_BYTES:
                    raise ValueError(f"release archive is larger than {MAX_ZIP_BYTES} bytes")
                fh.write(chunk)
        if not zipfile.is_zipfile(dest):
            raise ValueError("downloaded file is not a zip archive")
        _log.info("downloaded release %s -> %s (%d bytes)", info.latest_tag, dest, dest.stat().st_size)
        return dest
    except Exception as e:
        _log.warning("release download failed: %s", e)
        return None


def _safe_bat_members(zf: zipfile.ZipFile) -> list[str]:
    out = []
    for name in zf.namelist():
        base = Path(name).name
        if not base.lower().endswith(".bat"):
            continue
        if name.startswith("/") or ".." in Path(name).parts or ":" in name:
            continue  # never extract outside the temp dir
        out.append(name)
    return out


def stage_remote_strategies(zip_path: Path) -> bool:
    """Extract the .bat strategies from a release zip and regenerate the
    strategies JSON in the user data dir (used on the next start)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = _safe_bat_members(zf)
            if not members:
                _log.warning("no .bat files found in %s", zip_path)
                return False
            with tempfile.TemporaryDirectory() as td:
                for name in members:
                    target = Path(td) / Path(name).name
                    with zf.open(name) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                strategies = parse_dir(Path(td))
                if not strategies:
                    _log.warning("parser produced no strategies from %s", zip_path)
                    return False
                out = paths.updated_strategies_path()
                write_json(strategies, out)
                _log.info("staged %d strategies from release into %s", len(strategies), out)
                return True
    except Exception as e:
        _log.warning("stage_remote_strategies failed: %s", e)
        return False


def update_strategies(info: UpdateInfo) -> tuple[bool, str]:
    """Download the latest release and stage its strategies.

    Returns (ok, message). The message is a raw reason on failure; the caller
    localises the success text.
    """
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / f"zapret-{info.latest_tag}.zip"
        downloaded = download_release_zip(info, zip_path)
        if not downloaded:
            return False, "download failed"
        if not stage_remote_strategies(downloaded):
            return False, "could not parse the release"
    try:
        _tag_file().write_text(info.latest_tag, encoding="utf-8")
    except OSError as e:
        _log.warning("could not remember upstream tag: %s", e)
    return True, ""
