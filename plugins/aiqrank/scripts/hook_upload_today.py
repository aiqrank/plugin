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
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_BASE_URL = "https://aiqrank.com"
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"
LOCK_PATH = CONFIG_DIR / "upload.lock"
DISABLED_FLAG = CONFIG_DIR / "disabled"
LOG_PATH = CONFIG_DIR / "hook.log"

GATE_SECONDS = 24 * 60 * 60
MAX_WINDOW_DAYS = 30


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

        scan_days = _compute_scan_days(last_upload_at)
        try:
            metrics = _run_scan(scan_days)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
            logger.info(f"error {type(e).__name__}")
            return

        daily = metrics.get("daily") if isinstance(metrics, dict) else None
        if not isinstance(daily, list) or not daily:
            logger.info("ok sessions=0 devices=" + device_id[:8])
            _write_last_upload_at(_iso_now())
            return

        payload = {
            "device_id": device_id,
            "daily": daily,
            "inferred_role": metrics.get("inferred_role") or "other",
        }

        try:
            _post_upload(payload)
        except urllib.error.HTTPError as e:
            logger.info(f"error http={e.code}")
            return
        except urllib.error.URLError:
            logger.info("error network")
            return

        logger.info(f"ok sessions={len(daily)} devices={device_id[:8]}")
        _write_last_upload_at(_iso_now())
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


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
    if not LAST_UPLOAD_PATH.exists():
        return None
    try:
        raw = LAST_UPLOAD_PATH.read_text().strip()
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


def _compute_scan_days(last_upload_at: datetime | None) -> int:
    if last_upload_at is None:
        return MAX_WINDOW_DAYS
    today = date.today()
    last_date = last_upload_at.astimezone(timezone.utc).date()
    delta_days = (today - last_date).days + 1
    if delta_days < 1:
        return 1
    if delta_days > MAX_WINDOW_DAYS:
        return MAX_WINDOW_DAYS
    return delta_days


def _run_scan(days: int) -> dict:
    script = Path(__file__).resolve().parent / "scan_transcripts.py"
    cutoff_ts = time.time() - (days * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = subprocess.run(
        [sys.executable, str(script), "--days", str(days), "--mtime-after", cutoff_iso],
        capture_output=True,
        text=True,
        check=True,
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
