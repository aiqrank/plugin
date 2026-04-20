#!/usr/bin/env python3
"""
Submit per-day scanner metrics to AIQ Rank's API.

Flow:
  1. GET /api/metrics/latest_date with the device token → cursor date
     (or None if the user has no records yet).
  2. Filter the scanner's `daily` array to entries strictly after the
     cursor — that's the delta we need to upload.
  3. POST /api/metrics with the daily delta + role + computed_at.
  4. Server upserts the daily rows, computes a 30-day rollup, scores it,
     and returns the same response shape the user is used to.

Privacy: `first_messages_sample` is stripped before transmission.
Each day's metrics is also stripped to allowlisted scanner fields.

Usage:
  python3 submit_score.py --metrics metrics.json --role engineer \
      [--base-url https://aiqrank.com]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:4999"  # TEMP: local dev — revert to https://aiqrank.com before release
CONFIG_PATH = Path.home() / ".config" / "aiqrank" / "config.json"

VALID_ROLES = {
    "engineer", "product", "marketing", "sales", "revops", "research",
    "devops", "ops", "design", "founder", "executive", "other",
}

# Allowlist of scanner field names permitted in each per-day metrics blob.
# Must match AIQRank.Metrics.allowed_metric_keys/0 on the server.
ALLOWED_METRIC_KEYS = {
    "sessions",
    "main_sessions",
    "messages",
    "tool_calls",
    "sessions_with_tools",
    "sessions_with_orchestration",
    "sessions_with_context_leverage",
    "sessions_with_plan_mode",
    "parallel_agent_turns",
    "custom_skill_files_written",
    "custom_mcp_config_writes",
    "claude_md_writes",
    "user_messages",
    "user_corrections",
    "plan_mode_invocations",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_creation",
    "tokens_total",
    "max_concurrent_sessions",
    "max_messages_in_session",
    "max_parallel_agents",
    "tool_name_counts",
    "skill_counts",
    "mcp_server_counts",
    "agent_type_counts",
}

DEFAULT_BACKFILL_DAYS = 30


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--metrics",
        dest="metrics_path",
        required=True,
        help="Path to scan_transcripts.py output (with `daily` array)",
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=sorted(VALID_ROLES),
        help="User-confirmed role (suggested by infer_role.py)",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("AIQRANK_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)

    token = load_token()
    if not token:
        print("✗ No API token found — run /aiqrank setup first.", file=sys.stderr)
        return 2

    with open(args.metrics_path, "r") as fh:
        metrics = json.load(fh)

    daily = metrics.get("daily") or []
    if not isinstance(daily, list) or not daily:
        print("✗ Scanner output has no `daily` array — nothing to upload.", file=sys.stderr)
        return 8

    base_url = args.base_url.rstrip("/")

    try:
        cursor = fetch_latest_date(base_url, token)
    except urllib.error.HTTPError as exc:
        return handle_http_error(exc)
    except urllib.error.URLError as exc:
        print(f"✗ Couldn't reach {args.base_url}: {exc}", file=sys.stderr)
        return 3

    delta = filter_after_cursor(daily, cursor)
    if not delta:
        print(f"✓ Already up to date — server has data through {cursor}.")
        return 0

    payload = build_payload(delta, args.role)

    try:
        response = submit(base_url, token, payload)
    except urllib.error.HTTPError as exc:
        return handle_http_error(exc)
    except urllib.error.URLError as exc:
        print(f"✗ Couldn't reach {args.base_url}: {exc}", file=sys.stderr)
        return 3

    print(json.dumps(response, indent=2))
    update_last_scan_at()
    return 0


def fetch_latest_date(base_url: str, token: str) -> date | None:
    """GET /api/metrics/latest_date → date or None."""
    req = urllib.request.Request(
        base_url + "/api/metrics/latest_date",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())

    raw = body.get("latest_date")
    if raw is None:
        return None
    return date.fromisoformat(raw)


def filter_after_cursor(daily: list, cursor: date | None) -> list:
    """Keep entries whose date is on or after the cursor.

    `>=` (not `>`) so that re-running the plugin later the same day
    re-uploads today's fresher numbers — the server's upsert is replace,
    so last write wins. With strict `>`, a same-day re-run would never
    update today's row.

    If cursor is None, default to today − 30 so a brand-new user
    backfills 30 days."""
    if cursor is None:
        cursor = date.today() - timedelta(days=DEFAULT_BACKFILL_DAYS - 1)

    out = []
    for entry in daily:
        if not isinstance(entry, dict):
            continue
        date_str = entry.get("date")
        if not isinstance(date_str, str):
            continue
        try:
            entry_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        if entry_date >= cursor:
            out.append(entry)
    return out


def build_payload(daily_delta: list, role: str) -> dict:
    """Sanitize each day's metrics to allowlisted fields and assemble
    the request body. The server also sanitizes — this is defense-in-depth
    so an old plugin can't accidentally leak unknown fields if the server
    relaxes its allowlist."""
    sanitized = []
    for entry in daily_delta:
        m = entry.get("metrics") or {}
        sanitized.append(
            {
                "date": entry["date"],
                "metrics": {k: v for k, v in m.items() if k in ALLOWED_METRIC_KEYS},
            }
        )

    return {
        "daily": sanitized,
        "inferred_role": role,
        "computed_at": iso_now(),
    }


def submit(base_url: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/metrics",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def handle_http_error(exc: urllib.error.HTTPError) -> int:
    body = ""
    try:
        body = exc.read().decode("utf-8")
    except Exception:
        pass

    if exc.code == 401:
        print("✗ Your API token is invalid or expired.", file=sys.stderr)
        print("  Regenerate one at aiqrank.com/users/settings.", file=sys.stderr)
        return 4

    if exc.code == 422:
        print(f"✗ Metrics rejected (validation): {body}", file=sys.stderr)
        return 5

    if exc.code == 400:
        print(f"✗ Metrics rejected (anomaly guard): {body}", file=sys.stderr)
        return 6

    if exc.code == 413:
        print(f"✗ Metrics payload too large: {body}", file=sys.stderr)
        return 9

    print(f"✗ Server error {exc.code}: {body}", file=sys.stderr)
    return 7


def load_token() -> str | None:
    if not CONFIG_PATH.exists():
        return None

    try:
        data = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return None

    return data.get("api_token")


def update_last_scan_at() -> None:
    if not CONFIG_PATH.exists():
        return

    try:
        data = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return

    data["last_scan_at"] = iso_now()
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
