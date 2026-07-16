#!/usr/bin/env python3
"""
AIQ Rank transcript scanner.

Walks three roots and extracts activity metrics from the last 30 days:

  1. ~/.claude/projects/*              — interactive Claude Code sessions
  2. ~/Library/Application Support/Claude/local-agent-mode-sessions/
       {account}/{workspace}/local_*/.claude/projects/*  — Claude Cowork
       (Local Agent Mode) autonomous sessions
  3. ~/.codex/sessions/**/rollout-*.jsonl — Codex CLI rollouts (optional)

Events are bucketed into per-source dicts:

  - `claude_code` — interactive sessions under ~/.claude/projects/
  - `cowork`       — autonomous local-agent-mode sessions, plus any session
                     whose initialMessage marks it as a scheduled-task run.
  - `codex`        — Codex CLI rollouts (only emitted when ~/.codex/ exists)

Cowork-specific counters (`cowork_sessions`, `cowork_messages`,
`queue_events`, `scheduled_task_runs`, `scheduled_tasks_active`) only
appear in the cowork bucket. All other counters (messages, tool calls,
tokens, sessions, ...) follow the source they came from.

Buckets every event by its local-calendar-day timestamp, emits a `daily`
array per source (one entry per day that had activity) plus a `rollup`
aggregate over the full window per source.

Output JSON:

    {
      "window_days": 30,
      "by_source": {
        "claude_code": {
          "daily": [{"date": "YYYY-MM-DD", "metrics": {...}}, ...],
          "rollup": {...},
          "intervals_by_day": {"YYYY-MM-DD": [[start_ts, end_ts], ...]}
        },
        "cowork": {
          "daily": [...], "rollup": {...}, "intervals_by_day": {...}
        },
        "codex": {
          "daily": [...], "rollup": {...}, "intervals_by_day": {...}
        }
      },
      "first_messages_sample": [...]   # local-only, for role inference
    }

The `codex` source row is omitted when `~/.codex/` does not exist.

The plugin sends each source's `daily` (filtered by latest_date cursor)
plus role metadata to the server. The `first_messages_sample` never
leaves the machine. The per-source rollup is included so the
unauthenticated pair/teaser flow can show preview totals without
re-aggregating server-side. Per-source `intervals_by_day` is local-only
— the upload hook unions intervals across sources to compute a combined
concurrency peak, then strips them before posting to the server.

Python stdlib only (no third-party dependencies).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


# -- Constants --

DEFAULT_WINDOW_DAYS = 30

# A user's "max concurrent sessions" peak only counts if it held for at least
# this many seconds within the day. A 30-second freak overlap (one tool
# finishing as another starts) shouldn't move the metric. 300s (5 min) was
# chosen by analyzing the per-day distribution of one heavy user: ≥300s lowers
# the daily peak on ~50% of active days (typically by 1–2), while ≥600s
# yields diminishing returns. Override via AIQRANK_MIN_SUSTAINED_SECS.
DEFAULT_MIN_SUSTAINED_SECS = 300


def min_sustained_secs() -> int:
    raw = os.environ.get("AIQRANK_MIN_SUSTAINED_SECS")
    if raw is None or raw == "":
        return DEFAULT_MIN_SUSTAINED_SECS
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_MIN_SUSTAINED_SECS
    return v if v >= 0 else DEFAULT_MIN_SUSTAINED_SECS
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

# Codex-specific
CODEX_TOOL_TYPES = {"function_call", "custom_tool_call", "tool_use", "local_shell_call"}
CODEX_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE
)
CODEX_SKILLS_DIR_FRAGMENT = "/.codex/skills/"
CODEX_SKILL_NAME_RE = re.compile(r"\.codex/skills/([^/]+)/")
CODEX_CONFIG_TOML_BASENAMES = ("/.codex/config.toml",)
CODEX_MAX_LABEL_LENGTH = 100
CODEX_MAX_DICT_KEYS = 50
CODEX_OTHER_LABEL = "__other__"
_CODEX_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_CODEX_MALFORMED_TIMESTAMP_RE = re.compile(
    r'["\']timestamp["\']\s*:\s*["\']([^"\']+)["\']'
)

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


# Claude Desktop / cowork spawns MCP servers with a per-installation UUID in
# the tool name, e.g. `mcp__4507b484-062b-4cc6-85ff-0862c2c5567a__search`.
# Those UUIDs are device-identifying. Bucket them under a single sentinel so
# we keep the usage signal without shipping IDs.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DYNAMIC_MCP_SENTINEL = "dynamic"


def _normalize_tool_name(name: str) -> str:
    """Replace UUID-shaped MCP segments with a stable sentinel.

    `mcp__<uuid>__<action>` → `mcp__dynamic__<action>`. Every segment after
    the `mcp__` prefix is checked, not just the server segment, so any
    UUID-shaped identifier embedded in the action portion is also
    bucketed. Non-MCP names pass through unchanged.
    """
    if not name.startswith("mcp__"):
        return name
    parts = name.split("__")
    replaced = False
    for i in range(1, len(parts)):
        if _UUID_RE.match(parts[i]):
            parts[i] = _DYNAMIC_MCP_SENTINEL
            replaced = True
    return "__".join(parts) if replaced else name

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
    "cowork_sessions",
    "cowork_messages",
    "queue_events",
    "scheduled_task_runs",
    "reasoning_blocks",
    "command_diversity",
    "file_changes",
    "agents_md_writes",
    "prs_opened",
    "main_tool_calls",
    "main_user_messages",
)

_PEAK_FIELDS = (
    "max_concurrent_sessions",
    "max_messages_in_session",
    "max_parallel_agents",
    "scheduled_tasks_active",
)

_DICT_FIELDS = (
    "tool_name_counts",
    "skill_counts",
    "mcp_server_counts",
    "agent_type_counts",
    # Model usage is captured verbatim (e.g. "claude-opus-4-6") with no
    # normalization, so future analysis can distinguish model versions and
    # drift. Display normalizes to families. `model_usage` is main-session
    # only; `agent_model_usage` holds subagent transcripts (which carry the
    # subagent's model, not the user's choice). `model_tokens_out` is
    # per-model output tokens for main sessions.
    "model_usage",
    "agent_model_usage",
    "model_tokens_out",
    "effort_usage",
)

# List-valued fields that aggregate via set-union across days. Server-side
# scoring reads `authored_skill_names` for `bespoke_practice` (Claude /
# Codex / Cowork all share the helper).
_LIST_FIELDS = ("authored_skill_names",)


def _new_day_metrics() -> dict:
    """Empty metrics dict for one calendar day."""
    out: dict = {f: 0 for f in _COUNT_FIELDS}
    for f in _PEAK_FIELDS:
        out[f] = 0
    for f in _DICT_FIELDS:
        out[f] = {}
    for f in _LIST_FIELDS:
        out[f] = []
    return out


SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_COWORK = "cowork"
SOURCE_CODEX = "codex"
SOURCE_OPENCODE = "opencode"
SOURCE_CURSOR = "cursor"
ALL_SOURCES = (SOURCE_CLAUDE_CODE, SOURCE_COWORK, SOURCE_CODEX, SOURCE_OPENCODE, SOURCE_CURSOR)


def _host_homes() -> list[Path]:
    """Candidate home directories that may contain host AI tool data stores.

    Cowork can mount the user's home into its VM without changing the
    sandbox process HOME. Try the explicit override first, then a small set
    of cheap local candidates. This avoids a broad filesystem crawl while
    making mounted-home Cowork runs work without manual path wiring.
    """
    candidates: list[Path] = []

    raw = os.environ.get("AIQRANK_HOST_HOME")
    if raw:
        return _unique_paths([Path(raw).expanduser()])

    candidates.append(Path.home())
    for env_name in ("USERPROFILE",):
        raw_env = os.environ.get(env_name)
        if raw_env:
            candidates.append(Path(raw_env).expanduser())
    homedrive = os.environ.get("HOMEDRIVE")
    homepath = os.environ.get("HOMEPATH")
    if homedrive and homepath:
        candidates.append(Path(homedrive + homepath).expanduser())

    if sys.platform != "win32":
        try:
            import pwd as _pwd

            candidates.append(Path(_pwd.getpwuid(os.getuid()).pw_dir))
        except Exception:
            pass

    if sys.platform == "darwin":
        candidates.extend(_named_user_home_dirs(Path("/Users"), skip={"Shared"}))
    elif sys.platform == "win32":
        candidates.extend(_named_user_home_dirs(Path("C:/Users"), skip={"Public", "Default"}))
        candidates.extend(_named_user_home_dirs(Path("/mnt/c/Users"), skip={"Public", "Default"}))
    else:
        candidates.extend(_named_user_home_dirs(Path("/home")))
        candidates.extend(_named_user_home_dirs(Path("/mnt/c/Users"), skip={"Public", "Default"}))

    return _unique_paths(candidates)


def _host_home() -> Path:
    return _host_homes()[0]


def _unique_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        out.append(path.expanduser())
    return out


def _named_user_home_dirs(root: Path, skip: set[str] | None = None) -> list[Path]:
    skip = skip or set()
    names = _candidate_user_names()
    if not names:
        return []
    out: list[Path] = []
    for name in names:
        if name in skip or name.startswith("."):
            continue
        path = root / name
        if path.is_dir():
            out.append(path)
    return out


def _candidate_user_names() -> set[str]:
    names: set[str] = set()
    for env_name in ("USER", "LOGNAME", "USERNAME"):
        raw = os.environ.get(env_name)
        if raw:
            names.add(Path(raw).name)
    for env_name in ("USERPROFILE",):
        raw = os.environ.get(env_name)
        if raw:
            names.add(Path(raw).expanduser().name)
    try:
        names.add(Path.home().name)
    except OSError:
        pass
    return {name for name in names if name}


def _bucket(daily: dict[date, dict], d: date) -> dict:
    if d not in daily:
        daily[d] = _new_day_metrics()
    return daily[d]


def scan(
    claude_dir: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
    cowork_root: Path | list[Path] | None = None,
    scheduled_root: Path | None = None,
    codex_dir: Path | None = None,
) -> dict:
    """Scan Claude Code + Claude Cowork + Codex transcripts within the last `window_days`.

    Returns the per-source envelope described in the module docstring. The
    `codex` source row is omitted when `~/.codex/` does not exist.

    `cowork_root` overrides the default cowork sandbox location; tests use
    this to point at a synthetic tree. `scheduled_root` overrides the
    Cowork scheduled-task definition directory (default
    ~/Documents/Claude/Scheduled). `codex_dir` overrides the default Codex
    root (~/.codex); pass a non-existent path to disable Codex scanning in
    tests.
    """
    host_homes = _host_homes()
    host_home = host_homes[0]
    homes = [claude_dir] if claude_dir is not None else [h / ".claude" for h in host_homes]
    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)
    cutoff_date = datetime.fromtimestamp(cutoff_ts).date()  # see _window_active_daily

    # `cowork_root` resolution:
    #   - explicit value (tests, custom installs) is always honored; legacy
    #     single-Path overrides are normalized to a single-element list so
    #     downstream code uniformly walks a list of candidate roots
    #   - production (`claude_dir=None`) walks BOTH `claude-code-sessions`
    #     (post-rename) and `local-agent-mode-sessions` (legacy) under the
    #     per-OS Application Support / AppData / .config base
    #   - tests overriding `claude_dir` without `cowork_root` get a sentinel
    #     non-existent path so they never accidentally read real user data
    if cowork_root is None:
        cowork_root = (
            _default_cowork_roots_for_homes(host_homes)
            if claude_dir is None
            else homes[0] / "__cowork_disabled__"
        )

    # Same resolution policy for the scheduled-task definitions root: tests
    # that supply `claude_dir` get a sentinel non-existent path unless they
    # explicitly opt in via `scheduled_root`.
    if scheduled_root is None:
        scheduled_root = (
            _default_scheduled_root_for_homes(host_homes)
            if claude_dir is None
            else homes[0] / "__scheduled_disabled__"
        )

    # Codex root resolution: production walks the real ~/.codex; tests that
    # override `claude_dir` get a sentinel non-existent path unless they
    # explicitly opt in via `codex_dir`.
    if codex_dir is None:
        codex_dir = (
            host_home / ".codex"
            if claude_dir is None
            else homes[0] / "__codex_disabled__"
        )

    # Per-source per-day metric buckets and main-session intervals.
    daily_by_source: dict[str, dict[date, dict]] = {s: {} for s in ALL_SOURCES}
    intervals_by_source: dict[str, dict[date, list[tuple[float, float]]]] = {
        s: {} for s in ALL_SOURCES
    }
    first_messages_sample: list[str] = []
    custom_skills_seen: set[str] = set()
    codex_result: dict | None = None

    # Snapshot the user's locally-owned skills (bare-name slash commands).
    # Used by process_session to count `<command-name>/<n></command-name>`
    # invocations that the Skill-tool path doesn't capture.
    local_skills: set[str] = set()
    for home in homes:
        local_skills |= _local_claude_skills(home)
        local_skills |= _project_local_claude_skills(home)

    seen_claude_transcripts: set[str] = set()
    for home in homes:
        projects = home / "projects"
        if not projects.is_dir():
            continue

        claude_daily = daily_by_source[SOURCE_CLAUDE_CODE]
        claude_intervals = intervals_by_source[SOURCE_CLAUDE_CODE]

        for project_dir in sorted(projects.iterdir()):
            if not project_dir.is_dir():
                continue

            for jsonl_path in iter_transcript_files(project_dir, cutoff_ts, mtime_after_ts):
                try:
                    rel = str(jsonl_path.relative_to(home))
                except ValueError:
                    rel = str(jsonl_path)
                if rel in seen_claude_transcripts:
                    continue
                seen_claude_transcripts.add(rel)

                is_main = "subagents" not in jsonl_path.parts
                process_session(
                    jsonl_path,
                    claude_daily,
                    first_messages_sample,
                    custom_skills_seen,
                    claude_intervals,
                    is_main,
                    is_cowork=False,
                    local_skills=local_skills,
                )

            meta_cutoff = cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)
            for meta_path in project_dir.rglob("agent-*.meta.json"):
                if meta_path.stat().st_mtime < meta_cutoff:
                    continue
                meta_date = datetime.fromtimestamp(meta_path.stat().st_mtime).date()
                bucket = _bucket(claude_daily, meta_date)
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

    # Parity with the OpenCode/Cursor scanners: skills present on disk under
    # ~/.claude/skills/<name>/ (and project-local .claude/skills/) count as
    # authored even when no Write/Edit for them was captured in the retained
    # transcript window. Seed them into the most recent Claude day so the
    # backend's cross-day union picks them up. Only an existing active day is
    # touched — never a fabricated zero-activity day.
    claude_daily = daily_by_source[SOURCE_CLAUDE_CODE]
    if local_skills and claude_daily:
        _seed_authored_into_latest_day(claude_daily, local_skills, now_ts)

    # Cowork root: same Claude Code JSONL format, but nested inside sandboxed
    # session directories at .../local_*/.claude/projects/<projectDir>/*.jsonl.
    # Events land in the cowork source bucket.
    cowork_daily = daily_by_source[SOURCE_COWORK]
    cowork_intervals = intervals_by_source[SOURCE_COWORK]
    for jsonl_path in iter_cowork_transcript_files(
        cowork_root, cutoff_ts, mtime_after_ts
    ):
        process_session(
            jsonl_path,
            cowork_daily,
            first_messages_sample,
            custom_skills_seen,
            cowork_intervals,
            is_main=True,
            is_cowork=True,
            local_skills=local_skills,
        )

    # Cowork scheduled-task runs: walk session manifests at workspace level
    # (local_*.json siblings of the local_*/ sandbox dirs) and bucket each
    # run whose initialMessage marks it as a scheduled-task firing. Always
    # routed to the cowork source.
    distinct_scheduled_task_ids = _count_scheduled_task_runs(
        cowork_root, cowork_daily, cutoff_ts, mtime_after_ts
    )

    # Active scheduled-task definitions: prefer the on-disk count at
    # ~/Documents/Claude/Scheduled/<slug>/SKILL.md, but fall back to the
    # distinct count of schedules that actually fired in the window. macOS
    # TCC blocks Documents/ iteration unless the host process has Full Disk
    # Access — without the manifest fallback, FDA-less users score 0 even
    # with active schedules. Written into the most recent cowork active day
    # so it shows up in the cowork rollup peak.
    active_count = _count_active_scheduled_tasks(scheduled_root)
    if active_count == 0:
        active_count = len(distinct_scheduled_task_ids)
    if active_count > 0:
        if cowork_daily:
            latest_day = max(cowork_daily.keys())
            _bucket(cowork_daily, latest_day)["scheduled_tasks_active"] = active_count
        elif daily_by_source[SOURCE_CLAUDE_CODE]:
            # Fallback: surface the peak in the cowork bucket attached to
            # the latest interactive day so the count isn't dropped. The
            # cowork bucket gains a single-day entry.
            latest_day = max(daily_by_source[SOURCE_CLAUDE_CODE].keys())
            _bucket(cowork_daily, latest_day)["scheduled_tasks_active"] = active_count

    # Codex root: walk ~/.codex/sessions/**/rollout-*.jsonl. Skipped silently
    # when the directory doesn't exist (user hasn't installed Codex).
    codex_emit = codex_dir.exists()
    if codex_emit:
        codex_result = scan_codex(
            codex_dir,
            window_days=window_days,
            now_ts=now_ts,
            mtime_after_ts=mtime_after_ts,
        )
        if codex_result is not None:
            for date_str, intervals in codex_result.get("intervals_by_day", {}).items():
                try:
                    d = date.fromisoformat(date_str)
                except (TypeError, ValueError):
                    continue
                intervals_by_source[SOURCE_CODEX][d] = [
                    (float(interval[0]), float(interval[1]))
                    for interval in intervals
                    if isinstance(interval, (list, tuple)) and len(interval) >= 2
                ]

    # OpenCode root: read ~/.local/share/opencode/opencode.db via scan_opencode.
    # Skipped silently when the DB is absent.
    _opencode_default_db = (
        host_home / ".local" / "share" / "opencode" / "opencode.db"
    )
    opencode_emit = _opencode_default_db.is_file()
    if opencode_emit:
        # Per-source scanner failures must be local — a corrupt OpenCode DB
        # must NOT abort the orchestrator's primary Claude scan (plan §452).
        try:
            from scan_opencode import scan as _scan_opencode_fn  # noqa: E402

            _oc_result = _scan_opencode_fn(
                window_days=window_days,
                now_ts=now_ts,
                mtime_after_ts=mtime_after_ts,
            )
            _oc_daily_by_date = _daily_by_date_with_rollup_only(_oc_result, now_ts)
            daily_by_source[SOURCE_OPENCODE] = _oc_daily_by_date
            _oc_ivs = _oc_result.get("intervals_by_day") or {}
            _oc_intervals: dict[date, list[tuple[float, float]]] = {}
            for date_str, ivs in _oc_ivs.items():
                try:
                    d = date.fromisoformat(date_str)
                except ValueError:
                    continue
                _oc_intervals[d] = (
                    [
                        (float(iv[0]), float(iv[1]))
                        for iv in ivs
                        if isinstance(iv, (list, tuple)) and len(iv) >= 2
                    ]
                    if ivs
                    else []
                )
            intervals_by_source[SOURCE_OPENCODE] = _oc_intervals
        except Exception as e:
            sys.stderr.write(
                f"scan_opencode failed: {type(e).__name__}: {e}\n"
            )

    # Cursor root: read ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
    # via scan_cursor. Skipped silently when the DB is absent.
    _cursor_default_db = (
        host_home
        / "Library"
        / "Application Support"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )
    cursor_emit = _cursor_default_db.is_file()
    if cursor_emit:
        # Per-source scanner failures must be local — a corrupt Cursor DB
        # must NOT abort the orchestrator's primary Claude scan (plan §452).
        try:
            from scan_cursor import scan as _scan_cursor_fn  # noqa: E402

            _cur_result = _scan_cursor_fn(
                window_days=window_days,
                now_ts=now_ts,
                mtime_after_ts=mtime_after_ts,
            )
            _cur_daily_by_date = _daily_by_date_with_rollup_only(_cur_result, now_ts)
            daily_by_source[SOURCE_CURSOR] = _cur_daily_by_date
            _cur_ivs = _cur_result.get("intervals_by_day") or {}
            _cur_intervals: dict[date, list[tuple[float, float]]] = {}
            for date_str, ivs in _cur_ivs.items():
                try:
                    d = date.fromisoformat(date_str)
                except ValueError:
                    continue
                _cur_intervals[d] = (
                    [
                        (float(iv[0]), float(iv[1]))
                        for iv in ivs
                        if isinstance(iv, (list, tuple)) and len(iv) >= 2
                    ]
                    if ivs
                    else []
                )
            intervals_by_source[SOURCE_CURSOR] = _cur_intervals
        except Exception as e:
            sys.stderr.write(
                f"scan_cursor failed: {type(e).__name__}: {e}\n"
            )

    # Per-day concurrency from sweep-line over each day's clipped intervals,
    # computed per source. Only counts a level that held for
    # >= min_sustained_secs within the day — see DEFAULT_MIN_SUSTAINED_SECS.
    min_secs = min_sustained_secs()
    for source in ALL_SOURCES:
        if source == SOURCE_CODEX and codex_result is not None:
            continue
        for d, intervals in intervals_by_source[source].items():
            bucket = _bucket(daily_by_source[source], d)
            bucket["max_concurrent_sessions"] = max_concurrent_sustained(
                intervals, min_secs
            )

    by_source: dict[str, dict] = {}
    for source in ALL_SOURCES:
        # Codex/OpenCode/Cursor sources are suppressed entirely when their data
        # store is absent; the other sources always emit (possibly empty).
        if source == SOURCE_CODEX and not codex_emit:
            continue
        if source == SOURCE_OPENCODE and not opencode_emit:
            continue
        if source == SOURCE_CURSOR and not cursor_emit:
            continue
        if source == SOURCE_CODEX and codex_result is not None:
            by_source[source] = codex_result
            continue

        active_daily = _window_active_daily(
            daily_by_source[source], cutoff_date, _has_activity
        )
        daily_list = [
            {"date": d.isoformat(), "metrics": m}
            for d, m in sorted(active_daily.items())
        ]
        rollup = _rollup_from_daily(active_daily.values())
        intervals_serialized = _serialize_intervals(
            intervals_by_source[source], cutoff_date
        )

        by_source[source] = {
            "daily": daily_list,
            "rollup": rollup,
            "intervals_by_day": intervals_serialized,
        }

    return {
        "window_days": window_days,
        "by_source": by_source,
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
    if any(metrics.get(f) for f in _LIST_FIELDS):
        return True
    return False


def _window_active_daily(
    daily: dict[date, dict], cutoff_date: date, has_activity
) -> dict[date, dict]:
    """Keep only in-window days that recorded activity.

    Files are filtered by mtime, but events are bucketed by their own timestamp
    date, so a recently-touched long or resumed transcript can drag in
    day-buckets from before the window. Dropping `d < cutoff_date` keeps the
    emitted window matching `window_days`; `has_activity` also drops
    zero-activity (meta-only) days. The activity predicate is passed in because
    each scanner counts a different field set.
    """
    return {
        d: m for d, m in daily.items() if d >= cutoff_date and has_activity(m)
    }


def _serialize_intervals(
    intervals_by_day: dict[date, list], cutoff_date: date
) -> dict[str, list]:
    """ISO-key the in-window session intervals for the upload hook.

    The hook unions these across sources into a combined concurrency peak, then
    strips them before posting. Bounding by `cutoff_date` keeps that combined
    source within the window too.
    """
    return {
        d.isoformat(): [[s, e] for (s, e) in ivs]
        for d, ivs in intervals_by_day.items()
        if d >= cutoff_date
    }


def _seed_authored_into_latest_day(
    daily_by_date: dict[date, dict], names: Iterable[str], now_ts: float
) -> None:
    """Union `names` into the most recent day's `authored_skill_names`, adding
    only names not already present on any day. Mutates `daily_by_date` in place.

    Surfaces skills that exist on disk (or in a sub-scanner rollup) but have no
    reliable event day of their own. Callers that must not fabricate a
    zero-activity day should guard on a non-empty `daily_by_date` before calling.
    """
    incoming = {name for name in names if isinstance(name, str) and name}
    if not incoming:
        return
    existing = {
        name
        for m in daily_by_date.values()
        for name in ((m or {}).get("authored_skill_names") or [])
        if isinstance(name, str) and name
    }
    missing = incoming - existing
    if not missing:
        return
    target_day = max(
        daily_by_date.keys(), default=datetime.fromtimestamp(now_ts).date()
    )
    target = _bucket(daily_by_date, target_day)
    current = {
        name
        for name in (target.get("authored_skill_names") or [])
        if isinstance(name, str) and name
    }
    target["authored_skill_names"] = sorted(current | missing)


def _daily_by_date_with_rollup_only(result: dict, now_ts: float) -> dict[date, dict]:
    """Import a sub-scanner daily list and preserve metrics it can only put
    in rollup, such as local authored skills with no reliable event day."""
    daily_by_date: dict[date, dict] = {}
    for entry in result.get("daily") or []:
        try:
            d = date.fromisoformat(entry["date"])
        except (KeyError, ValueError):
            continue
        metrics = entry.get("metrics") or {}
        if isinstance(metrics, dict):
            daily_by_date[d] = metrics.copy()

    rollup = result.get("rollup") or {}
    if not isinstance(rollup, dict):
        return daily_by_date

    target_day = max(daily_by_date.keys(), default=datetime.fromtimestamp(now_ts).date())
    target = _bucket(daily_by_date, target_day)

    existing_skill_count = sum(
        int((m or {}).get("custom_skill_files_written", 0) or 0)
        for m in daily_by_date.values()
    )
    rollup_skill_count = int(rollup.get("custom_skill_files_written", 0) or 0)
    if rollup_skill_count > existing_skill_count:
        target["custom_skill_files_written"] = (
            int(target.get("custom_skill_files_written", 0) or 0)
            + rollup_skill_count
            - existing_skill_count
        )

    _seed_authored_into_latest_day(
        daily_by_date, rollup.get("authored_skill_names") or [], now_ts
    )

    return daily_by_date


def _rollup_from_daily(per_day_metrics) -> dict:
    """Aggregate a sequence of daily metrics maps into a single rollup."""
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


def max_concurrent_sustained(
    intervals: list[tuple[float, float]], min_secs: int = DEFAULT_MIN_SUSTAINED_SECS
) -> int:
    """Highest concurrency level that held for total >= min_secs.

    Sweep-line accumulates time-at-each-level, then walks levels high-to-low
    summing time-at-or-above. The first level whose cumulative time-at-or-
    above reaches `min_secs` is the answer. With min_secs=0 this degenerates
    to the absolute peak.
    """
    if not intervals:
        return 0

    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()

    time_at: dict[int, float] = {}
    current = 0
    prev_t: float | None = None
    for t, delta in events:
        if prev_t is not None and current > 0 and t > prev_t:
            time_at[current] = time_at.get(current, 0.0) + (t - prev_t)
        current += delta
        prev_t = t

    if not time_at:
        return 0

    cumulative = 0.0
    for level in sorted(time_at.keys(), reverse=True):
        cumulative += time_at[level]
        if cumulative >= min_secs:
            return level
    return 0


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


def _log_scan_diagnostic(message: str) -> None:
    """Append one diagnostic line to ~/.config/aiqrank/hook.log. Fail-soft.

    Used to record per-source path-resolution outcomes (`cowork_root_path
    resolved=... exists=... file_count=...`) so customer-side triage can
    tell whether a zero score is a path-resolution issue (root missing)
    or a content issue (root present but no events parsed). Direct file
    append (not RotatingFileHandler) so this remains fast and survives
    cross-process invocation from the hook.
    """
    try:
        log_dir = _host_home() / ".config" / "aiqrank"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "hook.log"
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with log_path.open("a") as fh:
            fh.write(f"{ts} scan {message}\n")
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _default_cowork_roots() -> list[Path]:
    """Return the list of candidate Cowork sandbox roots for the current OS.

    Returns BOTH `claude-code-sessions` (the new name Anthropic is migrating
    to) and `local-agent-mode-sessions` (the legacy name). `claude-code-sessions`
    is first so post-migration files win the file-relative-path dedup in
    `iter_cowork_transcript_files`.

    Per-platform base directory:
      - macOS:   ~/Library/Application Support/Claude/
      - Windows: %APPDATA%\\Roaming\\Claude\\
      - Linux:   ~/.config/Claude/   (best-effort; not in pilot scope)
    """
    return _default_cowork_roots_for_home(_host_home())


def _default_cowork_roots_for_homes(homes: list[Path]) -> list[Path]:
    roots: list[Path] = []
    for home in homes:
        roots.extend(_default_cowork_roots_for_home(home))
    return _unique_paths(roots)


def _default_cowork_roots_for_home(home: Path) -> list[Path]:
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Claude"
    elif sys.platform == "win32":
        base = home / "AppData" / "Roaming" / "Claude"
    else:
        base = home / ".config" / "Claude"
    return [base / "claude-code-sessions", base / "local-agent-mode-sessions"]


def _default_cowork_root() -> Path:
    """Backwards-compatible alias returning the *first* candidate root.

    Kept for any external caller; new code should use `_default_cowork_roots`.
    """
    return _default_cowork_roots()[0]


def _default_scheduled_root() -> Path:
    """Cowork scheduled-task definitions live as one subdirectory per task,
    each containing a SKILL.md. Convention is ~/Documents/Claude/Scheduled.
    """
    return _host_home() / "Documents" / "Claude" / "Scheduled"


def _default_scheduled_root_for_homes(homes: list[Path]) -> Path:
    roots = [home / "Documents" / "Claude" / "Scheduled" for home in homes]
    for root in roots:
        if root.is_dir():
            return root
    return roots[0]


def _count_active_scheduled_tasks(scheduled_root: Path) -> int:
    """Snapshot count of distinct scheduled-task definitions on disk.

    Each task lives at {scheduled_root}/{slug}/SKILL.md. Counts subdirectories
    whose SKILL.md exists. Returns 0 if the root doesn't exist.
    """
    if not scheduled_root.is_dir():
        return 0
    count = 0
    try:
        for entry in scheduled_root.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                count += 1
    except OSError:
        return 0
    return count


def _count_scheduled_task_runs(
    cowork_root: Path | list[Path],
    daily: dict,
    cutoff_ts: float,
    mtime_after_ts: float | None,
) -> set[str]:
    """Walk Cowork session manifests and increment scheduled_task_runs per day.

    A session is a scheduled-task firing when its `initialMessage` starts with
    `<scheduled-task` (the Cowork app prepends this wrapper to the session
    prompt for scheduled runs). Bucketed by the local-calendar day of
    `createdAt` (epoch ms).

    Manifests live at {cowork_root}/{account}/{workspace}/local_*.json (a
    sibling of the local_*/ sandbox directory).

    Accepts either a single root or a list of candidate roots. With multiple
    roots, dedups by manifest-relative path inside the root, mirroring the
    dedup in `iter_cowork_transcript_files` so the dual-directory scan
    survives Anthropic's mid-migration `local-agent-mode-sessions` →
    `claude-code-sessions` copy.

    Returns a set of distinct scheduled-task identifiers seen firing within
    the window — caller uses its size as the `scheduled_tasks_active` peak
    when the on-disk dir-scan is unavailable (macOS blocks `~/Documents/`
    iteration unless the host process has Full Disk Access).
    """
    distinct_tasks: set[str] = set()
    roots = [cowork_root] if isinstance(cowork_root, Path) else list(cowork_root)
    if not any(r.is_dir() for r in roots):
        return distinct_tasks

    effective_cutoff = (
        cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)
    )

    seen_manifest_relpaths: set[str] = set()
    manifest_paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for manifest_path in root.glob("*/*/local_*.json"):
            try:
                rel = str(manifest_path.relative_to(root))
            except ValueError:
                rel = str(manifest_path)
            if rel in seen_manifest_relpaths:
                continue
            seen_manifest_relpaths.add(rel)
            manifest_paths.append(manifest_path)

    for manifest_path in manifest_paths:
        try:
            if manifest_path.stat().st_mtime < effective_cutoff:
                continue
        except OSError:
            continue

        try:
            with manifest_path.open("r") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        initial = data.get("initialMessage")
        if not isinstance(initial, str) or not initial.startswith("<scheduled-task"):
            continue

        created_ms = data.get("createdAt")
        if not isinstance(created_ms, (int, float)) or created_ms <= 0:
            continue
        created_ts = created_ms / 1000.0
        if created_ts < cutoff_ts:
            continue

        d = datetime.fromtimestamp(created_ts).date()
        _bucket(daily, d)["scheduled_task_runs"] += 1

        # Identify the schedule that fired. Prefer scheduledTaskId (stable
        # across renames); fall back to parsing `name="..."` from the
        # <scheduled-task> wrapper. Either uniquely identifies a definition.
        task_id = data.get("scheduledTaskId")
        if isinstance(task_id, str) and task_id:
            distinct_tasks.add(task_id)
        else:
            m = _SCHEDULED_TASK_NAME_RE.search(initial)
            if m:
                distinct_tasks.add(m.group(1))

    return distinct_tasks


def iter_cowork_transcript_files(
    cowork_root: Path | list[Path],
    cutoff_ts: float,
    mtime_after_ts: float | None = None,
) -> Iterable[Path]:
    """Yields cowork transcript JSONL paths within the cutoff.

    Two on-disk shapes are supported:
      VM-bundle (Mac, Linux):
        {cowork_root}/{account}/{workspace}/local_*/.claude/projects/{proj}/*.jsonl
      local-agent (Windows, and Mac when no VM bundle is in use):
        {cowork_root}/{account}/{workspace}/local_*/.audit.jsonl

    The VM-bundle shape exposes Claude Code's transcript files mounted
    inside Cowork's Linux VM. The local-agent shape is Cowork's own audit
    log, written natively when no VM is involved. Schemas are compatible
    enough that `process_session` handles both (see `_audit_timestamp`
    fallback there).

    Accepts either a single root (legacy single-path callers) or a list of
    candidate roots. With multiple roots, dedups by file-relative path
    inside the root — this catches Anthropic's likely copy-then-delete
    migration shape (`local-agent-mode-sessions` → `claude-code-sessions`)
    where the same `<account>/<workspace>/...` tail can appear under both
    roots transiently. Iteration order matters: the first root in the list
    wins the dedup, so callers pass `claude-code-sessions` first.

    Scoped globs guard against wandering into sessions/, shim-lib/, etc.
    """
    roots = [cowork_root] if isinstance(cowork_root, Path) else list(cowork_root)
    effective_cutoff = cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)

    patterns = (
        "*/*/local_*/.claude/projects/*/*.jsonl",
        "*/*/local_*/.audit.jsonl",
    )

    seen_relpaths: set[str] = set()
    for root in roots:
        if not root.is_dir():
            _log_scan_diagnostic(
                f"cowork_root_path resolved={root} exists=False file_count=0"
            )
            continue

        emitted_count = 0
        for pattern in patterns:
            for jsonl_path in root.glob(pattern):
                try:
                    rel = str(jsonl_path.relative_to(root))
                except ValueError:
                    rel = str(jsonl_path)
                if rel in seen_relpaths:
                    continue
                try:
                    if jsonl_path.stat().st_mtime >= effective_cutoff:
                        seen_relpaths.add(rel)
                        emitted_count += 1
                        yield jsonl_path
                except OSError:
                    continue
        _log_scan_diagnostic(
            f"cowork_root_path resolved={root} exists=True file_count={emitted_count}"
        )


def iter_transcript_files(
    project_dir: Path,
    cutoff_ts: float,
    mtime_after_ts: float | None = None,
) -> Iterable[Path]:
    """Yields JSONL transcript file paths within the cutoff.

    When ``mtime_after_ts`` is provided, it acts as an additional lower
    bound on file mtime — files older than this timestamp are skipped.
    """
    effective_cutoff = cutoff_ts if mtime_after_ts is None else max(cutoff_ts, mtime_after_ts)

    for entry in project_dir.iterdir():
        if entry.is_file() and entry.suffix == ".jsonl":
            if entry.stat().st_mtime >= effective_cutoff:
                yield entry

    for subagent_path in project_dir.rglob("subagents/agent-*.jsonl"):
        if subagent_path.stat().st_mtime >= effective_cutoff:
            yield subagent_path


# Detect a `gh pr create` invocation in a Bash command. We strip heredoc
# bodies first, then require `gh pr create` as a command token (start of
# string or after a shell separator), so a PR whose `--body` text merely
# mentions the phrase does not count. The command string is tested and
# discarded — never stored or logged.
_HEREDOC_RE = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?\n.*?\n\s*\1\b", re.DOTALL)
_PR_CREATE_RE = re.compile(r"(?:^|[\n;&|`(])\s*gh\s+pr\s+create\b")


def _command_opens_pr(command: str) -> bool:
    """True if the Bash command opens a PR via `gh pr create`."""
    # Cheap reject: every match needs the literal "create" token. Guard on
    # that (not "gh pr create", which would miss `gh   pr   create`) and cap
    # length so the heredoc regex can't be a ReDoS lever on a huge command.
    if not command or "create" not in command or len(command) > 100_000:
        return False
    stripped = _HEREDOC_RE.sub(" ", command)
    return bool(_PR_CREATE_RE.search(stripped))


def process_session(
    path: Path,
    daily: dict[date, dict],
    first_messages_sample: list[str],
    custom_skills_seen: set[str],
    intervals_by_day: dict[date, list[tuple[float, float]]],
    is_main: bool = True,
    is_cowork: bool = False,
    local_skills: set[str] | None = None,
) -> None:
    """Parse a single JSONL transcript and update per-day buckets.

    `daily` and `intervals_by_day` belong to a single source — caller picks
    the cowork or claude_code dict before invoking. All counts are bucketed
    by each event's own timestamp date, so a session that crosses midnight
    contributes to two days. Session-level booleans (sessions,
    sessions_with_tools, ...) count once per (session, day).

    When `is_cowork` is True, the cowork-specific counters (cowork_messages,
    queue_events, cowork_sessions) are populated alongside the regular
    counters in the (cowork) source's bucket. When `is_cowork` is False,
    those counters remain 0.
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
                ts = _parse_timestamp(
                    event.get("timestamp") or event.get("_audit_timestamp")
                )
                d = _ts_to_date(ts) or fallback_date
                event_type = event.get("type")

                # Cowork emits queue-operation events (enqueue/dequeue) alongside
                # real messages. They're not conversational turns — count them
                # separately and skip the message bookkeeping. The is_cowork
                # guard documents intent: queue_events is a cowork-only metric;
                # if Claude Code ever emits these events from interactive
                # sessions, they should not silently inflate this counter.
                if event_type == "queue-operation":
                    if is_cowork:
                        _bucket(daily, d)["queue_events"] += 1
                    continue

                bucket = _bucket(daily, d)
                bucket["messages"] += 1
                msgs_per_day[d] = msgs_per_day.get(d, 0) + 1
                days_seen.add(d)
                if is_cowork:
                    bucket["cowork_messages"] += 1

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

                        # Capture the model verbatim. Subagent transcripts
                        # carry the subagent's model (a framework default the
                        # user didn't choose), so keep them in a separate
                        # histogram and out of the user-facing mix.
                        model = msg.get("model")
                        if model:
                            if is_main:
                                bucket["model_usage"][model] = (
                                    bucket["model_usage"].get(model, 0) + 1
                                )
                                bucket["model_tokens_out"][model] = (
                                    bucket["model_tokens_out"].get(model, 0) + to
                                )
                            else:
                                bucket["agent_model_usage"][model] = (
                                    bucket["agent_model_usage"].get(model, 0) + 1
                                )

                if event.get("type") == "user":
                    text = content_to_text(msg.get("content"))
                    if text:
                        bucket["user_messages"] += 1
                        if is_main:
                            bucket["main_user_messages"] += 1
                        if _CORRECTION_RE.search(text[:_CORRECTION_SCAN_PREFIX]):
                            bucket["user_corrections"] += 1

                        if not first_user_msg_captured and len(first_messages_sample) < 500:
                            first_messages_sample.append(text[:500])
                            first_user_msg_captured = True

                        # Slash-command invocations of locally-owned skills.
                        # Plugin skills (containing `:`) are skipped — they
                        # already register via the Skill tool path. Bare-name
                        # invocations of skills present on disk count as
                        # practice for `bespoke_practice` / `practice_depth`.
                        if local_skills:
                            for cmd_name in _COMMAND_NAME_RE.findall(text):
                                if ":" in cmd_name:
                                    continue
                                if cmd_name in local_skills:
                                    bucket["skill_counts"][cmd_name] = (
                                        bucket["skill_counts"].get(cmd_name, 0) + 1
                                    )

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
                        if is_main:
                            bucket["main_tool_calls"] += 1

                        name = tool_use.get("name", "")
                        if not name:
                            continue

                        name = _normalize_tool_name(name)

                        bucket["tool_name_counts"][name] = (
                            bucket["tool_name_counts"].get(name, 0) + 1
                        )

                        # PRs opened via AI: detect `gh pr create` in the Bash
                        # command. The command string is tested and discarded —
                        # never stored or logged (it can contain secrets).
                        if name == "Bash" and _command_opens_pr(
                            (tool_use.get("input") or {}).get("command", "")
                        ):
                            bucket["prs_opened"] += 1

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

                            # Track authorship: any Write/Edit under
                            # ~/.claude/skills/<name>/** counts as authoring
                            # that skill (excluding aiqrank itself).
                            if (
                                "/.claude/skills/" in target_path
                                and AIQRANK_SKILL_DIR_FRAGMENT not in target_path
                            ):
                                skill_name = _claude_skill_name_from_path(target_path)
                                if skill_name and skill_name not in bucket["authored_skill_names"]:
                                    bucket["authored_skill_names"].append(skill_name)

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
        if is_cowork:
            bucket["cowork_sessions"] += 1
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


