#!/usr/bin/env python3
"""SessionStart staleness, version, and cross-surface nudges.

Prints up to three lines on stdout:
  * 30-day staleness nudge when the last upload is missing or > 30 days old.
  * Plugin-update nudge when ~/.config/aiqrank/stale_version contains a
    version newer than this bundle (written by hook_upload_today.py based on
    the server's latest_plugin_version).
  * Monthly reminder when Claude Code CLI exists but lacks the AIQ Rank plugin.
Silent otherwise. Always exits 0.

Python stdlib only. Must be fast (~5ms).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from _version import PLUGIN_VERSION
from check_update import _version_tuple


def _host_home() -> Path:
    """The user's real home.

    AIQRANK_HOST_HOME matches scan_transcripts._host_homes(): Cowork can
    mount the user's home into its VM without changing the sandbox HOME.
    """
    override = os.environ.get("AIQRANK_HOST_HOME")
    return Path(override).expanduser() if override else Path.home()


def _claude_config_dir() -> Path:
    """Locate Claude Code's config directory on the host."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return _host_home() / ".claude"


CONFIG_DIR = Path.home() / ".config" / "aiqrank"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"
LOG_PATH = CONFIG_DIR / "hook.log"
STALE_VERSION_PATH = CONFIG_DIR / "stale_version"
CLI_INSTALL_NUDGE_PATH = CONFIG_DIR / "cli_install_nudge_at"
CLAUDE_CONFIG_DIR = _claude_config_dir()
CLAUDE_PLUGIN_REGISTRY_PATH = (
    CLAUDE_CONFIG_DIR / "plugins" / "installed_plugins.json"
)
CLAUDE_MARKETPLACES_PATH = (
    CLAUDE_CONFIG_DIR / "plugins" / "known_marketplaces.json"
)
# A GUI host (Desktop, Cowork) launches with a narrower PATH than a login
# shell, so a PATH miss is not proof the CLI is absent. Check where the
# installer actually puts it before concluding anything.
CLAUDE_CLI_FALLBACK_PATHS = (
    _host_home() / ".local" / "bin" / "claude",
    _host_home() / ".claude" / "local" / "bin" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
)
STALE_SECONDS = 30 * 24 * 60 * 60
CLI_INSTALL_NUDGE_SECONDS = 30 * 24 * 60 * 60
# Defence in depth — even though hook_upload_today.py validates before
# writing, the file on disk is server-controlled. Refuse to print anything
# that doesn't match a strict semver-like shape.
_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}(-[A-Za-z0-9.]+)?$")
_VERSION_MAX_LEN = 32
NUDGE_FMT = "AIQ Rank: it's been 30 days — run {command} to refresh your rank."
VERSION_NUDGE_FMT = (
    "AIQ Rank: plugin update available (v{latest}) — {instruction}"
)

# Customization switched from counting config edits to measuring the
# configuration present on the machine. Only 0.3.26+ reports that, so older
# installs lose the surface half of the dimension once the grace period ends.
# The warning is a statement of consequence, never an imperative — this text
# lands in an agent's SessionStart context, and "run ..." phrasing reads as a
# task to perform (see the CLI-install nudge below for the same reasoning).
CUSTOMIZATION_SURFACE_MIN_VERSION = "0.3.26"
CUSTOMIZATION_CUTOVER = date(2026, 10, 26)
CUSTOMIZATION_PENDING_FMT = (
    " Your Customization score drops on {cutover} unless this plugin is updated."
)
CUSTOMIZATION_PASSED = (
    " Customization is scoring below its real value until this plugin is updated."
)
# This line lands in an agent's SessionStart context, not only a human's
# terminal. Imperative "run `...`" phrasing reads to an agent with shell
# access as a task to perform, and it would be mutating the user's CLI
# plugin registry unasked. Keep it unmistakably a message to relay.
_CLI_INSTALL_NUDGE_PREFIX = (
    "AIQ Rank (notice for the user — nothing to run, do not execute this): "
    "AIQ Rank is not installed in the Claude Code CLI. If you also use Claude "
    "Code in Terminal, you can add it there whenever you like with "
)
CLI_INSTALL_NUDGE = (
    _CLI_INSTALL_NUDGE_PREFIX + "'claude plugin install aiqrank@aiqrank'."
)
CLI_INSTALL_NUDGE_WITH_MARKETPLACE = (
    _CLI_INSTALL_NUDGE_PREFIX
    + "'claude plugin marketplace add aiqrank/plugin' followed by "
    + "'claude plugin install aiqrank@aiqrank'."
)


