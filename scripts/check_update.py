#!/usr/bin/env python3
"""Notify at session start when a newer prod-readiness release is published.

Claude Code cannot hot-swap a plugin inside a running session (`claude plugin
update` requires a restart), so this hook only reports that an update exists.
It never blocks the session: any failure, timeout, or malformed response exits
quietly with status 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Taimoorkhan1122/prod-readiness"
    "/main/.claude-plugin/plugin.json"
)
PLUGIN_NAME = "prod-readiness"
CACHE_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 2.0
OPT_OUT_ENV = "PROD_READINESS_NO_UPDATE_CHECK"

VERSION_PART = re.compile(r"\d+")


def cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "prod-readiness" / "update-check.json"


def plugin_root() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[1]


def read_local_version() -> str | None:
    manifest = plugin_root() / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


def read_cached_version(now: float) -> str | None:
    try:
        cached = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    checked_at = cached.get("checked_at")
    version = cached.get("latest_version")
    if not isinstance(checked_at, (int, float)) or not isinstance(version, str):
        return None
    if now - checked_at >= CACHE_TTL_SECONDS:
        return None
    return version


def write_cache(version: str, now: float) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked_at": now, "latest_version": version}),
            encoding="utf-8",
        )
    except OSError:
        pass


def fetch_upstream_version() -> str | None:
    request = urllib.request.Request(
        UPSTREAM_MANIFEST_URL,
        headers={"User-Agent": f"{PLUGIN_NAME}-update-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = response.read(64_000).decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in VERSION_PART.findall(version)[:4])


def is_newer(latest: str, local: str) -> bool:
    latest_key, local_key = version_key(latest), version_key(local)
    if not latest_key or not local_key:
        return False
    return latest_key > local_key


def notice(local: str, latest: str) -> str:
    return (
        f"A newer {PLUGIN_NAME} release is available: {latest} (installed {local}). "
        f"Tell the user they can update with `claude plugin update {PLUGIN_NAME}` "
        "and restart Claude Code to apply it; the running session keeps using "
        f"{local}. Mention this once, then continue with their request. "
        f"They can silence this check by setting {OPT_OUT_ENV}=1."
    )


def main() -> int:
    if os.environ.get(OPT_OUT_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return 0

    local = read_local_version()
    if local is None:
        return 0

    now = time.time()
    latest = read_cached_version(now)
    if latest is None:
        latest = fetch_upstream_version()
        if latest is None:
            return 0
        write_cache(latest, now)

    if not is_newer(latest, local):
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": notice(local, latest),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # never break the session over an update check
        raise SystemExit(0)