def _claude_skill_name_from_path(file_path: str) -> str | None:
    """Extract `<name>` from a `~/.claude/skills/<name>/...` path."""
    marker = "/.claude/skills/"
    idx = file_path.find(marker)
    if idx < 0:
        return None
    rest = file_path[idx + len(marker):]
    if not rest or "/" not in rest:
        # Allow paths like /.claude/skills/<name> with no trailing /
        return rest or None
    name = rest.split("/", 1)[0]
    return name or None


def _local_claude_skills(home: Path) -> set[str]:
    """Snapshot of locally-owned Claude skills (bare names, no plugin namespace).

    A "local skill" is a subdirectory of `~/.claude/skills/` containing a
    SKILL.md file. These are skills the user authored or installed manually
    on this machine — they're invoked via `/<name>` slash commands and do
    NOT appear in `skill_counts` via the Skill tool path (that path captures
    plugin-namespaced skills like `compound-engineering:ce-plan`).

    Returns a set of bare names, or an empty set if `~/.claude/skills/` is
    missing or unreadable. Excludes the aiqrank self-skill.
    """
    skills_dir = home / "skills"
    return _enumerate_skill_dir(skills_dir)


def _project_local_claude_skills(home: Path) -> set[str]:
    """Snapshot of project-local Claude skills authored under any cwd the user
    has worked from.

    For each project dir under `<home>/projects/`, extracts the real cwd from
    the first transcript event that has one, then enumerates
    `<cwd>/.claude/skills/<name>/SKILL.md`. Returns the union across all
    visited projects.

    Without this, slash-command invocations of project-local skills like
    `/prune-branches` are dropped at scan_transcripts.py's `local_skills`
    membership filter because `_local_claude_skills` only sees
    `~/.claude/skills/`.
    """
    projects = home / "projects"
    if not projects.is_dir():
        return set()
    names: set[str] = set()
    seen_cwds: set[Path] = set()
    try:
        project_dirs = list(projects.iterdir())
    except OSError:
        return set()
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        cwd = _extract_cwd_from_project(project_dir)
        if cwd is None or cwd in seen_cwds:
            continue
        seen_cwds.add(cwd)
        names |= _enumerate_skill_dir(cwd / ".claude" / "skills")
    return names


