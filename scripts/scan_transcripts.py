#!/usr/bin/env python3
"""
MyAIScore transcript scanner.

Walks ~/.claude/projects/* and extracts activity metrics from Claude Code
transcripts modified in the last 60 days. Outputs JSON to stdout.

Emits metrics only — no conversation content, no code, no prompts. The user
confirms the preview in the skill before transmission to the server.

Python stdlib only (no third-party dependencies).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable


# -- Constants --

DEFAULT_WINDOW_DAYS = 60
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
# The plugin writes metrics for myaiscore itself; exclude self-references from
# custom-skill-creation detection.
MYAISCORE_SKILL_DIR_FRAGMENT = "/.claude/skills/myaiscore/"

# Heuristic "correction" detection. A user message is counted as a correction
# when its first ~120 chars contain one of these patterns — i.e. the user is
# pushing back on, negating, or redirecting Claude's prior turn. Only counts
# occurrences; no text is stored or transmitted.
_CORRECTION_RE = re.compile(
    r"""
    (?:^|[\s.,!?;:-])          # word boundary
    (?:
        no[,!.\s]              | # "no," / "no!" / "no " (bare 'no')
        don'?t\b                | # don't
        stop\b                  |
        wait[,!.\s]             | # "wait," at start of message
        undo\b                  |
        revert\b                |
        incorrect\b             |
        wrong\b                 |
        actually[,!.\s]         | # "actually," redirect
        try\s+again\b           |
        you\s+(?:missed|forgot|broke)\b |
        that'?s\s+not\s+(?:right|correct|what\s+i) |
        not\s+what\s+i\s+(?:meant|want|asked) |
        nope\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CORRECTION_SCAN_PREFIX = 120  # only scan first N chars for speed/signal


def scan(
    claude_dir: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
) -> dict:
    """Scan ~/.claude for transcript data within the last `window_days`."""
    home = claude_dir or Path.home() / ".claude"
    projects = home / "projects"
    now_ts = now_ts or time.time()
    cutoff_ts = now_ts - (window_days * 86400)

    results = {
        "window_days": window_days,
        "sessions": 0,
        "messages": 0,
        "tool_calls": 0,
        "sessions_with_tools": 0,
        "sessions_with_orchestration": 0,
        "sessions_with_context_leverage": 0,
        "tool_name_counts": {},
        "skill_counts": {},
        "mcp_server_counts": {},
        "agent_type_counts": {},
        "custom_skill_files_written": 0,
        "custom_mcp_config_writes": 0,
        "first_messages_sample": [],
        # Peak count of Agent tool_uses dispatched in a single assistant turn
        # (parallel sub-agent orchestration). >=2 means genuine parallelism.
        "max_parallel_agents": 0,
        # Count of assistant turns that dispatched >=2 Agents simultaneously.
        "parallel_agent_turns": 0,
        # Deepest single session (by message count) — a proxy for sustained
        # sessions vs short one-shots.
        "max_messages_in_session": 0,
        # User turns total and user turns matching a correction pattern.
        # The ratio is computed in scoring.py for display.
        "user_messages": 0,
        "user_corrections": 0,
    }

    if not projects.is_dir():
        return results

    custom_skills_seen: set[str] = set()

    for project_dir in sorted(projects.iterdir()):
        if not project_dir.is_dir():
            continue

        # Scan both main sessions (flat *.jsonl at the project root) and
        # subagent transcripts (<session_uuid>/subagents/agent-*.jsonl).
        for jsonl_path in iter_transcript_files(project_dir, cutoff_ts):
            process_session(jsonl_path, results, custom_skills_seen)

        # Agent type metadata lives in sibling .meta.json files.
        for meta_path in project_dir.rglob("agent-*.meta.json"):
            if meta_path.stat().st_mtime < cutoff_ts:
                continue

            try:
                with meta_path.open("r") as fh:
                    data = json.load(fh)
                agent_type = data.get("agentType")
                if agent_type:
                    results["agent_type_counts"][agent_type] = (
                        results["agent_type_counts"].get(agent_type, 0) + 1
                    )
            except (json.JSONDecodeError, OSError):
                continue

    results["custom_skill_files_written"] = len(custom_skills_seen)
    return results


def iter_transcript_files(project_dir: Path, cutoff_ts: float) -> Iterable[Path]:
    """Yields JSONL transcript file paths within the cutoff."""
    # Main session transcripts (flat files at project root)
    for entry in project_dir.iterdir():
        if entry.is_file() and entry.suffix == ".jsonl":
            if entry.stat().st_mtime >= cutoff_ts:
                yield entry

    # Subagent transcripts (nested under {session_uuid}/subagents/)
    for subagent_path in project_dir.rglob("subagents/agent-*.jsonl"):
        if subagent_path.stat().st_mtime >= cutoff_ts:
            yield subagent_path


def process_session(path: Path, results: dict, custom_skills_seen: set[str]) -> None:
    """Parse a single JSONL transcript file and update running counters."""
    results["sessions"] += 1

    session_used_tools = False
    session_used_orchestration = False
    session_used_context_leverage = False
    first_user_msg_captured = False
    session_message_count = 0
    # Claude Code writes each parallel tool_use as its own JSONL event but
    # tags them with the same `requestId` (they all came from one model
    # turn). Accumulate Agent dispatches per requestId so we can detect
    # parallel fan-out at session end.
    agents_by_request: dict[str, int] = {}

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

                results["messages"] += 1
                session_message_count += 1
                msg = event.get("message") or {}

                if event.get("type") == "user":
                    text = content_to_text(msg.get("content"))
                    # Only count human-typed user turns, not tool-result echoes.
                    # content_to_text returns "" for tool_result-only content.
                    if text:
                        results["user_messages"] += 1

                        # Correction detection: match in the first 120 chars.
                        if _CORRECTION_RE.search(text[:_CORRECTION_SCAN_PREFIX]):
                            results["user_corrections"] += 1

                        # Capture first user message from this session (for
                        # later role classification). Cap globally for memory.
                        if (
                            not first_user_msg_captured
                            and len(results["first_messages_sample"]) < 500
                        ):
                            results["first_messages_sample"].append(text[:500])
                            first_user_msg_captured = True

                # Tool_use calls live in assistant messages' content array
                if event.get("type") == "assistant":
                    turn_tool_uses = list(extract_tool_uses(msg))

                    # Count Agent dispatches in THIS event's content. Parallel
                    # fan-out is detected across events via requestId below,
                    # since Claude Code may split tool_uses into separate
                    # events that share one requestId.
                    agents_in_event = sum(
                        1 for t in turn_tool_uses if t.get("name") in ORCHESTRATION_TOOLS
                    )
                    request_id = event.get("requestId")
                    if agents_in_event and request_id:
                        agents_by_request[request_id] = (
                            agents_by_request.get(request_id, 0) + agents_in_event
                        )
                    elif agents_in_event:
                        # No requestId — treat each event as its own turn.
                        if agents_in_event > results["max_parallel_agents"]:
                            results["max_parallel_agents"] = agents_in_event
                        if agents_in_event >= 2:
                            results["parallel_agent_turns"] += 1

                    for tool_use in turn_tool_uses:
                        results["tool_calls"] += 1
                        session_used_tools = True

                        name = tool_use.get("name", "")
                        if not name:
                            continue

                        results["tool_name_counts"][name] = (
                            results["tool_name_counts"].get(name, 0) + 1
                        )

                        if name == SKILL_TOOL:
                            skill = (tool_use.get("input") or {}).get("skill")
                            if skill:
                                results["skill_counts"][skill] = (
                                    results["skill_counts"].get(skill, 0) + 1
                                )

                        if name.startswith("mcp__"):
                            server = name.split("__")[1] if len(name.split("__")) >= 3 else ""
                            if server:
                                results["mcp_server_counts"][server] = (
                                    results["mcp_server_counts"].get(server, 0) + 1
                                )

                        if name in ORCHESTRATION_TOOLS:
                            session_used_orchestration = True

                        if name in CONTEXT_LEVERAGE_TOOLS:
                            session_used_context_leverage = True

                        # Detect custom skill creation (Write/Edit on SKILL.md)
                        if name in ("Write", "Edit"):
                            target_path = (tool_use.get("input") or {}).get("file_path") or ""
                            if (
                                target_path.endswith("/SKILL.md")
                                and MYAISCORE_SKILL_DIR_FRAGMENT not in target_path
                            ):
                                custom_skills_seen.add(target_path)

                            # MCP config edits
                            if target_path.endswith("/.mcp.json") or target_path.endswith(
                                "/mcp.json"
                            ):
                                results["custom_mcp_config_writes"] = (
                                    results["custom_mcp_config_writes"] + 1
                                )
    except OSError:
        return

    # Fold per-request Agent tallies into session totals. One request =
    # one model turn, so if N Agents share a requestId, the model dispatched
    # N agents in parallel.
    for count in agents_by_request.values():
        if count > results["max_parallel_agents"]:
            results["max_parallel_agents"] = count
        if count >= 2:
            results["parallel_agent_turns"] += 1

    if session_message_count > results["max_messages_in_session"]:
        results["max_messages_in_session"] = session_message_count
    if session_used_tools:
        results["sessions_with_tools"] += 1
    if session_used_orchestration:
        results["sessions_with_orchestration"] += 1
    if session_used_context_leverage:
        results["sessions_with_context_leverage"] += 1


def extract_tool_uses(message: dict) -> Iterable[dict]:
    """Yields the tool_use content blocks from an assistant message."""
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def content_to_text(content) -> str:
    """Flattens message content (string or list of blocks) to plain text."""
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
