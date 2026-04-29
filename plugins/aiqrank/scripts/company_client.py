#!/usr/bin/env python3
"""Interactive AIQ Rank company client.

Subcommands:
  redeem <token>   Show consent text, prompt to confirm, then POST
                   /api/plugin/companies/redeem.
  leave <slug>     POST /api/plugin/companies/leave (self-leave).
  list             GET /api/plugin/companies/list and print the user's
                   companies.

Authenticates by `device_id` read from `~/.config/aiqrank/device.json`
(written by upload_metrics.py during normal pairing).

Uses Python stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _version import USER_AGENT  # noqa: E402

DEFAULT_BASE_URL = "https://aiqrank.com"
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"
CONSENT_VERSION = "v1"

CONSENT_TEMPLATE = (
    "Joining means company admins will have access to the names of skills\n"
    "and MCP servers you use, plus your daily activity metrics (sessions,\n"
    "tool calls, scores). They will not have access to your transcripts,\n"
    "prompts, or code.\n"
    "\n"
    "You can leave at any time with /aiqrank:company-leave <slug>.\n"
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="company_client.py")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive consent prompt (required for non-tty).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AIQRANK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
    )

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_redeem = sub.add_parser("redeem", help="Redeem a join token.")
    p_redeem.add_argument("token")
    p_leave = sub.add_parser("leave", help="Leave a company.")
    p_leave.add_argument("slug")
    sub.add_parser("list", help="List your companies.")

    args = parser.parse_args(argv)

    device_id = load_device_id()
    if not device_id:
        fail(
            "device not paired — run /aiqrank to scan and pair first, then re-run this command"
        )
        return 1

    if args.cmd == "redeem":
        return cmd_redeem(args.base_url, device_id, args.token, args.yes)
    if args.cmd == "leave":
        return cmd_leave(args.base_url, device_id, args.slug, args.yes)
    if args.cmd == "list":
        return cmd_list(args.base_url, device_id)

    parser.print_help()
    return 2


# --- redeem ---


def cmd_redeem(base_url: str, device_id: str, token: str, force_yes: bool) -> int:
    print(CONSENT_TEMPLATE)

    if not confirm("Join this company?", force_yes):
        print("Cancelled — no membership created.")
        return 0

    consent_shown_at = (
        _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    )

    payload = {
        "token": token,
        "device_id": device_id,
        "consent_shown_at": consent_shown_at,
        "consent_text_version": CONSENT_VERSION,
    }

    try:
        resp = http_request(
            base_url + "/api/plugin/companies/redeem", method="POST", payload=payload
        )
    except HttpError as exc:
        print_error_for_redeem(exc)
        return 1

    company = resp.get("company") or {}
    already = bool(resp.get("already_member"))
    name = company.get("name") or company.get("slug") or "the company"
    if already:
        print(f"Already a member of {name}.")
    else:
        print(f"Joined {name}.")
    return 0


def print_error_for_redeem(exc: "HttpError") -> None:
    err = exc.body.get("error") if isinstance(exc.body, dict) else None
    teaser_url = exc.body.get("teaser_url") if isinstance(exc.body, dict) else None

    if err == "device_not_found":
        msg = "device not paired with the server"
        if teaser_url:
            msg += f" — visit {teaser_url} to pair"
        fail(msg)
    elif err == "token_not_found":
        fail("invite token not found")
    elif err == "token_expired":
        fail("this invite has expired")
    elif err == "token_revoked":
        fail("this invite has been revoked")
    elif err == "token_max_uses":
        fail("this invite has reached its maximum uses")
    elif err == "rate_limited":
        fail("rate limited — wait a minute and try again")
    else:
        fail(f"server error ({exc.status})")


# --- leave ---


def cmd_leave(base_url: str, device_id: str, slug: str, force_yes: bool) -> int:
    if not confirm(f"Leave {slug}?", force_yes):
        print("Cancelled.")
        return 0

    payload = {"slug": slug, "device_id": device_id}

    try:
        http_request(
            base_url + "/api/plugin/companies/leave", method="POST", payload=payload
        )
    except HttpError as exc:
        err = exc.body.get("error") if isinstance(exc.body, dict) else None
        if err == "not_a_member":
            fail(f"you are not a member of {slug}")
        elif err == "company_not_found":
            fail(f"no company with slug {slug}")
        elif err == "last_admin":
            fail("you are the last admin — promote someone else first")
        else:
            fail(f"server error ({exc.status})")
        return 1

    print(f"Left {slug}.")
    return 0


# --- list ---


def cmd_list(base_url: str, device_id: str) -> int:
    try:
        resp = http_request(
            base_url + f"/api/plugin/companies/list?device_id={device_id}",
            method="GET",
        )
    except HttpError as exc:
        fail(f"server error ({exc.status})")
        return 1

    companies = resp.get("companies") or []
    if not companies:
        print("You have not joined any companies.")
        return 0

    width = max(len(c.get("slug", "")) for c in companies)
    for c in companies:
        slug = c.get("slug", "?")
        name = c.get("name", "?")
        role = c.get("role", "?")
        print(f"  {slug.ljust(width)}  {role:<6}  {name}")
    return 0


# --- shared ---


class HttpError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body


def http_request(url: str, *, method: str, payload=None) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (json.JSONDecodeError, OSError):
            body = {}
        raise HttpError(exc.code, body) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, {"error": f"network: {exc.reason}"}) from exc


def confirm(prompt: str, force_yes: bool) -> bool:
    if force_yes:
        return True
    if not sys.stdin.isatty():
        fail("non-interactive shell — pass --yes to skip the consent prompt")
        sys.exit(1)
    answer = input(f"{prompt} Type 'yes' to confirm: ").strip().lower()
    return answer == "yes"


def load_device_id():
    if not DEVICE_PATH.exists():
        return None
    try:
        data = json.loads(DEVICE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    did = data.get("device_id")
    return did if isinstance(did, str) and did else None


def fail(msg: str) -> None:
    print(f"AIQ Rank: {msg}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
