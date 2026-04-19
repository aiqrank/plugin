#!/usr/bin/env python3
"""
AIQ Rank transcript scanner.

Walks ~/.claude/projects/* and extracts activity metrics from Claude Code
transcripts modified in the last 30 days. Buckets every event by its
local-calendar-day timestamp, emits a `daily` array (one entry per day
that had activity) plus a `rollup` aggregate over the full window.

Output JSON:

    {
      "window_days": 30,
      "daily": [
        {"date": "YYYY-MM-DD", "metrics": { ...fields... }},
        ...
      ],
      "rollup": { ...same fields, summed/maxed/merged across days... },
      "first_messages_sample": [...]   # local-only, for role inference
    }

The plugin sends only `daily` (filtered by latest_date cursor) plus role
metadata to the server. The `first_messages_sample` never leaves the
machine. The rollup is included so the unauthenticated pair/teaser flow
can show preview totals without re-aggregating server-side.

Python stdlib only (no third-party dependencies).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


# -- Constants --

DEFAULT_WINDOW_DAYS = 30
CONTEXT_LEVERAGE_TOOLS = {
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "ScheduleWakeup",
    "CronCreate",
    "CronList",
    "CronDelete",
    "RemoteTrigger",
    "Monitor",
}
ORCHESTRATION_TOOLS = {"Agent"}
SKILL_TOOL = "Skill"
PLAN_MODE_TOOL = "ExitPlanMode"
AIQRANK_SKILL_DIR_FRAGMENT = "/.claude/skills/aiqrank/"

_CORRECTION_RE = re.compile(
    r"""
    (?:^|[\s.,!?;:-])
    (?:
        no[,!.\s]              |
        don'?t\b                |
        stop\b                  |
        wait[,!.\s]             |
        undo\b                  |
        revert\b                |
        incorrect\b             |
        wrong\b                 |
        actually[,!.\s]         |
        try\s+again\b           |
        you\s+(?:missed|forgot|broke)\b |
        that'?s\s+not\s+(?:right|correct|what\s+i) |
        not\s+what\s+i\s+(?:meant|want|asked) |
        nope\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CORRECTION_SCAN_PREFIX = 120

# Field aggregation rules — must match AIQRank.Metrics module attributes.
_COUNT_FIELDS = (
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
)

_PEAK_FIELDS = (
    "max_concurrent_sessions",
    "max_messages_in_session",
    "max_parallel_agents",
)

_DICT_FIELDS = (
    "tool_name_counts",
    "skill_counts",
    "mcp_server_counts",
    "agent_type_counts",
)


def _new_day_metrics() -> dict:
    """Empty metrics dict for one calendar day."""
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


def scan(
    claude_dir: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
) -> dict:
    """Scan ~/.claude for transcript data within the last `window_days`.

    Returns `{window_days, daily, rollup, first_messages_sample}` where
    `daily` is a date-sorted list of per-day metrics blobs.
    """
    home = claude_dir or Path.home() / ".claude"
    projects = home / "projects"
    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)

    daily: dict[date, dict] = {}
    first_messages_sample: list[str] = []
    custom_skills_seen: set[str] = set()
    # Per-day list of main-session intervals for sweep-line concurrency.
    intervals_by_day: dict[date, list[tuple[float, float]]] = {}

    if projects.is_dir():
        for project_dir in sorted(projects.iterdir()):
            if not project_dir.is_dir():
                continue

            for jsonl_path in iter_transcript_files(project_dir, cutoff_ts):
                is_main = "subagents" not in jsonl_path.parts
                process_session(
                    jsonl_path,
                    daily,
                    first_messages_sample,
                    custom_skills_seen,
                    intervals_by_day,
                    is_main,
                )

            for meta_path in project_dir.rglob("agent-*.meta.json"):
                if meta_path.stat().st_mtime < cutoff_ts:
                    continue
                meta_date = datetime.fromtimestamp(meta_path.stat().st_mtime).date()
                bucket = _bucket(daily, meta_date)
                try:
                    with meta_path.open("r") as fh:
                        data = json.load(fh)
                    agent_type = data.get("agentType")
                    if agent_type:
                        bucket["agent_type_counts"][agent_type] = (
                            bucket["agent_type_counts"].get(agent_type, 0) + 1
                        )
                except (json.JSONDecodeError, OSError):
                    continue

    # Per-day concurrency from sweep-line over each day's clipped intervals.
    for d, intervals in intervals_by_day.items():
        bucket = _bucket(daily, d)
        bucket["max_concurrent_sessions"] = _max_concurrent(intervals)

    # Drop any days that recorded zero activity (a meta-only day with no
    # parseable agentType, for example).
    daily = {d: m for d, m in daily.items() if _has_activity(m)}

    daily_list = [
        {"date": d.isoformat(), "metrics": m} for d, m in sorted(daily.items())
    ]

    rollup = _rollup_from_daily(daily.values())

    return {
        "window_days": window_days,
        "daily": daily_list,
        "rollup": rollup,
        "first_messages_sample": first_messages_sample,
    }


