#!/usr/bin/env python3
"""Silent background uploader — SessionStart hook.

Never writes to stdout/stderr. Logs to ~/.config/aiqrank/hook.log
(rotated at ~1MB, mode 0600). Always exits 0 so Claude Code startup
is never disrupted.

Python stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _version import PLUGIN_VERSION, USER_AGENT  # noqa: E402
from infer_role import classify_role  # noqa: E402
from scan_transcripts import max_concurrent_sustained, min_sustained_secs  # noqa: E402

# Server-supplied version strings written to disk and printed by the nudge
# hook are validated against this shape to block ANSI escapes, control
# characters, oversized payloads, and embedded newlines from a compromised
# server / MITM proxy / hostile AIQRANK_BASE_URL override.
_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}(-[A-Za-z0-9.]+)?$")
_VERSION_MAX_LEN = 32

DEFAULT_BASE_URL = "https://aiqrank.com"
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"
LAST_UPLOAD_PATH_CODEX = CONFIG_DIR / "last_upload_at_codex"
LOCK_PATH = CONFIG_DIR / "upload.lock"
DISABLED_FLAG = CONFIG_DIR / "disabled"
LOG_PATH = CONFIG_DIR / "hook.log"
# Server-reported latest version. Written when local < latest, removed when
# local catches up. Read by hook_nudge_if_stale.py to print one-line nudge.
STALE_VERSION_PATH = CONFIG_DIR / "stale_version"

GATE_SECONDS = 24 * 60 * 60
MAX_WINDOW_DAYS = 30
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Payload size guard: if the serialized JSON exceeds this, chunk Codex daily
# into <=7-day windows and POST sequentially.
PAYLOAD_SIZE_LIMIT_BYTES = 400 * 1024
CODEX_CHUNK_DAYS = 7
INLINE_SCAN_SOURCES = ("opencode", "cursor")


def _setup_logger() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Ensure log file exists with mode 0600 before RotatingFileHandler opens it.
    if not LOG_PATH.exists():
        try:
            fd = os.open(str(LOG_PATH), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)
        except OSError:
            pass
    else:
        try:
            os.chmod(LOG_PATH, 0o600)
        except OSError:
            pass

    logger = logging.getLogger("aiqrank.hook_upload")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Always rebind the handler to the current LOG_PATH so tests that patch
    # the module-level constant get a logger pointing at the right file.
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    handler = RotatingFileHandler(str(LOG_PATH), maxBytes=1_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _acquire_exclusive(fd: int) -> bool:
    """Try to acquire exclusive non-blocking lock on byte 0 of fd.

    Locks byte 0 specifically (always seeks to 0 first) so the locked range
    is independent of the file pointer. Cross-platform: fcntl on POSIX,
    msvcrt on Windows. The byte-0 invariant matters because msvcrt.locking
    is byte-range-based — if a future contributor adds a write that moves
    the file pointer, the lock target must not move with it.
    """
    try:
        os.lseek(fd, 0, 0)
    except OSError:
        return False
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def _release_lock(fd: int) -> None:
    """Release the exclusive lock acquired by _acquire_exclusive.

    Seeks to byte 0 before unlocking to match the locked range — same
    invariant as _acquire_exclusive.
    """
    try:
        os.lseek(fd, 0, 0)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _respawn_detached_and_exit() -> None:
    """If we're the foreground hook invocation, spawn a detached child and exit.

    Parent re-execs this script with AIQRANK_DETACHED=1 and os._exit(0)s
    so Claude Code's hook timeout fires regardless of upload duration.
    Child sees AIQRANK_DETACHED and returns no-op to continue the work.

    DEVNULL on stdin/stdout/stderr is required on both platforms so the
    detached child does not inherit Claude Code's hook pipes — without
    this, the hook timeout never fires on Windows.
    """
    if os.environ.get("AIQRANK_DETACHED") == "1":
        return
    new_env = dict(os.environ)
    new_env["AIQRANK_DETACHED"] = "1"
    detach_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": new_env,
    }
    if sys.platform == "win32":
        detach_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        detach_kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, *sys.argv], **detach_kwargs)
    except OSError:
        # If we can't even spawn, exit silently — better than blocking SessionStart.
        pass
    os._exit(0)


def _write_invocation_marker() -> None:
    """Touch ~/.config/aiqrank/last_hook_invocation to prove the hook fired.

    Independent diagnostic from the rotating log: if the marker is fresh
    but the log is stale, the hook fired but the detached child never
    reached the logger (detach broken or child crashed). If neither is
    fresh, the hook itself never ran (env-var/shell/Python PATH).
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        marker = CONFIG_DIR / "last_hook_invocation"
        marker.write_text(_iso_now() + "\n")
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def main() -> int:
    # Touch the invocation marker before anything else so we have an
    # independent breadcrumb that the hook fired, separate from the log.
    _write_invocation_marker()
    # Detach from Claude Code's hook so the upload can take longer than
    # the hook timeout without blocking SessionStart. Parent exits here;
    # only the detached child reaches the work below.
    _respawn_detached_and_exit()

    try:
        logger = _setup_logger()
    except Exception:
        # Can't even log — bail silently.
        return 0

    # Hook-entry "fired" log line for fault-domain triage.
    try:
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "<unset>")
        logger.info(
            f"hook fired plugin_root={plugin_root} "
            f"platform={sys.platform} python={sys.executable}"
        )
    except Exception:
        pass

    try:
        _run(logger)
    except Exception as e:
        try:
            logger.info(f"error {type(e).__name__}")
        except Exception:
            pass
    return 0


