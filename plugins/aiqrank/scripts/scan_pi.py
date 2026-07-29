#!/usr/bin/env python3
"""Privacy-safe structural activity scanner for the Pi coding agent.

The scanner reads Pi's local JSONL session history and emits the same
per-source envelope as the other AIQ Rank scanners. It never emits message
content, tool arguments, commands, paths, identifiers, tasks, outputs, or cost.
Python stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import stat
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import (  # noqa: E402
    _bucket,
    _cap_codex_dictionary,
    _has_activity,
    _normalize_codex_label,
    _normalize_tool_name,
    _rollup_from_daily,
    max_concurrent_sustained,
    min_sustained_secs,
)


DEFAULT_WINDOW_DAYS = 30
_CHILD_RUN_RE = re.compile(r"^run-\d+$")
_MCP_PATTERNS = (
    re.compile(r"^mcp__([^_]+)__"),
    re.compile(r"^mcp_([^_]+)_"),
)


class PiScanIncomplete(RuntimeError):
    """Raised when Pi history cannot be scanned as a complete snapshot."""


def resolve_session_dir(home: Path | None = None) -> Path:
    """Resolve Pi's session directory using Pi's documented precedence."""
    home = home or Path.home()
    explicit = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if explicit:
        return Path(explicit).expanduser()
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if agent_dir:
        return Path(agent_dir).expanduser() / "sessions"
    return home / ".pi" / "agent" / "sessions"


def scan(
    session_dir: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
    mtime_after_ts: float | None = None,
    home: Path | None = None,
) -> dict:
    """Scan Pi sessions and return a privacy-safe aggregate envelope.

    ``mtime_after_ts`` is accepted for parity with the combined scanner but is
    deliberately not used as an activity cutoff. Pi snapshots are authoritative
    over the full requested event-time window, even when a session file's mtime
    predates an incremental upload cursor.
    """
    del mtime_after_ts
    home = home or Path.home()
    root = session_dir or resolve_session_dir(home)
    now_ts = time.time() if now_ts is None else now_ts
    cutoff_ts = now_ts - (window_days * 86400)

    discovered = _discover_transcripts(root)
    if not discovered:
        return _empty_envelope(window_days)

    daily: dict[date, dict] = {}
    intervals_by_day: dict[date, list[tuple[float, float]]] = defaultdict(list)
    sessions_with_tools: dict[date, set[str]] = defaultdict(set)
    messages_per_session_day: dict[tuple[date, str], int] = defaultdict(int)
    main_cwds: set[Path] = set()
    launch_candidates: list[dict] = []
    child_days: dict[str, date] = {}

    for path, is_child in discovered:
        summary = _process_transcript(
            path,
            is_child=is_child,
            home=home,
            cutoff_ts=cutoff_ts,
            now_ts=now_ts,
            daily=daily,
            sessions_with_tools=sessions_with_tools,
            messages_per_session_day=messages_per_session_day,
        )
        if summary is None:
            continue

        if not is_child and summary["cwd"] is not None:
            main_cwds.add(summary["cwd"])
        launch_candidates.extend(summary["launches"])

        start_ts = summary["start_ts"]
        end_ts = summary["end_ts"]
        if start_ts is None or end_ts is None:
            continue
        if end_ts <= start_ts:
            end_ts = start_ts + 1.0

        canonical = _path_identity(path)
        first_day = None
        for day, (split_start, split_end) in _split_interval_by_day(start_ts, end_ts):
            first_day = first_day or day
            bucket = _bucket(daily, day)
            bucket["sessions"] += 1
            if not is_child:
                bucket["main_sessions"] += 1
            intervals_by_day[day].append((split_start, split_end))
        if is_child and first_day is not None:
            child_days[canonical] = first_day

    for day, session_keys in sessions_with_tools.items():
        _bucket(daily, day)["sessions_with_tools"] += len(session_keys)
    for (day, _session_key), count in messages_per_session_day.items():
        bucket = _bucket(daily, day)
        bucket["max_messages_in_session"] = max(
            bucket["max_messages_in_session"], count
        )

    _apply_orchestration(daily, launch_candidates, child_days)

    sustained = min_sustained_secs()
    for day, intervals in intervals_by_day.items():
        _bucket(daily, day)["max_concurrent_sessions"] = max_concurrent_sustained(
            intervals, sustained
        )

    daily = {day: metrics for day, metrics in daily.items() if _has_activity(metrics)}
    for metrics in daily.values():
        _finalize_metric_dicts(metrics)
    daily_list = [
        {"date": day.isoformat(), "metrics": metrics}
        for day, metrics in sorted(daily.items())
    ]
    rollup = _rollup_from_daily(daily.values())
    serialized_intervals = {
        day.isoformat(): [[start, end] for start, end in intervals]
        for day, intervals in sorted(intervals_by_day.items())
        if day in daily
    }
    return {
        "source": "pi",
        "window_days": window_days,
        "daily": daily_list,
        "rollup": rollup,
        "intervals_by_day": serialized_intervals,
    }


def _discover_transcripts(root: Path) -> list[tuple[Path, bool]]:
    out: list[tuple[Path, bool]] = []
    seen: set[str] = set()
    for path in _walk_files_fail_closed(root, "Pi session tree"):
        if path.suffix != ".jsonl":
            continue
        if path.name == "run-history.jsonl":
            continue
        identity = _path_identity(path)
        if identity in seen:
            continue
        seen.add(identity)
        is_child = path.name == "session.jsonl" and any(
            _CHILD_RUN_RE.fullmatch(parent.name) for parent in path.parents
        )
        out.append((path, is_child))
    return out


def _walk_files_fail_closed(root: Path, label: str) -> list[Path]:
    """Return confined regular files without hiding traversal failures."""
    try:
        resolved_root = root.resolve(strict=True)
        if not stat.S_ISDIR(resolved_root.stat().st_mode):
            return []
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PiScanIncomplete(f"{label} could not be resolved") from exc

    out: list[Path] = []
    pending = [resolved_root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise PiScanIncomplete(f"{label} could not be traversed") from exc

        for entry in ordered:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                    continue
                resolved_path = Path(entry.path).resolve(strict=True)
                if not stat.S_ISREG(resolved_path.stat().st_mode):
                    continue
            except OSError as exc:
                raise PiScanIncomplete(f"{label} entry could not be resolved") from exc
            if resolved_path.is_relative_to(resolved_root):
                out.append(resolved_path)
    return sorted(out)


def _process_transcript(
    path: Path,
    *,
    is_child: bool,
    home: Path,
    cutoff_ts: float,
    now_ts: float,
    daily: dict[date, dict],
    sessions_with_tools: dict[date, set[str]],
    messages_per_session_day: dict[tuple[date, str], int],
) -> dict | None:
    session_key = _path_identity(path)
    active_model = None
    active_effort = None
    cwd = None
    start_ts = None
    end_ts = None
    pending_skill_reads: dict[str, tuple[date, str]] = {}
    skill_configuration = None
    launches_by_call: dict[str, dict] = {}
    launches: list[dict] = []
    turn_number = 0

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PiScanIncomplete("Pi transcript could not be opened") from exc

    with fh:
        for raw_line in fh:
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue

            ts = _entry_timestamp(entry)
            in_window = ts is not None and cutoff_ts <= ts <= now_ts
            if in_window:
                start_ts = ts if start_ts is None else min(start_ts, ts)
                end_ts = ts if end_ts is None else max(end_ts, ts)
            event_type = entry.get("type")

            if event_type == "session":
                raw_cwd = entry.get("cwd")
                if isinstance(raw_cwd, str) and raw_cwd:
                    cwd = Path(raw_cwd).expanduser()
                continue
            if event_type == "model_change":
                model = entry.get("modelId") or entry.get("model")
                if isinstance(model, str) and model:
                    active_model = _safe_label(model)
                continue
            if event_type == "thinking_level_change":
                effort = entry.get("thinkingLevel") or entry.get("level")
                if isinstance(effort, str) and effort:
                    active_effort = _safe_label(effort)
                continue
            if event_type != "message" or not in_window:
                continue

            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            day = datetime.fromtimestamp(ts).date()
            bucket = _bucket(daily, day)

            if role in ("user", "assistant"):
                bucket["messages"] += 1
                messages_per_session_day[(day, session_key)] += 1
                if role == "user":
                    bucket["user_messages"] += 1

            if role == "assistant":
                turn_number += 1
                _record_assistant_usage(bucket, message.get("usage"))
                model = message.get("model")
                model_label = _safe_label(model) if isinstance(model, str) and model else active_model
                if model_label:
                    target = "agent_model_usage" if is_child else "model_usage"
                    _increment_dict(bucket[target], model_label)
                    if not is_child:
                        output_tokens = _safe_int((message.get("usage") or {}).get("output"))
                        if output_tokens:
                            bucket["model_tokens_out"][model_label] = (
                                bucket["model_tokens_out"].get(model_label, 0) + output_tokens
                            )
                if active_effort:
                    _increment_dict(bucket["effort_usage"], active_effort)

                content = message.get("content")
                if not isinstance(content, list):
                    content = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "thinking":
                        bucket["reasoning_blocks"] += 1
                    if block_type != "toolCall":
                        continue
                    tool_name = block.get("name")
                    if not isinstance(tool_name, str) or not tool_name:
                        continue
                    tool_label = _record_tool(bucket, tool_name)
                    sessions_with_tools[day].add(session_key)
                    call_id = block.get("id")
                    args = _tool_arguments(block.get("arguments"))

                    if tool_label.lower() == "read" and isinstance(call_id, str):
                        if skill_configuration is None:
                            skill_configuration = _skill_configuration(home, cwd)
                        skill_name = _skill_name_from_read(
                            args, cwd, skill_configuration
                        )
                        if skill_name:
                            pending_skill_reads[call_id] = (day, skill_name)

                    if tool_label.lower() == "subagent":
                        action = args.get("action") if isinstance(args, dict) else None
                        if isinstance(action, str) and action:
                            continue
                        candidate = _launch_candidate(
                            session_key=session_key,
                            cwd=cwd,
                            day=day,
                            turn=turn_number,
                            call_id=call_id,
                            args=args,
                        )
                        launches.append(candidate)
                        if isinstance(call_id, str) and call_id:
                            launches_by_call[call_id] = candidate
                continue

            if role == "bashExecution":
                _record_tool(bucket, "bashExecution")
                sessions_with_tools[day].add(session_key)
                continue

            if role == "toolResult":
                call_id = message.get("toolCallId")
                if isinstance(call_id, str) and call_id in pending_skill_reads:
                    read_day, skill_name = pending_skill_reads.pop(call_id)
                    if message.get("isError") is not True:
                        _increment_dict(_bucket(daily, read_day)["skill_counts"], skill_name)
                if isinstance(call_id, str) and call_id in launches_by_call:
                    details = message.get("details")
                    results = details.get("results") if isinstance(details, dict) else None
                    if isinstance(results, list):
                        launches_by_call[call_id]["results"] = [
                            {
                                "sessionFile": result.get("sessionFile"),
                                "agent": result.get("agent"),
                            }
                            for result in results
                            if isinstance(result, dict)
                        ]

    if start_ts is None or end_ts is None:
        return None
    return {
        "cwd": cwd,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "launches": launches,
    }


def _record_assistant_usage(bucket: dict, usage) -> None:
    if not isinstance(usage, dict):
        return
    input_tokens = _safe_int(usage.get("input"))
    output_tokens = _safe_int(usage.get("output"))
    cache_read = _safe_int(usage.get("cacheRead"))
    cache_write = _safe_int(usage.get("cacheWrite"))
    total = _safe_int(usage.get("totalTokens"))
    if total == 0:
        total = input_tokens + output_tokens
    bucket["tokens_input"] += input_tokens
    bucket["tokens_output"] += output_tokens
    bucket["tokens_cache_read"] += cache_read
    bucket["tokens_cache_creation"] += cache_write
    bucket["tokens_total"] += total


def _record_tool(bucket: dict, name: str) -> str:
    label = _safe_label(_normalize_tool_name(name))
    bucket["tool_calls"] += 1
    _increment_dict(bucket["tool_name_counts"], label)
    mcp_server = _extract_mcp_server(label)
    if mcp_server:
        _increment_dict(bucket["mcp_server_counts"], mcp_server)
    return label


def _finalize_metric_dicts(metrics: dict) -> None:
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


def _launch_candidate(
    *, session_key: str, cwd: Path | None, day: date, turn: int, call_id, args
) -> dict:
    args = args if isinstance(args, dict) else {}
    tasks = args.get("tasks")
    agents: list[str | None] = []
    if isinstance(tasks, list):
        for task in tasks:
            agent = task.get("agent") if isinstance(task, dict) else None
            agents.append(_safe_optional_label(agent))
    else:
        agents.append(_safe_optional_label(args.get("agent")))
    return {
        "session_key": session_key,
        "cwd": cwd,
        "day": day,
        "turn": turn,
        "call_id": call_id if isinstance(call_id, str) else "",
        "task_agents": agents,
        "results": [],
    }


def _apply_orchestration(
    daily: dict[date, dict], launch_candidates: list[dict], child_days: dict[str, date]
) -> None:
    agent_by_identity: dict[str, str] = {}
    day_by_identity = dict(child_days)
    sessions_by_day: dict[date, set[str]] = defaultdict(set)
    turns: dict[tuple[str, date, int], set[str]] = defaultdict(set)
    parent_identities: set[str] = set()

    for candidate in launch_candidates:
        identities: list[str] = []
        agents: list[str | None] = []
        results = candidate["results"]
        for index, result in enumerate(results):
            session_file = result.get("sessionFile")
            identity = None
            if isinstance(session_file, str) and session_file:
                identity = _session_file_identity(session_file, candidate["cwd"])
            if identity is None:
                identity = _parent_launch_identity(candidate, index)
            identities.append(identity)
            agents.append(_safe_optional_label(result.get("agent")))

        task_agents = candidate["task_agents"]
        for index in range(len(results), len(task_agents)):
            identities.append(_parent_launch_identity(candidate, index))
            agents.append(task_agents[index])
        if not identities and task_agents:
            identities.append(_parent_launch_identity(candidate, 0))
            agents.append(task_agents[0])
        if not identities:
            continue

        unique_identities = set(identities)
        parent_identities.update(unique_identities)
        sessions_by_day[candidate["day"]].add(candidate["session_key"])
        turn_key = (candidate["session_key"], candidate["day"], candidate["turn"])
        turns[turn_key].update(unique_identities)

        for identity, agent in zip(identities, agents):
            day_by_identity.setdefault(identity, candidate["day"])
            if agent and identity not in agent_by_identity:
                agent_by_identity[identity] = agent

    for identity, day in child_days.items():
        if identity in parent_identities:
            continue
        synthetic_session = f"child:{identity}"
        sessions_by_day[day].add(synthetic_session)
        turns[(synthetic_session, day, 0)].add(identity)

    for day, session_keys in sessions_by_day.items():
        _bucket(daily, day)["sessions_with_orchestration"] += len(session_keys)
    for (_session_key, day, _turn), identities in turns.items():
        count = len(identities)
        bucket = _bucket(daily, day)
        if count >= 2:
            bucket["parallel_agent_turns"] += 1
        bucket["max_parallel_agents"] = max(bucket["max_parallel_agents"], count)
    for identity, agent in agent_by_identity.items():
        day = day_by_identity.get(identity)
        if day is not None:
            _increment_dict(_bucket(daily, day)["agent_type_counts"], agent)


def _parent_launch_identity(candidate: dict, index: int) -> str:
    return "parent:" + "|".join(
        (candidate["session_key"], candidate["call_id"], str(candidate["turn"]), str(index))
    )


def _session_file_identity(raw: str, cwd: Path | None) -> str:
    path = Path(raw).expanduser()
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    return _path_identity(path)


def _skill_name_from_read(
    args: dict, cwd: Path | None, configuration: dict
) -> str | None:
    raw_path = args.get("path") if isinstance(args, dict) else None
    if not isinstance(raw_path, str) or not raw_path.endswith("SKILL.md"):
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    try:
        is_file = stat.S_ISREG(resolved.stat().st_mode)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PiScanIncomplete("Pi skill path could not be inspected") from exc
    if not is_file:
        return None
    if any(_settings_skill_enabled(resolved, scope) for scope in configuration["scopes"]):
        return _safe_label(resolved.parent.name)
    if any(_package_skill_enabled(resolved, package) for package in configuration["packages"]):
        return _safe_label(resolved.parent.name)
    return None


def _skill_configuration(home: Path, cwd: Path | None) -> dict:
    scopes = []
    packages = []
    global_base = home / ".pi" / "agent"
    global_settings = _read_settings(global_base / "settings.json")
    scopes.append(
        {
            "base": global_base,
            "auto_roots": [global_base / "skills", home / ".agents" / "skills"],
            "entries": _string_list(global_settings.get("skills")),
        }
    )
    packages.extend(
        _package_configurations(
            global_settings.get("packages"), "user", global_base, home, cwd
        )
    )

    if cwd is not None:
        project_base = cwd / ".pi"
        project_settings = _read_settings(project_base / "settings.json")
        project_roots = [project_base / "skills"]
        current = cwd
        while True:
            project_roots.append(current / ".agents" / "skills")
            if current.parent == current:
                break
            current = current.parent
        scopes.append(
            {
                "base": project_base,
                "auto_roots": project_roots,
                "entries": _string_list(project_settings.get("skills")),
            }
        )
        packages.extend(
            _package_configurations(
                project_settings.get("packages"),
                "project",
                project_base,
                home,
                cwd,
            )
        )
    return {"scopes": scopes, "packages": packages}


def _read_settings(path: Path) -> dict:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return {}
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PiScanIncomplete("Pi resource configuration could not be read") from exc
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _settings_skill_enabled(path: Path, scope: dict) -> bool:
    auto_enabled = any(_is_relative_to(path, root) for root in scope["auto_roots"])
    enabled = auto_enabled
    for raw in scope["entries"]:
        mode, pattern = _pattern_mode(raw)
        if not pattern:
            continue
        if _path_matches_setting(path, scope["base"], pattern):
            enabled = mode != "exclude"
    return enabled


def _path_matches_setting(path: Path, base: Path, pattern: str) -> bool:
    expanded = Path(pattern).expanduser()
    if expanded.is_absolute():
        candidate = expanded
    else:
        candidate = base / expanded
    if not _has_glob(pattern):
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        return path == resolved or _is_relative_to(path, resolved)
    try:
        relative = path.relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    return fnmatch.fnmatchcase(relative, pattern.lstrip("./"))


def _package_configurations(
    value, scope: str, base: Path, home: Path, cwd: Path | None
) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            source = item
            filters = None
        elif isinstance(item, dict) and isinstance(item.get("source"), str):
            source = item["source"]
            filters = _string_list(item.get("skills")) if "skills" in item else None
        else:
            continue
        package = {
            "scope": scope,
            "cwd": cwd,
            "filters": filters,
        }
        if source.startswith("npm:"):
            package["type"] = "npm"
            package["name"] = _npm_package_name(source[4:])
        elif source.startswith("git:") or re.match(
            r"^(?:https?|ssh|git)://", source
        ):
            package["type"] = "git"
            package["root"] = _git_package_root(source, scope, home, cwd)
        else:
            package["type"] = "local"
            raw_path = Path(source).expanduser()
            package["root"] = raw_path if raw_path.is_absolute() else base / raw_path
        out.append(package)
    return out


def _npm_package_name(spec: str) -> str:
    spec = spec.strip()
    if spec.startswith("@"):
        slash = spec.find("/")
        at = spec.find("@", slash + 1) if slash >= 0 else -1
        return spec if at < 0 else spec[:at]
    return spec.split("@", 1)[0]


def _git_package_root(
    source: str, scope: str, home: Path, cwd: Path | None
) -> Path | None:
    raw = source[4:] if source.startswith("git:") else source
    raw = re.sub(r"^[a-z]+://", "", raw)
    if raw.startswith("git@"):
        raw = raw.removeprefix("git@").replace(":", "/", 1)
    if "@" in raw:
        raw = raw.rsplit("@", 1)[0]
    parts = [part for part in raw.split("/") if part]
    if len(parts) < 3:
        return None
    host = parts[0]
    repo_path = Path(*parts[1:])
    if repo_path.suffix == ".git":
        repo_path = repo_path.with_suffix("")
    if scope == "project" and cwd is not None:
        return cwd / ".pi" / "git" / host / repo_path
    return home / ".pi" / "agent" / "git" / host / repo_path


def _package_skill_enabled(path: Path, package: dict) -> bool:
    root = _package_root_for_read(path, package)
    if root is None or not _is_relative_to(path, root):
        return False
    manifest = _read_settings(root / "package.json")
    pi_manifest = manifest.get("pi") if isinstance(manifest.get("pi"), dict) else {}
    raw_skill_entries = pi_manifest.get("skills")
    skill_entries = _string_list(raw_skill_entries)
    if isinstance(raw_skill_entries, list):
        enabled = _matches_resource_patterns(path, root, skill_entries, default=False)
    else:
        enabled = _is_relative_to(path, root / "skills")
    filters = package["filters"]
    if enabled and filters is not None:
        filter_default = bool(filters) and all(
            _pattern_mode(raw)[0] == "exclude" for raw in filters
        )
        enabled = _matches_resource_patterns(
            path, root, filters, default=filter_default
        )
    return enabled


def _package_root_for_read(path: Path, package: dict) -> Path | None:
    if package["type"] != "npm":
        root = package.get("root")
        if root is None:
            return None
        try:
            return root.resolve()
        except OSError:
            return None

    expected_name = package.get("name")
    if not expected_name:
        return None
    for candidate in path.parents:
        manifest = _read_settings(candidate / "package.json")
        if manifest.get("name") != expected_name:
            continue
        if "node_modules" not in candidate.parts:
            continue
        if package["scope"] == "project":
            cwd = package.get("cwd")
            if cwd is None or not _is_relative_to(
                candidate, cwd / ".pi" / "npm" / "node_modules"
            ):
                continue
        else:
            cwd = package.get("cwd")
            if cwd is not None and _is_relative_to(candidate, cwd):
                continue
        return candidate
    return None


def _matches_resource_patterns(
    path: Path, root: Path, patterns: list[str], *, default: bool
) -> bool:
    if not patterns:
        return False
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return False
    enabled = default
    skill_name = path.parent.name
    for raw in patterns:
        mode, pattern = _pattern_mode(raw)
        normalized = pattern.lstrip("./")
        if not normalized:
            continue
        matches = (
            fnmatch.fnmatchcase(relative, normalized)
            or fnmatch.fnmatchcase(skill_name, normalized)
            or relative == normalized.rstrip("/")
            or relative.startswith(normalized.rstrip("/") + "/")
        )
        if matches:
            enabled = mode != "exclude"
    return enabled


def _pattern_mode(raw: str) -> tuple[str, str]:
    if raw.startswith(("!", "-")):
        return "exclude", raw[1:]
    if raw.startswith("+"):
        return "include", raw[1:]
    return "include", raw


def _has_glob(value: str) -> bool:
    return any(char in value for char in "*?[")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _entry_timestamp(entry: dict) -> float | None:
    value = entry.get("timestamp")
    if value is None and isinstance(entry.get("message"), dict):
        value = entry["message"].get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        return numeric / 1000.0 if numeric > 100_000_000_000 else numeric
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        numeric = float(raw)
        return numeric / 1000.0 if numeric > 100_000_000_000 else numeric
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return None


def _split_interval_by_day(
    start_ts: float, end_ts: float
) -> Iterable[tuple[date, tuple[float, float]]]:
    current = start_ts
    while current < end_ts:
        current_dt = datetime.fromtimestamp(current)
        next_midnight = datetime.combine(current_dt.date() + timedelta(days=1), datetime.min.time())
        boundary = min(end_ts, next_midnight.timestamp())
        yield current_dt.date(), (current, boundary)
        current = boundary


def _extract_mcp_server(tool_name: str) -> str | None:
    for pattern in _MCP_PATTERNS:
        match = pattern.match(tool_name)
        if match:
            return _safe_label(match.group(1))
    return None


def _tool_arguments(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _safe_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_label(value: str) -> str:
    return _normalize_codex_label(value) or "unknown"


def _safe_optional_label(value) -> str | None:
    return _safe_label(value) if isinstance(value, str) and value else None


def _increment_dict(target: dict, key: str, amount: int = 1) -> None:
    target[key] = target.get(key, 0) + amount


def _path_identity(path: Path) -> str:
    try:
        return os.path.normcase(str(path.expanduser().resolve(strict=False)))
    except OSError:
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _empty_envelope(window_days: int) -> dict:
    return {
        "source": "pi",
        "window_days": window_days,
        "daily": [],
        "rollup": _rollup_from_daily([]),
        "intervals_by_day": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Pi sessions for aggregate AIQ metrics")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--session-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(scan(session_dir=args.session_dir, window_days=args.window_days)))


if __name__ == "__main__":
    main()
