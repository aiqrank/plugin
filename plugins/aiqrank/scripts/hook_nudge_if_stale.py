#!/usr/bin/env python3
"""SessionStart staleness + version nudges.

Prints up to two lines on stdout:
  * 30-day staleness nudge when the last upload is missing or > 30 days old.
  * Plugin-update nudge when ~/.config/aiqrank/stale_version exists (written
    by hook_upload_today.py based on the server's latest_plugin_version).
Silent otherwise. Always exits 0.

Python stdlib only. Must be fast (~5ms).
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "aiqrank"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"
STALE_VERSION_PATH = CONFIG_DIR / "stale_version"
STALE_SECONDS = 30 * 24 * 60 * 60
# Defence in depth — even though hook_upload_today.py validates before
# writing, the file on disk is server-controlled. Refuse to print anything
# that doesn't match a strict semver-like shape.
_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}(-[A-Za-z0-9.]+)?$")
_VERSION_MAX_LEN = 32
NUDGE = "AIQ Rank: it's been 30 days — run /aiqrank to refresh your rank."
VERSION_NUDGE_FMT = (
    "AIQ Rank: plugin update available (v{latest}). "
    "Run: claude plugin marketplace update aiqrank && claude plugin update aiqrank@aiqrank"
)


def main() -> int:
    try:
        if _is_stale():
            print(NUDGE)
        latest = _read_stale_version()
        if latest:
            print(VERSION_NUDGE_FMT.format(latest=latest))
    except Exception:
        pass
    return 0


def _read_stale_version() -> str | None:
    if not STALE_VERSION_PATH.exists():
        return None
    try:
        v = STALE_VERSION_PATH.read_text(errors="replace").strip()
    except OSError:
        return None
    if not v or len(v) > _VERSION_MAX_LEN or _VERSION_RE.match(v) is None:
        return None
    return v


def _is_stale() -> bool:
    if not LAST_UPLOAD_PATH.exists():
        return True

    raw = LAST_UPLOAD_PATH.read_text().strip()
    if not raw:
        return True

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    age = time.time() - dt.timestamp()
    return age > STALE_SECONDS


if __name__ == "__main__":
    sys.exit(main())