def _enumerate_skill_dir(skills_dir: Path) -> set[str]:
    """Return bare names of skill subdirectories containing SKILL.md,
    excluding the aiqrank self-skill. Shared helper for global and
    project-local enumeration."""
    if not skills_dir.is_dir():
        return set()
    try:
        entries = list(skills_dir.iterdir())
    except OSError:
        return set()
    names: set[str] = set()
    for entry in entries:
        if entry.name == "aiqrank":
            continue
        try:
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.add(entry.name)
        except OSError:
            continue
    return names


def _extract_cwd_from_project(project_dir: Path) -> Path | None:
    """Return the first valid `cwd` Path found in any transcript under
    `project_dir`, or None.

    Scans up to the first 10 events of each jsonl. Early events are usually
    system bookkeeping (e.g. queue-operation) without `cwd`; the first real
    session event carries it.
    """
    try:
        jsonls = sorted(project_dir.glob("*.jsonl"))
    except OSError:
        return None
    for jsonl in jsonls:
        try:
            with jsonl.open("r", encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i >= 10:
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = event.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return Path(cwd)
        except (OSError, UnicodeDecodeError):
            continue
    return None


# Slash-command marker emitted by Claude Code in the user message body when
# a `/<name>` invocation fires. Captures both bare names (`/pr-recap`) and
# plugin-namespaced names (`/compound-engineering:ce-review`). Only the
# bare-name matches are routed into `skill_counts` here, since plugin
# invocations already register through the Skill tool path.
_COMMAND_NAME_RE = re.compile(r"<command-name>/([\w:-]+)</command-name>")

# Extracts the schedule slug from a Cowork manifest's initialMessage prefix:
#   <scheduled-task name="daily-meeting-briefing" file="...">
_SCHEDULED_TASK_NAME_RE = re.compile(r'<scheduled-task\s+name="([^"]+)"')


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


# --- Codex scanner ----------------------------------------------------------


def scan_codex(
    codex_dir: Path,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
) -> dict | None:
    """Canonical Codex scanner used by every integrated and standalone path.

    Completeness is fail-closed because server rows use replacement upserts.
    A malformed record with a recoverable timestamp omits only that date. A
    stat/read/change failure (or malformed record without a date) marks the
    source failed and clears `daily`, preventing a partial replacement upload.
    """
    if not codex_dir.exists():
        return None

    sessions_root = codex_dir / "sessions"
    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)
    cutoff_date = datetime.fromtimestamp(cutoff_ts).date()
    # Codex rows are replacement upserts, so an mtime cursor would turn this
    # into an unsafe partial snapshot. Retain the argument for caller/CLI
    # compatibility but always scan the complete rolling window.
    effective_cutoff = cutoff_ts

    daily: dict[date, dict] = {}
    intervals_by_day: dict[date, list[tuple[float, float]]] = {}
    command_verbs_by_day: dict[date, set[str]] = {}
    unknown_event_types: dict[str, int] = {}
    launches_by_day: dict[date, set[str]] = {}
    activity_by_identity: dict[
        str, tuple[date, tuple[int, int, str, str, str]]
    ] = {}
    seen_launches: set[str] = set()
    seen_mcp_completions: set[str] = set()
    incomplete_dates: set[date] = set()
    unlocalizable_failures = 0
    failure_count = 0

    if sessions_root.is_dir():
        try:
            rollout_paths = list(sessions_root.rglob("rollout-*.jsonl"))
        except OSError:
            rollout_paths = []
            unlocalizable_failures += 1
            failure_count += 1

        for rollout_path in sorted(rollout_paths):
            try:
                if rollout_path.stat().st_mtime < effective_cutoff:
                    continue
            except OSError:
                unlocalizable_failures += 1
                failure_count += 1
                continue
            outcome = process_codex_session(
                rollout_path,
                daily,
                intervals_by_day,
                command_verbs_by_day,
                unknown_event_types,
                seen_mcp_completions,
            )
            incomplete_dates.update(outcome["incomplete_dates"])
            unlocalizable_failures += outcome["unlocalizable_failures"]
            failure_count += outcome["failure_count"]
            for d, identities in outcome["launches_by_day"].items():
                unique_launches = identities - seen_launches
                seen_launches.update(unique_launches)
                launches_by_day.setdefault(d, set()).update(unique_launches)
            for d, activity in outcome["activity_by_day"].items():
                for item in activity:
                    identity = item[2]
                    current = activity_by_identity.get(identity)
                    if current is None or abs(item[0] - item[1]) < abs(
                        current[1][0] - current[1][1]
                    ):
                        activity_by_identity[identity] = (d, item)

    for d, launch_ids in launches_by_day.items():
        if launch_ids:
            _bucket(daily, d)["parallel_agent_turns"] += len(launch_ids)

    activity_by_day: dict[date, list[tuple[int, int, str, str, str]]] = {}
    for d, item in activity_by_identity.values():
        activity_by_day.setdefault(d, []).append(item)

    for d, activity in activity_by_day.items():
        started_by_batch: dict[int, set[str]] = {}
        for batch_timestamp, _occurred, _identity, agent_thread_id, kind in activity:
            if kind == "started":
                started_by_batch.setdefault(batch_timestamp, set()).add(agent_thread_id)
        peak = max((len(agent_ids) for agent_ids in started_by_batch.values()), default=0)
        if peak:
            bucket = _bucket(daily, d)
            bucket["max_parallel_agents"] = max(bucket["max_parallel_agents"], peak)

    for d, verbs in command_verbs_by_day.items():
        _bucket(daily, d)["command_diversity"] = len(verbs)

    for d in incomplete_dates:
        daily.pop(d, None)
        intervals_by_day.pop(d, None)

    if unlocalizable_failures:
        daily.clear()
        intervals_by_day.clear()

    codex_skills = {
        _normalize_codex_label(name)
        for name in _enumerate_skill_dir(codex_dir / "skills")
    }
    if codex_skills and daily:
        _seed_authored_into_latest_day(daily, codex_skills, now_ts)

    for metrics in daily.values():
        _finalize_codex_metric_dicts(metrics)

    min_secs = min_sustained_secs()
    for d, intervals in intervals_by_day.items():
        bucket = _bucket(daily, d)
        bucket["max_concurrent_sessions"] = max_concurrent_sustained(
            intervals, min_secs
        )

    daily = _window_active_daily(daily, cutoff_date, _has_activity)
    daily_list = [
        {"date": d.isoformat(), "metrics": m} for d, m in sorted(daily.items())
    ]
    rollup = _rollup_from_daily(daily.values())
    if unlocalizable_failures:
        completeness_status = "failed"
    elif incomplete_dates:
        completeness_status = "partial"
    else:
        completeness_status = "complete"

    return {
        "daily": daily_list,
        "rollup": rollup,
        "intervals_by_day": _serialize_intervals(intervals_by_day, cutoff_date),
        "_unknown_event_types": _cap_codex_dictionary(unknown_event_types),
        "completeness": {
            "status": completeness_status,
            "omitted_dates": sorted(d.isoformat() for d in incomplete_dates),
            "failure_count": failure_count,
        },
    }


