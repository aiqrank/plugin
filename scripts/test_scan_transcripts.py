#!/usr/bin/env python3
"""Tests for scan_transcripts.py"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import scan  # noqa: E402


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def make_tool_call(name: str, input_data: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "input": input_data or {}},
            ]
        },
    }


def make_user_msg(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


class ScanTranscriptsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        (self.projects / "proj1").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_counts_sessions_and_messages_from_main_jsonl(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hello"), make_tool_call("Bash", {"command": "ls"})],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["tool_name_counts"]["Bash"], 1)
        self.assertEqual(result["sessions_with_tools"], 1)

    def test_counts_subagent_transcripts_too(self):
        # Main session — no tools
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hi")],
        )

        # Subagent session with tool calls
        write_jsonl(
            self.projects / "proj1" / "sessA" / "subagents" / "agent-xyz.jsonl",
            [make_tool_call("Read"), make_tool_call("Grep")],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["sessions"], 2)
        self.assertEqual(result["tool_name_counts"]["Read"], 1)
        self.assertEqual(result["tool_name_counts"]["Grep"], 1)

    def test_skips_files_older_than_window(self):
        old_file = self.projects / "proj1" / "old.jsonl"
        write_jsonl(old_file, [make_tool_call("Bash")])

        # Back-date file to 90 days ago
        old_ts = time.time() - (90 * 86400)
        os.utime(old_file, (old_ts, old_ts))

        fresh_file = self.projects / "proj1" / "fresh.jsonl"
        write_jsonl(fresh_file, [make_tool_call("Read")])

        result = scan(claude_dir=self.tmp, window_days=60)

        self.assertEqual(result["sessions"], 1)
        self.assertIn("Read", result["tool_name_counts"])
        self.assertNotIn("Bash", result["tool_name_counts"])

    def test_extracts_skill_names_from_skill_tool_use(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("Skill", {"skill": "commit", "args": ""}),
                make_tool_call("Skill", {"skill": "commit", "args": ""}),
                make_tool_call("Skill", {"skill": "review"}),
            ],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["skill_counts"]["commit"], 2)
        self.assertEqual(result["skill_counts"]["review"], 1)

    def test_extracts_mcp_server_names(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("mcp__pencil__batch_design"),
                make_tool_call("mcp__pencil__get_screenshot"),
                make_tool_call("mcp__granola__get_meetings"),
            ],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["mcp_server_counts"]["pencil"], 2)
        self.assertEqual(result["mcp_server_counts"]["granola"], 1)

    def test_detects_orchestration_and_context_leverage_per_session(self):
        write_jsonl(
            self.projects / "proj1" / "orch.jsonl",
            [make_tool_call("Agent", {"prompt": "..."})],
        )

        write_jsonl(
            self.projects / "proj1" / "ctx.jsonl",
            [make_tool_call("TaskCreate"), make_tool_call("ScheduleWakeup")],
        )

        write_jsonl(
            self.projects / "proj1" / "plain.jsonl",
            [make_tool_call("Bash")],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["sessions"], 3)
        self.assertEqual(result["sessions_with_orchestration"], 1)
        self.assertEqual(result["sessions_with_context_leverage"], 1)

    def test_detects_custom_skill_writes_and_excludes_self(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call(
                    "Write",
                    {
                        "file_path": "/Users/me/.claude/skills/my-tool/SKILL.md",
                        "content": "...",
                    },
                ),
                make_tool_call(
                    "Write",
                    {
                        "file_path": "/Users/me/.claude/skills/my-tool/SKILL.md",
                        "content": "update",
                    },
                ),
                make_tool_call(
                    "Write",
                    {
                        "file_path": "/Users/me/.claude/skills/myaiscore/SKILL.md",
                        "content": "self - should be excluded",
                    },
                ),
            ],
        )

        result = scan(claude_dir=self.tmp)

        # Deduplicates the same SKILL.md written twice; self-reference excluded
        self.assertEqual(result["custom_skill_files_written"], 1)

    def test_detects_mcp_config_writes(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("Write", {"file_path": "/Users/me/.mcp.json"}),
                make_tool_call("Edit", {"file_path": "/tmp/proj/.mcp.json"}),
            ],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["custom_mcp_config_writes"], 2)

    def test_extracts_agent_types_from_meta_json(self):
        meta_dir = self.projects / "proj1" / "sessA" / "subagents"
        meta_dir.mkdir(parents=True)
        (meta_dir / "agent-xyz.meta.json").write_text(
            json.dumps({"agentType": "compound-engineering:review:security-sentinel"})
        )
        (meta_dir / "agent-abc.meta.json").write_text(
            json.dumps({"agentType": "compound-engineering:review:security-sentinel"})
        )
        (meta_dir / "agent-def.meta.json").write_text(
            json.dumps({"agentType": "general-purpose"})
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(
            result["agent_type_counts"]["compound-engineering:review:security-sentinel"], 2
        )
        self.assertEqual(result["agent_type_counts"]["general-purpose"], 1)

    def test_captures_first_user_messages_for_role_classification(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("deploy the staging environment to fly"),
                make_user_msg("also run migrations"),  # not first
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "sessB.jsonl",
            [make_user_msg("write landing page copy for our new product launch")],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(len(result["first_messages_sample"]), 2)
        assert any("deploy" in m.lower() for m in result["first_messages_sample"])
        assert any("landing page" in m.lower() for m in result["first_messages_sample"])

    def test_handles_malformed_jsonl_gracefully(self):
        path = self.projects / "proj1" / "bad.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'this is not json\n{"type":"user","message":{"content":"valid"}}\n{ incomplete\n'
        )

        result = scan(claude_dir=self.tmp)

        # Only the valid line is counted
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["sessions"], 1)

    def test_returns_empty_metrics_when_projects_dir_missing(self):
        empty_tmp = Path(tempfile.mkdtemp())
        try:
            result = scan(claude_dir=empty_tmp)
            self.assertEqual(result["sessions"], 0)
            self.assertEqual(result["messages"], 0)
            self.assertEqual(result["tool_calls"], 0)
        finally:
            shutil.rmtree(empty_tmp)

    def test_user_with_only_basic_chat_no_tools(self):
        write_jsonl(
            self.projects / "proj1" / "chat.jsonl",
            [make_user_msg("what is 2+2"), {"type": "assistant", "message": {"content": "4"}}],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(result["sessions_with_tools"], 0)

    def test_tracks_max_parallel_agents_in_single_turn(self):
        # Claude Code splits parallel tool_uses from a single model turn
        # into separate assistant events that share a `requestId`. The
        # scanner must group by requestId to detect parallelism.
        def agent_event(request_id):
            return {
                "type": "assistant",
                "requestId": request_id,
                "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {}}]},
            }

        write_jsonl(
            self.projects / "proj1" / "parallel.jsonl",
            [
                make_user_msg("fan out"),
                # One turn dispatching 4 Agents in parallel (shared requestId)
                agent_event("req_A"),
                agent_event("req_A"),
                agent_event("req_A"),
                agent_event("req_A"),
                # A second turn dispatching 2 Agents (shared requestId)
                agent_event("req_B"),
                agent_event("req_B"),
                # A single-Agent turn — not parallel
                agent_event("req_C"),
            ],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["max_parallel_agents"], 4)
        self.assertEqual(result["parallel_agent_turns"], 2)

    def test_counts_user_corrections(self):
        write_jsonl(
            self.projects / "proj1" / "sess.jsonl",
            [
                make_user_msg("please add a feature"),           # not a correction
                make_user_msg("no, not like that"),               # correction ("no,")
                make_user_msg("stop — don't touch that file"),   # correction ("stop")
                make_user_msg("actually, let's revert it"),      # correction ("actually,")
                make_user_msg("looks good, ship it"),             # not a correction
                make_user_msg("that's wrong"),                    # correction ("wrong")
                # tool_result user-echo should NOT count (no human text)
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
                        ]
                    },
                },
            ],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["user_messages"], 6)
        self.assertEqual(result["user_corrections"], 4)

    def test_tracks_max_messages_in_session(self):
        write_jsonl(
            self.projects / "proj1" / "short.jsonl",
            [make_user_msg("hi"), make_user_msg("again")],
        )
        write_jsonl(
            self.projects / "proj1" / "long.jsonl",
            [make_user_msg(f"msg {i}") for i in range(12)],
        )

        result = scan(claude_dir=self.tmp)

        self.assertEqual(result["max_messages_in_session"], 12)


if __name__ == "__main__":
    unittest.main()
