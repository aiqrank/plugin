#!/usr/bin/env python3
"""30-day staleness nudge — SessionStart hook.

Prints exactly one line to stdout when the last upload is missing or
older than 30 days. Silent otherwise. Always exits 0.

Python stdlib only. Must be fast (~5ms).
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LAST_UPLOAD_PATH = Path.home() / ".config" / "aiqrank" / "last_upload_at"
STALE_SECONDS = 30 * 24 * 60 * 60
NUDGE = "AIQ Rank: it's been 30 days — run /aiqrank to refresh your rank."


def main() -> int:
    try:
        if _is_stale():
            print(NUDGE)
    except Exception:
        pass
    return 0


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