def process_codex_session(
    path: Path,
    daily: dict[date, dict],
    intervals_by_day: dict[date, list[tuple[float, float]]],
    command_verbs_by_day: dict[date, set[str]] | None = None,
    unknown_event_types: dict[str, int] | None = None,
    seen_mcp_completions: set[str] | None = None,
) -> dict:
    """Parse one Codex rollout and return aggregate-only completeness facts."""
    command_verbs_by_day = command_verbs_by_day if command_verbs_by_day is not None else {}
    unknown_event_types = unknown_event_types if unknown_event_types is not None else {}
    seen_mcp_completions = (
        seen_mcp_completions if seen_mcp_completions is not None else set()
    )
    days_seen: set[date] = set()
    days_with_tools: set[date] = set()
    days_with_orchestration: set[date] = set()
    days_with_plan_mode: set[date] = set()
    msgs_per_day: dict[date, int] = {}
    earliest_per_day: dict[date, float] = {}
    latest_per_day: dict[date, float] = {}
    launches_by_day: dict[date, set[str]] = {}
    activity_by_day: dict[date, list[tuple[int, int, str, str, str]]] = {}
    incomplete_dates: set[date] = set()
    unlocalizable_failures = 0
    failure_count = 0

    try:
        before_stat = path.stat()
        fallback_date = datetime.fromtimestamp(before_stat.st_mtime).date()
    except OSError:
        return {
            "incomplete_dates": set(),
            "unlocalizable_failures": 1,
            "failure_count": 1,
            "launches_by_day": {},
            "activity_by_day": {},
        }

    read_errors = 0

    def parsed_events(*, record_parse_failures: bool):
        nonlocal failure_count, read_errors, unlocalizable_failures
        try:
            fh = path.open("r")
        except OSError:
            read_errors += 1
            return

        try:
            with fh:
                for ordinal, raw_line in enumerate(fh):
                    line = raw_line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        if record_parse_failures:
                            failure_count += 1
                            hinted_date = _codex_malformed_line_date(line)
                            if hinted_date is None:
                                unlocalizable_failures += 1
                            else:
                                incomplete_dates.add(hinted_date)
                        continue
                    if not isinstance(event, dict):
                        if record_parse_failures:
                            failure_count += 1
                            unlocalizable_failures += 1
                        continue

                    ts = _parse_timestamp(event.get("timestamp"))
                    d = _ts_to_date(ts) or fallback_date
                    yield ordinal, event, d, ts
        except OSError:
            read_errors += 1

    output_success: dict[str, bool] = {}
    mcp_completion_ids: set[str] = set()
    for ordinal, event, _d, _ts in parsed_events(record_parse_failures=True):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        p_type = payload.get("type")
        call_id = payload.get("call_id")
        if p_type in ("function_call_output", "custom_tool_call_output") and isinstance(call_id, str):
            output_success[call_id] = output_success.get(call_id, False) or _codex_output_succeeded(payload)
        elif event.get("type") == "event_msg" and p_type == "exec_command_end" and isinstance(call_id, str):
            output_success[call_id] = int(payload.get("exit_code") or 0) == 0
        elif event.get("type") == "event_msg" and p_type == "mcp_tool_call_end":
            mcp_completion_ids.add(
                call_id if isinstance(call_id, str) and call_id else f"mcp-event-{ordinal}"
            )

    if read_errors:
        return {
            "incomplete_dates": incomplete_dates,
            "unlocalizable_failures": unlocalizable_failures + 1,
            "failure_count": failure_count + 1,
            "launches_by_day": {},
            "activity_by_day": {},
        }

    seen_direct_calls: set[str] = set()
    seen_activity_events: set[str] = set()
    skill_candidates: list[tuple[date, dict, str]] = []

    for ordinal, event, d, ts in parsed_events(record_parse_failures=False):
        if ts is not None:
            if d not in earliest_per_day or ts < earliest_per_day[d]:
                earliest_per_day[d] = ts
            if d not in latest_per_day or ts > latest_per_day[d]:
                latest_per_day[d] = ts

        days_seen.add(d)
        bucket = _bucket(daily, d)
        ev_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        p_type = payload.get("type")

        if ev_type == "session_meta" or ev_type == "compacted":
            continue

        if ev_type == "turn_context":
            _increment_codex_dict(bucket, "model_usage", payload.get("model"))
            _increment_codex_dict(bucket, "effort_usage", payload.get("effort"))
            continue

        if ev_type == "event_msg":
            if p_type == "user_message":
                text = payload.get("message")
                if isinstance(text, str) and text:
                    bucket["user_messages"] += 1
                    bucket["main_user_messages"] += 1
                    bucket["messages"] += 1
                    msgs_per_day[d] = msgs_per_day.get(d, 0) + 1
                    if _CORRECTION_RE.search(text[:_CORRECTION_SCAN_PREFIX]):
                        bucket["user_corrections"] += 1
                continue

            if p_type == "agent_message":
                bucket["messages"] += 1
                msgs_per_day[d] = msgs_per_day.get(d, 0) + 1
                continue

            if p_type == "agent_reasoning":
                bucket["reasoning_blocks"] += 1
                continue

            if p_type == "token_count":
                info = payload.get("info")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if isinstance(usage, dict):
                    ti = _codex_int(usage.get("input_tokens"))
                    to = _codex_int(usage.get("output_tokens"))
                    tr = _codex_int(usage.get("cached_input_tokens"))
                    bucket["tokens_input"] += ti
                    bucket["tokens_output"] += to
                    bucket["tokens_cache_read"] += tr
                    bucket["tokens_total"] += _codex_int(
                        usage.get("total_tokens"), default=ti + to + tr
                    )
                continue

            if p_type == "sub_agent_activity":
                event_id = payload.get("event_id")
                identity = (
                    event_id
                    if isinstance(event_id, str) and event_id
                    else f"activity-event-{ordinal}"
                )
                if identity in seen_activity_events:
                    continue
                seen_activity_events.add(identity)
                agent_thread_id = payload.get("agent_thread_id")
                kind = payload.get("kind")
                if isinstance(agent_thread_id, str) and isinstance(kind, str):
                    occurred = _codex_int(payload.get("occurred_at_ms"), default=ordinal)
                    activity_date = _ts_to_date(occurred / 1000) or d
                    batch_timestamp = int(ts * 1000) if ts is not None else occurred
                    activity_by_day.setdefault(activity_date, []).append(
                        (
                            batch_timestamp,
                            occurred,
                            identity,
                            agent_thread_id,
                            kind.lower(),
                        )
                    )
                    if kind.lower() == "started":
                        launches_by_day.setdefault(activity_date, set()).add(identity)
                continue

            if p_type == "mcp_tool_call_end":
                identity = payload.get("call_id")
                if not isinstance(identity, str) or not identity:
                    identity = f"mcp-event-{ordinal}"
                if identity in seen_mcp_completions:
                    continue
                seen_mcp_completions.add(identity)
                invocation = payload.get("invocation")
                if isinstance(invocation, dict):
                    server = invocation.get("server")
                    tool = invocation.get("tool")
                    if isinstance(server, str) and server:
                        tool_name = f"mcp__{server}__{tool or 'call'}"
                        _apply_codex_tool_effects(
                            bucket,
                            tool_name,
                            payload,
                            d,
                            identity,
                            days_with_orchestration,
                            days_with_plan_mode,
                            launches_by_day,
                            command_verbs_by_day,
                        )
                        days_with_tools.add(d)
                continue

            if p_type in {
                "task_started", "task_complete", "turn_aborted", "context_compacted",
                "exec_command_begin", "exec_command_end", "exec_command_output_delta",
                "patch_apply_begin", "patch_apply_end", "agent_message_delta",
                "agent_reasoning_delta", "agent_reasoning_raw_content",
                "agent_reasoning_raw_content_delta", "agent_reasoning_section_break",
                "mcp_tool_call_begin",
            }:
                continue

            _record_codex_unknown(unknown_event_types, ev_type, p_type)
            continue

        if ev_type == "response_item":
            if p_type == "reasoning":
                bucket["reasoning_blocks"] += 1
                continue

            if p_type in CODEX_TOOL_TYPES or p_type == "web_search_call":
                call_id = payload.get("call_id")
                identity = (
                    call_id
                    if isinstance(call_id, str) and call_id
                    else f"response-item-{ordinal}"
                )
                if identity in seen_direct_calls:
                    continue
                seen_direct_calls.add(identity)
                name = "web_search" if p_type == "web_search_call" else payload.get("name")
                if isinstance(name, str) and name.startswith("mcp__") and identity in mcp_completion_ids:
                    continue
                if not isinstance(name, str) or not name:
                    continue

                _apply_codex_tool_effects(
                    bucket,
                    name,
                    payload,
                    d,
                    identity,
                    days_with_orchestration,
                    days_with_plan_mode,
                    launches_by_day,
                    command_verbs_by_day,
                )
                days_with_tools.add(d)

                if isinstance(call_id, str) and call_id:
                    skill_candidates.append((d, payload, call_id))

                code = payload.get("input")
                if isinstance(code, str):
                    for nested_index, nested_name in enumerate(_extract_nested_codex_tools(code)):
                        if nested_name.startswith("mcp__"):
                            continue
                        nested_identity = f"{identity}:nested:{nested_index}"
                        _apply_codex_tool_effects(
                            bucket,
                            nested_name,
                            {"input": code},
                            d,
                            nested_identity,
                            days_with_orchestration,
                            days_with_plan_mode,
                            launches_by_day,
                            command_verbs_by_day,
                            nested=True,
                        )
                continue

            if p_type in {
                "function_call_output", "custom_tool_call_output", "message",
                "tool_search_output",
            }:
                continue

            _record_codex_unknown(unknown_event_types, ev_type, p_type)
            continue

        _record_codex_unknown(unknown_event_types, ev_type, p_type)

    if read_errors:
        failure_count += read_errors
        unlocalizable_failures += read_errors

    try:
        after_stat = path.stat()
        if (
            before_stat.st_mtime_ns != after_stat.st_mtime_ns
            or before_stat.st_size != after_stat.st_size
        ):
            failure_count += 1
            unlocalizable_failures += 1
    except OSError:
        failure_count += 1
        unlocalizable_failures += 1

    skill_names_seen: set[str] = set()
    for d, payload, call_id in skill_candidates:
        if not output_success.get(call_id, False):
            continue
        for skill_path in _codex_successful_skill_reads(payload):
            skill_name = _codex_skill_name(skill_path)
            if skill_name and skill_name not in skill_names_seen:
                skill_names_seen.add(skill_name)
                _increment_codex_dict(_bucket(daily, d), "skill_counts", skill_name)

    for d in days_seen:
        bucket = _bucket(daily, d)
        bucket["sessions"] += 1
        bucket["main_sessions"] += 1
    for d in days_with_tools:
        _bucket(daily, d)["sessions_with_tools"] += 1
    for d in days_with_orchestration:
        _bucket(daily, d)["sessions_with_orchestration"] += 1
    for d in days_with_plan_mode:
        _bucket(daily, d)["sessions_with_plan_mode"] += 1

    for d, count in msgs_per_day.items():
        bucket = _bucket(daily, d)
        bucket["max_messages_in_session"] = max(
            bucket["max_messages_in_session"], count
        )

    for d, start in earliest_per_day.items():
        end = latest_per_day.get(d, start)
        if end == start:
            end = start + 1.0
        intervals_by_day.setdefault(d, []).append((start, end))

    return {
        "incomplete_dates": incomplete_dates,
        "unlocalizable_failures": unlocalizable_failures,
        "failure_count": failure_count,
        "launches_by_day": launches_by_day,
        "activity_by_day": activity_by_day,
    }


