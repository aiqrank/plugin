#!/usr/bin/env python3
"""Apply a pending AIQ Rank update, then report the plugin root to scan from.

`upload_metrics.py` records the version the server advertises after each
successful upload (`check_update.record_latest_version`). When that recorded
version is newer than this bundle, the skill runs this script instead of
printing a command for the user to run themselves.

The update path is engine-specific:

  * Claude Code delegates to the plugin manager. The cache keeps versions side
    by side, so the refreshed scripts are usable in the running session — this
    script prints the resolved root so the caller's remaining steps use them.
    The skill text and hooks stay on the copy loaded at session start.
  * Codex fetches the release-pinned `install_codex.py` and refreshes the
    managed artifacts in place under `~/.aiqrank`.

The server supplies only a version number, and `check_update` validates it as
strict semver before it is ever stored. The repository, host, and path are
constants here, so the download target cannot be redirected by the server.

Every failure is soft: the caller keeps scanning with the current bundle and
the user sees the manual command, which is the pre-auto-update behaviour.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from _version import PLUGIN_VERSION, USER_AGENT
from check_update import (
    CONFIG_DIR,
    STALE_VERSION_PATH,
    _read_stale_version,
    _version_tuple,
)

# Written for the skill's later steps to read. Kept under the user-owned config
# directory rather than /tmp so another local account cannot point the scan at
# scripts of its choosing.
PLUGIN_ROOT_PATH = CONFIG_DIR / "plugin_root"

REPO_RAW_BASE = "https://raw.githubusercontent.com/aiqrank/plugin"
CLAUDE_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "aiqrank" / "aiqrank"
CODEX_CACHE = Path.home() / ".codex" / "plugins" / "cache" / "aiqrank" / "aiqrank"
CODEX_MANAGED_ROOT = Path.home() / ".aiqrank"
_UPDATE_TIMEOUT = 180
_MANUAL_CLAUDE = (
    "claude plugin marketplace update aiqrank && claude plugin update aiqrank@aiqrank"
)
_MANUAL_CODEX = "codex plugin marketplace upgrade aiqrank && codex plugin add aiqrank@aiqrank"


def main() -> int:
    engine = detect_engine()
    pending = pending_version()

    if pending is not None:
        if update(engine, pending):
            print(f"AIQ Rank: updated to v{pending}.")
            _clear_stale_signal()
        else:
            manual = _MANUAL_CODEX if engine == "codex" else _MANUAL_CLAUDE
            print(
                f"AIQ Rank: automatic update to v{pending} did not complete. "
                f"To update manually, run: {manual}"
            )

    root = resolve_root(engine)
    _publish_root(root)
    print(f"PLUGIN_ROOT={root}")
    return 0


def _publish_root(root: Path) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PLUGIN_ROOT_PATH.write_text(f"{root}\n")
    except OSError:
        pass


def detect_engine() -> str:
    """Identify the running engine so we never update the wrong install."""
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude"
    if os.environ.get("CODEX_PLUGIN_ROOT"):
        return "codex"
    if (CODEX_MANAGED_ROOT / "scripts").is_dir() or _newest_cached(CODEX_CACHE):
        return "codex"
    return "claude"


def pending_version() -> str | None:
    """Return the recorded server version when it is newer than this bundle."""
    latest = _read_stale_version()
    if latest is None:
        return None
    try:
        if _version_tuple(latest) <= _version_tuple(PLUGIN_VERSION):
            return None
    except ValueError:
        return None
    return latest


def update(engine: str, version: str) -> bool:
    if engine == "codex":
        return _update_codex(version)
    return _update_claude()


def _update_claude() -> bool:
    """Refresh the marketplace, then the plugin, via the Claude Code CLI."""
    claude = shutil.which("claude")
    if claude is None:
        return False

    for args in (
        [claude, "plugin", "marketplace", "update", "aiqrank"],
        [claude, "plugin", "update", "aiqrank@aiqrank"],
    ):
        if not _run(args):
            return False
    return True


def _update_codex(version: str) -> bool:
    """Refresh the Codex plugin and managed artifacts for `version`.

    The Codex CLI owns the marketplace cache. The release-pinned installer is
    then fed to Python on stdin rather than written to disk, so a partial
    download cannot be left behind as an executable file.
    """
    codex = shutil.which("codex")
    if codex is None:
        return False

    if not _run([codex, "plugin", "marketplace", "upgrade", "aiqrank"]):
        if not _run([codex, "plugin", "marketplace", "add", "aiqrank/plugin"]):
            return False
    if not _run([codex, "plugin", "add", "aiqrank@aiqrank"]):
        return False

    base = f"{REPO_RAW_BASE}/v{version}/plugins/aiqrank"
    source = _fetch(f"{base}/scripts/install_codex.py")
    if source is None:
        return False

    return _run([sys.executable, "-", "--base", base], stdin=source)


def _fetch(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _run(args: list[str], *, stdin: bytes | None = None) -> bool:
    try:
        result = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            timeout=_UPDATE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def resolve_root(engine: str) -> Path:
    """Resolve the newest usable plugin root for `engine`.

    Checked after updating so the caller runs the refreshed scripts. The cache
    is preferred over the injected root because the injected value points at
    the version loaded when the session started, which an update supersedes.
    """
    if engine == "codex":
        if (CODEX_MANAGED_ROOT / "scripts").is_dir():
            return CODEX_MANAGED_ROOT
        cache, injected = CODEX_CACHE, os.environ.get("CODEX_PLUGIN_ROOT")
    else:
        cache, injected = CLAUDE_CACHE, os.environ.get("CLAUDE_PLUGIN_ROOT")

    newest = _newest_cached(cache)
    if newest is not None:
        return newest
    if injected:
        return Path(injected)
    return Path(__file__).resolve().parent.parent


def _newest_cached(cache: Path) -> Path | None:
    """Highest semver directory under `cache` that actually carries scripts."""
    try:
        candidates = [path for path in cache.iterdir() if (path / "scripts").is_dir()]
    except OSError:
        return None

    versioned = []
    for path in candidates:
        try:
            versioned.append((_version_tuple(path.name), path))
        except ValueError:
            continue
    if not versioned:
        return None
    return max(versioned)[1]


def _clear_stale_signal() -> None:
    try:
        STALE_VERSION_PATH.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
