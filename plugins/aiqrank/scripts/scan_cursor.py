#!/usr/bin/env python3
"""
AIQ Rank Cursor scanner.

Reads Cursor's local SQLite key/value store at
``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`` and
emits a per-day metrics envelope identical in shape to ``scan_codex.py``
and ``scan_opencode.py``:

    {
      "source": "cursor",
      "window_days": N,
      "daily": [{"date": "YYYY-MM-DD", "metrics": {...}}, ...],
      "rollup": {...},
      "intervals_by_day": {"YYYY-MM-DD": [[start, end], ...]}
    }

Caveats — read before trusting any field:

  * Cursor's storage schema is reverse-engineered. Anysphere does not
    publish a stable schema; this implementation was tested against
    Cursor v3.2.11 (macOS). Schemas may shift between releases — every
    field is treated as best-effort. Missing or malformed fields
    contribute 0 and never raise. References:
    https://vibe-replay.com/blog/cursor-local-storage/
    https://github.com/junhoyeo/tokscale
  * Token coverage on bubble docs is sparse — Cursor only writes
    ``tokenCount`` for some message bubbles. Token totals are
    best-effort and will undercount.
  * Per-call MCP attribution is unreliable in Cursor's bubble
    ``toolFormerData`` — we therefore report MCP at the *config* level:
    keys under ``mcpServers`` in ``~/.cursor/mcp.json`` and recently
    modified per-project ``.cursor/mcp.json`` files, weighted once per
    active day.
  * Tool-call attribution from bubbles is sparse; we accept undercounts
    rather than guess.
  * Bubbles often don't carry their own timestamp — we bucket each
    bubble to its parent composer's ``createdAt`` day.
  * macOS-only paths in v1. Linux/Windows storage locations are not
    yet attempted.
  * Privacy: never emit ``bubble.text``, ``composerData.name`` or any
    other free-text content. Counts, names, and booleans only.

Python stdlib only.
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
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import (  # noqa: E402  -- shared helpers
    _local_claude_skills,
    _ts_to_date,
    max_concurrent_sustained,
)


DEFAULT_WINDOW_DAYS = 30

# Mirrors scan_codex / scan_opencode — keep keys symmetric across sources
# so the upload payload schema is uniform.
_COUNT_FIELDS = (
    "sessions",
    "main_sessions",  # Cursor has no parent/child split; same as `sessions`.
    "messages",
    "tool_calls",
    "sessions_with_tools",
    "sessions_with_orchestration",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_creation",
    "tokens_total",
    "reasoning_blocks",
    "claude_md_writes",          # .cursorrules + .cursor/rules/*.md mtime hits
    "custom_mcp_config_writes",  # ~/.cursor/mcp.json + per-project mcp.json mtimes
    "custom_skill_files_written",
)

_PEAK_FIELDS = (
    "max_messages_in_session",
    "max_concurrent_sessions",
)

_DICT_FIELDS = (
    "tool_name_counts",
    "skill_counts",
    "mcp_server_counts",
    "agent_type_counts",
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
    cursor_db_path: Path | None = None,
    cursor_user_dir: Path | None = None,
    cursor_home: Path | None = None,
    workspace_storage_dir: Path | None = None,
    home: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict:
    """Scan Cursor's local state for per-day activity metrics.

    Parameters are injectable for testing — defaults point at the real
    macOS paths.

    Parameters
    ----------
    cursor_db_path : Path | None
        Path to ``state.vscdb``. Defaults to
        ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``.
    cursor_user_dir : Path | None
        ``Application Support/Cursor/User``; used to default
        ``cursor_db_path`` and ``workspace_storage_dir``.
    cursor_home : Path | None
        ``~/.cursor`` (global mcp.json, project agent transcripts).
    workspace_storage_dir : Path | None
        Where ``workspaceStorage/*/workspace.json`` lives. Used to
        discover per-project folders for rules + per-project MCP.
    home : Path | None
        Override ``~`` (used by ``_local_claude_skills`` which expects
        ``~/.claude``). Defaults to ``Path.home()``.
    """
    home = home or Path.home()
    cursor_user_dir = cursor_user_dir or (
        home / "Library" / "Application Support" / "Cursor" / "User"
    )
    cursor_db_path = cursor_db_path or (
        cursor_user_dir / "globalStorage" / "state.vscdb"
    )
    cursor_home = cursor_home or (home / ".cursor")
    workspace_storage_dir = workspace_storage_dir or (
        cursor_user_dir / "workspaceStorage"
    )

    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)
    effective_cutoff = (
        cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)
    )

    daily: dict[date, dict] = {}
    intervals_by_day: dict[date, list[tuple[float, float]]] = {}
    composer_day: dict[str, date] = {}

    if cursor_db_path.is_file():
        conn = _open_ro(cursor_db_path)
        if conn is not None:
            try:
                _scan_composers(
                    conn,
                    daily,
                    intervals_by_day,
                    composer_day,
                    effective_cutoff,
                )
                _scan_bubbles(conn, daily, composer_day)
            finally:
                conn.close()
    else:
        sys.stderr.write(
            f"scan_cursor: state.vscdb not found at {cursor_db_path}\n"
        )

    workspace_folders = _discover_workspace_folders(workspace_storage_dir)

    _scan_project_rules(workspace_folders, daily, effective_cutoff)

    active_days = sorted(d for d, m in daily.items() if m["sessions"] > 0)
    _scan_mcp_servers(
        cursor_home,
        workspace_folders,
        daily,
        active_days,
        effective_cutoff,
    )

    _scan_agent_transcripts(cursor_home, daily, effective_cutoff)

    # Concurrency peak per day from collected intervals.
    for d, ivs in intervals_by_day.items():
        peak = max_concurrent_sustained(ivs)
        bucket = _bucket(daily, d)
        if peak > bucket["max_concurrent_sessions"]:
            bucket["max_concurrent_sessions"] = peak

    # Local skill snapshot — applied to rollup once (no per-day attribution),
    # mirroring scan_opencode.
    skill_names = _local_claude_skills(home / ".claude")

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
        "source": "cursor",
        "window_days": window_days,
        "daily": daily_list,
        "rollup": rollup,
        "intervals_by_day": intervals_serialized,
    }


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _open_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open the SQLite db read-only, immutable. Return None on any failure."""
    try:
        uri = f"file:{db_path}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None


def _split_intervals_by_day(
    start_ts: float, end_ts: float
) -> Iterable[tuple[date, float, float]]:
    """Yield (day, start, end) tuples — splitting at local midnight."""
    if end_ts < start_ts:
        end_ts = start_ts + 1.0
    cur_start = start_ts
    while True:
        d = datetime.fromtimestamp(cur_start).date()
        next_midnight = (
            datetime(d.year, d.month, d.day) + timedelta(days=1)
        ).timestamp()
        if end_ts <= next_midnight:
            yield d, cur_start, end_ts
            return
        yield d, cur_start, next_midnight
        cur_start = next_midnight


# ---------------------------------------------------------------------------
# Composers (sessions)
# ---------------------------------------------------------------------------


def _scan_composers(
    conn: sqlite3.Connection,
    daily: dict[date, dict],
    intervals_by_day: dict[date, list[tuple[float, float]]],
    composer_day: dict[str, date],
    cutoff_ts: float,
) -> None:
    try:
        cursor = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        )
    except sqlite3.Error:
        return

    msgs_per_session_per_day: dict[date, list[int]] = {}

    for key, value in cursor:
        if not isinstance(value, (str, bytes)):
            continue
        try:
            doc = json.loads(value)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue

        created_ms = doc.get("createdAt")
        if not isinstance(created_ms, (int, float)):
            continue
        last_ms = doc.get("lastUpdatedAt")
        start_ts = float(created_ms) / 1000.0
        if isinstance(last_ms, (int, float)) and float(last_ms) / 1000.0 >= start_ts:
            end_ts = float(last_ms) / 1000.0
        else:
            end_ts = start_ts + 1.0
        if end_ts < cutoff_ts:
            continue
        clipped_start_ts = max(start_ts, cutoff_ts)

        if isinstance(key, bytes):
            key = key.decode("utf-8", errors="replace")
        try:
            _, composer_id = key.split(":", 1)
        except ValueError:
            continue
        if not composer_id:
            continue

        primary_day = _ts_to_date(clipped_start_ts)
        if primary_day is None:
            continue
        composer_day[composer_id] = primary_day

        chunks = list(_split_intervals_by_day(clipped_start_ts, end_ts))
        is_agentic = bool(doc.get("isAgentic"))
        headers = doc.get("fullConversationHeadersOnly")
        msg_count = len(headers) if isinstance(headers, list) else 0

        for d, s, e in chunks:
            bucket = _bucket(daily, d)
            bucket["sessions"] += 1
            bucket["main_sessions"] += 1
            if is_agentic:
                bucket["sessions_with_orchestration"] += 1
            intervals_by_day.setdefault(d, []).append((s, e))

        primary_bucket = _bucket(daily, primary_day)
        primary_bucket["messages"] += msg_count
        msgs_per_session_per_day.setdefault(primary_day, []).append(msg_count)

    for d, msg_counts in msgs_per_session_per_day.items():
        if not msg_counts:
            continue
        peak = max(msg_counts)
        bucket = _bucket(daily, d)
        if peak > bucket["max_messages_in_session"]:
            bucket["max_messages_in_session"] = peak


# ---------------------------------------------------------------------------
# Bubbles (per-message data: thinking, tokens, tool calls)
# ---------------------------------------------------------------------------


def _scan_bubbles(
    conn: sqlite3.Connection,
    daily: dict[date, dict],
    composer_day: dict[str, date],
) -> None:
    if not composer_day:
        return

    # Single global query — parse composerId out of the key. Cheaper than
    # one query per composer when there are many composers.
    try:
        cursor = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        )
    except sqlite3.Error:
        return

    sessions_with_tools_seen: dict[date, set[str]] = {}

    for key, value in cursor:
        if isinstance(key, bytes):
            try:
                key = key.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if not isinstance(key, str):
            continue
        # Key format: bubbleId:<composerId>:<bubbleId>
        parts = key.split(":", 2)
        if len(parts) < 3:
            continue
        composer_id = parts[1]
        d = composer_day.get(composer_id)
        if d is None:
            continue  # Composer was outside the window — skip its bubbles.

        if not isinstance(value, (str, bytes)):
            continue
        try:
            doc = json.loads(value)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue

        bucket = _bucket(daily, d)

        # Reasoning: any non-empty `thinking` field.
        thinking = doc.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            bucket["reasoning_blocks"] += 1
        elif isinstance(thinking, dict) and thinking:
            bucket["reasoning_blocks"] += 1

        # Tokens: structured dict, plain int, or absent (sparse).
        tc = doc.get("tokenCount")
        if isinstance(tc, dict):
            ti = _safe_int(tc.get("input"))
            to = _safe_int(tc.get("output"))
            cr = _safe_int(tc.get("cacheRead") or tc.get("cache_read"))
            cc = _safe_int(tc.get("cacheCreation") or tc.get("cache_creation"))
            total = tc.get("total")
            bucket["tokens_input"] += ti
            bucket["tokens_output"] += to
            bucket["tokens_cache_read"] += cr
            bucket["tokens_cache_creation"] += cc
            bucket["tokens_total"] += (
                _safe_int(total) if total is not None else (ti + to + cr + cc)
            )
        elif isinstance(tc, (int, float)):
            bucket["tokens_total"] += _safe_int(tc)

        # Tool calls: toolFormerData may be a single dict, a list of dicts,
        # or absent. Be defensive.
        tfd = doc.get("toolFormerData")
        tool_entries: list[dict] = []
        if isinstance(tfd, dict):
            tool_entries = [tfd]
        elif isinstance(tfd, list):
            tool_entries = [t for t in tfd if isinstance(t, dict)]

        for entry in tool_entries:
            name = entry.get("toolName") or entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            bucket["tool_calls"] += 1
            bucket["tool_name_counts"][name] = (
                bucket["tool_name_counts"].get(name, 0) + 1
            )
            sessions_with_tools_seen.setdefault(d, set()).add(composer_id)

    for d, composers in sessions_with_tools_seen.items():
        _bucket(daily, d)["sessions_with_tools"] += len(composers)


def _safe_int(v) -> int:
    try:
        if v is None:
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Workspace + project-rules + MCP + agent transcripts
# ---------------------------------------------------------------------------


def _discover_workspace_folders(workspace_storage_dir: Path) -> list[Path]:
    """Read each ``workspaceStorage/*/workspace.json`` and return the
    referenced project folders as filesystem paths (skipping non-file URIs)."""
    if not workspace_storage_dir.is_dir():
        return []
    folders: list[Path] = []
    seen: set[Path] = set()
    try:
        entries = list(workspace_storage_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        ws_json = entry / "workspace.json"
        if not ws_json.is_file():
            continue
        try:
            with ws_json.open("r") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        folder_uri = doc.get("folder")
        if not isinstance(folder_uri, str):
            continue
        if not folder_uri.startswith("file://"):
            continue
        path_str = unquote(folder_uri[len("file://"):])
        p = Path(path_str)
        if p in seen:
            continue
        seen.add(p)
        folders.append(p)
    return folders


def _scan_project_rules(
    workspace_folders: list[Path],
    daily: dict[date, dict],
    cutoff_ts: float,
) -> None:
    """Count mtime hits on .cursorrules and .cursor/rules/*.md within window.

    One increment per (file, day). Files with mtime older than the cutoff
    are silently ignored.
    """
    seen: set[Path] = set()
    for folder in workspace_folders:
        candidates: list[Path] = []
        legacy = folder / ".cursorrules"
        if legacy.is_file():
            candidates.append(legacy)
        rules_dir = folder / ".cursor" / "rules"
        if rules_dir.is_dir():
            try:
                for p in rules_dir.glob("*.md"):
                    if p.is_file():
                        candidates.append(p)
            except OSError:
                pass

        for p in candidates:
            if p in seen:
                continue
            seen.add(p)
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m < cutoff_ts:
                continue
            d = datetime.fromtimestamp(m).date()
            _bucket(daily, d)["claude_md_writes"] += 1


def _scan_mcp_servers(
    cursor_home: Path,
    workspace_folders: list[Path],
    daily: dict[date, dict],
    active_days: list[date],
    cutoff_ts: float,
) -> None:
    """Parse global ~/.cursor/mcp.json and per-project .cursor/mcp.json files.

    Per-call attribution is unreliable, so we report config-level. Global
    config names are included whenever present. Per-project config names are
    included only when the config file was touched inside the scan window, so
    stale remembered workspaces do not leak unrelated internal server names.

    The mcp.json file mtime within the window also increments
    ``custom_mcp_config_writes`` once per file.
    """
    server_names: set[str] = set()

    def _mtime_in_window(path: Path) -> bool:
        try:
            m = path.stat().st_mtime
        except OSError:
            return False
        if m < cutoff_ts:
            return False
        d = datetime.fromtimestamp(m).date()
        _bucket(daily, d)["custom_mcp_config_writes"] += 1
        return True

    global_mcp = cursor_home / "mcp.json"
    if global_mcp.is_file():
        _mtime_in_window(global_mcp)
        for name in _read_mcp_server_names(global_mcp):
            server_names.add(name)

    for folder in workspace_folders:
        proj_mcp = folder / ".cursor" / "mcp.json"
        if not proj_mcp.is_file():
            continue
        if not _mtime_in_window(proj_mcp):
            continue
        for name in _read_mcp_server_names(proj_mcp):
            server_names.add(name)

    if not server_names or not active_days:
        return

    for d in active_days:
        bucket = _bucket(daily, d)
        for name in server_names:
            bucket["mcp_server_counts"][name] = (
                bucket["mcp_server_counts"].get(name, 0) + 1
            )


def _read_mcp_server_names(path: Path) -> list[str]:
    try:
        with path.open("r") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    return [k for k in servers.keys() if isinstance(k, str) and k]


def _scan_agent_transcripts(
    cursor_home: Path, daily: dict[date, dict], cutoff_ts: float
) -> None:
    """Walk ~/.cursor/projects/*/agent-transcripts/*.jsonl, counting agent
    types per day. Best-effort: missing dir, unreadable file, malformed line
    all degrade gracefully."""
    projects_dir = cursor_home / "projects"
    if not projects_dir.is_dir():
        return

    try:
        project_entries = list(projects_dir.iterdir())
    except OSError:
        return

    for project in project_entries:
        transcripts_dir = project / "agent-transcripts"
        if not transcripts_dir.is_dir():
            continue
        try:
            files = list(transcripts_dir.glob("*.jsonl"))
        except OSError:
            continue
        for jl in files:
            try:
                stat = jl.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff_ts:
                continue
            d = datetime.fromtimestamp(stat.st_mtime).date()
            try:
                fh = jl.open("r")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    agent_type = rec.get("type")
                    if isinstance(agent_type, str) and agent_type:
                        bucket = _bucket(daily, d)
                        bucket["agent_type_counts"][agent_type] = (
                            bucket["agent_type_counts"].get(agent_type, 0) + 1
                        )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


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

    result = scan(window_days=window_days, mtime_after_ts=mtime_after_ts)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