def _run(logger: logging.Logger) -> None:
    device_id = _read_device_id()
    if not device_id:
        logger.info("no device")
        return

    if _is_disabled():
        logger.info("disabled")
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not _acquire_exclusive(lock_fd):
            logger.info("busy")
            return

        last_upload_at = _read_last_upload_at()
        now_ts = time.time()
        if last_upload_at is not None:
            age = now_ts - last_upload_at.timestamp()
            if age < GATE_SECONDS:
                logger.info("gated")
                return

        try:
            claude_metrics = _run_scan()
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
            logger.info(f"error {type(e).__name__}")
            return

        claude_daily = _extract_source_daily(claude_metrics, "claude_code")
        cowork_daily = _extract_source_daily(claude_metrics, "cowork")
        inline_source_daily = {
            source: _extract_source_daily(claude_metrics, source)
            for source in INLINE_SCAN_SOURCES
        }

        sample = claude_metrics.get("first_messages_sample") if isinstance(claude_metrics, dict) else None
        if not isinstance(sample, list):
            sample = []
        inferred_role = classify_role(sample).get("inferred_role") or "engineer"

        codex_data = _maybe_scan_codex(logger)

        codex_daily = (codex_data or {}).get("daily") or []
        unknown_event_types = (codex_data or {}).get("_unknown_event_types") or {}

        if (
            not claude_daily
            and not cowork_daily
            and not codex_daily
            and not any(inline_source_daily.values())
        ):
            logger.info("ok sessions=0 devices=" + device_id[:8])
            _write_last_upload_at(_iso_now())
            return

        claude_intervals = _extract_source_intervals(claude_metrics, "claude_code")
        cowork_intervals = _extract_source_intervals(claude_metrics, "cowork")
        codex_intervals = (codex_data or {}).get("intervals_by_day") or {}
        inline_source_intervals = {
            source: _extract_source_intervals(claude_metrics, source)
            for source in INLINE_SCAN_SOURCES
        }
        combined_daily = _build_combined_daily(
            claude_intervals,
            codex_intervals,
            cowork_intervals,
            extra_intervals=inline_source_intervals.values(),
        )

        success = _post_by_source(
            device_id,
            claude_daily,
            codex_daily,
            cowork_daily,
            unknown_event_types,
            inferred_role,
            logger,
            combined_daily=combined_daily,
            extra_sources_daily=inline_source_daily,
        )
        if not success:
            return

        now_iso = _iso_now()
        _write_last_upload_at(now_iso)
        if codex_data is not None:
            _write_codex_upload_at(now_iso)
        logger.info(
            f"ok sessions={len(claude_daily)} cowork_days={len(cowork_daily)} "
            f"codex_days={len(codex_daily)} "
            f"opencode_days={len(inline_source_daily.get('opencode') or [])} "
            f"cursor_days={len(inline_source_daily.get('cursor') or [])} "
            f"devices={device_id[:8]}"
        )

    finally:
        _release_lock(lock_fd)
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _install_codex_prompt() -> None:
    """Copy the /aiqrank Codex prompt to ~/.codex/prompts/ if not already installed."""
    codex_prompts_dir = Path.home() / ".codex" / "prompts"
    dest = codex_prompts_dir / "aiqrank.md"
    if dest.exists():
        return
    source = Path(__file__).resolve().parent.parent / "codex_prompts" / "aiqrank.md"
    if not source.exists():
        return
    try:
        codex_prompts_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(source.read_text())
    except OSError:
        pass


