#!/usr/bin/env python3
"""Scheduled AIQ Rank update entrypoint.

Runs scan -> role inference -> upload in one non-interactive foreground
command. Intended for Cowork schedules and other automation; unlike the
SessionStart hook, this does not detach and does not open a browser.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_transcripts  # noqa: E402
import upload_metrics  # noqa: E402
from infer_role import classify_role  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--host-home", default=None)
    args = parser.parse_args(argv)

    prior_host_home = os.environ.get("AIQRANK_HOST_HOME")
    try:
        if args.host_home:
            os.environ["AIQRANK_HOST_HOME"] = args.host_home

        metrics = scan_transcripts.scan(window_days=args.days)
    finally:
        if args.host_home:
            if prior_host_home is None:
                os.environ.pop("AIQRANK_HOST_HOME", None)
            else:
                os.environ["AIQRANK_HOST_HOME"] = prior_host_home

    if not _has_daily_metrics(metrics):
        print("AIQ Rank daily update skipped: no daily metrics")
        return 0

    sample = metrics.get("first_messages_sample")
    if not isinstance(sample, list):
        sample = []
    role = classify_role(sample).get("inferred_role") or "engineer"

    _configure_upload_paths(args.host_home)
    metrics_path = _write_temp_metrics(metrics)
    try:
        rc = upload_metrics.main(
            ["--metrics", str(metrics_path), "--role", role, "--no-open"]
        )
    finally:
        try:
            metrics_path.unlink()
        except OSError:
            pass

    if rc == 0:
        print("AIQ Rank daily update complete")
    return rc


def _has_daily_metrics(metrics: dict) -> bool:
    by_source = metrics.get("by_source") if isinstance(metrics, dict) else None
    if isinstance(by_source, dict):
        return any(
            isinstance(block, dict)
            and isinstance(block.get("daily"), list)
            and len(block["daily"]) > 0
            for block in by_source.values()
        )

    daily = metrics.get("daily") if isinstance(metrics, dict) else None
    return isinstance(daily, list) and len(daily) > 0


def _write_temp_metrics(metrics: dict) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="aiqrank_metrics_", suffix=".json")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(metrics, fh)
            fh.write("\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def _configure_upload_paths(host_home_override: str | None) -> None:
    home = _upload_config_home(host_home_override)
    upload_metrics.CONFIG_DIR = home / ".config" / "aiqrank"
    upload_metrics.DEVICE_PATH = upload_metrics.CONFIG_DIR / "device.json"
    upload_metrics.LAST_UPLOAD_PATH = upload_metrics.CONFIG_DIR / "last_upload_at"


def _upload_config_home(host_home_override: str | None) -> Path:
    if host_home_override:
        return Path(host_home_override).expanduser()

    homes = scan_transcripts._host_homes()
    for home in homes:
        if (home / ".config" / "aiqrank" / "device.json").is_file():
            return home
    return homes[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