def _is_codex_host() -> bool:
    if os.environ.get("CODEX_PLUGIN_ROOT"):
        return True

    # Codex currently injects the installed root through the Claude-compatible
    # variable when it runs a Claude-format plugin hook. The cache path is the
    # stable host signal in that case.
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    parts = re.split(r"[/\\\\]+", root)
    try:
        codex_index = parts.index(".codex")
    except ValueError:
        return False
    return "plugins" in parts[codex_index + 1 :]


def _aiqrank_command() -> str:
    if _is_codex_host():
        return "$aiqrank:aiqrank"

    return "/aiqrank"


def _update_instruction() -> str:
    if _is_codex_host():
        return (
            "run $aiqrank:aiqrank; it updates the Codex plugin automatically."
        )

    return "run /aiqrank and it installs itself."


def _customization_warning(today: date | None = None) -> str:
    """Consequence clause appended to the version nudge for installs too old to
    report configuration surfaces.

    Empty for Codex, whose scanner cannot emit surfaces at any version and is
    therefore exempt from the cutover, and empty once this install is new
    enough that the cutover costs it nothing.
    """
    if _is_codex_host():
        return ""
    if _version_tuple(PLUGIN_VERSION) >= _version_tuple(
        CUSTOMIZATION_SURFACE_MIN_VERSION
    ):
        return ""

    today = today or date.today()
    if today >= CUSTOMIZATION_CUTOVER:
        return CUSTOMIZATION_PASSED
    return CUSTOMIZATION_PENDING_FMT.format(
        cutover=CUSTOMIZATION_CUTOVER.strftime("%d %b %Y").lstrip("0")
    )