def _apply_codex_tool_effects(
    bucket: dict,
    name: str,
    payload: dict,
    d: date,
    identity: str,
    days_with_orchestration: set[date],
    days_with_plan_mode: set[date],
    launches_by_day: dict[date, set[str]],
    command_verbs_by_day: dict[date, set[str]],
    *,
    nested: bool = False,
) -> None:
    """Apply one direct, nested, or MCP-completion tool signal."""
    name = _normalize_tool_name(_normalize_codex_label(name))
    if not name:
        return
    bucket["tool_calls"] += 1
    bucket["main_tool_calls"] += 1
    _increment_codex_dict(bucket, "tool_name_counts", name)

    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) >= 3 else ""
        if server:
            _increment_codex_dict(bucket, "mcp_server_counts", server)

    if name == "spawn_agent":
        days_with_orchestration.add(d)
        launches_by_day.setdefault(d, set()).add(identity)

        agent_type = _codex_arg(payload, "agent_type")
        if isinstance(agent_type, str) and agent_type:
            _increment_codex_dict(bucket, "agent_type_counts", agent_type)

    elif name == "wait_agent":
        targets = _codex_arg(payload, "targets")
        if isinstance(targets, list):
            n = len(targets)
            if n > bucket["max_parallel_agents"]:
                bucket["max_parallel_agents"] = n

    elif name == "update_plan":
        days_with_plan_mode.add(d)
        bucket["plan_mode_invocations"] += 1

    if name in {"shell", "exec_command"}:
        verb = _shell_verb(payload.get("arguments"))
        if verb:
            command_verbs_by_day.setdefault(d, set()).add(verb)

    target_paths: list[str] = []
    if name == "apply_patch":
        bucket["file_changes"] += 1
        target_paths = _codex_apply_patch_files(payload)
        if nested and not target_paths:
            code = payload.get("input")
            if isinstance(code, str):
                target_paths = re.findall(
                    r"(?:^|\\n|\n)\*\*\* (?:Update|Add|Delete) File: ([^\\\n]+)",
                    code,
                )
    elif name in ("Write", "Edit"):
        fp = _codex_arg(payload, "file_path") or _codex_arg(payload, "path")
        if isinstance(fp, str) and fp:
            target_paths = [fp]

    for target_path in target_paths:
        # Codex skill authorship.
        if CODEX_SKILLS_DIR_FRAGMENT in target_path:
            m = CODEX_SKILL_NAME_RE.search(target_path)
            if m:
                skill_name = m.group(1)
                if skill_name and skill_name not in bucket["authored_skill_names"]:
                    bucket["authored_skill_names"].append(skill_name)
                if target_path.endswith("/SKILL.md"):
                    bucket["custom_skill_files_written"] += 1

        # Codex AGENTS.md (Claude's CLAUDE.md equivalent). Tracked under
        # the same `claude_md_writes` field — server-side scoring is
        # field-name-agnostic per the dual-source plan.
        if (
            target_path == "AGENTS.md"
            or target_path == "CLAUDE.md"
            or target_path.endswith("/AGENTS.md")
            or target_path.endswith("/CLAUDE.md")
        ):
            bucket["claude_md_writes"] += 1
            if target_path.endswith("/AGENTS.md") or target_path == "AGENTS.md":
                bucket["agents_md_writes"] += 1

        # MCP config: `~/.codex/config.toml` plus any `*.mcp.json` style.
        if any(target_path.endswith(suffix) for suffix in CODEX_CONFIG_TOML_BASENAMES):
            bucket["custom_mcp_config_writes"] += 1
        elif target_path.endswith("/.mcp.json") or target_path.endswith("/mcp.json"):
            bucket["custom_mcp_config_writes"] += 1


