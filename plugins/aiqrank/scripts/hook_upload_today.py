#!/usr/bin/env python3
"""Silent background uploader — SessionStart hook.

Never writes to stdout/stderr. Logs to ~/.config/aiqrank/hook.log
(rotated at ~1MB, mode 0600). Always exits 0 so Claude Code startup
is never disrupted.

Python stdlib only.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infer_role import classify_role  # noqa: E402

DEFAULT_BASE_URL = "https://aiqrank.com"
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"
LAST_UPLOAD_PATH_CODEX = CONFIG_DIR / "last_upload_at_codex"
LOCK_PATH = CONFIG_DIR / "upload.lock"
DISABLED_FLAG = CONFIG_DIR / "disabled"
LOG_PATH = CONFIG_DIR / "hook.log"

GATE_SECONDS = 24 * 60 * 60
MAX_WINDOW_DAYS = 30
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Payload size guard: if the serialized JSON exceeds this, chunk Codex daily
# into <=7-day windows and POST sequentially.
PAYLOAD_SIZE_LIMIT_BYTES = 400 * 1024
CODEX_CHUNK_DAYS = 7


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


def main() -> int:
    try:
        logger = _setup_logger()
    except Exception:
        # Can't even log — bail silently.
        return 0

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
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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

        claude_daily = claude_metrics.get("daily") if isinstance(claude_metrics, dict) else None
        if not isinstance(claude_daily, list):
            claude_daily = []

        if not claude_daily:
            logger.info("ok sessions=0 devices=" + device_id[:8])
            _write_last_upload_at(_iso_now())
            return

        sample = claude_metrics.get("first_messages_sample") if isinstance(claude_metrics, dict) else None
        if not isinstance(sample, list):
            sample = []
        inferred_role = classify_role(sample).get("inferred_role") or "engineer"

        codex_data = _maybe_scan_codex(logger)

        if codex_data is None:
            payload = {
                "device_id": device_id,
                "daily": claude_daily,
                "inferred_role": inferred_role,
            }
            try:
                _post_upload(payload)
            except urllib.error.HTTPError as e:
                logger.info(f"error http={e.code}")
                return
            except urllib.error.URLError:
                logger.info("error network")
                return

            logger.info(f"ok sessions={len(claude_daily)} devices={device_id[:8]}")
            _write_last_upload_at(_iso_now())
        else:
            codex_daily = codex_data.get("daily") or []
            unknown_event_types = codex_data.get("_unknown_event_types") or {}

            success = _post_by_source(
                device_id,
                claude_daily,
                codex_daily,
                unknown_event_types,
                inferred_role,
                logger,
            )
            if not success:
                return

            now_iso = _iso_now()
            _write_last_upload_at(now_iso)
            _write_codex_upload_at(now_iso)
            logger.info(
                f"ok sessions={len(claude_daily)} codex_days={len(codex_daily)} devices={device_id[:8]}"
            )

    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
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
    unknown_event_types: dict,
    inferred_role: str,
    logger: logging.Logger,
) -> bool:
    """POST by_source payload, chunking Codex if payload exceeds PAYLOAD_SIZE_LIMIT_BYTES.

    Claude daily is always sent in full on each POST (assumed small enough).
    On the final successful chunk, caller should update cursors.

    Returns True on full success, False on any HTTP/network error.
    """
    # Try a single full POST first.
    payload = _build_by_source_payload(
        device_id, claude_daily, codex_daily, unknown_event_types, inferred_role
    )
    body_bytes = len(json.dumps(payload).encode("utf-8"))

    if body_bytes <= PAYLOAD_SIZE_LIMIT_BYTES:
        try:
            _post_upload(payload)
            return True
        except urllib.error.HTTPError as e:
            logger.info(f"error http={e.code}")
            return False
        except urllib.error.URLError:
            logger.info("error network")
            return False

    # Over limit: chunk Codex daily into <=CODEX_CHUNK_DAYS-day windows.
    logger.info(f"codex payload {body_bytes}B > limit, chunking")
    chunks = _chunk_daily(codex_daily, CODEX_CHUNK_DAYS)

    for i, chunk in enumerate(chunks):
        chunk_payload = _build_by_source_payload(
            device_id, claude_daily, chunk, unknown_event_types if i == 0 else {}, inferred_role
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


def _build_by_source_payload(
    device_id: str,
    claude_daily: list,
    codex_daily: list,
    unknown_event_types: dict,
    inferred_role: str,
) -> dict:
    """Build the by_source payload. Legacy `daily` mirrors claude_code for back-compat."""
    codex_source: dict = {"daily": codex_daily}
    if unknown_event_types:
        codex_source["_unknown_event_types"] = unknown_event_types

    return {
        "device_id": device_id,
        # Legacy field: mirrors claude_code for servers that haven't upgraded.
        "daily": claude_daily,
        "inferred_role": inferred_role,
        "by_source": {
            "claude_code": {"daily": claude_daily},
            "codex": codex_source,
        },
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
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main())
