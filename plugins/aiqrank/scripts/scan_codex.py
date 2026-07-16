#!/usr/bin/env python3
"""Compatibility CLI for the canonical Codex transcript scanner.

All parsing lives in :mod:`scan_transcripts`. This wrapper preserves the
historical standalone envelope consumed by existing prompts and upload paths.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import (  # noqa: E402
    DEFAULT_WINDOW_DAYS,
    _patch_touches_agents_md,
    _shell_verb,
    scan_codex as _canonical_scan_codex,
)


def scan(
    codex_dir: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict:
    """Delegate to the shared scanner and add the standalone source fields."""
    home = codex_dir or Path.home() / ".codex"
    canonical = _canonical_scan_codex(
        home,
        window_days=window_days,
        now_ts=now_ts,
        mtime_after_ts=mtime_after_ts,
    )
    if canonical is None:
        canonical = {
            "daily": [],
            "rollup": {},
            "intervals_by_day": {},
            "_unknown_event_types": {},
            "completeness": {
                "status": "complete",
                "omitted_dates": [],
                "failure_count": 0,
            },
        }
    return {
        "source": "codex",
        "window_days": window_days,
        **canonical,
    }


def main(argv: list[str]) -> int:
    window_days = DEFAULT_WINDOW_DAYS
    if "--days" in argv:
        idx = argv.index("--days")
        try:
            window_days = int(argv[idx + 1])
        except (IndexError, ValueError):
            pass

    mtime_after_ts: float | None = None
    if "--mtime-after" in argv:
        idx = argv.index("--mtime-after")
        try:
            raw = argv[idx + 1]
            mtime_after_ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (IndexError, ValueError):
            pass

    result = scan(
        window_days=window_days,
        now_ts=time.time(),
        mtime_after_ts=mtime_after_ts,
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
