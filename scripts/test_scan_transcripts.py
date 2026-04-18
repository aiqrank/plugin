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

    def rollup(self, result):
        return result["rollup"]

    def test_returns_daily_and_rollup(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hello"), make_tool_call("Bash")],
        )

        result = scan(claude_dir=self.tmp)

        self.assertIn("daily", result)
        self.assertIn("rollup", result)
        self.assertIsInstance(result["daily"], list)
        self.assertEqual(self.rollup(result)["sessions"], 1)
        self.assertEqual(self.rollup(result)["messages"], 2)

    def test_window_days_default_is_30(self):
        result = scan(claude_dir=self.tmp)
        self.assertEqual(result["window_days"], 30)

    def test_counts_sessions_and_messages_from_main_jsonl(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hello"), make_tool_call("Bash", {"command": "ls"})],
        )

        result = scan(claude_dir=self.tmp)
        r = self.rollup(result)

        self.assertEqual(r["sessions"], 1)
        self.assertEqual(r["messages"], 2)
        self.assertEqual(r["tool_calls"], 1)
        self.assertEqual(r["tool_name_counts"]["Bash"], 1)
        self.assertEqual(r["sessions_with_tools"], 1)

    def test_counts_subagent_transcripts_too(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hi")],
        )
        write_jsonl(
            self.projects / "proj1" / "sessA" / "subagents" / "agent-xyz.jsonl",
            [make_tool_call("Read"), make_tool_call("Grep")],
        )

        r = self.rollup(scan(claude_dir=self.tmp))

        self.assertEqual(r["sessions"], 2)
        self.assertEqual(r["tool_name_counts"]["Read"], 1)
        self.assertEqual(r["tool_name_counts"]["Grep"], 1)

    def test_skips_files_older_than_window(self):
        old_file = self.projects / "proj1" / "old.jsonl"
        write_jsonl(old_file, [make_tool_call("Bash")])

        old_ts = time.time() - (90 * 86400)
        os.utime(old_file, (old_ts, old_ts))

        fresh_file = self.projects / "proj1" / "fresh.jsonl"
        write_jsonl(fresh_file, [make_tool_call("Read")])

        r = self.rollup(scan(claude_dir=self.tmp, window_days=60))

        self.assertEqual(r["sessions"], 1)
        self.assertIn("Read", r["tool_name_counts"])
        self.assertNotIn("Bash", r["tool_name_counts"])

    def test_extracts_skill_names_from_skill_tool_use(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("Skill", {"skill": "commit", "args": ""}),
                make_tool_call("Skill", {"skill": "commit", "args": ""}),
                make_tool_call("Skill", {"skill": "review"}),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["skill_counts"]["commit"], 2)
        self.assertEqual(r["skill_counts"]["review"], 1)

    def test_extracts_mcp_server_names(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("mcp__pencil__batch_design"),
                make_tool_call("mcp__pencil__get_screenshot"),
                make_tool_call("mcp__granola__get_meetings"),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["mcp_server_counts"]["pencil"], 2)
        self.assertEqual(r["mcp_server_counts"]["granola"], 1)

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

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["sessions"], 3)
        self.assertEqual(r["sessions_with_orchestration"], 1)
        self.assertEqual(r["sessions_with_context_leverage"], 1)

    def test_detects_custom_skill_writes_and_excludes_self(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call(
                    "Write",
                    {"file_path": "/Users/me/.claude/skills/my-tool/SKILL.md"},
                ),
                make_tool_call(
                    "Write",
                    {"file_path": "/Users/me/.claude/skills/my-tool/SKILL.md"},
                ),
                make_tool_call(
                    "Write",
                    {"file_path": "/Users/me/.claude/skills/cyborgscore/SKILL.md"},
                ),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        # Same SKILL.md path counted once; cyborgscore self-reference excluded.
        self.assertEqual(r["custom_skill_files_written"], 1)

    def test_detects_mcp_config_writes(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("Write", {"file_path": "/Users/me/.mcp.json"}),
                make_tool_call("Edit", {"file_path": "/tmp/proj/.mcp.json"}),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["custom_mcp_config_writes"], 2)

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

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(
            r["agent_type_counts"]["compound-engineering:review:security-sentinel"], 2
        )
        self.assertEqual(r["agent_type_counts"]["general-purpose"], 1)

    def test_captures_first_user_messages_for_role_classification(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("deploy the staging environment to fly"),
                make_user_msg("also run migrations"),
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

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["messages"], 1)
        self.assertEqual(r["sessions"], 1)

    def test_returns_empty_metrics_when_projects_dir_missing(self):
        empty_tmp = Path(tempfile.mkdtemp())
        try:
            result = scan(claude_dir=empty_tmp)
            self.assertEqual(result["daily"], [])
            self.assertEqual(self.rollup(result)["sessions"], 0)
            self.assertEqual(self.rollup(result)["messages"], 0)
            self.assertEqual(self.rollup(result)["tool_calls"], 0)
        finally:
            shutil.rmtree(empty_tmp)

    def test_user_with_only_basic_chat_no_tools(self):
        write_jsonl(
            self.projects / "proj1" / "chat.jsonl",
            [make_user_msg("what is 2+2"), {"type": "assistant", "message": {"content": "4"}}],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["sessions"], 1)
        self.assertEqual(r["tool_calls"], 0)
        self.assertEqual(r["sessions_with_tools"], 0)

    def test_tracks_max_parallel_agents_in_single_turn(self):
        def agent_event(request_id, ts="2026-04-01T10:00:00Z"):
            return {
                "type": "assistant",
                "requestId": request_id,
                "timestamp": ts,
                "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {}}]},
            }

        write_jsonl(
            self.projects / "proj1" / "parallel.jsonl",
            [
                make_user_msg("fan out"),
                agent_event("req_A"),
                agent_event("req_A"),
                agent_event("req_A"),
                agent_event("req_A"),
                agent_event("req_B"),
                agent_event("req_B"),
                agent_event("req_C"),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["max_parallel_agents"], 4)
        self.assertEqual(r["parallel_agent_turns"], 2)

    def test_sums_token_usage_from_assistant_messages(self):
        write_jsonl(
            self.projects / "proj1" / "sess.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 200,
                        },
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "again"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                },
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["tokens_input"], 110)
        self.assertEqual(r["tokens_output"], 55)
        self.assertEqual(r["tokens_cache_read"], 1000)
        self.assertEqual(r["tokens_cache_creation"], 200)
        self.assertEqual(r["tokens_total"], 1365)

    def test_detects_concurrent_main_sessions(self):
        write_jsonl(
            self.projects / "proj1" / "A.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:30:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "B.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:15:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:45:00Z"},
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["main_sessions"], 2)
        self.assertEqual(r["max_concurrent_sessions"], 2)

    def test_back_to_back_sessions_are_not_concurrent(self):
        write_jsonl(
            self.projects / "proj1" / "A.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:30:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "B.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:30:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T11:00:00Z"},
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["max_concurrent_sessions"], 1)

    def test_subagent_transcripts_do_not_count_for_concurrency(self):
        session_uuid = "abc123"
        write_jsonl(
            self.projects / "proj1" / f"{session_uuid}.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:30:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / session_uuid / "subagents" / "agent-001.jsonl",
            [
                {"type": "user", "message": {"content": "x"}, "timestamp": "2026-04-01T10:10:00Z"},
                {"type": "user", "message": {"content": "y"}, "timestamp": "2026-04-01T10:20:00Z"},
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["main_sessions"], 1)
        self.assertEqual(r["sessions"], 2)
        self.assertEqual(r["max_concurrent_sessions"], 1)

    def test_counts_user_corrections(self):
        write_jsonl(
            self.projects / "proj1" / "sess.jsonl",
            [
                make_user_msg("please add a feature"),
                make_user_msg("no, not like that"),
                make_user_msg("stop — don't touch that file"),
                make_user_msg("actually, let's revert it"),
                make_user_msg("looks good, ship it"),
                make_user_msg("that's wrong"),
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

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["user_messages"], 6)
        self.assertEqual(r["user_corrections"], 4)

    def test_tracks_max_messages_in_session(self):
        write_jsonl(
            self.projects / "proj1" / "short.jsonl",
            [make_user_msg("hi"), make_user_msg("again")],
        )
        write_jsonl(
            self.projects / "proj1" / "long.jsonl",
            [make_user_msg(f"msg {i}") for i in range(12)],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["max_messages_in_session"], 12)

    def test_counts_claude_md_and_agents_md_writes(self):
        write_jsonl(
            self.projects / "proj1" / "s.jsonl",
            [
                make_tool_call("Write", {"file_path": "/repo/CLAUDE.md"}),
                make_tool_call("Edit", {"file_path": "/repo/AGENTS.md"}),
                make_tool_call("Write", {"file_path": "/repo/src/foo.py"}),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["claude_md_writes"], 2)

    def test_counts_plan_mode_invocations_and_sessions(self):
        write_jsonl(
            self.projects / "proj1" / "plan.jsonl",
            [
                make_user_msg("plan this"),
                make_tool_call("ExitPlanMode", {"plan": "..."}),
                make_tool_call("ExitPlanMode", {"plan": "..."}),
                make_tool_call("Bash"),
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "noplan.jsonl",
            [make_user_msg("hi"), make_tool_call("Bash")],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["plan_mode_invocations"], 2)
        self.assertEqual(r["sessions_with_plan_mode"], 1)


class DailyBucketingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        (self.projects / "proj1").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_session_spanning_two_days_contributes_to_both(self):
        write_jsonl(
            self.projects / "proj1" / "cross.jsonl",
            [
                # Spaced ~30 hours apart so the local-date difference is
                # guaranteed regardless of the machine's TZ.
                {
                    "type": "user",
                    "message": {"content": "first day"},
                    "timestamp": "2026-04-01T08:00:00Z",
                },
                {
                    "type": "user",
                    "message": {"content": "next day"},
                    "timestamp": "2026-04-02T14:00:00Z",
                },
            ],
        )

        result = scan(claude_dir=self.tmp, window_days=365 * 5)

        # Two daily entries (one per UTC day touched)
        dates = [d["date"] for d in result["daily"]]
        # Either UTC or local day depending on machine TZ — accept either pair
        # but assert the days are distinct.
        self.assertEqual(len(set(dates)), 2)

        # Each day records 1 message
        for d in result["daily"]:
            self.assertEqual(d["metrics"]["messages"], 1)

        # Rollup sums: 2 messages, 1 session that touched 2 days = 2 sessions
        # (one per day, since `sessions` counts (session, day) tuples)
        self.assertEqual(result["rollup"]["messages"], 2)
        self.assertEqual(result["rollup"]["sessions"], 2)

    def test_daily_array_is_sorted_oldest_first(self):
        # Two sessions, each on a distinct day
        write_jsonl(
            self.projects / "proj1" / "later.jsonl",
            [
                {
                    "type": "user",
                    "message": {"content": "x"},
                    "timestamp": "2026-04-05T12:00:00Z",
                },
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "earlier.jsonl",
            [
                {
                    "type": "user",
                    "message": {"content": "x"},
                    "timestamp": "2026-04-03T12:00:00Z",
                },
            ],
        )

        result = scan(claude_dir=self.tmp, window_days=365 * 5)
        dates = [d["date"] for d in result["daily"]]
        self.assertEqual(dates, sorted(dates))

    def test_per_day_dict_fields_are_isolated(self):
        # Day A: only Bash. Day B: only Read. Rollup: both.
        write_jsonl(
            self.projects / "proj1" / "dayA.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-04-01T10:00:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]
                    },
                }
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "dayB.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-04-02T10:00:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Read", "input": {}}]
                    },
                }
            ],
        )

        result = scan(claude_dir=self.tmp, window_days=365 * 5)

        days = {d["date"]: d["metrics"] for d in result["daily"]}
        self.assertEqual(days["2026-04-01"]["tool_name_counts"], {"Bash": 1})
        self.assertEqual(days["2026-04-02"]["tool_name_counts"], {"Read": 1})
        self.assertEqual(
            result["rollup"]["tool_name_counts"], {"Bash": 1, "Read": 1}
        )

    def test_rollup_equals_aggregation_of_daily(self):
        # Multiple events across two days; rollup totals must match a manual
        # aggregation of the daily entries.
        write_jsonl(
            self.projects / "proj1" / "sess.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-04-01T10:00:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-04-02T10:00:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                        "usage": {"input_tokens": 200, "output_tokens": 75},
                    },
                },
            ],
        )

        result = scan(claude_dir=self.tmp, window_days=365 * 5)

        sum_bash = sum(d["metrics"]["tool_name_counts"].get("Bash", 0) for d in result["daily"])
        sum_tokens = sum(d["metrics"]["tokens_total"] for d in result["daily"])

        self.assertEqual(result["rollup"]["tool_name_counts"]["Bash"], sum_bash)
        self.assertEqual(result["rollup"]["tokens_total"], sum_tokens)

    def test_max_concurrent_per_day_then_max_in_rollup(self):
        # Day 1: two overlapping sessions → per-day peak 2, rollup peak 2.
        write_jsonl(
            self.projects / "proj1" / "A.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:30:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "B.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:15:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:45:00Z"},
            ],
        )

        result = scan(claude_dir=self.tmp, window_days=365 * 5)
        self.assertEqual(result["rollup"]["max_concurrent_sessions"], 2)


if __name__ == "__main__":
    unittest.main()
