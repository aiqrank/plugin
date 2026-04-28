#!/usr/bin/env python3
"""Company-membership CLI — backs the /aiqrank:company slash command.

Subcommands:
    list                  — list companies the user belongs to
    join <token>          — fetch consent text, prompt, redeem token
    leave <slug>          — confirm and leave a company

Auth via ~/.config/aiqrank/device.json. If the device isn't paired, prints
the teaser URL so the user can sign in. Errors print one short line.

Python stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _version import USER_AGENT  # noqa: E402

DEFAULT_BASE_URL = "https://aiqrank.com"
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiqrank-company", add_help=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="list companies you belong to")
    join = sub.add_parser("join", help="redeem an invite token")
    join.add_argument("token", help="invite token from a company admin")
    leave = sub.add_parser("leave", help="leave a company")
    leave.add_argument("slug", help="company slug")
    args = parser.parse_args(argv)

    device_id = _read_device_id()
    if device_id is None:
        _err(
            "Sign in first. Run /aiqrank to pair this machine, then retry."
        )
        return 1

    if args.action == "list":
        return _cmd_list(device_id)
    if args.action == "join":
        return _cmd_join(device_id, args.token)
    if args.action == "leave":
        return _cmd_leave(device_id, args.slug)
    return 2


# --- subcommands ------------------------------------------------------------


def _cmd_list(device_id: str) -> int:
    body, status = _request("GET", f"/api/plugin/companies/list?device_id={device_id}")
    if status != 200:
        return _handle_auth_error(status, body)

    companies = body.get("companies", []) if isinstance(body, dict) else []
    if not companies:
        print("You are not a member of any company.")
        return 0

    width = max(len(c.get("slug", "")) for c in companies)
    for c in companies:
        slug = c.get("slug", "?")
        name = c.get("name", "?")
        role = c.get("role", "?")
        print(f"  {slug:<{width}}  {name}  ({role})")
    return 0


def _cmd_join(device_id: str, token: str) -> int:
    consent_body, status = _request("GET", "/api/plugin/companies/consent")
    if status != 200 or not isinstance(consent_body, dict):
        _err("Could not fetch consent text from server.")
        return 1
    consent_version = consent_body.get("version")
    consent_text = consent_body.get("text")
    if not isinstance(consent_version, str) or not isinstance(consent_text, str):
        _err("Server returned malformed consent payload.")
        return 1

    print(consent_text)
    print()
    answer = input('Type "yes" to accept and join: ').strip().lower()
    if answer != "yes":
        print("Cancelled.")
        return 0

    payload = {
        "device_id": device_id,
        "token": token,
        "consent_shown_at": _iso_now(),
        "consent_text_version": consent_version,
    }
    body, status = _request("POST", "/api/plugin/companies/redeem", payload)
    if status == 200 and isinstance(body, dict) and body.get("ok"):
        company = body.get("company") or {}
        slug = company.get("slug", "?")
        name = company.get("name", "?")
        role = company.get("role", "?")
        if body.get("already_member"):
            print(f"Already a member of {name} ({slug}) — role {role}.")
        else:
            print(f"Joined {name} ({slug}) — role {role}.")
        return 0

    return _handle_auth_error(status, body, default="Join failed.")


def _cmd_leave(device_id: str, slug: str) -> int:
    answer = input(f'Type "yes" to leave "{slug}": ').strip().lower()
    if answer != "yes":
        print("Cancelled.")
        return 0

    payload = {"device_id": device_id, "slug": slug}
    body, status = _request("POST", "/api/plugin/companies/leave", payload)
    if status == 200 and isinstance(body, dict) and body.get("ok"):
        print(f"Left {slug}.")
        return 0

    return _handle_auth_error(status, body, default="Leave failed.")


# --- helpers ----------------------------------------------------------------


def _read_device_id() -> str | None:
    if not DEVICE_PATH.exists():
        return None
    try:
        data = json.loads(DEVICE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    did = data.get("device_id") if isinstance(data, dict) else None
    return did if isinstance(did, str) and did else None


def _request(method: str, path: str, payload: dict | None = None) -> tuple[object, int]:
    base_url = os.environ.get("AIQRANK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    url = base_url + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _parse_body(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return _parse_body(e.read()), e.code
    except (urllib.error.URLError, TimeoutError) as e:
        _err(f"Network error: {e}")
        return None, 0


def _parse_body(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _handle_auth_error(status: int, body: object, default: str = "Request failed.") -> int:
    if status == 401 and isinstance(body, dict):
        teaser = body.get("teaser_url")
        if isinstance(teaser, str) and teaser:
            _err(f"Sign in first: {teaser}")
            return 1

    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, str):
            _err(_friendly_error(err, default))
            return 1

    if status == 0:
        return 1

    _err(f"{default} (HTTP {status})")
    return 1


_FRIENDLY_ERRORS = {
    "token_not_found": "Invite token not recognized.",
    "token_expired": "Invite token has expired — ask the admin for a new one.",
    "token_revoked": "Invite token has been revoked.",
    "token_max_uses": "Invite token has been used the maximum number of times.",
    "company_not_found": "Company not found.",
    "not_a_member": "You're not a member of that company.",
    "device_not_found": "This machine isn't paired yet — run /aiqrank first.",
    "rate_limited": "Too many requests — wait a minute and try again.",
}


def _friendly_error(code: str, default: str) -> str:
    return _FRIENDLY_ERRORS.get(code, f"{default} ({code})")


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