def _maybe_scan_codex(logger: logging.Logger) -> dict | None:
    """Return Codex scan results if ~/.codex/sessions/ exists, else None."""
    if not CODEX_SESSIONS_DIR.is_dir():
        return None

    # Opportunistically install the Codex /aiqrank prompt on first discovery.
    _install_codex_prompt()

    last_codex_at = _read_codex_upload_at()
    script = Path(__file__).resolve().parent / "scan_codex.py"

    cmd = [sys.executable, str(script)]
    if last_codex_at is None:
        # First run: 30-day backfill, no --mtime-after.
        cmd += ["--days", str(MAX_WINDOW_DAYS)]
    else:
        cmd += ["--days", str(MAX_WINDOW_DAYS)]
        cutoff_iso = last_codex_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        cmd += ["--mtime-after", cutoff_iso]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.info("codex scan error TimeoutExpired")
        return None
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        logger.info(f"codex scan error {type(e).__name__}")
        return None


def _post_by_source(
    device_id: str,
    claude_daily: list,
    codex_daily: list,
    cowork_daily: list,
    unknown_event_types: dict,
    inferred_role: str,
    logger: logging.Logger,
    combined_daily: list | None = None,
    extra_sources_daily: dict[str, list] | None = None,
) -> bool:
    """POST by_source payload, chunking sources if payload exceeds PAYLOAD_SIZE_LIMIT_BYTES.

    All daily lists (claude_code, codex, cowork, and inline scanner sources)
    can grow large.
    When the combined payload is over the cap, every source is split into
    <=CODEX_CHUNK_DAYS-day windows and paired index-wise. The longest
    source's window count drives the loop; missing windows for shorter
    sources are sent as an empty list. `combined_daily` is small (only
    carries the peak int per day) so it ships in full on the first chunk.

    On the final successful chunk, caller should update cursors.

    Returns True on full success, False on any HTTP/network error.
    """
    # Try a single full POST first.
    payload = _build_by_source_payload(
        device_id,
        claude_daily,
        codex_daily,
        cowork_daily,
        unknown_event_types,
        inferred_role,
        combined_daily=combined_daily,
        extra_sources_daily=extra_sources_daily,
    )
    body_bytes = len(json.dumps(payload).encode("utf-8"))

    if body_bytes <= PAYLOAD_SIZE_LIMIT_BYTES:
        try:
            _post_upload(payload)
            return True
        except urllib.error.HTTPError as e:
            # If the server rejected an unknown source (older server that
            # hasn't deployed support for a newer source yet), drop it and
            # retry once. Without this, cursors never advance and the next
            # session re-sends the same rejected payload — pinning users on
            # older servers in a permanent rejection loop.
            unknown_source = _unknown_source_from_error(e)
            if combined_daily and unknown_source == "combined":
                logger.info("server rejected combined source, retrying without it")
                return _post_by_source(
                    device_id,
                    claude_daily,
                    codex_daily,
                    cowork_daily,
                    unknown_event_types,
                    inferred_role,
                    logger,
                    combined_daily=None,
                    extra_sources_daily=extra_sources_daily,
                )
            if cowork_daily and unknown_source == "cowork":
                logger.info("server rejected cowork source, retrying without it")
                return _post_by_source(
                    device_id,
                    claude_daily,
                    codex_daily,
                    [],
                    unknown_event_types,
                    inferred_role,
                    logger,
                    combined_daily=combined_daily,
                    extra_sources_daily=extra_sources_daily,
                )
            extra_sources_daily = extra_sources_daily or {}
            if unknown_source in extra_sources_daily and extra_sources_daily.get(unknown_source):
                logger.info(f"server rejected {unknown_source} source, retrying without it")
                retry_extra = dict(extra_sources_daily)
                retry_extra[unknown_source] = []
                return _post_by_source(
                    device_id,
                    claude_daily,
                    codex_daily,
                    cowork_daily,
                    unknown_event_types,
                    inferred_role,
                    logger,
                    combined_daily=None,
                    extra_sources_daily=retry_extra,
                )
            logger.info(f"error http={e.code}")
            return False
        except urllib.error.URLError:
            logger.info("error network")
            return False
        except json.JSONDecodeError:
            # 200 OK with non-JSON body (CDN error page, proxy interstitial).
            # Log and bail without advancing the gate so we retry next session.
            logger.info("error json")
            return False

    # Over limit: chunk each source's daily list into <=CODEX_CHUNK_DAYS-day
    # windows and pair them index-wise. Any list can be empty.
    logger.info(f"payload {body_bytes}B > limit, chunking")
    claude_chunks = _chunk_daily(claude_daily, CODEX_CHUNK_DAYS)
    codex_chunks = _chunk_daily(codex_daily, CODEX_CHUNK_DAYS)
    cowork_chunks = _chunk_daily(cowork_daily, CODEX_CHUNK_DAYS)
    extra_sources_daily = extra_sources_daily or {}
    extra_chunks = {
        source: _chunk_daily(daily, CODEX_CHUNK_DAYS)
        for source, daily in extra_sources_daily.items()
    }
    n_chunks = max(
        [len(claude_chunks), len(codex_chunks), len(cowork_chunks)]
        + [len(chunks) for chunks in extra_chunks.values()]
    )

    for i in range(n_chunks):
        claude_chunk = claude_chunks[i] if i < len(claude_chunks) else []
        codex_chunk = codex_chunks[i] if i < len(codex_chunks) else []
        cowork_chunk = cowork_chunks[i] if i < len(cowork_chunks) else []
        extra_chunk = {
            source: chunks[i] if i < len(chunks) else []
            for source, chunks in extra_chunks.items()
        }
        chunk_payload = _build_by_source_payload(
            device_id,
            claude_chunk,
            codex_chunk,
            cowork_chunk,
            unknown_event_types if i == 0 else {},
            inferred_role,
            combined_daily=combined_daily if i == 0 else [],
            extra_sources_daily=extra_chunk,
        )
        try:
            _post_upload(chunk_payload)
        except urllib.error.HTTPError as e:
            logger.info(f"error chunk={i} http={e.code}")
            return False
        except urllib.error.URLError:
            logger.info(f"error chunk={i} network")
            return False

    return True


