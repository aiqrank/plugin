#!/usr/bin/env python3
"""AIQ Rank upload — POST daily metrics to /api/teaser/upload.

The returned teaser URL is printed but not opened unless ``--open`` is passed.

Replaces pair_device.py + submit_score.py. Python stdlib only.

Usage:
  python3 upload_metrics.py --metrics <path> --role <role>
  python3 upload_metrics.py --scan --role <role>    # scan first
  python3 upload_metrics.py --metrics <path> --role <role> --open

On success: prints exactly one line to stdout:
    Rank updated at <teaser_url>
With --open:
    Opening your rank at <teaser_url>
On failure: prints exactly one line to stderr and exits 1:
    AIQ Rank upload failed: <short reason>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _version import USER_AGENT  # noqa: E402
import check_update  # noqa: E402

DEFAULT_BASE_URL = "https://aiqrank.com"
# Manual `--scan` backfills a full 30-day snapshot (~4x the 7-day background
# window), so it gets a proportionally larger safety net than the background
# hook's LOCAL_SCAN_TIMEOUT_SEC (240s). This is a hang guard, not a tight SLA:
# the CLI is interactive and Ctrl-C still works, but a bounded timeout stops an
# indefinite freeze if scan_transcripts.py stalls on a large/stuck transcript dir.
MANUAL_SCAN_TIMEOUT_SEC = 600
CONFIG_DIR = Path.home() / ".config" / "aiqrank"
DEVICE_PATH = CONFIG_DIR / "device.json"
LAST_UPLOAD_PATH = CONFIG_DIR / "last_upload_at"
STANDALONE_SOURCES = {"codex", "pi"}
RETRYABLE_SOURCES = {
    "cowork",
    "combined",
    "opencode",
    "cursor",
    "pi",
    "hermes",
    "openclaw",
    "nanoclaw",
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", dest="metrics_path", default=None)
    parser.add_argument("--role", required=True)
    parser.add_argument("--scan", action="store_true", help="Run scan_transcripts.py --days 30 first.")
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="Open the result URL in a browser after a successful upload.",
    )
    browser_group.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Do not open the result URL (default; kept for compatibility).",
    )
    parser.set_defaults(open_browser=False)
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
        response = post_upload_with_compatibility_retry(base_url, payload_body)
    except urllib.error.HTTPError as exc:
        fail(f"http {exc.code}: {_http_error_reason(exc)}")
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
    check_update.record_latest_version(response.get("latest_plugin_version"))

    if args.open_browser:
        print(f"Opening your rank at {teaser_url}")
        open_in_browser(teaser_url)
    else:
        print(f"Rank updated at {teaser_url}")
    return 0


def run_scan() -> dict:
    script = Path(__file__).resolve().parent / "scan_transcripts.py"
    result = subprocess.run(
        [sys.executable, str(script), "--days", "30"],
        capture_output=True,
        text=True,
        check=True,
        timeout=MANUAL_SCAN_TIMEOUT_SEC,
    )
    return json.loads(result.stdout)


def build_payload(metrics: dict, role: str) -> dict | None:
    if not isinstance(metrics, dict):
        return None

    by_source = metrics.get("by_source")
    if isinstance(by_source, dict):
        upload_sources = {}
        for source, source_data in by_source.items():
            if not isinstance(source, str) or not isinstance(source_data, dict):
                continue
            completeness = source_data.get("completeness")
            if (
                source == "codex"
                and isinstance(completeness, dict)
                and completeness.get("status") == "failed"
            ):
                continue
            source_daily = source_data.get("daily")
            if not isinstance(source_daily, list):
                continue
            upload_source = {"daily": source_daily}
            unknown_event_types = source_data.get("_unknown_event_types")
            if source == "codex" and isinstance(unknown_event_types, dict):
                upload_source["_unknown_event_types"] = unknown_event_types
            upload_sources[source] = upload_source

        daily = metrics.get("daily")
        if not isinstance(daily, list):
            daily = []
            claude = upload_sources.get("claude_code")
            if isinstance(claude, dict) and isinstance(claude.get("daily"), list):
                daily = claude["daily"]

        has_source_daily = any(
            source_data["daily"] for source_data in upload_sources.values()
        )
        if not daily and not has_source_daily:
            return None
        return {"daily": daily, "by_source": upload_sources, "inferred_role": role}

    source = metrics.get("source")
    daily = metrics.get("daily")
    if not isinstance(daily, list) or not daily:
        return None
    if isinstance(source, str) and source in STANDALONE_SOURCES:
        source_data = {"daily": daily}
        unknown_event_types = metrics.get("_unknown_event_types")
        if isinstance(unknown_event_types, dict):
            source_data["_unknown_event_types"] = unknown_event_types
        return {"daily": [], "by_source": {source: source_data}, "inferred_role": role}
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


def post_upload_with_compatibility_retry(base_url: str, payload: dict) -> dict:
    """Retry legacy 422 source rejections without losing core metrics."""
    by_source = payload.get("by_source")
    if not isinstance(by_source, dict):
        return post_upload(base_url, payload)

    retry_payload = dict(payload)
    retry_sources = dict(by_source)
    retry_payload["by_source"] = retry_sources

    while True:
        try:
            return post_upload(base_url, retry_payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise

            body = _read_http_error_body(exc)
            unknown_source = _unknown_source_from_body(body)
            if unknown_source is not None:
                if unknown_source not in RETRYABLE_SOURCES or unknown_source not in retry_sources:
                    _remember_http_error_body(exc, body)
                    raise
                retry_sources.pop(unknown_source, None)
                continue

            if _is_too_many_sources_body(body):
                dropped = next(
                    (
                        source
                        for source in reversed(list(retry_sources))
                        if source in RETRYABLE_SOURCES
                    ),
                    None,
                )
                if dropped is not None:
                    retry_sources.pop(dropped, None)
                    continue

            _remember_http_error_body(exc, body)
            raise


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


def _read_http_error_body(err: urllib.error.HTTPError) -> dict | None:
    try:
        body = json.loads(err.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    finally:
        err.close()
    return body if isinstance(body, dict) else None


def _remember_http_error_body(err: urllib.error.HTTPError, body: dict | None) -> None:
    # HTTPError's stream is single-read. Preserve the parsed body for the
    # caller's final diagnostic after compatibility retries are exhausted.
    setattr(err, "_aiqrank_body", body)


def _http_error_reason(err: urllib.error.HTTPError) -> str:
    """Read a bounded, log-safe explanation from a server rejection."""
    body = getattr(err, "_aiqrank_body", None)
    if body is None:
        body = _read_http_error_body(err)

    if not isinstance(body, dict):
        return "unparseable"
    error = body.get("error")
    if not isinstance(error, str) or not error:
        return "unparseable"
    reason = re.sub(r"[\x00-\x1f\x7f]", "", error)[:160]
    source = body.get("source")
    if isinstance(source, str) and source:
        source = re.sub(r"[\x00-\x1f\x7f]", "", source)[:80]
        reason = f"{reason} source={source}"
    return reason or "unparseable"


def _unknown_source_from_body(body: dict | None) -> str | None:
    if not isinstance(body, dict) or body.get("error") != "unknown source":
        return None
    source = body.get("source")
    return source if isinstance(source, str) and source else None


def _is_too_many_sources_body(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    return isinstance(error, str) and error.startswith("too many sources")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
