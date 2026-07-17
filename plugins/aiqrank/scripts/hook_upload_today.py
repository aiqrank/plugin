#!/usr/bin/env python3
"""Silent background uploader — runs on SessionStart and SessionEnd.

SessionStart detaches and exits so the upload can take longer than the
hook timeout without blocking the prompt. SessionEnd (used in Claude
Code on the web, where containers are torn down at session end) runs
synchronously and relies on the hook timeout to bound runtime.

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
from install_codex import install_bundled  # noqa: E402
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
LOCK_PATH = CONFIG_DIR / "upload.lock"
DISABLED_FLAG = CONFIG_DIR / "disabled"
LOG_PATH = CONFIG_DIR / "hook.log"
# Server-reported latest version. Written when local < latest, removed when
# local catches up. Read by hook_nudge_if_stale.py to print one-line nudge.
STALE_VERSION_PATH = CONFIG_DIR / "stale_version"

MAX_WINDOW_DAYS = 30
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Payload size guard: every serialized request must stay below this threshold.
PAYLOAD_SIZE_LIMIT_BYTES = 400 * 1024
INLINE_SCAN_SOURCES = ("opencode", "cursor", "pi")


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

    In Claude Code on the web (CLAUDE_CODE_REMOTE=true), the container
    is reclaimed as soon as the session ends, so a detached child gets
    killed mid-upload. Run synchronously instead and rely on the hook's
    timeout to bound runtime.
    """
    if os.environ.get("AIQRANK_DETACHED") == "1":
        return
    if _is_cloud_remote():
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
    # In Claude Code on the web, SessionStart fires on a fresh container
    # before the user has done anything, so there's nothing to upload.
    # SessionEnd owns the cloud upload path. Skip SessionStart entirely
    # in cloud mode to avoid a wasted scan + POST every session. Local
    # mode keeps running both hooks (the daily gate makes the second one
    # a fast `gated` no-op).
    if _is_cloud_remote() and os.environ.get("AIQRANK_HOOK") == "session_start":
        logger.info("cloud skip session_start")
        return

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
        # Cloud containers are ephemeral and each session's data is
        # distinct, so skip the once-per-UTC-day gate. The server is
        # idempotent on (device_id, date), so re-uploading is safe.
        if last_upload_at is not None and not _is_cloud_remote():
            today_utc = datetime.now(timezone.utc).date()
            if last_upload_at.astimezone(timezone.utc).date() == today_utc:
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

        codex_data = _maybe_scan_codex(logger, claude_metrics)

        codex_daily = (codex_data or {}).get("daily") or []
        unknown_event_types = (codex_data or {}).get("_unknown_event_types") or {}

        if (
            not claude_daily
            and not cowork_daily
            and not codex_daily
            and not any(inline_source_daily.values())
        ):
            # Don't advance last_upload_at on an empty scan: a degenerate
            # result (scanner couldn't find sessions, transit cursor glitch,
            # etc.) must not arm the gate and lock the user out for the rest
            # of the UTC day.
            logger.info("ok sessions=0 devices=" + device_id[:8])
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
        logger.info(
            f"ok sessions={len(claude_daily)} cowork_days={len(cowork_daily)} "
            f"codex_days={len(codex_daily)} "
            f"opencode_days={len(inline_source_daily.get('opencode') or [])} "
            f"cursor_days={len(inline_source_daily.get('cursor') or [])} "
            f"pi_days={len(inline_source_daily.get('pi') or [])} "
            f"devices={device_id[:8]}"
        )

    finally:
        _release_lock(lock_fd)
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _maybe_scan_codex(logger: logging.Logger, full_scan: dict) -> dict | None:
    """Return the canonical Codex block already produced by the full scan.

    `partial` results are upload-safe because the scanner has removed every
    localized bad date. A `failed` result has an unlocalizable failure, so the
    Codex source is omitted while other complete sources can still upload.
    """
    if not CODEX_SESSIONS_DIR.is_dir():
        return None

    plugin_root = Path(__file__).resolve().parent.parent
    try:
        install_bundled(plugin_root, warn=logger.info)
    except Exception as exc:
        logger.info(f"codex managed install error {type(exc).__name__}")

    by_source = full_scan.get("by_source") if isinstance(full_scan, dict) else None
    codex = by_source.get("codex") if isinstance(by_source, dict) else None
    if not isinstance(codex, dict):
        return None

    completeness = codex.get("completeness")
    status = completeness.get("status") if isinstance(completeness, dict) else "complete"
    failure_count = completeness.get("failure_count", 0) if isinstance(completeness, dict) else 0
    failure_count = failure_count if isinstance(failure_count, int) and failure_count >= 0 else 0
    omitted_dates = completeness.get("omitted_dates", []) if isinstance(completeness, dict) else []
    omitted_count = len(omitted_dates) if isinstance(omitted_dates, list) else 0

    if status == "failed":
        logger.info(f"codex completeness failed failures={failure_count}")
        return None
    if status == "partial":
        logger.info(
            f"codex completeness partial failures={failure_count} omitted_dates={omitted_count}"
        )
    return codex


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
    """POST byte-bounded source snapshots with compatibility retries."""
    extra_sources_daily = extra_sources_daily or {}
    source_daily = {
        "claude_code": list(claude_daily),
        "codex": list(codex_daily),
        "cowork": list(cowork_daily),
        **{source: list(daily) for source, daily in extra_sources_daily.items()},
        "combined": list(combined_daily or []),
    }
    retryable_sources = {"cowork", "combined", *extra_sources_daily.keys()}
    try:
        chunks = _build_byte_bounded_chunks(
            device_id,
            source_daily,
            unknown_event_types,
            inferred_role,
        )
    except ValueError:
        logger.info("error payload entry exceeds limit")
        return False

    accepted_sources: set[str] = set()
    rejected_sources: set[str] = set()
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        for source in rejected_sources:
            chunk["sources"].pop(source, None)
        if not _chunk_has_content(chunk):
            index += 1
            continue

        payload = _payload_from_source_map(
            device_id,
            chunk["sources"],
            chunk["unknown_event_types"],
            inferred_role,
        )
        try:
            _post_upload(payload)
            accepted_sources.update(
                source for source, daily in chunk["sources"].items() if daily
            )
            index += 1
        except urllib.error.HTTPError as e:
            unknown_source = _unknown_source_from_error(e)
            if (
                unknown_source not in retryable_sources
                or unknown_source not in chunk["sources"]
                or not chunk["sources"].get(unknown_source)
            ):
                logger.info(f"error chunk={index} http={e.code}")
                return False
            if unknown_source in accepted_sources:
                logger.info(
                    f"error chunk={index} source={unknown_source} partial snapshot"
                )
                return False
            logger.info(
                f"server rejected {unknown_source} source, retrying without it"
            )
            rejected_sources.add(unknown_source)
            for remaining in chunks[index:]:
                remaining["sources"].pop(unknown_source, None)
            # Retry this chunk if supported data or metadata remains.
            continue
        except urllib.error.URLError:
            logger.info(f"error chunk={index} network")
            return False
        except json.JSONDecodeError:
            logger.info(f"error chunk={index} json")
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
    finally:
        err.close()
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
    by_source: dict = {
        "claude_code": {"daily": claude_daily},
    }
    if codex_daily or unknown_event_types:
        codex_source: dict = {"daily": codex_daily}
        if unknown_event_types:
            codex_source["_unknown_event_types"] = unknown_event_types
        by_source["codex"] = codex_source
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


