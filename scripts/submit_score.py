#!/usr/bin/env python3
"""
Submit a scored payload to MyAIScore's API.

Usage:
  python3 submit_score.py --from score.json [--base-url https://myaiscore.com]

Reads JSON score payload from --from file, POSTs with Authorization: Bearer
<token> (token read from ~/.config/myaiscore/config.json), prints the
returned profile URL.
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="src", required=True, help="Path to JSON score payload")
    parser.add_argument("--base-url", default=os.environ.get("MYAISCORE_BASE_URL", DEFAULT_BASE_URL))
    args = parser.parse_args(argv)

    token = load_token()
    if not token:
        print("✗ No API token found — run /myaiscore setup first.", file=sys.stderr)
        return 2

    with open(args.src, "r") as fh:
        payload = json.load(fh)

    # `stats` is a local-only summary (scanner counts, top-N lists) shown in
    # the skill preview. Strip it before transmission so the privacy promise
    # — "only the score + skill/MCP names cross the wire" — stays honest.
    payload.pop("stats", None)

    # Ensure computed_at is set (the server enforces it)
    payload.setdefault("computed_at", iso_now())

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
        print(f"✗ Score payload was rejected (validation): {body}", file=sys.stderr)
        return 5

    if exc.code == 400:
        print(f"✗ Score payload was rejected (anti-gaming check): {body}", file=sys.stderr)
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
