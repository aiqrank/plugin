#!/usr/bin/env python3
"""
AIQ Rank OpenCode scanner.

Walks ~/.local/share/opencode/opencode.db (SQLite, Drizzle ORM) and
extracts activity metrics from sessions and messages within the last
`window_days`. Current OpenCode stores sessions, messages, and message
parts in `session`, `message`, and `part` tables. Older/dev fixtures used
plural `sessions` / `messages` tables with embedded `data.parts`; this
scanner supports both shapes.

Output JSON envelope is symmetric with scan_codex.py:

    {
      "source": "opencode",
      "window_days": N,
      "daily": [
        {"date": "YYYY-MM-DD", "metrics": { ...fields... }},
        ...
      ],
      "rollup": { ...same fields, summed/maxed/merged across days... },
      "intervals_by_day": { "YYYY-MM-DD": [[start, end], ...] }
    }

Privacy: never emit `data.text`, `data.content`, message bodies, or
file paths. Only counts, tool names, per-day numeric buckets, and
locally-authored skill names (from ~/.claude/skills/).

Best-effort / unmeasurable fields:
  * `user_messages` — OpenCode's message rows do not consistently expose
    a `role` field across versions. We treat any message whose `data.role`
    is "user" as a user message; absent that, we conservatively skip
    incrementing this field. Defaults to 0 when role is unobservable.
  * `custom_skill_files_written` / `authored_skill_names` — populated
    from the local ~/.claude/skills/ directory via the shared helper.
    OpenCode reads skills from the same path. Counts are added to the
    rollup once at the end (not distributed per-day) since OpenCode does
    not expose file-write events we can attribute to a specific day.

Python stdlib only — no third-party deps. SQLite via stdlib `sqlite3`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import (  # noqa: E402  -- shared helpers
    _local_claude_skills,
    max_concurrent_sustained,
)


DEFAULT_WINDOW_DAYS = 30


_COUNT_FIELDS = (
    "sessions",
    "main_sessions",
    "messages",
    "tool_calls",
    "sessions_with_tools",
    "sessions_with_orchestration",
    "parallel_agent_turns",
    "user_messages",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_creation",
    "tokens_total",
    "claude_md_writes",
    "custom_mcp_config_writes",
    "reasoning_blocks",
    "custom_skill_files_written",
)

_PEAK_FIELDS = (
    "max_messages_in_session",
    "max_concurrent_sessions",
    "max_parallel_agents",
)

_DICT_FIELDS = (
    "tool_name_counts",
    "skill_counts",
    "mcp_server_counts",
)

_LIST_FIELDS = (
    "authored_skill_names",
)


def _new_day_metrics() -> dict:
    out: dict = {f: 0 for f in _COUNT_FIELDS}
    for f in _PEAK_FIELDS:
        out[f] = 0
    for f in _DICT_FIELDS:
        out[f] = {}
    for f in _LIST_FIELDS:
        out[f] = []
    return out


def _bucket(daily: dict[date, dict], d: date) -> dict:
    if d not in daily:
        daily[d] = _new_day_metrics()
    return daily[d]


def scan(
    opencode_db: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
    home: Path | None = None,
    opencode_config_root: Path | None = None,
) -> dict:
    """Scan an OpenCode SQLite DB and emit a per-day metrics envelope.

    Parameters
    ----------
    opencode_db : Path | None
        Path to the OpenCode SQLite database. Defaults to
        ``~/.local/share/opencode/opencode.db``. Missing file → empty envelope.
    window_days : int
        Look-back window. Activity older than (now - window_days) is excluded.
    now_ts : float | None
        Override "now" for tests. Defaults to time.time().
    mtime_after_ts : float | None
        Optional ISO-derived timestamp; effective cutoff = max of cutoff_ts
        and this value.
    home : Path | None
        Override ~/.claude (used for the local-skills helper). Defaults to
        ``~/.claude``.
    opencode_config_root : Path | None
        Override ~/.config/opencode (used for AGENTS.md / opencode.json
        mtime walks). Defaults to ``~/.config/opencode``.
    """
    db_path = opencode_db or (Path.home() / ".local" / "share" / "opencode" / "opencode.db")
    home = home or (Path.home() / ".claude")
    opencode_config_root = opencode_config_root or (Path.home() / ".config" / "opencode")

    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)
    effective_cutoff = (
        cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)
    )
    cutoff_ms = int(effective_cutoff * 1000)

    daily: dict[date, dict] = {}
    intervals_by_day: dict[date, list[tuple[float, float]]] = {}

    if not db_path.is_file():
        return _empty_envelope(window_days)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return _empty_envelope(window_days)

    try:
        sessions_by_id: dict[str, dict] = {}

        tables = _resolve_tables(conn)

        # ── sessions ──────────────────────────────────────────────────────
        for row in _iter_sessions(conn, tables["sessions"], cutoff_ms):
            sid, parent_id, t_created_ms, t_updated_ms, directory = row
            start_ts = (t_created_ms or 0) / 1000.0
            end_ts = (t_updated_ms or t_created_ms or 0) / 1000.0
            if end_ts <= start_ts:
                end_ts = start_ts + 1.0
            clipped_start_ts = max(start_ts, effective_cutoff)
            if end_ts <= clipped_start_ts:
                end_ts = clipped_start_ts + 1.0
            sessions_by_id[sid] = {
                "parent_id": parent_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "directory": directory,
            }

            # Split session interval by local-day so concurrency math is
            # per-day. Sessions spanning midnight contribute one interval
            # per day they touch.
            for d, (s, e) in _split_interval_by_day(clipped_start_ts, end_ts):
                bucket = _bucket(daily, d)
                bucket["sessions"] += 1
                if parent_id is None:
                    bucket["main_sessions"] += 1
                intervals_by_day.setdefault(d, []).append((s, e))

        # ── messages ──────────────────────────────────────────────────────
        sessions_with_tools_by_day: dict[date, set[str]] = {}
        sessions_with_orch_by_day: dict[date, set[str]] = {}
        msgs_per_session_per_day: dict[tuple[date, str], int] = {}
        agent_counts_by_message: dict[tuple[date, str], int] = {}

        for row in _iter_messages(conn, tables["messages"], cutoff_ms):
            mid, session_id, t_created_ms, raw = row
            try:
                data = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue

            ts = (t_created_ms or 0) / 1000.0
            d = datetime.fromtimestamp(ts).date()
            bucket = _bucket(daily, d)

            bucket["messages"] += 1
            if session_id:
                key = (d, session_id)
                msgs_per_session_per_day[key] = msgs_per_session_per_day.get(key, 0) + 1

            role = data.get("role")
            if role == "user":
                bucket["user_messages"] += 1

            # Tokens (assistant messages typically carry these).
            tokens = data.get("tokens")
            if isinstance(tokens, dict):
                ti = _safe_int(tokens.get("input"))
                to = _safe_int(tokens.get("output"))
                tr = _safe_int(tokens.get("reasoning"))
                bucket["tokens_input"] += ti
                bucket["tokens_output"] += to + tr
                cache = tokens.get("cache")
                if isinstance(cache, dict):
                    bucket["tokens_cache_read"] += _safe_int(cache.get("read"))
                    bucket["tokens_cache_creation"] += _safe_int(cache.get("write"))
                bucket["tokens_total"] += ti + to + tr

            # Walk parts.
            parts = data.get("parts")
            agent_count_in_msg = 0
            if isinstance(parts, list):
                for part in parts:
                    if _process_part(bucket, part, d, session_id, sessions_with_tools_by_day, sessions_with_orch_by_day):
                        agent_count_in_msg += 1

            if agent_count_in_msg >= 2:
                bucket["parallel_agent_turns"] += 1
                if agent_count_in_msg > bucket["max_parallel_agents"]:
                    bucket["max_parallel_agents"] = agent_count_in_msg

        if tables["parts"] is not None:
            for row in _iter_parts(conn, tables["parts"], cutoff_ms):
                _pid, mid, session_id, t_created_ms, raw = row
                try:
                    part = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(part, dict):
                    continue

                ts = (t_created_ms or 0) / 1000.0
                d = datetime.fromtimestamp(ts).date()
                bucket = _bucket(daily, d)
                if _process_part(
                    bucket,
                    part,
                    d,
                    session_id,
                    sessions_with_tools_by_day,
                    sessions_with_orch_by_day,
                ):
                    key = (d, mid or "")
                    agent_counts_by_message[key] = agent_counts_by_message.get(key, 0) + 1

    finally:
        conn.close()

    for (d, _mid), count in agent_counts_by_message.items():
        if count >= 2:
            bucket = _bucket(daily, d)
            bucket["parallel_agent_turns"] += 1
            if count > bucket["max_parallel_agents"]:
                bucket["max_parallel_agents"] = count

    # Promote derived per-day counts.
    for d, sids in sessions_with_tools_by_day.items():
        _bucket(daily, d)["sessions_with_tools"] = len(sids)
    for d, sids in sessions_with_orch_by_day.items():
        _bucket(daily, d)["sessions_with_orchestration"] = len(sids)

    for (d, _sid), count in msgs_per_session_per_day.items():
        bucket = _bucket(daily, d)
        if count > bucket["max_messages_in_session"]:
            bucket["max_messages_in_session"] = count

    # Concurrency peak per day.
    for d, ivs in intervals_by_day.items():
        _bucket(daily, d)["max_concurrent_sessions"] = max_concurrent_sustained(ivs)

    # AGENTS.md mtime walks (per-session directory + global config).
    _walk_agents_md(daily, sessions_by_id, opencode_config_root, effective_cutoff, now_ts)

    # opencode.json mtime walks (per-session directory + global config).
    _walk_mcp_config(daily, sessions_by_id, opencode_config_root, effective_cutoff, now_ts)

    # Local skill snapshot — applied to rollup once (no per-day attribution).
    skill_names = _local_claude_skills(home)

    daily = {d: m for d, m in daily.items() if _has_activity(m)}
    daily_list = [
        {"date": d.isoformat(), "metrics": m} for d, m in sorted(daily.items())
    ]
    rollup = _rollup_from_daily(daily.values())

    if skill_names:
        rollup["custom_skill_files_written"] += len(skill_names)
        existing = set(rollup.get("authored_skill_names") or [])
        existing.update(skill_names)
        rollup["authored_skill_names"] = sorted(existing)

    intervals_serialized = {
        d.isoformat(): [[s, e] for (s, e) in ivs]
        for d, ivs in intervals_by_day.items()
    }

    return {
        "source": "opencode",
        "window_days": window_days,
        "daily": daily_list,
        "rollup": rollup,
        "intervals_by_day": intervals_serialized,
    }


def _empty_envelope(window_days: int) -> dict:
    return {
        "source": "opencode",
        "window_days": window_days,
        "daily": [],
        "rollup": _new_day_metrics(),
        "intervals_by_day": {},
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _resolve_tables(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        "sessions": "session" if _table_exists(conn, "session") else "sessions",
        "messages": "message" if _table_exists(conn, "message") else "messages",
        "parts": "part" if _table_exists(conn, "part") else None,
    }


def _iter_sessions(conn: sqlite3.Connection, table: str | None, cutoff_ms: int) -> Iterable[tuple]:
    if table is None:
        return iter(())
    try:
        cur = conn.execute(
            f"SELECT id, parent_id, time_created, time_updated, directory "
            f"FROM {table} WHERE coalesce(time_updated, time_created, 0) >= ?",
            (cutoff_ms,),
        )
    except sqlite3.Error:
        return iter(())
    return cur


def _iter_messages(conn: sqlite3.Connection, table: str | None, cutoff_ms: int) -> Iterable[tuple]:
    if table is None:
        return iter(())
    try:
        cur = conn.execute(
            f"SELECT id, session_id, time_created, data "
            f"FROM {table} WHERE time_created >= ?",
            (cutoff_ms,),
        )
    except sqlite3.Error:
        return iter(())
    return cur


def _iter_parts(conn: sqlite3.Connection, table: str | None, cutoff_ms: int) -> Iterable[tuple]:
    if table is None:
        return iter(())
    try:
        cur = conn.execute(
            f"SELECT id, message_id, session_id, time_created, data "
            f"FROM {table} WHERE time_created >= ?",
            (cutoff_ms,),
        )
    except sqlite3.Error:
        return iter(())
    return cur


def _process_part(
    bucket: dict,
    part: dict,
    d: date,
    session_id: str | None,
    sessions_with_tools_by_day: dict[date, set[str]],
    sessions_with_orch_by_day: dict[date, set[str]],
) -> bool:
    if not isinstance(part, dict):
        return False
    ptype = part.get("type")
    if ptype == "tool":
        tool = part.get("tool") or part.get("toolName") or part.get("name") or ""
        if isinstance(tool, str) and tool:
            _count_tool(bucket, tool)
            if session_id:
                sessions_with_tools_by_day.setdefault(d, set()).add(session_id)
    elif ptype == "reasoning":
        bucket["reasoning_blocks"] += 1
    elif ptype == "agent":
        if session_id:
            sessions_with_orch_by_day.setdefault(d, set()).add(session_id)
        return True
    return False


def _split_interval_by_day(start_ts: float, end_ts: float) -> Iterable[tuple[date, tuple[float, float]]]:
    """Yield (day, (start, end)) tuples — splitting intervals at local midnight."""
    if end_ts < start_ts:
        end_ts = start_ts
    cursor = start_ts
    while True:
        d = datetime.fromtimestamp(cursor).date()
        # Next local midnight after `cursor` — use timedelta(days=1) so DST
        # transitions don't drift the boundary by an hour.
        next_midnight = (
            datetime(d.year, d.month, d.day) + timedelta(days=1)
        ).timestamp()
        if end_ts <= next_midnight:
            yield d, (cursor, end_ts)
            return
        yield d, (cursor, next_midnight)
        cursor = next_midnight


def _count_tool(bucket: dict, name: str) -> None:
    if not name:
        return
    bucket["tool_calls"] += 1
    bucket["tool_name_counts"][name] = bucket["tool_name_counts"].get(name, 0) + 1

    # MCP attribution. OpenCode uses single-underscore convention
    # (mcp_<server>_<tool>) per https://opencode.ai/docs/rules/. We also
    # accept the Claude double-underscore convention defensively.
    server = _extract_mcp_server(name)
    if server:
        bucket["mcp_server_counts"][server] = (
            bucket["mcp_server_counts"].get(server, 0) + 1
        )


def _extract_mcp_server(tool_name: str) -> str | None:
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3 and parts[1]:
            return parts[1]
        return None
    if tool_name.startswith("mcp_"):
        # mcp_<server>_<rest>
        rest = tool_name[len("mcp_"):]
        if not rest:
            return None
        idx = rest.find("_")
        if idx <= 0:
            return None
        return rest[:idx]
    return None


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _walk_agents_md(
    daily: dict[date, dict],
    sessions_by_id: dict[str, dict],
    opencode_config_root: Path,
    cutoff_ts: float,
    now_ts: float,
) -> None:
    seen: set[str] = set()

    candidates: list[Path] = []
    for sess in sessions_by_id.values():
        directory = sess.get("directory")
        if isinstance(directory, str) and directory:
            candidates.append(Path(directory) / "AGENTS.md")
    candidates.append(opencode_config_root / "AGENTS.md")

    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            mt = path.stat().st_mtime
        except OSError:
            continue
        if mt < cutoff_ts or mt > now_ts + 86400:
            continue
        d = datetime.fromtimestamp(mt).date()
        _bucket(daily, d)["claude_md_writes"] += 1


def _walk_mcp_config(
    daily: dict[date, dict],
    sessions_by_id: dict[str, dict],
    opencode_config_root: Path,
    cutoff_ts: float,
    now_ts: float,
) -> None:
    seen: set[str] = set()

    candidates: list[Path] = []
    for sess in sessions_by_id.values():
        directory = sess.get("directory")
        if isinstance(directory, str) and directory:
            candidates.append(Path(directory) / "opencode.json")
    candidates.append(opencode_config_root / "opencode.json")

    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            mt = path.stat().st_mtime
        except OSError:
            continue
        if mt < cutoff_ts or mt > now_ts + 86400:
            continue
        d = datetime.fromtimestamp(mt).date()
        _bucket(daily, d)["custom_mcp_config_writes"] += 1


def _has_activity(metrics: dict) -> bool:
    if any(metrics.get(f, 0) for f in _COUNT_FIELDS):
        return True
    if any(metrics.get(f, 0) for f in _PEAK_FIELDS):
        return True
    if any(metrics.get(f) for f in _DICT_FIELDS):
        return True
    if any(metrics.get(f) for f in _LIST_FIELDS):
        return True
    return False


def _rollup_from_daily(per_day_metrics) -> dict:
    out = _new_day_metrics()
    list_accums: dict[str, set[str]] = {f: set() for f in _LIST_FIELDS}
    for m in per_day_metrics:
        for f in _COUNT_FIELDS:
            out[f] = out.get(f, 0) + int(m.get(f, 0) or 0)
        for f in _PEAK_FIELDS:
            out[f] = max(out.get(f, 0), int(m.get(f, 0) or 0))
        for f in _DICT_FIELDS:
            for k, v in (m.get(f) or {}).items():
                out[f][k] = out[f].get(k, 0) + int(v or 0)
        for f in _LIST_FIELDS:
            for v in m.get(f) or []:
                if isinstance(v, str) and v:
                    list_accums[f].add(v)
    for f in _LIST_FIELDS:
        out[f] = sorted(list_accums[f])
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

    if "--help" in argv or "-h" in argv:
        sys.stdout.write(
            "Usage: scan_opencode.py [--days N] [--mtime-after ISO]\n"
            "  --days N           look-back window in days (default 30)\n"
            "  --mtime-after ISO  raise the effective cutoff to this ISO timestamp\n"
        )
        return 0

    result = scan(window_days=window_days, mtime_after_ts=mtime_after_ts)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
