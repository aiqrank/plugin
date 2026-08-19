#!/usr/bin/env python3
"""Privacy-bounded scanners for Hermes, OpenClaw, and NanoClaw.

These runtimes mix human conversations, scheduled work, and child agents in
the same stores.  The scanners intentionally keep those classes separate:
only human-rooted conversations increment ``sessions``/``main_sessions``;
cron, heartbeat, and system runs increment ``scheduled_task_runs``; spawned
children contribute orchestration evidence.  No transcript text, identifiers,
paths, prompts, outputs, or costs are returned.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


class AgentRuntimeScanIncomplete(RuntimeError):
    """Raised when a detected runtime store cannot be read completely."""


MAX_PROVIDER_FILES = 2_000
MAX_PROVIDER_FILE_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_TOTAL_BYTES = 256 * 1024 * 1024
MAX_EVENT_JSON_BYTES = 1024 * 1024
MAX_SQLITE_ROWS = 200_000
MAX_SQLITE_TEXT_BYTES = 64 * 1024
MAX_SQLITE_RETAINED_KEY_BYTES = 64 * 1024 * 1024


def scan_hermes(
    state_db: Path | None = None,
    *,
    window_days: int = 30,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict:
    configured_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    db_path = state_db or configured_home / "state.db"
    hermes_home = db_path.parent if state_db is not None else configured_home
    if not db_path.is_file():
        return _empty_result(window_days)

    now_ts = now_ts or datetime.now(tz=timezone.utc).timestamp()
    cutoff = max(now_ts - window_days * 86400, mtime_after_ts or 0)
    daily: dict[date, dict] = {}
    intervals: dict[date, list[tuple[float, float]]] = defaultdict(list)
    children: dict[tuple[date, str], list[tuple[float, float]]] = defaultdict(list)

    try:
        with _read_only_db(db_path) as db:
            _require_columns(
                db,
                "sessions",
                {
                    "id",
                    "source",
                    "parent_session_id",
                    "started_at",
                    "ended_at",
                    "message_count",
                    "tool_call_count",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "model",
                    "model_config",
                    "end_reason",
                },
            )
            session_columns = _columns(db, "sessions")
            rewind_expr = "rewind_count" if "rewind_count" in session_columns else "0"
            _require_columns(
                db,
                "messages",
                {
                    "session_id",
                    "role",
                    "content",
                    "tool_call_id",
                    "tool_calls",
                    "tool_name",
                    "effect_disposition",
                    "timestamp",
                },
            )
            if db.execute(
                """
                SELECT 1 FROM sessions
                 WHERE max(
                         length(CAST(COALESCE(id, '') AS BLOB)),
                         length(CAST(COALESCE(source, '') AS BLOB)),
                         length(CAST(COALESCE(parent_session_id, '') AS BLOB)),
                         length(CAST(COALESCE(model, '') AS BLOB)),
                         length(CAST(COALESCE(model_config, '') AS BLOB)),
                         length(CAST(COALESCE(end_reason, '') AS BLOB))
                       ) > ?
                 LIMIT 1
                """,
                (MAX_SQLITE_TEXT_BYTES,),
            ).fetchone():
                raise AgentRuntimeScanIncomplete("Hermes session metadata exceeds scan limits")
            rows = db.execute(
                f"""
                SELECT id, source, parent_session_id, started_at, ended_at,
                       message_count, tool_call_count, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, reasoning_tokens, model,
                       model_config, end_reason, {rewind_expr} AS rewind_count,
                       COALESCE(
                         ended_at,
                         (SELECT MAX(m.timestamp) FROM messages AS m WHERE m.session_id = sessions.id),
                         started_at
                       ) AS last_active,
                       (SELECT p.end_reason FROM sessions AS p WHERE p.id = sessions.parent_session_id)
                         AS parent_end_reason
                  FROM sessions
                 WHERE COALESCE(
                         ended_at,
                         (SELECT MAX(m.timestamp) FROM messages AS m WHERE m.session_id = sessions.id),
                         started_at,
                         0
                       ) >= ?
                   AND COALESCE(started_at, ended_at, 0) <= ?
                 LIMIT ?
                """,
                (cutoff, now_ts, MAX_SQLITE_ROWS + 1),
            )

            session_kinds: dict[str, str] = {}
            session_starts: dict[str, float] = {}
            session_parents: dict[str, str | None] = {}
            session_branches: dict[str, bool] = {}
            counted_session_days: set[tuple[str, date]] = set()
            activity_bounds: dict[tuple[str, date], list[float]] = {}
            ids: list[str] = []
            retained_key_bytes = 0
            for index, row in enumerate(rows):
                if index >= MAX_SQLITE_ROWS:
                    raise AgentRuntimeScanIncomplete("Hermes session history exceeds scan limits")
                session_id = str(row[0])
                parent_id = str(row[2]) if row[2] else None
                retained_key_bytes += len(session_id.encode()) + len((parent_id or "").encode())
                if retained_key_bytes > MAX_SQLITE_RETAINED_KEY_BYTES:
                    raise AgentRuntimeScanIncomplete("Hermes session keys exceed scan limits")
                ids.append(session_id)
                session_parents[session_id] = parent_id
                start = _timestamp(row[3])
                end = _timestamp(row[4]) or _timestamp(row[16]) or start
                if start is None:
                    continue
                end = max(start, min(end or start, now_ts))
                day = datetime.fromtimestamp(start).date()
                bucket = _bucket(daily, day)
                branch = _is_hermes_branch(row[13], row[17])
                kind = _runtime_kind(row[1], row[2], row[13], row[17])
                session_kinds[session_id] = kind
                session_starts[session_id] = start
                session_branches[session_id] = branch
                if kind == "scheduled":
                    if start >= cutoff:
                        bucket["queue_events"] += 1
                        if _hermes_scheduled_completed(row[1], row[14]):
                            bucket["scheduled_task_runs"] += 1
                elif kind == "child":
                    parent = str(row[2] or "unlinked")
                    _add_child_intervals(children, parent, start, end, cutoff, now_ts)

                if kind in {"interactive", "continuation"} and start >= cutoff:
                    bucket["user_corrections"] += _int(row[15])

                if start >= cutoff:
                    bucket["tokens_input"] += _int(row[7])
                    bucket["tokens_output"] += _int(row[8])
                    bucket["tokens_cache_read"] += _int(row[9])
                    bucket["tokens_cache_creation"] += _int(row[10])
                    bucket["tokens_total"] += sum(_int(value) for value in row[7:11])
                    if isinstance(row[12], str) and row[12]:
                        target = "agent_model_usage" if kind == "child" else "model_usage"
                        _increment(bucket[target], _safe_label(row[12]))

            if ids:
                message_rows = 0
                message_columns = _columns(db, "messages")
                active_clause = (
                    " AND COALESCE(active, 1) != 0"
                    if "active" in message_columns
                    else ""
                )
                reasoning_columns = [
                    column
                    for column in ("reasoning", "reasoning_content", "reasoning_details")
                    if column in message_columns
                ]
                reasoning_expr = (
                    " OR ".join(f"NULLIF({column}, '') IS NOT NULL" for column in reasoning_columns)
                    or "0"
                )
                tool_results: dict[tuple[str, str], dict] = {}
                tool_result_rows = 0
                for offset in range(0, len(ids), 400):
                    batch = ids[offset : offset + 400]
                    placeholders = ",".join("?" for _ in batch)
                    oversized_query = (
                        "SELECT 1 FROM messages "
                        f"WHERE session_id IN ({placeholders}) "
                        "AND timestamp >= ? AND timestamp <= ? "
                        "AND ((role = 'tool' AND length(COALESCE(content, '')) > ?) "
                        "OR (role = 'assistant' AND length(COALESCE(tool_calls, '')) > ?) "
                        "OR length(CAST(COALESCE(session_id, '') AS BLOB)) > ? "
                        "OR length(CAST(COALESCE(tool_call_id, '') AS BLOB)) > ?)"
                        f"{active_clause} LIMIT 1"
                    )
                    if db.execute(
                        oversized_query,
                        [
                            *batch,
                            cutoff,
                            now_ts,
                            MAX_EVENT_JSON_BYTES,
                            MAX_EVENT_JSON_BYTES,
                            MAX_SQLITE_TEXT_BYTES,
                            MAX_SQLITE_TEXT_BYTES,
                        ],
                    ).fetchone():
                        raise AgentRuntimeScanIncomplete("Hermes tool event exceeds scan limits")
                    query = (
                        "SELECT session_id, tool_call_id, content, effect_disposition FROM messages "
                        f"WHERE session_id IN ({placeholders}) AND role = 'tool' "
                        "AND tool_call_id IS NOT NULL AND timestamp >= ? AND timestamp <= ?"
                        f"{active_clause}"
                    )
                    for result_session_id, call_id, content, disposition in db.execute(
                        query,
                        [*batch, cutoff, now_ts],
                    ):
                        tool_result_rows += 1
                        if tool_result_rows > MAX_SQLITE_ROWS:
                            raise AgentRuntimeScanIncomplete(
                                "Hermes tool result history exceeds scan limits"
                        )
                        if isinstance(call_id, str) and call_id:
                            retained_key_bytes += len(str(result_session_id).encode()) + len(
                                call_id.encode()
                            )
                            if retained_key_bytes > MAX_SQLITE_RETAINED_KEY_BYTES:
                                raise AgentRuntimeScanIncomplete(
                                    "Hermes tool result keys exceed scan limits"
                                )
                            tool_results[(str(result_session_id), call_id)] = _hermes_tool_result(
                                content,
                                disposition,
                            )

                for offset in range(0, len(ids), 400):
                    batch = ids[offset : offset + 400]
                    placeholders = ",".join("?" for _ in batch)
                    query = (
                        "SELECT session_id, role, tool_calls, timestamp, "
                        f"({reasoning_expr}) AS has_reasoning FROM messages "
                        f"WHERE session_id IN ({placeholders}) AND timestamp >= ? AND timestamp <= ?"
                        f"{active_clause}"
                    )
                    for message in db.execute(query, [*batch, cutoff, now_ts]):
                        message_rows += 1
                        if message_rows > MAX_SQLITE_ROWS:
                            raise AgentRuntimeScanIncomplete("Hermes message history exceeds scan limits")
                        timestamp = _timestamp(message[3])
                        if timestamp is None:
                            continue
                        session_id = str(message[0])
                        if session_branches.get(session_id) and timestamp < session_starts[session_id]:
                            continue
                        day = datetime.fromtimestamp(timestamp).date()
                        bucket = _bucket(daily, day)
                        role = message[1]
                        kind = session_kinds.get(session_id)
                        human_activity = role in {"user", "assistant"} and kind in {
                            "interactive",
                            "continuation",
                        }
                        if role in {"user", "assistant"}:
                            bucket["messages"] += 1
                        if role == "assistant" and human_activity and bool(message[4]):
                            bucket["reasoning_blocks"] += 1
                        if human_activity:
                            root = _hermes_root(session_id, session_kinds, session_parents)
                            key = (root, day)
                            if key not in counted_session_days:
                                bucket["sessions"] += 1
                                bucket["main_sessions"] += 1
                                counted_session_days.add(key)
                            bounds = activity_bounds.setdefault(key, [timestamp, timestamp])
                            bounds[0] = min(bounds[0], timestamp)
                            bounds[1] = max(bounds[1], timestamp)
                        if role == "user" and kind in {
                            "interactive",
                            "continuation",
                        }:
                            bucket["user_messages"] += 1
                        if role == "assistant":
                            score_eligible = kind != "scheduled"
                            for call in _hermes_tool_calls(message[2]):
                                bucket["tool_calls"] += 1
                                _record_tool_name(
                                    bucket,
                                    call["name"],
                                    score_eligible=score_eligible,
                                )
                                result = tool_results.get((session_id, call.get("id")))
                                if score_eligible and result and result["success"]:
                                    _record_hermes_scoring_call(
                                        bucket,
                                        call,
                                        result,
                                        hermes_home,
                                    )

            for (_root, day), (start, end) in activity_bounds.items():
                intervals[day].append((start, end))
    except (OSError, sqlite3.Error) as exc:
        raise AgentRuntimeScanIncomplete("Hermes history could not be read completely") from exc

    _apply_children(daily, children)
    return _result(daily, intervals, window_days, now_ts)


def scan_openclaw(
    state_dir: Path | None = None,
    *,
    window_days: int = 30,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict:
    root = state_dir or Path(os.environ.get("OPENCLAW_STATE_DIR", Path.home() / ".openclaw"))
    if not root.is_dir():
        return _empty_result(window_days)

    db_paths = sorted((root / "agents").glob("*/agent/openclaw-agent.sqlite"))
    if not db_paths:
        return _empty_result(window_days)

    now_ts = now_ts or datetime.now(tz=timezone.utc).timestamp()
    cutoff = max(now_ts - window_days * 86400, mtime_after_ts or 0)
    daily: dict[date, dict] = {}
    intervals: dict[date, list[tuple[float, float]]] = defaultdict(list)
    children: dict[tuple[date, str], list[tuple[float, float]]] = defaultdict(list)

    try:
        for db_path in db_paths:
            with _read_only_db(db_path) as db:
                tables = _tables(db)
                if {"session_nodes", "session_windows", "transcript_events"} <= tables:
                    _scan_openclaw_current(db, tables, daily, intervals, children, cutoff, now_ts)
                elif {"sessions", "session_entries", "transcript_events"} <= tables:
                    _scan_openclaw_legacy_db(db, tables, daily, intervals, children, cutoff, now_ts)
                else:
                    raise AgentRuntimeScanIncomplete("unsupported OpenClaw agent database schema")
    except (OSError, sqlite3.Error) as exc:
        raise AgentRuntimeScanIncomplete("OpenClaw history could not be read completely") from exc

    _apply_children(daily, children)
    return _result(daily, intervals, window_days, now_ts)


def scan_nanoclaw(
    roots: list[Path] | None = None,
    *,
    window_days: int = 30,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict:
    candidates = roots if roots is not None else resolve_nanoclaw_roots()
    detected = _unique_paths(
        [root.resolve() for root in candidates if (root / "data" / "v2.db").is_file()]
    )
    if not detected:
        return _empty_result(window_days)

    now_ts = now_ts or datetime.now(tz=timezone.utc).timestamp()
    cutoff = max(now_ts - window_days * 86400, mtime_after_ts or 0)
    daily: dict[date, dict] = {}
    intervals: dict[date, list[tuple[float, float]]] = defaultdict(list)

    try:
        for root in detected:
            central = root / "data" / "v2.db"
            with _read_only_db(central) as db:
                _require_columns(
                    db,
                    "sessions",
                    {"id", "agent_group_id", "messaging_group_id", "thread_id", "created_at", "last_active"},
                )
                rows = db.execute(
                    """
                    SELECT id, agent_group_id, messaging_group_id, thread_id, created_at, last_active
                      FROM sessions
                    WHERE julianday(COALESCE(last_active, created_at)) >= julianday(?, 'unixepoch')
                       AND julianday(created_at) <= julianday(?, 'unixepoch')
                     LIMIT ?
                    """,
                    (cutoff, now_ts, MAX_SQLITE_ROWS + 1),
                )

                for index, row in enumerate(rows):
                    if index >= MAX_SQLITE_ROWS:
                        raise AgentRuntimeScanIncomplete("NanoClaw session history exceeds scan limits")
                    start = _timestamp(row[4])
                    end = _timestamp(row[5]) or start
                    if start is None or end is None or end < cutoff or start > now_ts:
                        continue
                    end = max(start, min(end, now_ts))
                    scheduled = str(row[3] or "").startswith("system:")

                    # NanoClaw keeps one long-lived system task session. The
                    # individual ``messages_in(kind='task')`` rows are the
                    # runs; counting the container session would double them.
                    group_id = _safe_path_component(row[1])
                    session_id = _safe_path_component(row[0])
                    sessions_root = (root / "data" / "v2-sessions").resolve()
                    inbound = _confined_path(
                        sessions_root / group_id / session_id / "inbound.db",
                        sessions_root,
                    )
                    outbound = inbound.with_name("outbound.db")
                    human_bounds, agent_bounds = _scan_nanoclaw_messages(
                        inbound,
                        outbound,
                        daily,
                        cutoff,
                        now_ts,
                    )
                    if not scheduled:
                        for active_day, (first_event, last_event) in human_bounds.items():
                            active_bucket = _bucket(daily, active_day)
                            active_bucket["sessions"] += 1
                            active_bucket["main_sessions"] += 1
                            intervals[active_day].append((first_event, last_event))
                    for active_day in agent_bounds:
                        active_bucket = _bucket(daily, active_day)
                        active_bucket["sessions_with_orchestration"] += 1
                        active_bucket["max_parallel_agents"] = max(
                            active_bucket["max_parallel_agents"],
                            1,
                        )
            _scan_nanoclaw_provider_history(root, daily, cutoff, mtime_after_ts)
    except (OSError, sqlite3.Error) as exc:
        raise AgentRuntimeScanIncomplete("NanoClaw history could not be read completely") from exc

    return _result(daily, intervals, window_days, now_ts)


def _scan_nanoclaw_provider_history(root: Path, daily: dict[date, dict], cutoff: float, mtime_after_ts) -> None:
    """Merge private provider transcripts only for metrics absent from NanoClaw DBs.

    NanoClaw's split databases provide session/message provenance, while the
    provider-owned Claude history provides tool, skill, MCP, token, reasoning,
    and nested-agent evidence. Session and message counts stay DB-owned so the
    same activity is never counted twice.
    """
    from scan_transcripts import iter_transcript_files, process_session

    provider_daily: dict[date, dict] = {}
    ignored_intervals: dict[date, list[tuple[float, float]]] = {}
    local_skills: set[str] = set()
    sessions_root = (root / "data" / "v2-sessions").resolve()
    seen_paths: set[Path] = set()
    provider_bytes = 0
    provider_files = 0

    for skills_root in sessions_root.glob("*/.claude-shared/skills"):
        if not skills_root.is_dir():
            continue
        resolved = _confined_path(skills_root, sessions_root)
        local_skills.update(path.name for path in resolved.iterdir() if path.is_dir())

    project_roots = sorted(sessions_root.glob("*/.claude-shared/projects/*"))
    for project_dir in project_roots:
        if not project_dir.is_dir():
            continue
        resolved_project = _confined_path(project_dir, sessions_root)
        if resolved_project in seen_paths:
            continue
        seen_paths.add(resolved_project)
        for transcript in iter_transcript_files(resolved_project, cutoff, mtime_after_ts):
            resolved_transcript = _confined_path(transcript, sessions_root)
            if resolved_transcript in seen_paths:
                continue
            seen_paths.add(resolved_transcript)
            try:
                size = resolved_transcript.stat().st_size
            except OSError as exc:
                raise AgentRuntimeScanIncomplete("NanoClaw provider history could not be bounded") from exc
            provider_files += 1
            provider_bytes += size
            if (
                provider_files > MAX_PROVIDER_FILES
                or size > MAX_PROVIDER_FILE_BYTES
                or provider_bytes > MAX_PROVIDER_TOTAL_BYTES
            ):
                raise AgentRuntimeScanIncomplete("NanoClaw provider history exceeds scan limits")
            process_session(
                resolved_transcript,
                provider_daily,
                [],
                set(),
                ignored_intervals,
                is_main=True,
                is_cowork=False,
                local_skills=local_skills,
            )

    skipped_counts = {
        "sessions",
        "main_sessions",
        "messages",
        "user_messages",
        "cowork_sessions",
        "cowork_messages",
        "queue_events",
        "scheduled_task_runs",
        "scheduled_tasks_active",
        "max_concurrent_sessions",
    }
    for day, metrics in provider_daily.items():
        target = _bucket(daily, day)
        for field, value in metrics.items():
            if field in skipped_counts:
                continue
            if isinstance(value, dict):
                for label, count in value.items():
                    target[field][label] = target[field].get(label, 0) + _int(count)
            elif isinstance(value, list):
                target[field] = sorted(set(target.get(field) or []) | set(value))
            elif field in {
                "max_parallel_agents",
                "max_messages_in_session",
                "planning_measurement_version",
            }:
                target[field] = max(target.get(field, 0), _int(value))
            else:
                target[field] = target.get(field, 0) + _int(value)


def resolve_nanoclaw_roots() -> list[Path]:
    raw = os.environ.get("AIQRANK_NANOCLAW_ROOTS")
    if raw:
        return _unique_paths([Path(value).expanduser() for value in raw.split(os.pathsep) if value])

    home = Path.home()
    candidates = [
        home / "nanoclaw",
        home / "nanoclaw-v2",
        home / "dev" / "nanoclaw",
        home / "dev" / "nanoclaw-v2",
    ]
    return _unique_paths(candidates)


def _scan_openclaw_current(db, tables, daily, intervals, children, cutoff, now_ts) -> None:
    rows = db.execute(
        """
        SELECT w.session_id, w.session_key, w.started_at, w.ended_at,
               w.created_at, w.updated_at, w.parent_session_key, w.spawned_by,
               n.created_via, n.created_actor_type, n.parent_session_key
          FROM session_windows AS w
          JOIN session_nodes AS n ON n.session_key = w.session_key
         WHERE COALESCE(w.ended_at, w.updated_at, w.created_at) >= ?
           AND w.created_at <= ?
         LIMIT ?
        """,
        (_sqlite_time(cutoff), _sqlite_time(now_ts), MAX_SQLITE_ROWS + 1),
    )
    _scan_openclaw_rows(db, tables, rows, daily, intervals, children, cutoff, now_ts)


def _scan_openclaw_legacy_db(db, tables, daily, intervals, children, cutoff, now_ts) -> None:
    rows = db.execute(
        """
        SELECT s.session_id, s.session_key, NULL, NULL, s.created_at, s.updated_at,
               NULL, NULL, NULL, NULL, NULL
          FROM sessions AS s
         WHERE s.updated_at >= ? AND s.created_at <= ?
         LIMIT ?
        """,
        (_sqlite_time(cutoff), _sqlite_time(now_ts), MAX_SQLITE_ROWS + 1),
    )
    _scan_openclaw_rows(db, tables, rows, daily, intervals, children, cutoff, now_ts)


def _scan_openclaw_rows(db, tables, rows, daily, intervals, children, cutoff, now_ts) -> None:
    sessions: dict[str, tuple[str, float]] = {}
    for index, row in enumerate(rows):
        if index >= MAX_SQLITE_ROWS:
            raise AgentRuntimeScanIncomplete("OpenClaw session history exceeds scan limits")
        start = _timestamp(row[2]) or _timestamp(row[4])
        end = _timestamp(row[3]) or _timestamp(row[5]) or start
        if start is None or end is None or end < cutoff or start > now_ts:
            continue
        end = max(start, min(end, now_ts))
        day = datetime.fromtimestamp(start).date()
        bucket = _bucket(daily, day)
        parent = row[6] or row[7] or row[10]
        kind = _openclaw_kind(row[1], row[8], row[9], parent)
        if kind == "scheduled":
            # Every scheduled window is an attempt; completion credit needs a
            # terminal window — a recorded ended_at — so in-flight or aborted
            # runs cannot inflate schedule discipline. Mirrors NanoClaw's
            # status == "completed" gate (the schema here has no status
            # column, so ended_at is the only terminal signal).
            bucket["queue_events"] += 1
            if _timestamp(row[3]) is not None:
                bucket["scheduled_task_runs"] += 1
        elif kind == "child":
            _add_child_intervals(
                children,
                str(parent or "unlinked"),
                start,
                end,
                cutoff,
                now_ts,
            )

        sessions[str(row[0])] = (kind, start)

    _scan_openclaw_events(db, tables, sessions, daily, intervals, cutoff, now_ts)


def _scan_nanoclaw_messages(
    inbound: Path,
    outbound: Path,
    daily: dict[date, dict],
    cutoff: float,
    now_ts: float,
) -> tuple[dict[date, list[float]], dict[date, list[float]]]:
    if not inbound.is_file() or not outbound.is_file():
        raise AgentRuntimeScanIncomplete("NanoClaw session database is missing")

    human_bounds: dict[date, list[float]] = {}
    agent_bounds: dict[date, list[float]] = {}

    with _read_only_db(inbound) as db:
        _require_columns(
            db,
            "messages_in",
            {"kind", "timestamp", "status", "series_id", "source_session_id"},
        )
        inbound_rows = db.execute(
            """
            SELECT kind, timestamp, status, series_id, source_session_id
              FROM messages_in
             WHERE julianday(timestamp) >= julianday(?, 'unixepoch')
               AND julianday(timestamp) <= julianday(?, 'unixepoch')
            """,
            (cutoff, now_ts),
        )
        for index, (kind, timestamp, status, series_id, source_session_id) in enumerate(inbound_rows):
            if index >= MAX_SQLITE_ROWS:
                raise AgentRuntimeScanIncomplete("NanoClaw inbound history exceeds scan limits")
            ts = _timestamp(timestamp)
            if ts is None or not cutoff <= ts <= now_ts:
                continue
            bucket = _bucket(daily, datetime.fromtimestamp(ts).date())
            day = datetime.fromtimestamp(ts).date()
            bucket["messages"] += 1
            scheduled = kind == "task"
            if kind in {"chat", "chat-sdk"} and not source_session_id:
                bucket["user_messages"] += 1
                _record_bounds(human_bounds, day, ts)
            elif source_session_id:
                _record_bounds(agent_bounds, day, ts)
            if scheduled:
                bucket["queue_events"] += 1
                if status == "completed":
                    bucket["scheduled_task_runs"] += 1

    with _read_only_db(outbound) as db:
        if not _table_exists(db, "messages_out"):
            raise AgentRuntimeScanIncomplete("unsupported NanoClaw outbound schema")
        columns = _columns(db, "messages_out")
        if "timestamp" in columns:
            timestamp_col = "timestamp"
        elif "created_at" in columns:
            timestamp_col = "created_at"
        else:
            raise AgentRuntimeScanIncomplete("unsupported NanoClaw messages_out schema")
        outbound_rows = db.execute(
            f"""
            SELECT {timestamp_col}
              FROM messages_out
             WHERE julianday({timestamp_col}) >= julianday(?, 'unixepoch')
               AND julianday({timestamp_col}) <= julianday(?, 'unixepoch')
            """,
            (cutoff, now_ts),
        )
        for index, (timestamp,) in enumerate(outbound_rows):
            if index >= MAX_SQLITE_ROWS:
                raise AgentRuntimeScanIncomplete("NanoClaw outbound history exceeds scan limits")
            ts = _timestamp(timestamp)
            if ts is not None and cutoff <= ts <= now_ts:
                bucket = _bucket(daily, datetime.fromtimestamp(ts).date())
                bucket["messages"] += 1

    return human_bounds, agent_bounds


def _scan_openclaw_events(db, tables, sessions, daily, intervals, cutoff, now_ts) -> None:
    session_ids = list(sessions)
    event_count = 0
    active_session_days: set[tuple[str, date]] = set()
    activity_bounds: dict[tuple[str, date], list[float]] = {}
    for offset in range(0, len(session_ids), 400):
        batch = session_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        if {
            "session_transcript_active_events",
            "session_transcript_index_state",
        } <= tables:
            query = (
                "SELECT 1 FROM session_transcript_index_state "
                f"WHERE needs_rebuild != 0 AND session_id IN ({placeholders}) LIMIT 1"
            )
            if db.execute(query, batch).fetchone() is not None:
                raise AgentRuntimeScanIncomplete("OpenClaw transcript index needs rebuilding")

        bounds = [_sqlite_time(cutoff), _sqlite_time(now_ts)]
        if "session_transcript_active_events" in tables:
            oversized_query = f"""
                SELECT 1
                  FROM transcript_events AS e
                  JOIN session_transcript_active_events AS a
                    ON a.session_id = e.session_id AND a.event_seq = e.seq
                 WHERE e.session_id IN ({placeholders})
                   AND e.created_at >= ? AND e.created_at <= ?
                   AND length(e.event_json) > ?
                 LIMIT 1
            """
            query = f"""
                SELECT e.session_id, e.event_json, e.created_at
                  FROM transcript_events AS e
                  JOIN session_transcript_active_events AS a
                    ON a.session_id = e.session_id AND a.event_seq = e.seq
                 WHERE e.session_id IN ({placeholders})
                   AND e.created_at >= ? AND e.created_at <= ?
                 ORDER BY e.session_id, a.active_position
            """
        else:
            oversized_query = f"""
                SELECT 1
                  FROM transcript_events
                 WHERE session_id IN ({placeholders})
                   AND created_at >= ? AND created_at <= ?
                   AND length(event_json) > ?
                 LIMIT 1
            """
            query = f"""
                SELECT session_id, event_json, created_at
                  FROM transcript_events
                 WHERE session_id IN ({placeholders})
                   AND created_at >= ? AND created_at <= ?
                 ORDER BY session_id, seq
            """

        if db.execute(
            oversized_query,
            [*batch, *bounds, MAX_EVENT_JSON_BYTES],
        ).fetchone() is not None:
            raise AgentRuntimeScanIncomplete("OpenClaw transcript event exceeds scan limits")

        for session_id, raw, created_at in db.execute(query, [*batch, *bounds]):
            event_count += 1
            if event_count > MAX_SQLITE_ROWS:
                raise AgentRuntimeScanIncomplete("OpenClaw event history exceeds scan limits")
            session = sessions.get(str(session_id))
            if session is None:
                continue
            kind, start = session
            event_ts = _timestamp(created_at) or start
            if cutoff <= event_ts <= now_ts:
                day = datetime.fromtimestamp(event_ts).date()
                bucket = _bucket(daily, day)
                if _record_openclaw_event(bucket, raw, kind) and kind == "interactive":
                    key = (str(session_id), day)
                    if key not in active_session_days:
                        bucket["sessions"] += 1
                        bucket["main_sessions"] += 1
                        active_session_days.add(key)
                    bounds_for_day = activity_bounds.setdefault(key, [event_ts, event_ts])
                    bounds_for_day[0] = min(bounds_for_day[0], event_ts)
                    bounds_for_day[1] = max(bounds_for_day[1], event_ts)

    for (_session_id, day), (start, end) in activity_bounds.items():
        intervals[day].append((start, end))


def _record_openclaw_event(bucket: dict, raw: str, kind: str) -> bool:
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(event, dict) or event.get("type") != "message":
        return False
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    if role in ("user", "assistant"):
        bucket["messages"] += 1
    if role == "user" and kind == "interactive":
        bucket["user_messages"] += 1
    if role != "assistant":
        return role == "user"

    usage = message.get("usage") or {}
    if isinstance(usage, dict):
        input_tokens = _int(usage.get("input") or usage.get("input_tokens"))
        output_tokens = _int(usage.get("output") or usage.get("output_tokens"))
        cache_read = _int(usage.get("cacheRead") or usage.get("cache_read_input_tokens"))
        cache_write = _int(usage.get("cacheWrite") or usage.get("cache_creation_input_tokens"))
        bucket["tokens_input"] += input_tokens
        bucket["tokens_output"] += output_tokens
        bucket["tokens_cache_read"] += cache_read
        bucket["tokens_cache_creation"] += cache_write
        bucket["tokens_total"] += input_tokens + output_tokens + cache_read + cache_write

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("thinking", "reasoning"):
                bucket["reasoning_blocks"] += 1
            if block.get("type") in ("tool_use", "toolCall", "tool_call"):
                name = block.get("name") or block.get("tool_name")
                if isinstance(name, str) and name:
                    _record_tool_call(bucket, name)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = call.get("name") or function.get("name")
            if isinstance(name, str) and name:
                _record_tool_call(bucket, name)
    return True


def _runtime_kind(source, parent, model_config=None, parent_end_reason=None) -> str:
    label = str(source or "").lower()
    if label in {"subagent", "spawn", "agent"}:
        return "child"
    if label in {"cron", "heartbeat", "hook", "webhook", "system", "automation"}:
        return "scheduled"
    if parent and not _is_hermes_branch(model_config, parent_end_reason):
        return "continuation"
    return "interactive"


def _is_hermes_branch(model_config, parent_end_reason=None) -> bool:
    try:
        config = json.loads(model_config) if isinstance(model_config, str) else model_config
    except json.JSONDecodeError:
        return False
    return (
        isinstance(config, dict) and bool(config.get("_branched_from"))
    ) or parent_end_reason == "branched"


def _hermes_root(session_id, kinds, parents) -> str:
    current = session_id
    seen: set[str] = set()
    while kinds.get(current) == "continuation" and current not in seen:
        seen.add(current)
        parent = parents.get(current)
        if not parent:
            break
        current = parent
    return current


def _openclaw_kind(session_key, created_via, actor_type, parent) -> str:
    key = str(session_key or "").lower()
    if parent or created_via == "spawn" or actor_type == "agent" or ":subagent:" in key:
        return "child"
    if created_via in {"cron", "internal"} or actor_type == "system" or any(
        marker in key for marker in (":cron:", ":heartbeat:", ":hook:", ":webhook:")
    ):
        return "scheduled"
    return "interactive"


def _hermes_scheduled_completed(source, end_reason) -> bool:
    source_label = str(source or "").lower()
    reason = str(end_reason or "").lower()
    if source_label == "cron":
        return reason == "cron_complete"
    if source_label == "webhook":
        return reason == "webhook_complete"
    return reason in {"complete", "completed", "success", "succeeded", "agent_close"}


def _hermes_tool_calls(raw_calls) -> list[dict]:
    try:
        calls = json.loads(raw_calls) if isinstance(raw_calls, str) else raw_calls
    except json.JSONDecodeError:
        return []
    parsed: list[dict] = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = call.get("name") or function.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = call.get("arguments") or function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            parsed.append(
                {
                    "id": call.get("id") if isinstance(call.get("id"), str) else None,
                    "name": name,
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            )
    return parsed


def _hermes_tool_result(content, disposition) -> dict:
    disposition_label = str(disposition or "").lower()
    if disposition_label in {
        "blocked",
        "error",
        "failed",
        "failure",
        "pending",
        "rejected",
        "staged",
    }:
        return {"success": False, "path": None}
    try:
        result = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return {"success": False, "path": None}
    if isinstance(result, dict):
        deferred = result.get("staged") is True or result.get("pending") is True
        success = (
            not deferred
            and result.get("success") is not False
            and result.get("is_error") is not True
        )
        if result.get("error") and "success" not in result:
            success = False
        path = result.get("path") if isinstance(result.get("path"), str) else None
        return {"success": success, "path": path}
    return {"success": False, "path": None}


def _record_hermes_scoring_call(bucket, call: dict, result: dict, hermes_home: Path) -> None:
    name = call["name"]
    arguments = call["arguments"]
    if name == "skill_view":
        skill = arguments.get("name")
        if isinstance(skill, str) and skill:
            _increment(bucket["skill_counts"], _safe_label(skill))
        return

    if name == "skill_manage":
        action = str(arguments.get("action") or "").lower()
        skill = arguments.get("name")
        authored = action == "create" or (
            action in {"edit", "patch", "write_file"}
            and _hermes_local_skill_path(result.get("path"), hermes_home)
        )
        if authored and isinstance(skill, str) and skill:
            label = _safe_label(skill)
            if label not in bucket["authored_skill_names"]:
                bucket["authored_skill_names"].append(label)
        return

    if name not in {"write_file", "patch"}:
        return
    target = arguments.get("path") or arguments.get("file_path")
    if not isinstance(target, str) or not target:
        return
    normalized = target.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    if basename in {"AGENTS.md", "CLAUDE.md", "SOUL.md"}:
        bucket["claude_md_writes"] += 1
        if basename == "AGENTS.md":
            bucket["agents_md_writes"] += 1
    if basename in {".mcp.json", "mcp.json"}:
        bucket["custom_mcp_config_writes"] += 1


def _hermes_local_skill_path(raw_path, hermes_home: Path) -> bool:
    if not isinstance(raw_path, str) or not raw_path:
        return False
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return False
    try:
        path.resolve().relative_to((hermes_home / "skills").resolve())
    except (OSError, ValueError):
        return False
    return True


def _apply_children(
    daily: dict[date, dict],
    children: dict[tuple[date, str], list[tuple[float, float]]],
) -> None:
    for (day, _parent), intervals in children.items():
        bucket = _bucket(daily, day)
        count = _max_overlapping(intervals)
        bucket["sessions_with_orchestration"] += 1
        bucket["max_parallel_agents"] = max(bucket["max_parallel_agents"], count)
        if count >= 2:
            bucket["parallel_agent_turns"] += 1


def _add_interval(intervals, start: float, end: float, cutoff: float, now_ts: float) -> None:
    clipped_start = max(start, cutoff)
    clipped_end = min(max(start, end), now_ts)
    if clipped_end < clipped_start:
        return
    if clipped_end == clipped_start:
        intervals[datetime.fromtimestamp(clipped_start).date()].append(
            (clipped_start, clipped_end)
        )
        return
    cursor = clipped_start
    while cursor < clipped_end:
        day = datetime.fromtimestamp(cursor).date()
        next_day = datetime.combine(
            date.fromordinal(day.toordinal() + 1),
            datetime.min.time(),
        ).timestamp()
        segment_end = min(clipped_end, next_day)
        intervals[day].append((cursor, segment_end))
        if segment_end >= clipped_end:
            break
        cursor = next_day


def _add_child_intervals(children, parent, start, end, cutoff, now_ts) -> None:
    split: dict[date, list[tuple[float, float]]] = defaultdict(list)
    _add_interval(split, start, end, cutoff, now_ts)
    for day, values in split.items():
        children[(day, parent)].extend(values)


def _record_bounds(bounds: dict[date, list[float]], day: date, timestamp: float) -> None:
    values = bounds.setdefault(day, [timestamp, timestamp])
    values[0] = min(values[0], timestamp)
    values[1] = max(values[1], timestamp)


def _record_tool_call(bucket: dict, name: str) -> None:
    bucket["tool_calls"] += 1
    _record_tool_name(bucket, name)


def _record_tool_name(bucket: dict, name: str, *, score_eligible: bool = True) -> None:
    from scan_transcripts import _normalize_tool_name

    label = _safe_label(_normalize_tool_name(name))
    _increment(bucket["tool_name_counts"], label)
    parts = label.split("__", 2)
    server = parts[1] if len(parts) == 3 and parts[0].lower() == "mcp" else None
    if server and score_eligible:
        _increment(bucket["mcp_server_counts"], server)


def _result(daily, intervals, window_days, now_ts) -> dict:
    from scan_transcripts import (
        _cap_codex_dictionary,
        _has_activity,
        _rollup_from_daily,
        max_concurrent_sustained,
        min_sustained_secs,
    )

    cutoff_date = datetime.fromtimestamp(now_ts - window_days * 86400).date()
    active = {day: metrics for day, metrics in daily.items() if day >= cutoff_date and _has_activity(metrics)}
    for metrics in active.values():
        for field in (
            "tool_name_counts",
            "skill_counts",
            "mcp_server_counts",
            "agent_type_counts",
            "model_usage",
            "agent_model_usage",
            "effort_usage",
            "model_tokens_out",
        ):
            metrics[field] = _cap_codex_dictionary(metrics.get(field) or {})
    minimum = min_sustained_secs()
    for day, day_intervals in intervals.items():
        if day in active:
            active[day]["max_concurrent_sessions"] = max_concurrent_sustained(day_intervals, minimum)
    return {
        "detected": True,
        "daily": [{"date": day.isoformat(), "metrics": metrics} for day, metrics in sorted(active.items())],
        "rollup": _rollup_from_daily(active.values()),
        "intervals_by_day": {
            day.isoformat(): [[start, end] for start, end in values]
            for day, values in sorted(intervals.items())
            if day >= cutoff_date
        },
    }


def _empty_result(window_days: int) -> dict:
    return {"detected": False, "daily": [], "rollup": {}, "intervals_by_day": {}}


def _bucket(daily: dict[date, dict], day: date) -> dict:
    from scan_transcripts import _new_day_metrics

    if day not in daily:
        daily[day] = _new_day_metrics()
    return daily[day]


@contextmanager
def _read_only_db(path: Path):
    db = None
    try:
        db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=2)
        db.execute("PRAGMA query_only = ON")
    except (OSError, sqlite3.Error) as exc:
        if db is not None:
            db.close()
        raise AgentRuntimeScanIncomplete("runtime database could not be opened") from exc
    try:
        yield db
    finally:
        db.close()


def _tables(db) -> set[str]:
    return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_exists(db, table: str) -> bool:
    return table in _tables(db)


def _columns(db, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def _require_columns(db, table: str, required: set[str]) -> None:
    missing = required - _columns(db, table)
    if missing:
        raise AgentRuntimeScanIncomplete(f"unsupported {table} schema")


def _timestamp(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return _timestamp(float(raw))
        except ValueError:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _sqlite_time(timestamp: float) -> int:
    return int(timestamp * 1000)


def _max_overlapping(intervals: list[tuple[float, float]]) -> int:
    events = [(start, 1) for start, _end in intervals]
    events.extend((end, -1) for _start, end in intervals)
    current = peak = 0
    for _timestamp_value, delta in sorted(events, key=lambda event: (event[0], event[1])):
        current += delta
        peak = max(peak, current)
    return peak


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _increment(mapping: dict, key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def _safe_label(value: str) -> str:
    from scan_transcripts import _normalize_codex_label

    return _normalize_codex_label(value)


def _safe_path_component(value) -> str:
    component = str(value or "")
    if not component or component in {".", ".."} or "/" in component or "\\" in component:
        raise AgentRuntimeScanIncomplete("NanoClaw session path is not confined")
    return component


def _confined_path(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise AgentRuntimeScanIncomplete("NanoClaw session path escapes its runtime root")
    return resolved


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser().absolute())
        if key not in seen:
            seen.add(key)
            result.append(path.expanduser())
    return result
