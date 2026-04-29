#!/usr/bin/env python3
"""Interactive AIQ Rank upload — POST daily metrics to /api/teaser/upload
and open the returned teaser URL in the user's browser.

Replaces pair_device.py + submit_score.py. Python stdlib only.

Usage:
  python3 upload_metrics.py --metrics <path> --role <role>
  python3 upload_metrics.py --scan --role <role>    # scan first

On success: prints exactly one line to stdout:
    Opening your rank at <teaser_url>
On failure: prints exactly one line to stderr and exits 1:
    AIQ Rank upload failed: <short reason>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _version import USER_AGENT  # noqa: E402

DEFAULT_BASE_URL = "https://aiqrank.com"
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", dest="metrics_path", default=None)
    parser.add_argument("--role", required=True)
    parser.add_argument("--scan", action="store_true", help="Run scan_transcripts.py --days 30 first.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the result URL in a browser.")
    args = parser.parse_args(argv)

    base_url = os.environ.get("AIQRANK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    try:
        if args.scan:
            metrics = run_scan()
        else:
            if not args.metrics_path:
                fail("missing --metrics or --scan")
                return 1
            with open(args.metrics_path, "r") as fh:
                metrics = json.load(fh)
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        fail(f"could not read metrics ({type(exc).__name__})")
        return 1

    payload_body = build_payload(metrics, args.role)
    if payload_body is None:
        fail("no daily metrics to upload")
        return 1

    device_id = load_device_id()
    if device_id:
        payload_body["device_id"] = device_id

    try:
        response = post_upload(base_url, payload_body)
    except urllib.error.HTTPError as exc:
        fail(f"http {exc.code}")
        return 1
    except urllib.error.URLError:
        fail("network error")
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"unexpected ({type(exc).__name__})")
        return 1

    teaser_url = response.get("teaser_url")
    new_device_id = response.get("device_id")
    if not teaser_url or not new_device_id:
        fail("invalid server response")
        return 1

    save_device_id(new_device_id)
    save_last_upload_at(iso_now())

    if args.no_open:
        print(f"Rank updated at {teaser_url}")
    else:
        print(f"Opening your rank at {teaser_url}")
        open_in_browser(teaser_url)
    return 0


def run_scan() -> dict:
    script = Path(__file__).resolve().parent / "scan_transcripts.py"
    result = subprocess.run(
        [sys.executable, str(script), "--days", "30"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def build_payload(metrics: dict, role: str) -> dict | None:
    if not isinstance(metrics, dict):
        return None

    by_source = metrics.get("by_source")
    if isinstance(by_source, dict):
        daily = metrics.get("daily")
        if not isinstance(daily, list):
            daily = []
            claude = by_source.get("claude_code")
            if isinstance(claude, dict) and isinstance(claude.get("daily"), list):
                daily = claude["daily"]

        has_source_daily = any(
            isinstance(source_data, dict)
            and isinstance(source_data.get("daily"), list)
            and len(source_data["daily"]) > 0
            for source_data in by_source.values()
        )
        if not daily and not has_source_daily:
            return None
        return {"daily": daily, "by_source": by_source, "inferred_role": role}

    daily = metrics.get("daily")
    if not isinstance(daily, list) or not daily:
        return None
    return {"daily": daily, "inferred_role": role}


def post_upload(base_url: str, payload: dict) -> dict:
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
        return json.loads(resp.read())


def load_device_id() -> str | None:
    if not DEVICE_PATH.exists():
        return None
    try:
        data = json.loads(DEVICE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    did = data.get("device_id")
    return did if isinstance(did, str) and did else None


def save_device_id(device_id: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICE_PATH.write_text(json.dumps({"device_id": device_id}) + "\n")
    try:
        os.chmod(DEVICE_PATH, 0o600)
    except OSError:
        pass


def save_last_upload_at(ts: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAST_UPLOAD_PATH.write_text(ts + "\n")


def open_in_browser(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        elif sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", url], check=False)
    except Exception:
        pass


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fail(reason: str) -> None:
    print(f"AIQ Rank upload failed: {reason}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
