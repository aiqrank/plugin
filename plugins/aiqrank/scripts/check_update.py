#!/usr/bin/env python3
"""Persist and display the Codex plugin update notice.

`upload_metrics.py` records the server's latest plugin version after a
successful interactive Codex upload. The Codex prompt runs this script before
the next scan, so users receive the update command without an unsupported
Codex hook.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _version import PLUGIN_VERSION

CONFIG_DIR = Path.home() / ".config" / "aiqrank"
STALE_VERSION_PATH = CONFIG_DIR / "stale_version"
_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}(-[A-Za-z0-9.]+)?$")
_VERSION_MAX_LEN = 32


def record_latest_version(latest: object) -> None:
    """Persist a validated server version only when it exceeds this bundle."""
    if not _safe_version(latest):
        return

    try:
        if _version_tuple(PLUGIN_VERSION) < _version_tuple(latest):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            STALE_VERSION_PATH.write_text(latest + "\n")
        else:
            STALE_VERSION_PATH.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def main() -> int:
    latest = _read_stale_version()
    if latest is None:
        return 0

    if _version_tuple(latest) <= _version_tuple(PLUGIN_VERSION):
        try:
            STALE_VERSION_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return 0

    print(
        f"AIQ Rank: plugin update available (v{latest}). "
        "Run in a terminal: curl -sSL https://aiqrank.com/setup/codex | bash; "
        "then start a new Codex session and run $aiqrank:aiqrank."
    )
    return 1


def _read_stale_version() -> str | None:
    try:
        latest = STALE_VERSION_PATH.read_text().strip()
    except OSError:
        return None
    return latest if _safe_version(latest) else None


def _safe_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _VERSION_MAX_LEN
        and _VERSION_RE.fullmatch(value) is not None
    )


def _version_tuple(version: str) -> tuple[int, int, int, int, int, str]:
    base, separator, suffix = version.partition("-")
    parts = [int(component) for component in base.split(".")]
    parts.extend([0] * (4 - len(parts)))
    return (*parts[:4], 1 if not separator else 0, suffix)


if __name__ == "__main__":
    raise SystemExit(main())