def _codex_malformed_line_date(line: str) -> date | None:
    match = _CODEX_MALFORMED_TIMESTAMP_RE.search(line)
    if not match:
        return None
    return _ts_to_date(_parse_timestamp(match.group(1)))


def _record_codex_unknown(store: dict[str, int], top_type, payload_type) -> None:
    key = _normalize_codex_label(
        f"{top_type or '<no-type>'}:{payload_type or '<no-payload-type>'}"
    )
    store[key] = store.get(key, 0) + 1


def _codex_int(value, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _normalize_codex_label(value) -> str:
    if not isinstance(value, str):
        return ""
    label = _CODEX_LABEL_RE.sub("_", value.strip()).strip("_")
    return label[:CODEX_MAX_LABEL_LENGTH]


def _increment_codex_dict(bucket: dict, field: str, raw_label, amount: int = 1) -> None:
    label = _normalize_codex_label(raw_label)
    if not label:
        return
    target = bucket[field]
    target[label] = target.get(label, 0) + amount


def _cap_codex_dictionary(values: dict) -> dict:
    clean: dict[str, int] = {}
    for key, value in values.items():
        label = _normalize_codex_label(key)
        count = _codex_int(value)
        if label and count > 0:
            clean[label] = clean.get(label, 0) + count
    if len(clean) <= CODEX_MAX_DICT_KEYS:
        return clean
    ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
    kept = dict(ordered[: CODEX_MAX_DICT_KEYS - 1])
    kept[CODEX_OTHER_LABEL] = sum(value for _key, value in ordered[CODEX_MAX_DICT_KEYS - 1 :])
    return kept


def _finalize_codex_metric_dicts(metrics: dict) -> None:
    for field in ("tool_name_counts", "skill_counts", "mcp_server_counts", "agent_type_counts"):
        metrics[field] = _cap_codex_dictionary(metrics.get(field) or {})


def _codex_output_succeeded(payload: dict) -> bool:
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error", "cancelled"}:
        return False
    output = payload.get("output")
    try:
        text = output if isinstance(output, str) else json.dumps(output)
    except (TypeError, ValueError):
        text = ""
    lowered = text[:500].lower()
    return not any(
        marker in lowered
        for marker in (
            "script failed",
            "error: command failed",
            "no such file or directory",
            '"exit_code": 1',
        )
    )


def _codex_successful_skill_reads(payload: dict) -> list[Path]:
    name = payload.get("name")
    if not isinstance(name, str):
        return []
    text = _codex_call_text(payload)
    if not text:
        return []
    return [
        path
        for path in _codex_skill_paths(text)
        if path.is_file() and _codex_path_was_read(name, text, path)
    ]


def _codex_call_text(payload: dict) -> str:
    raw = payload.get("arguments")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            command = parsed.get("cmd") or parsed.get("command")
            if isinstance(command, list):
                return " ".join(str(part) for part in command)
            if isinstance(command, str):
                return command
            for key in ("path", "file_path"):
                if isinstance(parsed.get(key), str):
                    return parsed[key]
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        return raw_input
    return ""


def _codex_path_was_read(name: str, text: str, path: Path) -> bool:
    if name.lower() in {"read", "read_file"}:
        return True
    if name not in {"shell", "exec_command", "exec"}:
        return False
    path_forms = {str(path), str(path).replace(str(Path.home()), "~", 1)}
    for path_form in path_forms:
        for match in re.finditer(re.escape(path_form), text):
            # Modern Codex wraps nested exec_command calls in JavaScript, so
            # delimiters such as `;` surround the outer call rather than the
            # shell command. Inspect a bounded window around the concrete path
            # instead of assuming a plain shell command line.
            segment = text[max(0, match.start() - 300) : match.end() + 80]
            lowered = segment.lower()
            if re.search(
                r"(?:>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bchmod\b|\bsed\s+-i\b)",
                lowered,
            ):
                continue
            if re.search(r"\b(?:cat|sed|head|tail|bat|less)\b", lowered):
                return True
    return False


def _codex_skill_paths(text: str) -> list[Path]:
    candidates: set[str] = set()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        cleaned = token.strip("'\"(){}[],;:")
        if cleaned.endswith("/SKILL.md") and (cleaned.startswith("/") or cleaned.startswith("~/")):
            candidates.add(cleaned)

    # JavaScript orchestration embeds the shell command inside an object
    # literal, so shlex tokens include JSON/JS punctuation around the path.
    # Extract absolute or home-relative paths directly without retaining any
    # surrounding command text in scanner output.
    candidates.update(
        match.group(1).rstrip()
        for match in re.finditer(
            r"((?:~|/)[^\"'`\\\n\r{}()\[\],;]*?/SKILL\.md)",
            text,
        )
    )
    return [Path(candidate).expanduser() for candidate in sorted(candidates)]


def _codex_skill_name(path: Path) -> str | None:
    try:
        with path.open("r") as fh:
            for _ in range(30):
                line = fh.readline()
                if not line:
                    break
                match = re.match(r"\s*name:\s*['\"]?([^'\"\n]+)", line)
                if match:
                    name = _normalize_codex_label(match.group(1))
                    return name or None
    except OSError:
        return None
    name = _normalize_codex_label(path.parent.name)
    return name or None


def _extract_nested_codex_tools(code: str) -> list[str]:
    """Lexically find `tools.<name>(...)` outside JS strings/comments."""
    names: list[str] = []
    i = 0
    state = "normal"
    quote = ""
    while i < len(code):
        char = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ""
        if state == "line_comment":
            if char == "\n":
                state = "normal"
            i += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                state = "normal"
                i += 2
            else:
                i += 1
            continue
        if state == "string":
            if char == "\\":
                i += 2
            elif char == quote:
                state = "normal"
                i += 1
            else:
                i += 1
            continue
        if char == "/" and nxt == "/":
            state = "line_comment"
            i += 2
            continue
        if char == "/" and nxt == "*":
            state = "block_comment"
            i += 2
            continue
        if char in {"'", '"', "`"}:
            state = "string"
            quote = char
            i += 1
            continue
        if code.startswith("tools.", i):
            start = i + len("tools.")
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", code[start:])
            if match:
                end = start + len(match.group(0))
                cursor = end
                while cursor < len(code) and code[cursor].isspace():
                    cursor += 1
                if cursor < len(code) and code[cursor] == "(":
                    names.append(match.group(0))
                    i = cursor + 1
                    continue
        i += 1
    return names


def _shell_verb(arguments) -> str | None:
    if not isinstance(arguments, str) or not arguments:
        return None
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    command = parsed.get("cmd")
    if isinstance(command, str) and command.strip():
        return _first_meaningful_word(command)
    command = parsed.get("command")
    if not isinstance(command, list) or not command:
        return None
    first = command[0] if isinstance(command[0], str) else None
    if not first:
        return None
    base = os.path.basename(first)
    if base in {"bash", "sh", "zsh", "env"}:
        for candidate in reversed(command):
            if isinstance(candidate, str) and candidate and not candidate.startswith("-"):
                return _first_meaningful_word(candidate)
    return base


def _first_meaningful_word(command: str) -> str | None:
    for token in command.split():
        if "=" in token and token.split("=", 1)[0].isupper():
            continue
        return os.path.basename(token)
    return None


def _patch_touches_agents_md(patch_input: str) -> bool:
    return any(
        Path(path).name.lower() == "agents.md"
        for path in _codex_apply_patch_files({"input": patch_input})
    )


def _codex_arg(payload: dict, key: str):
    """Return one argument value from a Codex tool-call payload.

    Codex puts arguments either as a JSON-encoded string on `arguments`
    (for `function_call`) or under `input` / argument-named keys on
    other variants. We try both.
    """
    raw = payload.get("arguments")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and key in parsed:
            return parsed[key]

    if isinstance(payload.get(key), (str, int, float, list, dict)):
        return payload[key]

    inp = payload.get("input")
    if isinstance(inp, dict) and key in inp:
        return inp[key]

    return None


def _codex_apply_patch_files(payload: dict) -> list[str]:
    """Extract file paths from an `apply_patch` payload's patch text.

    Codex emits the patch as `payload.input` (a string starting with
    `*** Begin Patch`); each affected file is announced with
    `*** Update File:` / `*** Add File:` / `*** Delete File:`.
    """
    inp = payload.get("input")
    if not isinstance(inp, str) or not inp:
        return []
    return CODEX_PATCH_FILE_RE.findall(inp)


def main(argv: list[str]) -> int:
    window_days = DEFAULT_WINDOW_DAYS
    if "--days" in argv:
        idx = argv.index("--days")
        try:
            window_days = int(argv[idx + 1])
        except (IndexError, ValueError):
            pass

    host_home_override: str | None = None
    if "--host-home" in argv:
        idx = argv.index("--host-home")
        try:
            host_home_override = argv[idx + 1]
        except IndexError:
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

    prior_host_home = os.environ.get("AIQRANK_HOST_HOME")
    try:
        if host_home_override is not None:
            os.environ["AIQRANK_HOST_HOME"] = host_home_override
        result = scan(window_days=window_days, mtime_after_ts=mtime_after_ts)
    finally:
        if host_home_override is not None:
            if prior_host_home is None:
                os.environ.pop("AIQRANK_HOST_HOME", None)
            else:
                os.environ["AIQRANK_HOST_HOME"] = prior_host_home

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