def _payload_from_source_map(
    device_id: str,
    sources: dict[str, list],
    unknown_event_types: dict,
    inferred_role: str,
) -> dict:
    extras = {
        source: daily
        for source, daily in sources.items()
        if source not in {"claude_code", "codex", "cowork", "combined"}
    }
    return _build_by_source_payload(
        device_id,
        sources.get("claude_code", []),
        sources.get("codex", []),
        sources.get("cowork", []),
        unknown_event_types,
        inferred_role,
        combined_daily=sources.get("combined", []),
        extra_sources_daily=extras,
    )


def _build_byte_bounded_chunks(
    device_id: str,
    source_daily: dict[str, list],
    unknown_event_types: dict,
    inferred_role: str,
) -> list[dict]:
    """Partition source rows without ever producing an oversized request."""
    source_order = list(source_daily)
    full = {
        "sources": {source: list(daily) for source, daily in source_daily.items()},
        "unknown_event_types": dict(unknown_event_types),
    }
    if _encoded_payload_size(device_id, full, inferred_role) <= PAYLOAD_SIZE_LIMIT_BYTES:
        return [full]

    metadata_only = {
        "sources": {},
        "unknown_event_types": dict(unknown_event_types),
    }
    if (
        _encoded_payload_size(device_id, metadata_only, inferred_role)
        > PAYLOAD_SIZE_LIMIT_BYTES
    ):
        raise ValueError("upload metadata exceeds payload size limit")

    items = []
    max_rows = max((len(rows) for rows in source_daily.values()), default=0)
    for row_index in range(max_rows):
        for source in source_order:
            rows = source_daily[source]
            if row_index < len(rows):
                items.append((source, rows[row_index]))

    chunks = []
    current = metadata_only
    for source, row in items:
        candidate = _copy_chunk(current)
        candidate["sources"].setdefault(source, []).append(row)
        if _encoded_payload_size(device_id, candidate, inferred_role) <= PAYLOAD_SIZE_LIMIT_BYTES:
            current = candidate
            continue

        if _chunk_has_content(current):
            chunks.append(current)
            current = {"sources": {}, "unknown_event_types": {}}
        candidate = _copy_chunk(current)
        candidate["sources"].setdefault(source, []).append(row)
        if _encoded_payload_size(device_id, candidate, inferred_role) > PAYLOAD_SIZE_LIMIT_BYTES:
            raise ValueError("single source row exceeds payload size limit")
        current = candidate

    if _chunk_has_content(current):
        chunks.append(current)
    return chunks


