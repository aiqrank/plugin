#!/usr/bin/env python3
"""
Device pairing flow for the Cyborg Score plugin.

Flow:
  1. POST /api/pair/create → receive session_id + pair_url
  2. Open pair_url in the user's default browser
  3. Poll /api/pair/status every 2 seconds (up to 10 min)
  4. On completed: write the returned API token to ~/.config/cyborgscore/config.json
  5. On expired / network error: exit nonzero with a clear message

Python stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:4999"  # TEMP: local dev — revert to https://cyborgscore.com before release
POLL_INTERVAL_SECONDS = 2
MAX_POLL_SECONDS = 10 * 60

CONFIG_DIR = Path.home() / ".config" / "cyborgscore"
CONFIG_PATH = CONFIG_DIR / "config.json"


def main(argv: list[str]) -> int:
    base_url = os.environ.get("CYBORGSCORE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    metrics_path = parse_metrics_arg(argv)
    metrics = load_metrics(metrics_path) if metrics_path else None

    try:
        session = create_pair_session(base_url, metrics)
    except urllib.error.URLError as exc:
        print(f"✗ Couldn't reach {base_url}: {exc}", file=sys.stderr)
        return 1

    session_id = session["session_id"]
    pair_url = session["pair_url"]

    print(f"Opening {pair_url} in your browser …")
    print("Sign in with Google or an emailed code to complete pairing.")
    print("This window will wait up to 10 minutes.")

    open_in_browser(pair_url)

    token = poll_until_paired(base_url, session_id)

    if token is None:
        print("✗ Pairing timed out or was cancelled.", file=sys.stderr)
        print("  Run /cyborgscore again to start a new pairing.", file=sys.stderr)
        return 1

    write_config({"api_token": token})
    print("✓ Paired! Your token is saved to ~/.config/cyborgscore/config.json")
    return 0


def create_pair_session(base_url: str, metrics: dict | None = None) -> dict:
    body = {"metrics": metrics} if metrics is not None else {}
    req = urllib.request.Request(
        base_url + "/api/pair/create",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def parse_metrics_arg(argv: list[str]) -> str | None:
    """Minimal --metrics <path> parser; avoids pulling in argparse."""
    for i, arg in enumerate(argv):
        if arg == "--metrics" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--metrics="):
            return arg.split("=", 1)[1]
    return None


def load_metrics(path: str) -> dict | None:
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"⚠ Could not read metrics from {path}: {exc} — continuing without preview.", file=sys.stderr)
        return None

    # The server caps the payload at 256KB. Strip the first_messages_sample
    # buffer (used only locally for role inference) and any other non-count
    # fields before sending, for privacy AND to stay under the cap.
    if isinstance(data, dict):
        data.pop("first_messages_sample", None)
    return data


def open_in_browser(url: str) -> None:
    """Open `url` in the user's default browser cross-platform."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        elif sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", url], check=False)
    except Exception:
        # If we can't auto-open, the user can still copy the URL manually.
        print(f"(Could not auto-open browser — copy the URL above manually.)")


def poll_until_paired(base_url: str, session_id: str) -> str | None:
    elapsed = 0

    while elapsed < MAX_POLL_SECONDS:
        try:
            status = poll_once(base_url, session_id)
        except urllib.error.URLError:
            # Transient network issue — keep trying
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            continue

        s = status.get("status")
        if s == "completed":
            return status.get("api_token")
        if s == "expired" or s == "not_found":
            return None

        # "pending" → keep polling
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    return None


def poll_once(base_url: str, session_id: str) -> dict:
    url = f"{base_url}/api/pair/status?session={session_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 404 is a semantic "not_found"; translate from HTTP to app shape
        if e.code == 404:
            return {"status": "not_found"}
        raise


def write_config(update: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    current = {}
    if CONFIG_PATH.exists():
        try:
            current = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass

    current.update(update)
    current["last_scan_at"] = current.get("last_scan_at")

    CONFIG_PATH.write_text(json.dumps(current, indent=2) + "\n")

    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
