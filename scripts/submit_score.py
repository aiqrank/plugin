#!/usr/bin/env python3
"""
Submit raw scanner metrics to MyAIScore's API. The server computes the
tier and score; the client only sends counts.

Usage:
  python3 submit_score.py --metrics metrics.json --role engineer \
      [--base-url https://myaiscore.com]

`--metrics` is the output of `scan_transcripts.py` — raw counts and
distributions. `--role` is the user-confirmed role (suggested by
`infer_role.py`, confirmed or overridden by the user in the skill flow).

Privacy: the client strips `first_messages_sample` from the metrics
before transmission. The server never sees conversation content.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://myaiscore.com"
CONFIG_PATH = Path.home() / ".config" / "myaiscore" / "config.json"

VALID_ROLES = {"engineer", "product", "gtm", "research", "devops", "ops", "design", "other"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--metrics",
        dest="metrics_path",
        required=True,
        help="Path to scan_transcripts.py output (raw scanner metrics)",
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=sorted(VALID_ROLES),
        help="User-confirmed role (suggested by infer_role.py)",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("MYAISCORE_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)

    token = load_token()
    if not token:
        print("✗ No API token found — run /myaiscore setup first.", file=sys.stderr)
        return 2

    with open(args.metrics_path, "r") as fh:
        metrics = json.load(fh)

    payload = build_payload(metrics, args.role)

    try:
        response = submit(args.base_url.rstrip("/"), token, payload)
    except urllib.error.HTTPError as exc:
        return handle_http_error(exc)
    except urllib.error.URLError as exc:
        print(f"✗ Couldn't reach {args.base_url}: {exc}", file=sys.stderr)
        return 3

    print(json.dumps(response, indent=2))
    update_last_scan_at()
    return 0


def build_payload(metrics: dict, role: str) -> dict:
    """
    Send only raw scanner counts plus the confirmed role + computed_at.
    Strip anything text-based, derived, or pre-scored — the server runs
    the scoring formula itself so users can't tamper with their tier.
    """
    # Allowlist of fields permitted in the transmitted payload. Keeps the
    # wire format explicit; any scanner additions must be added here to be
    # transmitted. Message text (first_messages_sample) and any derived
    # display stats are intentionally omitted.
    RAW_FIELDS = {
        "window_days",
        "sessions",
        "main_sessions",
        "max_concurrent_sessions",
        "messages",
        "max_messages_in_session",
        "tool_calls",
        "sessions_with_tools",
        "sessions_with_orchestration",
        "sessions_with_context_leverage",
        "parallel_agent_turns",
        "max_parallel_agents",
        "custom_skill_files_written",
        "custom_mcp_config_writes",
        "user_messages",
        "user_corrections",
        "tokens_input",
        "tokens_output",
        "tokens_cache_read",
        "tokens_cache_creation",
        "tokens_total",
        "tool_name_counts",
        "skill_counts",
        "mcp_server_counts",
        "agent_type_counts",
    }

    payload = {k: v for k, v in metrics.items() if k in RAW_FIELDS}
    payload["inferred_role"] = role
    payload["computed_at"] = iso_now()
    return payload


def submit(base_url: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/scores",
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
        print("  Regenerate one at myaiscore.com/users/settings.", file=sys.stderr)
        return 4

    if exc.code == 422:
        print(f"✗ Metrics rejected (validation): {body}", file=sys.stderr)
        return 5

    if exc.code == 400:
        print(f"✗ Metrics rejected (anomaly guard): {body}", file=sys.stderr)
        return 6

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
    # Use UTC ISO8601 with Z suffix (server expects ISO8601)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