def _encoded_payload_size(device_id: str, chunk: dict, inferred_role: str) -> int:
    payload = _payload_from_source_map(
        device_id,
        chunk["sources"],
        chunk["unknown_event_types"],
        inferred_role,
    )
    return len(json.dumps(payload).encode("utf-8"))


def _copy_chunk(chunk: dict) -> dict:
    return {
        "sources": {
            source: list(daily) for source, daily in chunk["sources"].items()
        },
        "unknown_event_types": dict(chunk["unknown_event_types"]),
    }


def _chunk_has_content(chunk: dict) -> bool:
    return bool(chunk["unknown_event_types"]) or any(chunk["sources"].values())


_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,128}")


def _read_device_id() -> str | None:
    # Env-var fallback so cloud/CI environments can seed identity
    # without a setup script that writes device.json into an ephemeral
    # ~/.config/aiqrank. Validate shape because the value lands in log
    # lines (`device_id[:8]`) and the upload payload — a newline-bearing
    # value would forge log entries; whitespace or pathological values
    # would produce a malformed identity.
    env_id = (os.environ.get("AIQRANK_DEVICE_ID") or "").strip()
    if env_id and _DEVICE_ID_RE.fullmatch(env_id):
        return env_id
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


def _is_cloud_remote() -> bool:
    # Accept the same truthy spellings _is_disabled does — strict
    # `== "true"` would silently disable the cloud branches if Claude
    # Code on the web ever emits "1" / "True" / "yes".
    return os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() in ("1", "true", "yes")


def _scan_timeout_sec() -> int:
    # Cloud SessionEnd is bounded at 60s wall clock by Claude Code (see
    # hooks.json), and the synchronous pipeline stacks _run_scan +
    # _maybe_scan_codex + N × _post_upload — so each leg must fit
    # comfortably inside that ceiling. Local (detached) runs keep the
    # original generous budget.
    return 20 if _is_cloud_remote() else 60


def _upload_timeout_sec() -> int:
    return 10 if _is_cloud_remote() else 30


def _read_last_upload_at() -> datetime | None:
    return _read_timestamp_file(LAST_UPLOAD_PATH)


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


def _run_scan() -> dict:
    script = Path(__file__).resolve().parent / "scan_transcripts.py"
    result = subprocess.run(
        [sys.executable, str(script), "--days", str(MAX_WINDOW_DAYS)],
        capture_output=True,
        text=True,
        check=True,
        timeout=_scan_timeout_sec(),
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
    with urllib.request.urlopen(req, timeout=_upload_timeout_sec()) as resp:
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