def _is_unknown_source_error(err: urllib.error.HTTPError, source: str) -> bool:
    """True iff the server returned 422 with body
    `{error: 'unknown source', source: <source>}` — meaning we should drop
    that source and retry. Used to gracefully degrade when uploading to a
    server that hasn't deployed support for a newer source yet (e.g., a
    newer plugin running against an older server).
    """
    return _unknown_source_from_error(err) == source


def _unknown_source_from_error(err: urllib.error.HTTPError) -> str | None:
    if err.code != 422:
        return None
    try:
        body = json.loads(err.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("error") != "unknown source":
        return None
    source = body.get("source")
    return source if isinstance(source, str) and source else None


def _is_unknown_combined_source_error(err: urllib.error.HTTPError) -> bool:
    """Backwards-compatible wrapper kept for tests; prefer `_is_unknown_source_error`."""
    return _is_unknown_source_error(err, "combined")


def _build_combined_daily(
    claude_intervals_by_day: dict,
    codex_intervals_by_day: dict,
    cowork_intervals_by_day: dict | None = None,
    extra_intervals=None,
) -> list:
    """Build a per-day daily list whose only metric is `max_concurrent_sessions`,
    computed by unioning intervals across Claude Code, Codex, and Cowork and
    running the sustained sweep. Empty list if no source has intervals.
    """
    cowork_intervals_by_day = cowork_intervals_by_day or {}
    extra_intervals = list(extra_intervals or [])
    if (
        not claude_intervals_by_day
        and not codex_intervals_by_day
        and not cowork_intervals_by_day
        and not any(extra_intervals)
    ):
        return []

    days = (
        set(claude_intervals_by_day.keys())
        | set(codex_intervals_by_day.keys())
        | set(cowork_intervals_by_day.keys())
    )
    for source_intervals in extra_intervals:
        if isinstance(source_intervals, dict):
            days |= set(source_intervals.keys())
    min_secs = min_sustained_secs()

    out = []
    for day in sorted(days):
        merged: list[tuple[float, float]] = []
        for source_intervals in (
            claude_intervals_by_day,
            codex_intervals_by_day,
            cowork_intervals_by_day,
            *extra_intervals,
        ):
            if not isinstance(source_intervals, dict):
                continue
            for raw in source_intervals.get(day, []):
                if isinstance(raw, (list, tuple)) and len(raw) == 2:
                    merged.append((float(raw[0]), float(raw[1])))
        peak = max_concurrent_sustained(merged, min_secs)
        if peak > 0:
            out.append({"date": day, "metrics": {"max_concurrent_sessions": peak}})
    return out


def _extract_source_daily(claude_metrics: dict, source: str) -> list:
    """Pull a source's `daily` list out of the scanner's by_source envelope."""
    if not isinstance(claude_metrics, dict):
        return []
    by_source = claude_metrics.get("by_source")
    if not isinstance(by_source, dict):
        return []
    source_block = by_source.get(source)
    if not isinstance(source_block, dict):
        return []
    daily = source_block.get("daily")
    return daily if isinstance(daily, list) else []


def _extract_source_intervals(claude_metrics: dict, source: str) -> dict:
    """Pull a source's `intervals_by_day` out of the scanner's by_source envelope."""
    if not isinstance(claude_metrics, dict):
        return {}
    by_source = claude_metrics.get("by_source")
    if not isinstance(by_source, dict):
        return {}
    source_block = by_source.get(source)
    if not isinstance(source_block, dict):
        return {}
    intervals = source_block.get("intervals_by_day")
    return intervals if isinstance(intervals, dict) else {}


def _build_by_source_payload(
    device_id: str,
    claude_daily: list,
    codex_daily: list,
    cowork_daily: list,
    unknown_event_types: dict,
    inferred_role: str,
    combined_daily: list | None = None,
    extra_sources_daily: dict[str, list] | None = None,
) -> dict:
    """Build the by_source payload. Legacy `daily` mirrors claude_code for back-compat.

    The cowork source is only included when it has at least one daily entry,
    so older servers that haven't added "cowork" to their allow-list don't
    reject the payload when the user has no autonomous activity.
    """
    codex_source: dict = {"daily": codex_daily}
    if unknown_event_types:
        codex_source["_unknown_event_types"] = unknown_event_types

    by_source: dict = {
        "claude_code": {"daily": claude_daily},
        "codex": codex_source,
    }
    if cowork_daily:
        by_source["cowork"] = {"daily": cowork_daily}
    for source, daily in (extra_sources_daily or {}).items():
        if daily:
            by_source[source] = {"daily": daily}
    if combined_daily:
        by_source["combined"] = {"daily": combined_daily}

    return {
        "device_id": device_id,
        # Legacy field: mirrors claude_code for servers that haven't upgraded.
        "daily": claude_daily,
        "inferred_role": inferred_role,
        "by_source": by_source,
    }


def _chunk_daily(daily: list, chunk_size: int) -> list[list]:
    """Split a daily list into chunks of at most chunk_size entries each."""
    if not daily:
        return [[]]
    return [daily[i : i + chunk_size] for i in range(0, len(daily), chunk_size)]


def _read_device_id() -> str | None:
    if not DEVICE_PATH.exists():
        return None
    try:
        data = json.loads(DEVICE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    did = data.get("device_id")
    return did if isinstance(did, str) and did else None


def _is_disabled() -> bool:
    if os.environ.get("HOOK_DISABLED", "").lower() in ("1", "true", "yes"):
        return True
    return DISABLED_FLAG.exists()


def _read_last_upload_at() -> datetime | None:
    return _read_timestamp_file(LAST_UPLOAD_PATH)


def _read_codex_upload_at() -> datetime | None:
    return _read_timestamp_file(LAST_UPLOAD_PATH_CODEX)


def _read_timestamp_file(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _write_last_upload_at(ts: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAST_UPLOAD_PATH.write_text(ts + "\n")


def _write_codex_upload_at(ts: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAST_UPLOAD_PATH_CODEX.write_text(ts + "\n")


def _run_scan() -> dict:
    script = Path(__file__).resolve().parent / "scan_transcripts.py"
    result = subprocess.run(
        [sys.executable, str(script), "--days", str(MAX_WINDOW_DAYS)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def _post_upload(payload: dict) -> dict:
    base_url = os.environ.get("AIQRANK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/teaser/upload",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        response = json.loads(resp.read())
    if isinstance(response, dict):
        _record_latest_version(response.get("latest_plugin_version"))
    return response


def _version_tuple(s: str) -> tuple:
    """Sortable key for a semver-shaped version string.

    * Splits on the first '-' so prereleases sort BEFORE the matching
      release: '1.0.0-rc1' < '1.0.0'.
    * Pads to 4 numeric components so '1.0' compares equal to '1.0.0'.
    * Compares prerelease suffixes lexically, which is good enough for
      'rc1' < 'rc2' / 'beta' < 'rc'. Not full semver — we own both
      sides of the comparison so tighter rules aren't worth the code.
    """
    base, sep, suffix = s.partition("-")
    parts = [int(re.match(r"\d+", c).group(0)) if re.match(r"\d+", c) else 0 for c in base.split(".")]
    while len(parts) < 4:
        parts.append(0)
    is_release = 1 if not sep else 0
    return tuple(parts) + (is_release, suffix)


def _is_safe_version(s: object) -> bool:
    return (
        isinstance(s, str)
        and 0 < len(s) <= _VERSION_MAX_LEN
        and _VERSION_RE.match(s) is not None
    )


def _record_latest_version(latest: str | None) -> None:
    """Persist the server-reported latest plugin version when the local
    install is behind, or clear the stale-version flag when caught up.
    Silent on bad input or filesystem errors; the nudge is best-effort.
    Server-supplied strings are validated against a strict semver-like
    shape to block ANSI / control-character injection."""
    if not _is_safe_version(latest):
        return
    try:
        if _version_tuple(PLUGIN_VERSION) < _version_tuple(latest):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            STALE_VERSION_PATH.write_text(latest + "\n")
        else:
            STALE_VERSION_PATH.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main())
