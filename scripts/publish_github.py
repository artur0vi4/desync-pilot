#!/usr/bin/env python3
"""Publish the working tree to GitHub through the Git Data API.

Why: on some networks ``github.com:443`` is unreachable (git push fails at
connect), while ``api.github.com`` still works. This script uploads the current
files as blobs, builds a tree and a commit, moves the branch and (optionally)
creates the tag that triggers the release workflow.

Auth: the token is taken from the configured git credential helper (Git
Credential Manager), so nothing secret ever appears on the command line.
The token needs the ``repo`` scope.

Usage:
    python scripts/publish_github.py --owner artur0vi4 --repo desync-pilot \
        --message "Desync Pilot 1.3.1" --tag v1.3.1
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".presets-cache", ".idea", ".vscode"}
SKIP_FILES = {"config.ini"}
API = "https://api.github.com"


def read_token(host: str = "github.com") -> str:
    env = dict(os.environ, GCM_INTERACTIVE="never", GIT_TERMINAL_PROMPT="0")
    out = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    creds = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    token = creds.get("password", "")
    if not token:
        raise SystemExit("no stored GitHub credentials; run `git push` once or configure GCM")
    return token


class Client:
    def __init__(self, token: str, owner: str, repo: str) -> None:
        self.token = token
        self.owner = owner
        self.repo = repo

    def call(self, path: str, method: str = "GET", data=None, attempts: int = 8, timeout: float = 60.0):
        payload = json.dumps(data).encode() if data is not None else None
        last = None
        for i in range(attempts):
            req = urllib.request.Request(
                API + path, method=method, data=payload,
                headers={"Authorization": "Bearer " + self.token,
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "desync-pilot-publish",
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code >= 500 or e.code == 429:
                    last = f"HTTP {e.code}"
                    time.sleep(1.0 + i)
                    continue
                return e.code, body
            except Exception as e:  # timeouts, resets — the network is flaky
                last = f"{type(e).__name__}: {e}"
                time.sleep(1.0 + i)
        return 0, last

    def repo_path(self, suffix: str = "") -> str:
        return f"/repos/{self.owner}/{self.repo}{suffix}"


def collect_files() -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES or name.endswith((".pyc", ".pyo", ".log")):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(ROOT).as_posix()
            files.append((rel, full))
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--message", default="Publish working tree")
    ap.add_argument("--tag", help="also create this tag (triggers the release workflow)")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    client = Client(read_token(), args.owner, args.repo)
    files = collect_files()
    print(f"files to upload: {len(files)}")

    def upload(item: tuple[str, Path]):
        rel, full = item
        st, body = client.call(client.repo_path("/git/blobs"), "POST",
                               {"content": base64.b64encode(full.read_bytes()).decode(),
                                "encoding": "base64"}, timeout=120)
        if st != 201:
            return rel, None, f"{st} {body}"
        return rel, body["sha"], None

    shas: dict[str, str] = {}
    errors: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(upload, f) for f in files]
        for fut in as_completed(futures):
            rel, sha, err = fut.result()
            if err:
                errors.append((rel, err))
            else:
                shas[rel] = sha
    if errors:
        for rel, err in errors[:5]:
            print(f"  upload failed: {rel}: {err}", file=sys.stderr)
        return 1

    tree = [{"path": rel, "mode": "100644", "type": "blob", "sha": sha} for rel, sha in shas.items()]
    st, body = client.call(client.repo_path("/git/trees"), "POST", {"tree": tree})
    if st != 201:
        print(f"tree failed: {st} {body}", file=sys.stderr)
        return 1
    tree_sha = body["sha"]

    st, ref = client.call(client.repo_path(f"/git/ref/heads/{args.branch}"))
    parent = ref["object"]["sha"] if st == 200 else None

    st, commit = client.call(client.repo_path("/git/commits"), "POST",
                             {"message": args.message, "tree": tree_sha,
                              "parents": [parent] if parent else []})
    if st != 201:
        print(f"commit failed: {st} {commit}", file=sys.stderr)
        return 1
    commit_sha = commit["sha"]
    print("commit:", commit_sha[:10])

    method = "PATCH" if parent else "POST"
    path = client.repo_path(f"/git/refs/heads/{args.branch}") if parent else client.repo_path("/git/refs")
    payload = {"sha": commit_sha, "force": False} if parent else {"ref": f"refs/heads/{args.branch}", "sha": commit_sha}
    st, body = client.call(path, method, payload)
    print("branch:", st, args.branch)
    if st not in (200, 201):
        print(body, file=sys.stderr)
        return 1

    if args.tag:
        st, body = client.call(client.repo_path("/git/refs"), "POST",
                               {"ref": f"refs/tags/{args.tag}", "sha": commit_sha})
        print("tag:", st, args.tag)
        if st != 201:
            print(body, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
