#!/usr/bin/env python3
"""
AIQ Rank Codex CLI scanner.

Walks ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl and extracts activity
metrics from Codex CLI rollouts modified in the last 30 days. Each rollout
file is one Codex session. Buckets every event by its local-calendar-day
timestamp, emits a `daily` array (one entry per day with activity) plus a
`rollup` aggregate over the full window.

Output JSON:

    {
      "source": "codex",
      "window_days": 30,
      "daily": [
        {"date": "YYYY-MM-DD", "metrics": { ...fields... }},
        ...
      ],
      "rollup": { ...same fields, summed/maxed/merged across days... },
      "_unknown_event_types": { "type:payload.type": N, ... }
    }

Privacy: no raw user/assistant text leaves the parser. cwd is hashed with
sha256[:12]. Only counts, names, and regex-derived booleans are emitted.

Event shape (Codex CLI v0.34.0):
  Top-level `type` is one of:
    - `session_meta`    : session boundary; payload.{cwd, cli_version, git, ...}
    - `event_msg`       : payload.type discriminates kind
        - `user_message`       → user_messages + correction regex
        - `agent_message`      → messages
        - `agent_reasoning`    → reasoning_blocks
        - `token_count`        → tokens from payload.info.last_token_usage
        - `task_started` / `task_complete` / `turn_aborted` / `context_compacted`
    - `response_item`   : payload.type discriminates kind
        - `function_call`      → tool_calls; name="shell" → command_diversity
                                 name=~mcp__<server>__* → mcp_server_counts
        - `custom_tool_call`   → tool_calls; name="apply_patch" → file_changes
                                 (parse input for AGENTS.md → agents_md_writes)
        - `web_search_call`    → tool_calls
        - `function_call_output` / `custom_tool_call_output` / `reasoning` / `message`
    - `turn_context`    : ignored (redundant cwd/model info)
    - `compacted`       : ignored

Python stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import (  # noqa: E402  -- shared helpers
    _CORRECTION_RE,
    _CORRECTION_SCAN_PREFIX,
    _parse_timestamp,
    _ts_to_date,
)


DEFAULT_WINDOW_DAYS = 30

# Aggregation shape — mirrors scan_transcripts.py so the upload payload is
# symmetric across sources. Codex-specific fields are added here.
_COUNT_FIELDS = (
    "sessions",
    "main_sessions",
    "messages",
    "tool_calls",
    "sessions_with_tools",
    "user_messages",
    "user_corrections",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_creation",
    "tokens_total",
    "claude_md_writes",  # AGENTS.md writes land here, mirroring Claude scanner
    # Codex extensions — require Metrics.allowed_metric_keys to include these
    "reasoning_blocks",
    "file_changes",
    "agents_md_writes",
    "command_diversity",
)

_PEAK_FIELDS = (
    "max_messages_in_session",
)

_DICT_FIELDS = (
    "tool_name_counts",
    "mcp_server_counts",
)


def _new_day_metrics() -> dict:
    out: dict = {f: 0 for f in _COUNT_FIELDS}
    for f in _PEAK_FIELDS:
        out[f] = 0
    for f in _DICT_FIELDS:
        out[f] = {}
    return out


def _bucket(daily: dict[date, dict], d: date) -> dict:
    if d not in daily:
        daily[d] = _new_day_metrics()
    return daily[d]


def _hash_cwd(cwd: str) -> str:
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:12]


def scan(
    codex_dir: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict:
    """Scan ~/.codex/sessions for Codex CLI rollouts within the last `window_days`."""
    home = codex_dir or Path.home() / ".codex"
    sessions_root = home / "sessions"
    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)
    effective_cutoff = (
        cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)
    )

    daily: dict[date, dict] = {}
    # Per-day set of distinct shell command verbs, collapsed to counts at end.
    command_verbs_per_day: dict[date, set[str]] = {}
    unknown_event_types: dict[str, int] = {}
    # Per-day list of (start, end) session intervals. Used by the upload hook
    # to compute a combined-source concurrency peak across Claude + Codex.
    intervals_by_day: dict[date, list[tuple[float, float]]] = {}

    if sessions_root.is_dir():
        for rollout in _iter_rollout_files(sessions_root, effective_cutoff):
            _process_session(
                rollout,
                daily,
                command_verbs_per_day,
                unknown_event_types,
                intervals_by_day,
            )

    # Emit command_diversity = distinct verbs seen per day.
    for d, verbs in command_verbs_per_day.items():
        _bucket(daily, d)["command_diversity"] = len(verbs)

    daily = {d: m for d, m in daily.items() if _has_activity(m)}

    daily_list = [
        {"date": d.isoformat(), "metrics": m} for d, m in sorted(daily.items())
    ]
    rollup = _rollup_from_daily(daily.values())

    intervals_serialized = {
        d.isoformat(): [[s, e] for (s, e) in ivs]
        for d, ivs in intervals_by_day.items()
    }

    return {
        "source": "codex",
        "window_days": window_days,
        "daily": daily_list,
        "rollup": rollup,
        "_unknown_event_types": unknown_event_types,
        "intervals_by_day": intervals_serialized,
    }


def _iter_rollout_files(sessions_root: Path, cutoff_ts: float) -> Iterable[Path]:
    for rollout in sessions_root.rglob("rollout-*.jsonl"):
        try:
            if rollout.stat().st_mtime >= cutoff_ts:
                yield rollout
        except OSError:
            continue


def _process_session(
    path: Path,
    daily: dict[date, dict],
    command_verbs_per_day: dict[date, set[str]],
    unknown_event_types: dict[str, int],
    intervals_by_day: dict[date, list[tuple[float, float]]] | None = None,
) -> None:
    """Parse a single Codex rollout and update per-day buckets."""
    try:
        fallback_date = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return

    days_seen: set[date] = set()
    days_with_tools: set[date] = set()
    msgs_per_day: dict[date, int] = {}
    # Per-day earliest/latest timestamp seen in this session — used to record
    # a (start, end) interval per session per local-day for cross-source
    # concurrency. Sessions that span midnight contribute one interval per day.
    earliest_per_day: dict[date, float] = {}
    latest_per_day: dict[date, float] = {}

    try:
        fh = path.open("r")
    except OSError:
        return

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = _parse_timestamp(event.get("timestamp"))
            d = _ts_to_date(ts) or fallback_date
            bucket = _bucket(daily, d)
            days_seen.add(d)
            if ts is not None:
                if d not in earliest_per_day or ts < earliest_per_day[d]:
                    earliest_per_day[d] = ts
                if d not in latest_per_day or ts > latest_per_day[d]:
                    latest_per_day[d] = ts

            top_type = event.get("type")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            payload_type = payload.get("type")

            if top_type == "session_meta":
                # Hash cwd for observability only — not persisted to output today,
                # but computed so future per-repo analytics can join on the hash
                # without us ever seeing the plaintext path.
                _hash_cwd(payload.get("cwd") or "")
                continue

            if top_type == "turn_context":
                # Redundant with session_meta for our metrics.
                continue

            if top_type == "compacted":
                continue

            if top_type == "event_msg":
                if payload_type == "user_message":
                    text = payload.get("message") or ""
                    if isinstance(text, str) and text:
                        bucket["user_messages"] += 1
                        bucket["messages"] += 1
                        msgs_per_day[d] = msgs_per_day.get(d, 0) + 1
                        if _CORRECTION_RE.search(text[:_CORRECTION_SCAN_PREFIX]):
                            bucket["user_corrections"] += 1
                    continue

                if payload_type == "agent_message":
                    bucket["messages"] += 1
                    msgs_per_day[d] = msgs_per_day.get(d, 0) + 1
                    continue

                if payload_type == "agent_reasoning":
                    bucket["reasoning_blocks"] += 1
                    continue

                if payload_type == "token_count":
                    info = payload.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    if isinstance(last, dict):
                        ti = int(last.get("input_tokens") or 0)
                        to = int(last.get("output_tokens") or 0)
                        tc = int(last.get("cached_input_tokens") or 0)
                        bucket["tokens_input"] += ti
                        bucket["tokens_output"] += to
                        bucket["tokens_cache_read"] += tc
                        total = last.get("total_tokens")
                        bucket["tokens_total"] += int(
                            total if total is not None else (ti + to)
                        )
                    continue

                if payload_type in (
                    "task_started",
                    "task_complete",
                    "turn_aborted",
                    "context_compacted",
                    # Command/patch status events — the originating function_call
                    # on response_item is already counted; these are results.
                    "exec_command_begin",
                    "exec_command_end",
                    "exec_command_output_delta",
                    "patch_apply_begin",
                    "patch_apply_end",
                    # Streaming text deltas — presence is captured by the final
                    # agent_message / agent_reasoning events.
                    "agent_message_delta",
                    "agent_reasoning_delta",
                    "agent_reasoning_raw_content",
                    "agent_reasoning_raw_content_delta",
                    "agent_reasoning_section_break",
                ):
                    # Known but unused for metrics today.
                    continue

                _record_unknown(unknown_event_types, top_type, payload_type)
                continue

            if top_type == "response_item":
                if payload_type == "function_call":
                    name = payload.get("name") or ""
                    _count_tool(bucket, name)
                    days_with_tools.add(d)
                    # Both `shell` (old Codex) and `exec_command` (new Codex)
                    # run user commands. Extract the verb so command_diversity
                    # reflects tool breadth across CLI versions.
                    if name in ("shell", "exec_command"):
                        verb = _shell_verb(payload.get("arguments"))
                        if verb:
                            command_verbs_per_day.setdefault(d, set()).add(verb)
                    continue

                if payload_type == "custom_tool_call":
                    name = payload.get("name") or ""
                    _count_tool(bucket, name)
                    days_with_tools.add(d)
                    if name == "apply_patch":
                        patch_input = payload.get("input") or ""
                        if isinstance(patch_input, str) and patch_input:
                            bucket["file_changes"] += 1
                            if _patch_touches_agents_md(patch_input):
                                bucket["agents_md_writes"] += 1
                                bucket["claude_md_writes"] += 1
                    continue

                if payload_type == "web_search_call":
                    _count_tool(bucket, "web_search")
                    days_with_tools.add(d)
                    continue

                if payload_type in (
                    "function_call_output",
                    "custom_tool_call_output",
                    "reasoning",
                    "message",
                ):
                    # Known but unused for metrics.
                    continue

                _record_unknown(unknown_event_types, top_type, payload_type)
                continue

            _record_unknown(unknown_event_types, top_type, payload_type)

    for d in days_seen:
        bucket = _bucket(daily, d)
        bucket["sessions"] += 1
        bucket["main_sessions"] += 1
    for d in days_with_tools:
        _bucket(daily, d)["sessions_with_tools"] += 1
    for d, count in msgs_per_day.items():
        bucket = _bucket(daily, d)
        if count > bucket["max_messages_in_session"]:
            bucket["max_messages_in_session"] = count

    if intervals_by_day is not None:
        for d, start in earliest_per_day.items():
            end = latest_per_day.get(d, start)
            if end == start:
                end = start + 1.0
            intervals_by_day.setdefault(d, []).append((start, end))


def _count_tool(bucket: dict, name: str) -> None:
    if not name:
        return
    bucket["tool_calls"] += 1
    bucket["tool_name_counts"][name] = bucket["tool_name_counts"].get(name, 0) + 1
    # MCP convention carried over from Claude: mcp__<server>__<tool>
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3 and parts[1]:
            server = parts[1]
            bucket["mcp_server_counts"][server] = (
                bucket["mcp_server_counts"].get(server, 0) + 1
            )


def _shell_verb(arguments) -> str | None:
    """Extract the first command token from a shell function_call's arguments.

    Codex serializes shell arguments in two shapes across versions:

    - Old `shell`: `{"command": ["bash", "-lc", "git status"]}` — array form.
    - New `exec_command`: `{"cmd": "git status", "workdir": "..."}` — string form.

    We want the verb a user would recognize — for `bash -lc "git status"` that's
    `git`, not `bash`. Heuristic for the array form: if the first token is a
    shell wrapper, peek at the last token (the command string) and take its
    first meaningful word.
    """
    if not isinstance(arguments, str) or not arguments:
        return None
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    cmd_string = parsed.get("cmd")
    if isinstance(cmd_string, str) and cmd_string.strip():
        return _first_meaningful_word(cmd_string)

    cmd = parsed.get("command")
    if not isinstance(cmd, list) or not cmd:
        return None
    first = cmd[0] if isinstance(cmd[0], str) else None
    if not first:
        return None
    wrappers = {"bash", "sh", "zsh", "env"}
    base_first = os.path.basename(first)
    if base_first in wrappers and len(cmd) >= 2:
        for candidate in reversed(cmd):
            if isinstance(candidate, str) and candidate and not candidate.startswith("-"):
                return _first_meaningful_word(candidate)
        return base_first
    return base_first


def _first_meaningful_word(s: str) -> str | None:
    """First non-env-assignment word in `s`, basename-stripped."""
    for token in s.split():
        if "=" in token and token.split("=", 1)[0].isupper():
            continue
        return os.path.basename(token)
    return None


def _patch_touches_agents_md(patch_input: str) -> bool:
    """True if an apply_patch input edits AGENTS.md.

    Codex apply_patch format uses lines like:
        *** Add File: path/to/file
        *** Update File: path/to/file
        *** Delete File: path/to/file
    """
    for line in patch_input.splitlines():
        s = line.strip()
        if s.startswith("*** ") and "AGENTS.md" in s:
            return True
    return False


def _record_unknown(store: dict[str, int], top_type, payload_type) -> None:
    key = f"{top_type or '<no-type>'}:{payload_type or '<no-payload-type>'}"
    store[key] = store.get(key, 0) + 1


def _has_activity(metrics: dict) -> bool:
    if any(metrics.get(f, 0) for f in _COUNT_FIELDS):
        return True
    if any(metrics.get(f, 0) for f in _PEAK_FIELDS):
        return True
    if any(metrics.get(f) for f in _DICT_FIELDS):
        return True
    return False


def _rollup_from_daily(per_day_metrics) -> dict:
    out = _new_day_metrics()
    for m in per_day_metrics:
        for f in _COUNT_FIELDS:
            out[f] = out.get(f, 0) + int(m.get(f, 0) or 0)
        for f in _PEAK_FIELDS:
            out[f] = max(out.get(f, 0), int(m.get(f, 0) or 0))
        for f in _DICT_FIELDS:
            for k, v in (m.get(f) or {}).items():
                out[f][k] = out[f].get(k, 0) + int(v or 0)
    return out


def main(argv: list[str]) -> int:
    window_days = DEFAULT_WINDOW_DAYS
    if "--days" in argv:
        idx = argv.index("--days")
        try:
            window_days = int(argv[idx + 1])
        except (IndexError, ValueError):
            pass

    mtime_after_ts: float | None = None
    if "--mtime-after" in argv:
        idx = argv.index("--mtime-after")
        try:
            raw = argv[idx + 1]
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            mtime_after_ts = dt.timestamp()
        except (IndexError, ValueError):
            pass

    result = scan(window_days=window_days, mtime_after_ts=mtime_after_ts)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