def _log_hook_fired() -> None:
    """Append one "hook fired" line for fault-domain triage. Fail-soft.

    Direct file append (not RotatingFileHandler) to keep the synchronous
    hook fast — this script's budget is ~5ms. Same log file as
    hook_upload_today.py so the customer's triage scans one location.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "<unset>")
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        line = (
            f"{ts} hook fired script=nudge_if_stale "
            f"plugin_root={plugin_root} platform={sys.platform} "
            f"python={sys.executable}\n"
        )
        with LOG_PATH.open("a") as fh:
            fh.write(line)
        try:
            os.chmod(LOG_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def main() -> int:
    _log_hook_fired()
    try:
        if _is_stale():
            print(NUDGE_FMT.format(command=_aiqrank_command()))
        latest = _read_stale_version()
        if latest:
            print(
                VERSION_NUDGE_FMT.format(
                    latest=latest, instruction=_update_instruction()
                )
                + _customization_warning()
            )
        now = time.time()
        cli_install_nudge = _cli_install_nudge(now)
        if cli_install_nudge:
            # Deliver first, then start the cooldown. Killed between the two,
            # the reminder simply repeats next session. The reverse order
            # burns 30 days having shown the user nothing.
            print(cli_install_nudge, flush=True)
            _record_cli_install_nudge(now)
    except Exception:
        pass
    return 0


def _cli_install_nudge(now: float) -> str | None:
    """Return a monthly CLI-install reminder when it is actionable.

    Reads Claude Code's local manifests directly so SessionStart never waits
    for a CLI subprocess. Unknown or malformed state fails silent. The caller
    prints the result and then records the cooldown.
    """
    if _is_codex_host():
        return None

    if not _claude_cli_present():
        _clear_cli_install_nudge()
        return None

    installed = _cli_plugin_installed()
    if installed is None:
        return None
    if installed:
        _clear_cli_install_nudge()
        return None

    due = _cli_install_nudge_due(now)
    if due is not True:
        return None

    marketplace_installed = _cli_marketplace_installed()
    if marketplace_installed is None:
        return None

    if not _cli_install_nudge_recordable():
        return None

    if marketplace_installed:
        return CLI_INSTALL_NUDGE
    return CLI_INSTALL_NUDGE_WITH_MARKETPLACE


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _claude_cli_present() -> bool:
    """True when the Claude Code CLI is reachable from this session.

    Hand-rolled instead of shutil.which: importing shutil pulls in zlib,
    bz2, lzma and zstd to register archive formats, which costs most of
    this hook's ~5ms budget for one lookup.
    """
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if entry and _executable(Path(entry) / "claude"):
            return True
    return any(_executable(path) for path in CLAUDE_CLI_FALLBACK_PATHS)


def _read_json_object(path: Path) -> dict | bool | None:
    """Read one JSON object. False means absent, None means unusable."""
    try:
        raw = path.read_text(errors="replace")
    except FileNotFoundError:
        return False
    except OSError:
        return None

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _cli_plugin_installed() -> bool | None:
    payload = _read_json_object(CLAUDE_PLUGIN_REGISTRY_PATH)
    if payload is False:
        # No registry inside an existing config tree means the plugin is
        # genuinely absent. No config tree at all means the CLI keeps its
        # state somewhere this hook cannot see, so claim nothing.
        return False if CLAUDE_CONFIG_DIR.is_dir() else None
    if payload is None:
        return None

    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        return None

    installs = plugins.get("aiqrank@aiqrank")
    if installs is None:
        return False
    if isinstance(installs, list):
        return bool(installs)
    # Older Claude Code manifests used one object per installed plugin.
    if isinstance(installs, dict):
        return True
    return None


def _cli_marketplace_installed() -> bool | None:
    marketplaces = _read_json_object(CLAUDE_MARKETPLACES_PATH)
    if marketplaces is False:
        return False
    if marketplaces is None:
        return None
    return "aiqrank" in marketplaces


def _cli_install_nudge_due(now: float) -> bool | None:
    try:
        raw = CLI_INSTALL_NUDGE_PATH.read_text(errors="replace").strip()
    except FileNotFoundError:
        return True
    except OSError:
        return None

    try:
        shown_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # A truncated write or a hand edit must not silence the reminder
        # forever. Heal the marker, the way _read_stale_version does.
        _clear_cli_install_nudge()
        return True
    if shown_at.tzinfo is None:
        shown_at = shown_at.replace(tzinfo=timezone.utc)

    age = now - shown_at.timestamp()
    if age < 0:
        # Clock rollback or a hand-edited future timestamp. Same treatment.
        _clear_cli_install_nudge()
        return True
    return age >= CLI_INSTALL_NUDGE_SECONDS


def _iso_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _cli_install_nudge_recordable() -> bool:
    """Whether the cooldown could be persisted once the reminder is shown.

    Checked before printing rather than after: a config directory this hook
    cannot write to would otherwise repeat the same reminder every session.
    """
    parent = CLI_INSTALL_NUDGE_PATH.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(parent, os.W_OK)


def _record_cli_install_nudge(now: float) -> bool:
    """Persist the marker atomically. A reader never sees a partial write."""
    tmp = CLI_INSTALL_NUDGE_PATH.parent / (CLI_INSTALL_NUDGE_PATH.name + ".tmp")
    try:
        CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(_iso_timestamp(now) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CLI_INSTALL_NUDGE_PATH)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _clear_cli_install_nudge() -> None:
    try:
        CLI_INSTALL_NUDGE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _read_stale_version() -> str | None:
    if not STALE_VERSION_PATH.exists():
        return None
    try:
        v = STALE_VERSION_PATH.read_text(errors="replace").strip()
    except OSError:
        return None
    if not v or len(v) > _VERSION_MAX_LEN or _VERSION_RE.match(v) is None:
        return None
    try:
        if _version_tuple(v) <= _version_tuple(PLUGIN_VERSION):
            # A release can leave an older marker behind while the user is
            # upgrading. It is no longer actionable once this bundle is at
            # least that new, so heal the marker and stay silent.
            STALE_VERSION_PATH.unlink(missing_ok=True)
            return None
    except (ValueError, OSError):
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