def _has_activity(metrics: dict) -> bool:
    """True if the day saw any countable activity."""
    if any(metrics.get(f, 0) for f in _COUNT_FIELDS):
        return True
    if any(metrics.get(f, 0) for f in _PEAK_FIELDS):
        return True
    if any(metrics.get(f) for f in _DICT_FIELDS):
        return True
    return False


def _rollup_from_daily(per_day_metrics) -> dict:
    """Aggregate a sequence of daily metrics maps into a single rollup."""
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


def _max_concurrent(intervals: list[tuple[float, float]]) -> int:
    """Sweep-line over (start, +1)/(end, -1); max running sum = concurrency."""
    if not intervals:
        return 0

    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()

    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        if current > peak:
            peak = current
    return peak


def _parse_timestamp(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.timestamp()


def _ts_to_date(ts: float | None) -> date | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).date()


def iter_transcript_files(project_dir: Path, cutoff_ts: float) -> Iterable[Path]:
    """Yields JSONL transcript file paths within the cutoff."""
    for entry in project_dir.iterdir():
        if entry.is_file() and entry.suffix == ".jsonl":
            if entry.stat().st_mtime >= cutoff_ts:
                yield entry

    for subagent_path in project_dir.rglob("subagents/agent-*.jsonl"):
        if subagent_path.stat().st_mtime >= cutoff_ts:
            yield subagent_path


