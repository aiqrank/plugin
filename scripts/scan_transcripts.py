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
                msg = event.get("message") or {}

                # Capture first user message from this session (for later role
                # classification). Cap the sample globally to protect memory.
                if (
                    not first_user_msg_captured
                    and event.get("type") == "user"
                    and len(results["first_messages_sample"]) < 500
                ):
                    content = msg.get("content")
                    text = content_to_text(content)
                    if text:
                        results["first_messages_sample"].append(text[:500])
                        first_user_msg_captured = True

                # Tool_use calls live in assistant messages' content array
                if event.get("type") == "assistant":
                    for tool_use in extract_tool_uses(msg):
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