def process_session(
    path: Path,
    daily: dict[date, dict],
    first_messages_sample: list,
    custom_skills_seen: set[str],
    intervals_by_day: dict[date, list[tuple[float, float]]],
    is_main: bool = True,
) -> None:
    """Parse a single JSONL transcript and update per-day buckets.

    All counts are bucketed by each event's own timestamp date, so a session
    that crosses midnight contributes to two days. Session-level booleans
    (sessions, sessions_with_tools, ...) count once per (session, day).
    """
    # Per-session-per-day: which days saw which kinds of activity.
    days_seen: set[date] = set()
    days_with_tools: set[date] = set()
    days_with_orchestration: set[date] = set()
    days_with_context_leverage: set[date] = set()
    days_with_plan_mode: set[date] = set()
    # Per-day message counts within this session — used to update daily
    # max_messages_in_session at the end.
    msgs_per_day: dict[date, int] = {}
    # Per-day timestamp interval [(start, end)] for this session, for the
    # daily concurrency sweep.
    earliest_per_day: dict[date, float] = {}
    latest_per_day: dict[date, float] = {}
    # Agents grouped by (date, requestId) — same model turn shows up as one
    # bucket per day (a single turn is virtually always within one day).
    agents_by_request_day: dict[tuple[date, str], int] = {}
    first_user_msg_captured = False

    # Fallback bucket date when an event lacks a parseable timestamp:
    # the file's mtime date. Real transcripts always include timestamps,
    # but legacy or partial events shouldn't be silently dropped.
    try:
        fallback_date = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return

    try:
        with path.open("r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = event.get("message") or {}
                ts = _parse_timestamp(event.get("timestamp"))
                d = _ts_to_date(ts) or fallback_date

                bucket = _bucket(daily, d)
                bucket["messages"] += 1
                msgs_per_day[d] = msgs_per_day.get(d, 0) + 1
                days_seen.add(d)

                if ts is not None:
                    if d not in earliest_per_day or ts < earliest_per_day[d]:
                        earliest_per_day[d] = ts
                    if d not in latest_per_day or ts > latest_per_day[d]:
                        latest_per_day[d] = ts

                if event.get("type") == "assistant":
                    usage = msg.get("usage") or {}
                    if isinstance(usage, dict):
                        ti = int(usage.get("input_tokens") or 0)
                        to = int(usage.get("output_tokens") or 0)
                        tr = int(usage.get("cache_read_input_tokens") or 0)
                        tc = int(usage.get("cache_creation_input_tokens") or 0)
                        bucket["tokens_input"] += ti
                        bucket["tokens_output"] += to
                        bucket["tokens_cache_read"] += tr
                        bucket["tokens_cache_creation"] += tc
                        bucket["tokens_total"] += ti + to + tr + tc

                if event.get("type") == "user":
                    text = content_to_text(msg.get("content"))
                    if text:
                        bucket["user_messages"] += 1
                        if _CORRECTION_RE.search(text[:_CORRECTION_SCAN_PREFIX]):
                            bucket["user_corrections"] += 1

                        if not first_user_msg_captured and len(first_messages_sample) < 500:
                            first_messages_sample.append(text[:500])
                            first_user_msg_captured = True

                if event.get("type") == "assistant":
                    turn_tool_uses = list(extract_tool_uses(msg))

                    agents_in_event = sum(
                        1 for t in turn_tool_uses if t.get("name") in ORCHESTRATION_TOOLS
                    )
                    request_id = event.get("requestId")
                    if agents_in_event and request_id:
                        key = (d, request_id)
                        agents_by_request_day[key] = (
                            agents_by_request_day.get(key, 0) + agents_in_event
                        )
                    elif agents_in_event:
                        if agents_in_event > bucket["max_parallel_agents"]:
                            bucket["max_parallel_agents"] = agents_in_event
                        if agents_in_event >= 2:
                            bucket["parallel_agent_turns"] += 1

                    for tool_use in turn_tool_uses:
                        bucket["tool_calls"] += 1
                        days_with_tools.add(d)

                        name = tool_use.get("name", "")
                        if not name:
                            continue

                        bucket["tool_name_counts"][name] = (
                            bucket["tool_name_counts"].get(name, 0) + 1
                        )

                        if name == SKILL_TOOL:
                            skill = (tool_use.get("input") or {}).get("skill")
                            if skill:
                                bucket["skill_counts"][skill] = (
                                    bucket["skill_counts"].get(skill, 0) + 1
                                )

                        if name.startswith("mcp__"):
                            parts = name.split("__")
                            server = parts[1] if len(parts) >= 3 else ""
                            if server:
                                bucket["mcp_server_counts"][server] = (
                                    bucket["mcp_server_counts"].get(server, 0) + 1
                                )

                        if name in ORCHESTRATION_TOOLS:
                            days_with_orchestration.add(d)

                        if name in CONTEXT_LEVERAGE_TOOLS:
                            days_with_context_leverage.add(d)

                        if name == PLAN_MODE_TOOL:
                            days_with_plan_mode.add(d)
                            bucket["plan_mode_invocations"] += 1

                        if name in ("Write", "Edit"):
                            target_path = (tool_use.get("input") or {}).get("file_path") or ""
                            if (
                                target_path.endswith("/SKILL.md")
                                and AIQRANK_SKILL_DIR_FRAGMENT not in target_path
                                and target_path not in custom_skills_seen
                            ):
                                custom_skills_seen.add(target_path)
                                bucket["custom_skill_files_written"] += 1

                            if target_path.endswith("/.mcp.json") or target_path.endswith(
                                "/mcp.json"
                            ):
                                bucket["custom_mcp_config_writes"] += 1

                            if target_path.endswith("/CLAUDE.md") or target_path.endswith(
                                "/AGENTS.md"
                            ):
                                bucket["claude_md_writes"] += 1
    except OSError:
        return

    # Fold (date, requestId) Agent tallies into per-day peaks.
    for (d, _rid), count in agents_by_request_day.items():
        bucket = _bucket(daily, d)
        if count > bucket["max_parallel_agents"]:
            bucket["max_parallel_agents"] = count
        if count >= 2:
            bucket["parallel_agent_turns"] += 1

    for d in days_seen:
        bucket = _bucket(daily, d)
        bucket["sessions"] += 1
        if is_main:
            bucket["main_sessions"] += 1
    for d in days_with_tools:
        _bucket(daily, d)["sessions_with_tools"] += 1
    for d in days_with_orchestration:
        _bucket(daily, d)["sessions_with_orchestration"] += 1
    for d in days_with_context_leverage:
        _bucket(daily, d)["sessions_with_context_leverage"] += 1
    for d in days_with_plan_mode:
        _bucket(daily, d)["sessions_with_plan_mode"] += 1

    for d, count in msgs_per_day.items():
        bucket = _bucket(daily, d)
        if count > bucket["max_messages_in_session"]:
            bucket["max_messages_in_session"] = count

    if is_main:
        for d, start in earliest_per_day.items():
            end = latest_per_day.get(d, start)
            if end == start:
                end = start + 1.0
            intervals_by_day.setdefault(d, []).append((start, end))


def extract_tool_uses(message: dict) -> Iterable[dict]:
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def content_to_text(content) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)

    return ""


def main(argv: list[str]) -> int:
    window_days = DEFAULT_WINDOW_DAYS
    if "--days" in argv:
        idx = argv.index("--days")
        try:
            window_days = int(argv[idx + 1])
        except (IndexError, ValueError):
            pass

    result = scan(window_days=window_days)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
