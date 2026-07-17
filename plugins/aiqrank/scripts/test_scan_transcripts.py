#!/usr/bin/env python3
"""Tests for scan_transcripts.py"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_transcripts import (  # noqa: E402
    scan,
    scan_codex,
    process_codex_session,
    _seed_authored_into_latest_day,
)

# Fixed wall clock for tests whose fixtures carry explicit 2026-04-01 event
# timestamps. The scanner now drops day-buckets older than `now - window_days`,
# so these tests anchor `now_ts` to keep their fixtures inside the default
# window regardless of when the suite runs.
_FIXTURE_NOW = datetime(2026, 4, 2, tzinfo=timezone.utc).timestamp()


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


def claude_block(result):
    return result["by_source"]["claude_code"]


def cowork_block(result):
    return result["by_source"]["cowork"]


class ScanTranscriptsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        (self.projects / "proj1").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rollup(self, result):
        return claude_block(result)["rollup"]

    def test_returns_daily_and_rollup(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hello"), make_tool_call("Bash")],
        )

        result = scan(claude_dir=self.tmp)

        self.assertIn("by_source", result)
        claude = claude_block(result)
        self.assertIn("daily", claude)
        self.assertIn("rollup", claude)
        self.assertIsInstance(claude["daily"], list)
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

    def test_drops_event_buckets_older_than_window(self):
        # A recently-touched (resumed) transcript whose mtime passes the file
        # filter, but which carries events from before the window. Those events
        # are bucketed by their own date and must NOT leak into the emitted
        # daily list / intervals, or the per-source day count can blow past the
        # server's per-source cap and 422 the upload.
        write_jsonl(
            self.projects / "proj1" / "resumed.jsonl",
            [
                # In-window day (just inside the default 30-day window).
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:30:00Z"},
                # Out-of-window days dragged in by the long/resumed transcript.
                {"type": "user", "message": {"content": "old1"}, "timestamp": "2026-01-15T10:00:00Z"},
                {"type": "user", "message": {"content": "old2"}, "timestamp": "2026-01-15T10:30:00Z"},
                {"type": "user", "message": {"content": "older"}, "timestamp": "2025-12-01T09:00:00Z"},
            ],
        )

        block = claude_block(scan(claude_dir=self.tmp, window_days=30, now_ts=_FIXTURE_NOW))
        dates = {row["date"] for row in block["daily"]}
        self.assertEqual(dates, {"2026-04-01"})
        # The in-window day's interval survives (guards against an off-by-one
        # that drops the in-window bucket along with the out-of-window ones).
        self.assertIn("2026-04-01", block["intervals_by_day"])
        self.assertNotIn("2026-01-15", block["intervals_by_day"])
        self.assertNotIn("2025-12-01", block["intervals_by_day"])

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

    def test_seeds_authored_skill_names_from_on_disk_skills(self):
        # A skill that exists on disk but was never written inside a captured
        # transcript should still count as authored (parity with OpenCode/Cursor).
        for name in ("my-skill", "aiqrank"):
            skill_dir = self.tmp / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n")

        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("<command-name>/my-skill</command-name>"),
                make_user_msg("<command-name>/my-skill</command-name>"),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        # Authored from disk (even with no skills-dir Write event captured), and
        # only that skill — the aiqrank self-skill is never credited as authored.
        self.assertEqual(r["authored_skill_names"], ["my-skill"])
        # Usage is still recorded, so bespoke_practice (authored AND used >=2)
        # can fire server-side.
        self.assertEqual(r["skill_counts"]["my-skill"], 2)

    def test_on_disk_skills_do_not_fabricate_a_day_without_activity(self):
        # Load-bearing guard: on-disk skills must NOT surface when there is zero
        # in-window Claude activity — seeding only ever touches an existing
        # active day, never a synthesized one.
        skill_dir = self.tmp / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n")

        block = claude_block(scan(claude_dir=self.tmp))
        self.assertEqual(block["daily"], [])
        self.assertEqual(block["rollup"]["authored_skill_names"], [])

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

    def test_uuid_shaped_mcp_server_names_are_bucketed(self):
        # Claude Desktop / cowork sometimes spawns MCP servers under a
        # per-installation UUID; bucketing keeps the count without leaking IDs.
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_tool_call("mcp__4507b484-062b-4cc6-85ff-0862c2c5567a__search"),
                make_tool_call("mcp__4507b484-062b-4cc6-85ff-0862c2c5567a__list"),
                make_tool_call("mcp__6ab117fc-c70c-4338-9eb7-10d11d55df0c__get_meetings"),
                make_tool_call("mcp__pencil__batch_design"),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))

        # Three UUID-suffixed calls all collapse under "dynamic".
        self.assertEqual(r["mcp_server_counts"]["dynamic"], 3)
        self.assertEqual(r["mcp_server_counts"]["pencil"], 1)
        # Match the production _UUID_RE shape (canonical 8-4-4-4-12 hex,
        # case-insensitive) so an uppercase UUID would still trip the leak
        # detector.
        uuid_re = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            re.IGNORECASE,
        )
        for key in r["mcp_server_counts"]:
            self.assertIsNone(
                uuid_re.search(key), f"UUID-shaped key leaked: {key}"
            )
        # tool_name_counts also has the UUID swapped for the sentinel.
        self.assertIn("mcp__dynamic__search", r["tool_name_counts"])
        self.assertIn("mcp__dynamic__list", r["tool_name_counts"])
        self.assertIn("mcp__dynamic__get_meetings", r["tool_name_counts"])
        for key in r["tool_name_counts"]:
            self.assertIsNone(
                uuid_re.search(key), f"UUID-shaped tool name leaked: {key}"
            )

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
                    {"file_path": "/Users/me/.claude/skills/aiqrank/SKILL.md"},
                ),
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp))
        # Same SKILL.md path counted once; aiqrank self-reference excluded.
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
            self.assertEqual(claude_block(result)["daily"], [])
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

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
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

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
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

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
        self.assertEqual(r["max_concurrent_sessions"], 1)

    def test_brief_overlap_does_not_count_as_concurrent(self):
        # Two sessions whose intervals overlap for only 60s — under the
        # default 300s sustained threshold, the day's peak should be 1.
        write_jsonl(
            self.projects / "proj1" / "A.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:01:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "B.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:30Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:05:00Z"},
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
        self.assertEqual(r["main_sessions"], 2)
        self.assertEqual(r["max_concurrent_sessions"], 1)

    def test_sustained_overlap_counts_as_concurrent(self):
        # Two sessions overlapping for ~25 minutes — well above the 300s
        # threshold. This is the canonical "real parallelism" case.
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
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:05:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:35:00Z"},
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
        self.assertEqual(r["max_concurrent_sessions"], 2)

    def test_brief_3way_spike_falls_back_to_2way_sustained(self):
        # A and B overlap for 20 minutes (well past 300s).
        # C briefly joins for 30 seconds — the 3-way overlap is too short
        # to count, but the sustained 2-way peak survives.
        write_jsonl(
            self.projects / "proj1" / "A.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:20:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "B.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:05:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:25:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "C.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:10:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:10:30Z"},
            ],
        )

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
        self.assertEqual(r["max_concurrent_sessions"], 2)

    def test_min_sustained_secs_env_override(self):
        # With AIQRANK_MIN_SUSTAINED_SECS=0 (no smoothing), the 60s overlap
        # case from the brief-overlap test should report concurrency=2.
        write_jsonl(
            self.projects / "proj1" / "A.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:00Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:01:00Z"},
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "B.jsonl",
            [
                {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-04-01T10:00:30Z"},
                {"type": "user", "message": {"content": "bye"}, "timestamp": "2026-04-01T10:05:00Z"},
            ],
        )

        prior = os.environ.get("AIQRANK_MIN_SUSTAINED_SECS")
        os.environ["AIQRANK_MIN_SUSTAINED_SECS"] = "0"
        try:
            r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
        finally:
            if prior is None:
                del os.environ["AIQRANK_MIN_SUSTAINED_SECS"]
            else:
                os.environ["AIQRANK_MIN_SUSTAINED_SECS"] = prior

        self.assertEqual(r["max_concurrent_sessions"], 2)

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

        r = self.rollup(scan(claude_dir=self.tmp, now_ts=_FIXTURE_NOW))
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
        claude = claude_block(result)

        # Two daily entries (one per UTC day touched)
        dates = [d["date"] for d in claude["daily"]]
        # Either UTC or local day depending on machine TZ — accept either pair
        # but assert the days are distinct.
        self.assertEqual(len(set(dates)), 2)

        # Each day records 1 message
        for d in claude["daily"]:
            self.assertEqual(d["metrics"]["messages"], 1)

        # Rollup sums: 2 messages, 1 session that touched 2 days = 2 sessions
        # (one per day, since `sessions` counts (session, day) tuples)
        self.assertEqual(claude["rollup"]["messages"], 2)
        self.assertEqual(claude["rollup"]["sessions"], 2)

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
        dates = [d["date"] for d in claude_block(result)["daily"]]
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
        claude = claude_block(result)

        days = {d["date"]: d["metrics"] for d in claude["daily"]}
        self.assertEqual(days["2026-04-01"]["tool_name_counts"], {"Bash": 1})
        self.assertEqual(days["2026-04-02"]["tool_name_counts"], {"Read": 1})
        self.assertEqual(
            claude["rollup"]["tool_name_counts"], {"Bash": 1, "Read": 1}
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
        claude = claude_block(result)

        sum_bash = sum(d["metrics"]["tool_name_counts"].get("Bash", 0) for d in claude["daily"])
        sum_tokens = sum(d["metrics"]["tokens_total"] for d in claude["daily"])

        self.assertEqual(claude["rollup"]["tool_name_counts"]["Bash"], sum_bash)
        self.assertEqual(claude["rollup"]["tokens_total"], sum_tokens)

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
        self.assertEqual(claude_block(result)["rollup"]["max_concurrent_sessions"], 2)


class CoworkScannerTests(unittest.TestCase):
    """Claude Cowork (Local Agent Mode) sandboxed sessions are routed into
    their own per-source bucket (`by_source.cowork`). Cowork-specific
    counters (cowork_sessions, cowork_messages, queue_events) live only in
    that bucket; the claude_code bucket reflects only interactive activity.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Interactive Claude Code root
        self.projects = self.tmp / "projects"
        (self.projects / "proj1").mkdir(parents=True)
        # Cowork sandbox root mimics the real layout:
        #   {root}/{account}/{workspace}/local_*/.claude/projects/{proj}/*.jsonl
        self.cowork_root = self.tmp / "cowork-sessions"
        self.cowork_projects = (
            self.cowork_root
            / "acct-uuid"
            / "ws-uuid"
            / "local_sess-uuid"
            / ".claude"
            / "projects"
            / "some-proj"
        )
        self.cowork_projects.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _scan(self):
        return scan(claude_dir=self.tmp, cowork_root=self.cowork_root)

    def test_counts_cowork_sessions_messages_and_queue_events(self):
        write_jsonl(
            self.cowork_projects / "conv1.jsonl",
            [
                make_user_msg("hello cowork"),
                make_tool_call("Bash", {"command": "ls"}),
                {"type": "queue-operation", "operation": "enqueue"},
                {"type": "queue-operation", "operation": "dequeue"},
            ],
        )

        result = self._scan()
        cowork_r = cowork_block(result)["rollup"]
        claude_r = claude_block(result)["rollup"]

        # The two queue-operation events do NOT count as messages.
        self.assertEqual(cowork_r["messages"], 2)
        # But they do get tallied.
        self.assertEqual(cowork_r["queue_events"], 2)
        # One cowork session, two cowork messages (user + assistant).
        self.assertEqual(cowork_r["cowork_sessions"], 1)
        self.assertEqual(cowork_r["cowork_messages"], 2)
        # Session and tool counts land in the cowork source bucket.
        self.assertEqual(cowork_r["sessions"], 1)
        self.assertEqual(cowork_r["tool_name_counts"]["Bash"], 1)
        self.assertEqual(cowork_r["tool_calls"], 1)
        # Cross-source isolation: nothing leaked into claude_code.
        self.assertEqual(claude_r["sessions"], 0)
        self.assertEqual(claude_r["messages"], 0)

    def test_regular_sessions_do_not_increment_cowork_counters(self):
        write_jsonl(
            self.projects / "proj1" / "interactive.jsonl",
            [make_user_msg("hi"), make_tool_call("Bash")],
        )

        result = self._scan()
        claude_r = claude_block(result)["rollup"]
        cowork_r = cowork_block(result)["rollup"]

        # Interactive activity lands in claude_code only.
        self.assertEqual(claude_r["messages"], 2)
        # Cowork bucket stays empty — cross-source isolation.
        self.assertEqual(cowork_r["messages"], 0)
        self.assertEqual(cowork_r["cowork_messages"], 0)
        self.assertEqual(cowork_r["cowork_sessions"], 0)
        self.assertEqual(cowork_r["queue_events"], 0)
        self.assertEqual(cowork_block(result)["daily"], [])

    def test_cowork_and_interactive_sessions_split_by_source(self):
        write_jsonl(
            self.projects / "proj1" / "interactive.jsonl",
            [make_user_msg("hi"), make_user_msg("again")],
        )
        write_jsonl(
            self.cowork_projects / "conv1.jsonl",
            [
                make_user_msg("autonomous task"),
                make_tool_call("Read"),
                {"type": "queue-operation", "operation": "enqueue"},
            ],
        )

        result = self._scan()
        claude_r = claude_block(result)["rollup"]
        cowork_r = cowork_block(result)["rollup"]

        # Interactive activity in claude_code source only.
        self.assertEqual(claude_r["sessions"], 1)
        self.assertEqual(claude_r["messages"], 2)
        # Cowork activity in cowork source only.
        self.assertEqual(cowork_r["sessions"], 1)
        self.assertEqual(cowork_r["messages"], 2)
        # Cowork-specific subset.
        self.assertEqual(cowork_r["cowork_sessions"], 1)
        self.assertEqual(cowork_r["cowork_messages"], 2)
        self.assertEqual(cowork_r["queue_events"], 1)
        # No bleed of cowork counters into claude_code.
        self.assertEqual(claude_r["cowork_sessions"], 0)
        self.assertEqual(claude_r["cowork_messages"], 0)
        self.assertEqual(claude_r["queue_events"], 0)

    def test_missing_cowork_root_is_silent(self):
        # No cowork directory present at all — scan must not crash.
        shutil.rmtree(self.cowork_root)
        write_jsonl(
            self.projects / "proj1" / "sess.jsonl",
            [make_user_msg("hi")],
        )

        result = self._scan()
        self.assertEqual(claude_block(result)["rollup"]["sessions"], 1)
        self.assertEqual(cowork_block(result)["rollup"]["cowork_sessions"], 0)
        self.assertEqual(cowork_block(result)["daily"], [])

    def test_queue_operation_in_non_cowork_session_is_ignored(self):
        # Defense against future protocol drift — queue_events is a
        # cowork-only metric. If a queue-operation event ever appears in
        # an interactive transcript, it must NOT inflate queue_events.
        write_jsonl(
            self.projects / "proj1" / "interactive.jsonl",
            [
                make_user_msg("hi"),
                {"type": "queue-operation", "operation": "enqueue"},
            ],
        )

        result = self._scan()
        # queue_events is a cowork-only metric — must not surface in either
        # source from an interactive transcript.
        self.assertEqual(claude_block(result)["rollup"]["queue_events"], 0)
        self.assertEqual(cowork_block(result)["rollup"]["queue_events"], 0)
        self.assertEqual(cowork_block(result)["rollup"]["cowork_sessions"], 0)

    def test_uuid_in_any_mcp_segment_is_normalized(self):
        # Both server-segment and trailing-segment UUIDs are swept, not just
        # parts[1]. Defends against tool names of the shape
        # `mcp__<uuid>__action__<uuid>` where one ID is the server and the
        # other identifies a per-installation resource.
        write_jsonl(
            self.projects / "proj1" / "sess.jsonl",
            [
                make_tool_call(
                    "mcp__4507b484-062b-4cc6-85ff-0862c2c5567a"
                    "__action__6ab117fc-c70c-4338-9eb7-10d11d55df0c"
                ),
            ],
        )

        # The interactive write goes into claude_code, not cowork.
        r = claude_block(self._scan())["rollup"]

        uuid_re = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            re.IGNORECASE,
        )
        for key in r["tool_name_counts"]:
            self.assertIsNone(
                uuid_re.search(key),
                f"UUID-shaped key leaked into tool_name_counts: {key}",
            )
        self.assertIn("mcp__dynamic__action__dynamic", r["tool_name_counts"])

    def test_multiple_cowork_sessions_same_day(self):
        # Two distinct cowork JSONLs — should count as two cowork sessions.
        write_jsonl(
            self.cowork_projects / "conv1.jsonl",
            [make_user_msg("task A")],
        )
        # A second session may live under a second local_* sandbox, simulating
        # two independent cowork runs.
        other_sandbox = (
            self.cowork_root
            / "acct-uuid"
            / "ws-uuid"
            / "local_other"
            / ".claude"
            / "projects"
            / "another-proj"
        )
        other_sandbox.mkdir(parents=True)
        write_jsonl(
            other_sandbox / "conv2.jsonl",
            [make_user_msg("task B")],
        )

        r = cowork_block(self._scan())["rollup"]
        self.assertEqual(r["cowork_sessions"], 2)
        self.assertEqual(r["cowork_messages"], 2)


class ScheduledTaskTests(unittest.TestCase):
    """Cowork scheduled-task fields surface how heavily a user leans on
    autonomous, time-triggered runs versus only interactive sessions.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        # Cowork sandbox root with workspace-level local_*.json manifests.
        self.cowork_root = self.tmp / "cowork-sessions"
        self.workspace_dir = self.cowork_root / "acct-uuid" / "ws-uuid"
        # The manifest sits at workspace level alongside (but not inside)
        # the local_*/ sandbox directories the transcript scanner walks.
        self.workspace_dir.mkdir(parents=True)
        self.scheduled_root = self.tmp / "Scheduled"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write_manifest(self, sid: str, initial_message: str, created_ms: int):
        path = self.workspace_dir / f"{sid}.json"
        with path.open("w") as fh:
            json.dump(
                {
                    "sessionId": sid,
                    "initialMessage": initial_message,
                    "createdAt": created_ms,
                },
                fh,
            )
        # Bump mtime so the cutoff filter doesn't skip the file.
        ts = created_ms / 1000.0
        os.utime(path, (ts, ts))

    def _write_skill_dir(self, slug: str):
        d = self.scheduled_root / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {slug}\n\nScheduled task body.\n")

    def _scan(self):
        return scan(
            claude_dir=self.tmp,
            cowork_root=self.cowork_root,
            scheduled_root=self.scheduled_root,
        )

    # Dates are relative to "now" so the default 30-day scan window always
    # includes them — hardcoded calendar dates silently fall out of window
    # as time passes.
    def _recent_dt(self, days_ago: int, hour: int, minute: int = 0) -> datetime:
        base = (datetime.now() - timedelta(days=days_ago)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return base

    def _recent_ms(self, days_ago: int, hour: int, minute: int = 0) -> int:
        return int(self._recent_dt(days_ago, hour, minute).timestamp() * 1000)

    def test_counts_scheduled_task_runs_from_manifest_marker(self):
        # Two scheduled runs, one ad-hoc run — only the two count.
        self._write_manifest(
            "local_aaaa",
            '<scheduled-task name="granola-crm-sync" file="...">do stuff</scheduled-task>',
            self._recent_ms(5, 10, 0),
        )
        self._write_manifest(
            "local_bbbb",
            '<scheduled-task name="daily-briefing" file="...">brief me</scheduled-task>',
            self._recent_ms(4, 8, 0),
        )
        self._write_manifest(
            "local_cccc",
            "Hey Claude, fix this bug",  # no marker — interactive run
            self._recent_ms(4, 14, 0),
        )

        r = cowork_block(self._scan())["rollup"]
        self.assertEqual(r["scheduled_task_runs"], 2)

    def test_scheduled_runs_bucket_by_local_day(self):
        day1 = self._recent_dt(5, 7, 0).date().isoformat()
        day2 = self._recent_dt(4, 7, 0).date().isoformat()
        # Two runs on day1 (one early, one late) + one on day2.
        self._write_manifest(
            "local_aa", '<scheduled-task name="x">go</scheduled-task>', self._recent_ms(5, 7, 0)
        )
        self._write_manifest(
            "local_bb", '<scheduled-task name="y">go</scheduled-task>', self._recent_ms(5, 23, 30)
        )
        self._write_manifest(
            "local_cc", '<scheduled-task name="z">go</scheduled-task>', self._recent_ms(4, 7, 0)
        )

        cowork = cowork_block(self._scan())
        per_day = {entry["date"]: entry["metrics"]["scheduled_task_runs"] for entry in cowork["daily"]}
        self.assertEqual(per_day.get(day1), 2)
        self.assertEqual(per_day.get(day2), 1)
        self.assertEqual(cowork["rollup"]["scheduled_task_runs"], 3)

    def test_active_count_is_subdirs_with_skill_md(self):
        # Three definitions on disk; one missing SKILL.md doesn't count.
        self._write_skill_dir("daily-briefing")
        self._write_skill_dir("granola-crm-sync")
        self._write_skill_dir("crm-followup-check")
        empty = self.scheduled_root / "empty-no-skill"
        empty.mkdir(parents=True)

        # Need at least one active day for the snapshot to attach.
        self._write_manifest(
            "local_aa",
            '<scheduled-task name="daily-briefing">go</scheduled-task>',
            self._recent_ms(5, 7, 0),
        )

        r = cowork_block(self._scan())["rollup"]
        # Peak field — rolls up via MAX, so the latest-day value is the rollup.
        self.assertEqual(r["scheduled_tasks_active"], 3)

    def test_active_count_is_zero_when_dir_missing(self):
        # No manifests, no scheduled root — fields should be absent/zero.
        cowork = cowork_block(self._scan())
        # No active days at all — daily is empty, rollup peak stays 0.
        self.assertEqual(cowork["rollup"]["scheduled_tasks_active"], 0)
        self.assertEqual(cowork["rollup"]["scheduled_task_runs"], 0)


class OpenCodeOrchestrationTests(unittest.TestCase):
    """Integration tests verifying scan_transcripts.py routes OpenCode data
    into by_source["opencode"] when passed an opencode_dir override via the
    scan_opencode sub-scanner.

    We use scan_opencode.scan() directly (via its opencode_db parameter) to
    create a synthetic DB and confirm the orchestrator picks it up.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _make_opencode_db(self, db_path: Path, now_ts: float) -> None:
        """Create a minimal opencode.db with one session and one message."""
        import sqlite3 as _sqlite3
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions "
            "(id TEXT PRIMARY KEY, parent_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, directory TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages "
            "(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)"
        )
        ts_ms = int(now_ts * 1000)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("sess-1", None, ts_ms, ts_ms, "/tmp/proj"),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            ("msg-1", "sess-1", ts_ms, '{"role":"user"}'),
        )
        conn.commit()
        conn.close()

    def test_opencode_source_in_by_source_when_db_present(self):
        """scan_opencode.scan() with a valid db populates the expected envelope."""
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from scan_opencode import scan as scan_opencode_fn

        db_path = self.tmp / "opencode.db"
        now_ts = time.time()
        self._make_opencode_db(db_path, now_ts)

        result = scan_opencode_fn(
            opencode_db=db_path,
            window_days=30,
            now_ts=now_ts,
            home=self.tmp / ".claude",
            opencode_config_root=self.tmp / ".config" / "opencode",
        )

        self.assertEqual(result["source"], "opencode")
        self.assertIn("daily", result)
        self.assertIn("rollup", result)
        self.assertIsInstance(result["daily"], list)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["rollup"]["sessions"], 1)
        self.assertEqual(result["rollup"]["messages"], 1)

    def test_opencode_empty_envelope_when_db_absent(self):
        """scan_opencode.scan() returns empty envelope when DB is missing."""
        from scan_opencode import scan as scan_opencode_fn

        result = scan_opencode_fn(
            opencode_db=self.tmp / "nonexistent.db",
            window_days=30,
        )

        self.assertEqual(result["source"], "opencode")
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["rollup"]["sessions"], 0)

    def test_scan_transcripts_preserves_opencode_rollup_only_skills(self):
        fake_home = self.tmp / "home"
        db_path = fake_home / ".local" / "share" / "opencode" / "opencode.db"
        skills_dir = fake_home / ".claude" / "skills" / "daily-review"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("---\nname: daily-review\n---\n")

        import sqlite3 as _sqlite3

        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE session "
            "(id TEXT PRIMARY KEY, parent_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, directory TEXT)"
        )
        conn.execute(
            "CREATE TABLE message "
            "(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, data TEXT)"
        )
        ts_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("sess-1", None, ts_ms, ts_ms, "/tmp/proj"),
        )
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("msg-1", "sess-1", ts_ms, ts_ms, '{"role":"user"}'),
        )
        conn.commit()
        conn.close()

        with mock.patch.object(Path, "home", return_value=fake_home):
            result = scan(claude_dir=self.tmp, now_ts=time.time())

        opencode = result["by_source"]["opencode"]
        self.assertEqual(opencode["rollup"]["sessions"], 1)
        self.assertEqual(opencode["rollup"]["custom_skill_files_written"], 1)
        self.assertEqual(opencode["rollup"]["authored_skill_names"], ["daily-review"])
        self.assertEqual(len(opencode["daily"]), 1)
        self.assertEqual(opencode["daily"][0]["metrics"]["custom_skill_files_written"], 1)


class PiOrchestrationTests(unittest.TestCase):
    NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude_dir = self.tmp / "claude"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.pi_dir = self.tmp / "pi-sessions"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write_pi_session(self):
        path = self.pi_dir / "project" / "main.jsonl"
        path.parent.mkdir(parents=True)
        entries = [
            {
                "type": "session",
                "id": "private-session-id",
                "timestamp": "2026-07-15T10:00:00+00:00",
                "cwd": str(self.tmp / "workspace"),
            },
            {
                "type": "message",
                "timestamp": "2026-07-15T10:01:00+00:00",
                "message": {"role": "user", "content": "private prompt"},
            },
            {
                "type": "message",
                "timestamp": "2026-07-15T10:10:00+00:00",
                "message": {
                    "role": "assistant",
                    "model": "pi-model",
                    "content": [{"type": "text", "text": "private response"}],
                    "usage": {"input": 3, "output": 2, "totalTokens": 5},
                },
            },
        ]
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    def test_pi_source_is_emitted_when_session_dir_exists(self):
        self._write_pi_session()

        result = scan(
            claude_dir=self.claude_dir,
            pi_dir=self.pi_dir,
            now_ts=self.NOW,
        )

        pi = result["by_source"]["pi"]
        self.assertEqual(pi["rollup"]["sessions"], 1)
        self.assertEqual(pi["rollup"]["messages"], 2)

    def test_pi_source_is_omitted_when_session_dir_is_absent(self):
        result = scan(
            claude_dir=self.claude_dir,
            pi_dir=self.pi_dir,
            now_ts=self.NOW,
        )

        self.assertNotIn("pi", result["by_source"])

    def test_pi_scanner_failure_does_not_abort_other_sources(self):
        self.pi_dir.mkdir()
        claude_session = self.claude_dir / "projects" / "project" / "session.jsonl"
        claude_session.parent.mkdir(parents=True)
        claude_session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-07-15T10:00:00+00:00",
                    "message": {"role": "user", "content": "private prompt"},
                }
            )
            + "\n"
        )

        with mock.patch("scan_pi.scan", side_effect=RuntimeError("broken pi store")):
            result = scan(
                claude_dir=self.claude_dir,
                pi_dir=self.pi_dir,
                now_ts=self.NOW,
            )

        self.assertNotIn("pi", result["by_source"])
        self.assertEqual(result["by_source"]["claude_code"]["rollup"]["sessions"], 1)

    def test_pi_resolver_failure_does_not_abort_other_sources(self):
        host_home = self.tmp / "host-home"
        claude_session = host_home / ".claude" / "projects" / "project" / "session.jsonl"
        claude_session.parent.mkdir(parents=True)
        claude_session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-07-15T10:00:00+00:00",
                    "message": {"role": "user", "content": "private prompt"},
                }
            )
            + "\n"
        )

        with mock.patch("scan_transcripts._host_homes", return_value=[host_home]), \
             mock.patch(
                 "scan_transcripts._resolve_default_pi_dir",
                 side_effect=RuntimeError("broken Pi resolver"),
             ):
            result = scan(
                cowork_root=self.tmp / "no-cowork",
                scheduled_root=self.tmp / "no-scheduled",
                codex_dir=self.tmp / "no-codex",
                now_ts=self.NOW,
            )

        self.assertNotIn("pi", result["by_source"])
        self.assertEqual(result["by_source"]["claude_code"]["rollup"]["sessions"], 1)


class CursorOrchestrationTests(unittest.TestCase):
    """Integration tests verifying scan_cursor.scan() returns the expected
    envelope shape. The full orchestrator integration is tested at the
    scan_opencode / scan_cursor unit level; here we confirm the sub-scanner
    envelope matches the contract scan_transcripts.py expects.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _make_state_vscdb(self, db_path: Path, now_ts: float) -> None:
        """Create a minimal state.vscdb with one composer entry."""
        import sqlite3 as _sqlite3
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cursorDiskKV "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        created_ms = int(now_ts * 1000)
        last_ms = created_ms + 1000
        composer_doc = json.dumps({
            "createdAt": created_ms,
            "lastUpdatedAt": last_ms,
            "isAgentic": False,
            "fullConversationHeadersOnly": [{"id": "h1"}, {"id": "h2"}],
        })
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("composerData:comp-1", composer_doc),
        )
        conn.commit()
        conn.close()

    def test_cursor_source_envelope_with_valid_db(self):
        """scan_cursor.scan() with a valid db returns the expected envelope."""
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from scan_cursor import scan as scan_cursor_fn

        db_path = self.tmp / "state.vscdb"
        now_ts = time.time()
        self._make_state_vscdb(db_path, now_ts)

        result = scan_cursor_fn(
            cursor_db_path=db_path,
            cursor_home=self.tmp / ".cursor",
            workspace_storage_dir=self.tmp / "workspaceStorage",
            home=self.tmp,
            window_days=30,
            now_ts=now_ts,
        )

        self.assertEqual(result["source"], "cursor")
        self.assertIn("daily", result)
        self.assertIn("rollup", result)
        self.assertIsInstance(result["daily"], list)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["rollup"]["sessions"], 1)

    def test_cursor_empty_envelope_when_db_absent(self):
        """scan_cursor.scan() returns empty envelope when state.vscdb is missing."""
        from scan_cursor import scan as scan_cursor_fn

        result = scan_cursor_fn(
            cursor_db_path=self.tmp / "nonexistent.vscdb",
            cursor_home=self.tmp / ".cursor",
            workspace_storage_dir=self.tmp / "workspaceStorage",
            home=self.tmp,
            window_days=30,
        )

        self.assertEqual(result["source"], "cursor")
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["rollup"]["sessions"], 0)


class CodexOrchestrationTests(unittest.TestCase):
    """Integration tests verifying scan_transcripts.py credits on-disk Codex
    skills under ~/.codex/skills/<name>/ as authored (parity with Claude Code),
    even when no write for them was captured in the transcript window.
    """

    _CODEX_FIXTURES = Path(__file__).resolve().parents[4] / "test" / "fixtures" / "codex"
    _CODEX_NOW = datetime(2026, 4, 21, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude_dir = self.tmp / "claude"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.codex_dir = self.tmp / "codex"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _stage_codex_session(self) -> None:
        sessions = self.codex_dir / "sessions" / "2026" / "04" / "20"
        sessions.mkdir(parents=True)
        dest = sessions / "rollout-fixture_normal.jsonl"
        shutil.copy(self._CODEX_FIXTURES / "fixture_normal.jsonl", dest)
        os.utime(dest, (self._CODEX_NOW, self._CODEX_NOW))

    def test_seeds_authored_skill_names_from_on_disk_codex_skills(self):
        self._stage_codex_session()
        for name in ("codex-skill", "aiqrank"):
            skill_dir = self.codex_dir / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n")

        result = scan(
            claude_dir=self.claude_dir,
            codex_dir=self.codex_dir,
            now_ts=self._CODEX_NOW,
        )

        rollup = result["by_source"]["codex"]["rollup"]
        self.assertEqual(rollup["sessions"], 1)
        self.assertIn("codex-skill", rollup["authored_skill_names"])
        self.assertNotIn("aiqrank", rollup["authored_skill_names"])

    def _write_codex_session(self, name: str, events: list[dict | str]) -> Path:
        sessions = self.codex_dir / "sessions" / "2026" / "04" / "20"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"rollout-{name}.jsonl"
        with path.open("w") as fh:
            for event in events:
                fh.write(event if isinstance(event, str) else json.dumps(event))
                fh.write("\n")
        os.utime(path, (self._CODEX_NOW, self._CODEX_NOW))
        return path

    @staticmethod
    def _codex_event(payload: dict, *, event_type: str = "response_item") -> dict:
        return {
            "timestamp": "2026-04-20T12:00:00.000Z",
            "type": event_type,
            "payload": payload,
        }

    def test_modern_events_are_deduplicated_and_attributed(self):
        skill = self.codex_dir / "skills" / "review" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: review\n---\n")
        tool = lambda name, call_id, args="{}": self._codex_event(
            {"type": "function_call", "name": name, "call_id": call_id, "arguments": args}
        )
        output = lambda call_id, text="ok": self._codex_event(
            {"type": "function_call_output", "call_id": call_id, "output": text}
        )

        nested = (
            'const a = await tools.update_plan({plan: []});\n'
            'const b = await tools.exec_command({"cmd":"pwd"});\n'
            'const c = await tools.apply_patch("*** Begin Patch\\n'
            '*** Update File: AGENTS.md\\n*** End Patch");\n'
            'const d = await tools.mcp__context7__query_docs({query: "secret"});'
        )
        events = [
            self._codex_event({"type": "user_message", "message": "hello"}, event_type="event_msg"),
            self._codex_event({"type": "agent_reasoning", "text": "private"}, event_type="event_msg"),
            self._codex_event({"type": "reasoning", "summary": "also private"}),
            tool("spawn_agent", "spawn-1", json.dumps({"agent_type": "reviewer"})),
            tool("spawn_agent", "spawn-1", json.dumps({"agent_type": "reviewer"})),
            tool("update_plan", "plan-1"),
            tool("update_plan", "plan-1"),
            self._codex_event({"type": "custom_tool_call", "name": "exec", "call_id": "outer-1", "input": nested}),
            self._codex_event({
                "type": "sub_agent_activity", "event_id": "spawn-1",
                "agent_thread_id": "agent-a", "agent_path": "/root/a", "kind": "started",
                "occurred_at_ms": 1_776_681_600_000,
            }, event_type="event_msg"),
            self._codex_event({
                "type": "sub_agent_activity", "event_id": "spawn-1",
                "agent_thread_id": "agent-a", "agent_path": "/root/a", "kind": "started",
                "occurred_at_ms": 1_776_681_600_000,
            }, event_type="event_msg"),
            self._codex_event({
                "type": "sub_agent_activity", "event_id": "spawn-2",
                "agent_thread_id": "agent-b", "agent_path": "/root/b", "kind": "started",
                "occurred_at_ms": 1_776_681_601_000,
            }, event_type="event_msg"),
            tool("mcp__context7__query_docs", "mcp-1"),
            self._codex_event({
                "type": "mcp_tool_call_end", "call_id": "mcp-1",
                "invocation": {"server": "context7", "tool": "query_docs", "arguments": {"token": "secret"}},
                "result": {"Ok": {"content": []}},
            }, event_type="event_msg"),
            self._codex_event({
                "type": "mcp_tool_call_end", "call_id": "mcp-1",
                "invocation": {"server": "context7", "tool": "query_docs", "arguments": {"token": "secret"}},
                "result": {"Ok": {"content": []}},
            }, event_type="event_msg"),
            tool("exec_command", "skill-1", json.dumps({"cmd": f"sed -n '1,40p' {skill}"})),
            output("skill-1", "skill contents"),
        ]
        self._write_codex_session("modern", events)

        result = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)
        rollup = result["rollup"]

        self.assertEqual(rollup["sessions"], 1)
        self.assertEqual(rollup["tool_calls"], 8)
        self.assertEqual(rollup["parallel_agent_turns"], 2)
        self.assertEqual(rollup["max_parallel_agents"], 2)
        self.assertEqual(rollup["plan_mode_invocations"], 2)
        self.assertEqual(rollup["reasoning_blocks"], 2)
        self.assertEqual(rollup["mcp_server_counts"], {"context7": 1})
        self.assertEqual(rollup["skill_counts"], {"review": 1})
        self.assertEqual(rollup["file_changes"], 1)
        self.assertEqual(rollup["agents_md_writes"], 1)

    def test_broadcast_sub_agent_activity_is_deduplicated_across_sessions(self):
        spawn = self._codex_event({
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": "shared-spawn",
            "arguments": "{}",
        })
        activity = self._codex_event({
            "type": "sub_agent_activity",
            "event_id": "shared-spawn",
            "agent_thread_id": "agent-a",
            "agent_path": "/root/a",
            "kind": "started",
            "occurred_at_ms": 1_776_681_600_000,
        }, event_type="event_msg")
        self._write_codex_session("parent", [spawn, activity])
        self._write_codex_session("child", [activity])

        rollup = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)["rollup"]

        self.assertEqual(rollup["parallel_agent_turns"], 1)
        self.assertEqual(rollup["max_parallel_agents"], 1)
        self.assertEqual(rollup["sessions_with_orchestration"], 1)

    def test_broadcast_timestamp_does_not_inflate_agent_concurrency(self):
        def activity(event_id, thread_id, timestamp, occurred_at_ms):
            event = self._codex_event({
                "type": "sub_agent_activity",
                "event_id": event_id,
                "agent_thread_id": thread_id,
                "agent_path": f"/root/{thread_id}",
                "kind": "started",
                "occurred_at_ms": occurred_at_ms,
            }, event_type="event_msg")
            event["timestamp"] = timestamp
            return event

        first = activity(
            "spawn-1", "agent-a", "2026-04-20T12:00:00.000Z", 1_776_681_600_000
        )
        second = activity(
            "spawn-2", "agent-b", "2026-04-20T12:00:01.000Z", 1_776_681_601_000
        )
        broadcast_first = dict(first, timestamp="2026-04-20T14:00:00.000Z")
        broadcast_second = dict(second, timestamp="2026-04-20T14:00:00.000Z")
        self._write_codex_session("a-broadcast", [broadcast_first, broadcast_second])
        self._write_codex_session("z-original", [first, second])

        rollup = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)["rollup"]

        self.assertEqual(rollup["parallel_agent_turns"], 2)
        self.assertEqual(rollup["max_parallel_agents"], 1)

    def test_broadcast_mcp_completion_is_deduplicated_across_sessions(self):
        completion = self._codex_event({
            "type": "mcp_tool_call_end",
            "call_id": "shared-mcp",
            "invocation": {"server": "context7", "tool": "query_docs"},
            "result": {"Ok": {"content": []}},
        }, event_type="event_msg")
        self._write_codex_session("mcp-parent", [completion])
        self._write_codex_session("mcp-child", [completion])

        rollup = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)["rollup"]

        self.assertEqual(rollup["tool_calls"], 1)
        self.assertEqual(rollup["mcp_server_counts"], {"context7": 1})

    def test_skill_attribution_requires_existing_successful_read(self):
        good = self.codex_dir / "skills" / "good" / "SKILL.md"
        good.parent.mkdir(parents=True)
        good.write_text("---\nname: good\n---\n")
        nested = self.codex_dir / "skills" / "nested" / "SKILL.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("---\nname: nested\n---\n")
        missing = self.codex_dir / "skills" / "missing" / "SKILL.md"
        events = []
        for call_id, command, output in [
            ("good-1", f"cat {good}", "contents"),
            ("good-2", f"sed -n '1,20p' {good}", "contents again"),
            ("search", f"rg SKILL.md {good.parent}", str(good)),
            ("missing", f"cat {missing}", "No such file or directory"),
            ("failed", f"cat {good}", "Error: command failed"),
        ]:
            events.extend([
                self._codex_event({
                    "type": "function_call", "name": "exec_command", "call_id": call_id,
                    "arguments": json.dumps({"cmd": command}),
                }),
                self._codex_event({
                    "type": "function_call_output", "call_id": call_id, "output": output,
                }),
            ])
        events.append(self._codex_event({
            "type": "message", "role": "assistant", "content": f"Mention {missing} in prose only",
        }))
        events.extend([
            self._codex_event({
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "nested-read",
                "input": (
                    "const r = await tools.exec_command({"
                    f'"cmd":"sed -n \'1,40p\' {nested}"'
                    "});"
                ),
            }),
            self._codex_event({
                "type": "custom_tool_call_output",
                "call_id": "nested-read",
                "output": "skill contents",
            }),
        ])
        self._write_codex_session("skills", events)

        rollup = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)["rollup"]
        self.assertEqual(rollup["skill_counts"], {"good": 1, "nested": 1})

    def test_localized_malformed_record_omits_only_its_date(self):
        self._write_codex_session("good", [
            self._codex_event({"type": "user_message", "message": "good"}, event_type="event_msg")
        ])
        bad_path = self.codex_dir / "sessions" / "2026" / "04" / "19" / "rollout-bad.jsonl"
        bad_path.parent.mkdir(parents=True)
        bad_path.write_text(
            '{"timestamp":"2026-04-19T12:00:00Z","type":"event_msg","payload":BAD}\n'
        )
        os.utime(bad_path, (self._CODEX_NOW, self._CODEX_NOW))

        result = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)

        self.assertEqual([row["date"] for row in result["daily"]], ["2026-04-20"])
        self.assertEqual(result["completeness"], {
            "status": "partial", "omitted_dates": ["2026-04-19"], "failure_count": 1,
        })

    def test_unlocalizable_malformed_record_aborts_codex_source(self):
        self._write_codex_session("bad", ["not-json-and-no-timestamp"])

        result = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)

        self.assertEqual(result["daily"], [])
        self.assertEqual(result["completeness"]["status"], "failed")
        self.assertEqual(result["completeness"]["omitted_dates"], [])
        self.assertEqual(result["completeness"]["failure_count"], 1)

    def test_mtime_cursor_does_not_truncate_full_window_snapshot(self):
        self._write_codex_session("older-than-cursor", [
            self._codex_event(
                {"type": "user_message", "message": "hello"},
                event_type="event_msg",
            )
        ])

        result = scan_codex(
            self.codex_dir,
            now_ts=self._CODEX_NOW,
            mtime_after_ts=self._CODEX_NOW + 1,
        )

        self.assertEqual(result["rollup"]["sessions"], 1)
        self.assertEqual(result["rollup"]["user_messages"], 1)

    def test_output_is_aggregate_only_for_adversarial_payloads(self):
        secret_path = "/Users/private/project?token=top-secret"
        events = [
            self._codex_event({
                "type": "custom_tool_call", "name": "exec", "call_id": "private",
                "input": (
                    'const r = await tools.exec_command({"cmd":"API_TOKEN=top-secret env"});'
                    'await tools.apply_patch("*** Begin Patch\\n+top-secret\\n*** End Patch");'
                ),
            }),
            self._codex_event({
                "type": "mcp_tool_call_end", "call_id": "mcp-private",
                "invocation": {"server": "context7", "tool": "query", "arguments": {"url": secret_path}},
                "result": {"Ok": {}},
            }, event_type="event_msg"),
        ]
        self._write_codex_session("privacy", events)

        blob = json.dumps(scan_codex(self.codex_dir, now_ts=self._CODEX_NOW))
        for raw in ("top-secret", secret_path, "API_TOKEN", "*** Begin Patch"):
            self.assertNotIn(raw, blob)

    def test_missing_call_ids_fall_back_to_response_ordinals(self):
        self._write_codex_session("missing-ids", [
            self._codex_event({"type": "function_call", "name": "exec_command", "arguments": "{}"}),
            self._codex_event({"type": "function_call", "name": "exec_command", "arguments": "{}"}),
        ])

        rollup = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)["rollup"]
        self.assertEqual(rollup["tool_calls"], 2)

    def test_high_cardinality_labels_collapse_into_other_bucket(self):
        nested = "\n".join(
            f"await tools.tool_{index}({{}});" for index in range(60)
        )
        self._write_codex_session("labels", [
            self._codex_event({
                "type": "custom_tool_call", "name": "exec", "call_id": "outer", "input": nested,
            })
        ])

        counts = scan_codex(self.codex_dir, now_ts=self._CODEX_NOW)["rollup"]["tool_name_counts"]
        self.assertEqual(len(counts), 50)
        self.assertIn("__other__", counts)
        self.assertEqual(sum(counts.values()), 61)

    def test_file_change_while_reading_is_unlocalizable(self):
        path = self._write_codex_session("changing", [
            self._codex_event({"type": "user_message", "message": "hello"}, event_type="event_msg")
        ])
        original_stat = Path.stat
        path_stat_calls = 0

        def changing_stat(target, *args, **kwargs):
            nonlocal path_stat_calls
            stat = original_stat(target, *args, **kwargs)
            if target == path:
                path_stat_calls += 1
                if path_stat_calls >= 2:
                    values = list(stat)
                    values[6] += 1
                    return os.stat_result(values)
            return stat

        with mock.patch.object(Path, "stat", changing_stat):
            outcome = process_codex_session(path, {}, {})

        self.assertEqual(outcome["unlocalizable_failures"], 1)
        self.assertEqual(outcome["failure_count"], 1)


class SeedAuthoredHelperTests(unittest.TestCase):
    """Unit tests for the shared _seed_authored_into_latest_day writer used by
    the Claude, Codex, and OpenCode/Cursor seeding paths."""

    _NOW = datetime(2026, 4, 21, tzinfo=timezone.utc).timestamp()

    def test_unions_into_latest_day_deduped_and_sorted(self):
        day1 = datetime(2026, 4, 19).date()
        day2 = datetime(2026, 4, 20).date()
        daily = {
            day1: {"authored_skill_names": ["A"]},
            day2: {"authored_skill_names": ["B"]},
        }

        _seed_authored_into_latest_day(daily, {"A", "C"}, self._NOW)

        # "C" is new -> added to the latest day; "A" already lives on day1 so it
        # is NOT re-added; "B" (the target day's own name) is preserved; sorted.
        self.assertEqual(daily[day2]["authored_skill_names"], ["B", "C"])
        self.assertEqual(daily[day1]["authored_skill_names"], ["A"])

    def test_no_new_names_leaves_days_untouched(self):
        day = datetime(2026, 4, 20).date()
        daily = {day: {"authored_skill_names": ["A"]}}

        _seed_authored_into_latest_day(daily, {"A"}, self._NOW)

        self.assertEqual(daily[day]["authored_skill_names"], ["A"])


class WindowsPathAndDualScanTests(unittest.TestCase):
    """U2: cross-platform Cowork path resolution + dual-directory scan + dedup + diagnostic log."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Two cowork roots simulating the in-flight Anthropic rename:
        #   claude-code-sessions (post-rename, iterated first, wins dedup)
        #   local-agent-mode-sessions (legacy)
        self.new_root = self.tmp / "claude-code-sessions"
        self.old_root = self.tmp / "local-agent-mode-sessions"
        # The relative tail used in tests:
        # acct-uuid/ws-uuid/local_sess-uuid/.claude/projects/some-proj/conv1.jsonl
        self.rel_tail = (
            Path("acct-uuid")
            / "ws-uuid"
            / "local_sess-uuid"
            / ".claude"
            / "projects"
            / "some-proj"
        )
        # Diagnostic log lands under HOME/.config/aiqrank/hook.log; redirect HOME.
        self._home_patch = mock.patch.dict(
            os.environ, {"HOME": str(self.tmp)}, clear=False
        )
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        shutil.rmtree(self.tmp)

    def _write_session(self, root: Path, name: str, events: list[dict]) -> Path:
        full = root / self.rel_tail / name
        write_jsonl(full, events)
        return full

    def test_default_cowork_roots_macos_returns_both_directory_names(self):
        from scan_transcripts import _default_cowork_roots
        with mock.patch.object(sys, "platform", "darwin"):
            roots = _default_cowork_roots()
        self.assertEqual(len(roots), 2)
        self.assertIn("claude-code-sessions", str(roots[0]))
        self.assertIn("local-agent-mode-sessions", str(roots[1]))
        # claude-code-sessions iterates first so post-rename wins dedup.
        self.assertTrue(str(roots[0]).endswith("claude-code-sessions"))
        # macOS base path.
        self.assertIn("Library/Application Support/Claude", str(roots[0]))

    def test_host_home_override_resolves_mounted_cowork_data(self):
        # Cowork can mount the user's home somewhere inside the VM while
        # Path.home() still points at the sandbox user. AIQRANK_HOST_HOME
        # tells the scanner where the mounted host home lives.
        sandbox_home = self.tmp / "sandbox-home"
        host_home = self.tmp / "mounted-host-home"
        mounted_root = (
            host_home
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude-code-sessions"
        )
        rel_tail = (
            Path("acct-uuid")
            / "ws-uuid"
            / "local_sess-uuid"
            / ".claude"
            / "projects"
            / "some-proj"
        )
        write_jsonl(mounted_root / rel_tail / "conv1.jsonl", [make_user_msg("hello")])

        with mock.patch.dict(
            os.environ,
            {"HOME": str(sandbox_home), "AIQRANK_HOST_HOME": str(host_home)},
            clear=False,
        ), mock.patch.object(sys, "platform", "darwin"):
            result = scan()

        cowork_r = cowork_block(result)["rollup"]
        self.assertEqual(cowork_r["cowork_sessions"], 1)
        self.assertEqual(cowork_r["cowork_messages"], 1)

    def test_default_cowork_roots_windows_returns_appdata_paths(self):
        from scan_transcripts import _default_cowork_roots
        with mock.patch.object(sys, "platform", "win32"):
            roots = _default_cowork_roots()
        self.assertEqual(len(roots), 2)
        # Windows base: AppData/Roaming/Claude. Path is platform-aware so
        # this resolves with whatever separator the host uses.
        self.assertIn("AppData", str(roots[0]))
        self.assertIn("Roaming", str(roots[0]))
        self.assertIn("Claude", str(roots[0]))
        self.assertTrue(str(roots[0]).endswith("claude-code-sessions"))
        self.assertTrue(str(roots[1]).endswith("local-agent-mode-sessions"))

    def test_windows_userprofile_candidate_resolves_mounted_cowork_data(self):
        sandbox_home = self.tmp / "sandbox-home"
        host_home = self.tmp / "windows-host-home"
        mounted_root = host_home / "AppData" / "Roaming" / "Claude" / "claude-code-sessions"
        write_jsonl(mounted_root / self.rel_tail / "conv1.jsonl", [make_user_msg("hello")])

        with mock.patch.dict(
            os.environ,
            {"HOME": str(sandbox_home), "USERPROFILE": str(host_home)},
            clear=False,
        ), mock.patch.object(sys, "platform", "win32"):
            result = scan()

        cowork_r = cowork_block(result)["rollup"]
        self.assertEqual(cowork_r["cowork_sessions"], 1)
        self.assertEqual(cowork_r["cowork_messages"], 1)

    def test_default_cowork_roots_linux_returns_xdg_config(self):
        from scan_transcripts import _default_cowork_roots
        with mock.patch.object(sys, "platform", "linux"):
            roots = _default_cowork_roots()
        self.assertEqual(len(roots), 2)
        self.assertIn(".config/Claude", str(roots[0]).replace("\\", "/"))

    def test_linux_home_candidate_roots_use_xdg_config_shape(self):
        from scan_transcripts import _default_cowork_roots_for_home

        with mock.patch.object(sys, "platform", "linux"):
            roots = _default_cowork_roots_for_home(Path("/home/alex"))

        self.assertEqual(
            str(roots[0]).replace("\\", "/"),
            "/home/alex/.config/Claude/claude-code-sessions",
        )
        self.assertEqual(
            str(roots[1]).replace("\\", "/"),
            "/home/alex/.config/Claude/local-agent-mode-sessions",
        )

    def test_dual_directory_scan_dedups_same_relative_path(self):
        # Same JSONL exists under both roots (Anthropic mid-migration shape).
        # First-iterated root (claude-code-sessions) wins; the legacy copy
        # is skipped. Result: events counted once, not twice.
        events = [make_user_msg("hello")]
        self._write_session(self.new_root, "conv1.jsonl", events)
        self._write_session(self.old_root, "conv1.jsonl", events)

        result = scan(
            claude_dir=self.tmp / "claude-home",  # disables interactive scan
            cowork_root=[self.new_root, self.old_root],
        )
        cowork_r = cowork_block(result)["rollup"]
        # Exactly one cowork session, not two.
        self.assertEqual(cowork_r["cowork_sessions"], 1)

    def test_dual_directory_scan_distinct_paths_both_contribute(self):
        # Different relative paths under each root → both contribute.
        events = [make_user_msg("hello")]
        # New root has session A
        full_a = self.new_root / self.rel_tail / "conv-a.jsonl"
        write_jsonl(full_a, events)
        # Old root has DIFFERENT session B (different uuid path)
        rel_b = (
            Path("acct-uuid")
            / "ws-uuid"
            / "local_other-uuid"
            / ".claude"
            / "projects"
            / "some-proj"
        )
        full_b = self.old_root / rel_b / "conv-b.jsonl"
        write_jsonl(full_b, events)

        result = scan(
            claude_dir=self.tmp / "claude-home",
            cowork_root=[self.new_root, self.old_root],
        )
        cowork_r = cowork_block(result)["rollup"]
        self.assertEqual(cowork_r["cowork_sessions"], 2)

    def test_local_agent_audit_jsonl_shape_is_counted(self):
        # Cowork on Windows (and Mac without a VM bundle) writes its own
        # audit log directly under local_*/ as `.audit.jsonl`, with
        # `_audit_timestamp` instead of `timestamp`. Verify both the glob
        # and the timestamp fallback handle this shape end-to-end.
        audit_path = (
            self.new_root
            / "acct-uuid"
            / "ws-uuid"
            / "local_sess-uuid"
            / ".audit.jsonl"
        )
        write_jsonl(
            audit_path,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Hello from Windows"},
                    "_audit_timestamp": datetime.now().isoformat(),
                }
            ],
        )

        result = scan(
            claude_dir=self.tmp / "claude-home",
            cowork_root=[self.new_root],
        )
        cowork_r = cowork_block(result)["rollup"]
        self.assertEqual(cowork_r["cowork_sessions"], 1)
        self.assertEqual(cowork_r["cowork_messages"], 1)

    def test_legacy_single_path_cowork_root_still_works(self):
        # Backwards compat shim: a single Path is normalized to a list.
        events = [make_user_msg("hello")]
        self._write_session(self.new_root, "conv1.jsonl", events)
        result = scan(
            claude_dir=self.tmp / "claude-home",
            cowork_root=self.new_root,  # legacy single-path form
        )
        cowork_r = cowork_block(result)["rollup"]
        self.assertEqual(cowork_r["cowork_sessions"], 1)

    def test_diagnostic_log_records_per_root_resolution(self):
        events = [make_user_msg("hello")]
        self._write_session(self.new_root, "conv1.jsonl", events)
        # old_root deliberately doesn't exist
        scan(
            claude_dir=self.tmp / "claude-home",
            cowork_root=[self.new_root, self.tmp / "nonexistent-root"],
        )
        log = (self.tmp / ".config" / "aiqrank" / "hook.log").read_text()
        # Both roots get a log line: one True/file_count>=1, one False/0.
        self.assertIn("cowork_root_path resolved=", log)
        self.assertIn("exists=True", log)
        self.assertIn("exists=False", log)
        self.assertIn("file_count=1", log)

    def test_count_scheduled_task_runs_dedup_across_roots(self):
        from scan_transcripts import _count_scheduled_task_runs
        # Manifest with same relative path under both roots.
        manifest_data = {
            "initialMessage": "<scheduled-task name=\"daily-summary\">go</scheduled-task>",
            "createdAt": int(time.time() * 1000),
        }
        new_manifest = self.new_root / "acct-uuid" / "ws-uuid" / "local_sess.json"
        old_manifest = self.old_root / "acct-uuid" / "ws-uuid" / "local_sess.json"
        new_manifest.parent.mkdir(parents=True, exist_ok=True)
        old_manifest.parent.mkdir(parents=True, exist_ok=True)
        new_manifest.write_text(json.dumps(manifest_data))
        old_manifest.write_text(json.dumps(manifest_data))

        cutoff_ts = time.time() - 30 * 86400
        distinct = _count_scheduled_task_runs(
            [self.new_root, self.old_root],
            daily={},
            cutoff_ts=cutoff_ts,
            mtime_after_ts=None,
        )
        # Single distinct task name despite two physical manifests.
        self.assertEqual(len(distinct), 1)

    def test_main_host_home_flag_overrides_sandbox_home(self):
        host_home = self.tmp / "mounted-host-home-cli"
        mounted_root = (
            host_home
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude-code-sessions"
        )
        write_jsonl(mounted_root / self.rel_tail / "conv1.jsonl", [make_user_msg("hello")])

        from scan_transcripts import main

        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.dict(os.environ, {"HOME": str(self.tmp / "sandbox-home")}, clear=False), \
             mock.patch("sys.stdout") as stdout:
            self.assertEqual(main(["--days", "30", "--host-home", str(host_home)]), 0)

        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        result = json.loads(output)
        self.assertEqual(cowork_block(result)["rollup"]["cowork_sessions"], 1)


class ProjectLocalSkillsTests(unittest.TestCase):
    """Project-local custom skills (under `<cwd>/.claude/skills/`) must be
    enumerated so slash-command invocations like `/prune-branches` get
    credited even when the skill isn't installed globally at
    `~/.claude/skills/`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _make_skill(self, repo: Path, name: str) -> None:
        skill_dir = repo / ".claude" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n")

    def _make_project_with_cwd(
        self, claude_dir: Path, project_name: str, cwd: Path, events: list[dict]
    ) -> Path:
        jsonl = claude_dir / "projects" / project_name / "sess.jsonl"
        stamped = [{**e, "cwd": str(cwd)} for e in events]
        write_jsonl(jsonl, stamped)
        return jsonl

    def test_enumerates_project_local_skills_from_cwd(self):
        from scan_transcripts import _project_local_claude_skills

        repo = self.tmp / "repo"
        repo.mkdir()
        self._make_skill(repo, "prune-branches")
        self._make_skill(repo, "red-team-architecture")

        claude_dir = self.tmp / ".claude"
        self._make_project_with_cwd(
            claude_dir, "-tmp-repo", repo, [make_user_msg("hello")]
        )

        names = _project_local_claude_skills(claude_dir)
        self.assertEqual(names, {"prune-branches", "red-team-architecture"})

    def test_missing_cwd_field_is_ignored(self):
        from scan_transcripts import _project_local_claude_skills

        claude_dir = self.tmp / ".claude"
        jsonl = claude_dir / "projects" / "-tmp-orphan" / "sess.jsonl"
        write_jsonl(jsonl, [make_user_msg("no cwd here")])

        self.assertEqual(_project_local_claude_skills(claude_dir), set())

    def test_stale_cwd_skipped_without_error(self):
        from scan_transcripts import _project_local_claude_skills

        claude_dir = self.tmp / ".claude"
        jsonl = claude_dir / "projects" / "-tmp-gone" / "sess.jsonl"
        write_jsonl(
            jsonl,
            [{**make_user_msg("hi"), "cwd": "/nonexistent/repo/that/was/deleted"}],
        )

        self.assertEqual(_project_local_claude_skills(claude_dir), set())

    def test_aiqrank_skill_excluded(self):
        from scan_transcripts import _project_local_claude_skills

        repo = self.tmp / "repo"
        repo.mkdir()
        self._make_skill(repo, "aiqrank")
        self._make_skill(repo, "real-skill")

        claude_dir = self.tmp / ".claude"
        self._make_project_with_cwd(
            claude_dir, "-tmp-repo", repo, [make_user_msg("hello")]
        )

        self.assertEqual(
            _project_local_claude_skills(claude_dir), {"real-skill"}
        )

    def test_cwd_in_later_event_is_found(self):
        """Early events (queue-operation, etc.) often lack `cwd`. The scanner
        must look past them — up to the first 10 events of a transcript."""
        from scan_transcripts import _project_local_claude_skills

        repo = self.tmp / "repo"
        repo.mkdir()
        self._make_skill(repo, "foo")

        claude_dir = self.tmp / ".claude"
        jsonl = claude_dir / "projects" / "-tmp-repo" / "sess.jsonl"
        events = [
            {"type": "queue-operation", "operation": "enqueue"},
            {"type": "queue-operation", "operation": "dequeue"},
            {**make_user_msg("real event"), "cwd": str(repo)},
        ]
        write_jsonl(jsonl, events)

        self.assertEqual(_project_local_claude_skills(claude_dir), {"foo"})

    def test_slash_command_credited_when_skill_is_project_local(self):
        """End-to-end: a project-local skill invoked via `/foo` text shows
        up in `skill_counts`, even with `~/.claude/skills/` empty."""
        repo = self.tmp / "repo"
        repo.mkdir()
        self._make_skill(repo, "prune-branches")

        claude_dir = self.tmp / ".claude"
        (claude_dir / "projects" / "-tmp-repo").mkdir(parents=True)
        jsonl = claude_dir / "projects" / "-tmp-repo" / "sess.jsonl"
        write_jsonl(
            jsonl,
            [
                {**make_user_msg("<command-name>/prune-branches</command-name>"),
                 "cwd": str(repo)},
            ],
        )

        result = scan(claude_dir=claude_dir)
        rollup = claude_block(result)["rollup"]
        self.assertEqual(rollup["skill_counts"].get("prune-branches"), 1)


def make_assistant(model=None, output_tokens=0, content=None):
    msg: dict = {"content": content if content is not None else []}
    if model is not None:
        msg["model"] = model
    if output_tokens:
        msg["usage"] = {"output_tokens": output_tokens}
    return {"type": "assistant", "message": msg}


class ModelAndPrSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        (self.projects / "proj1").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rollup(self, result):
        return claude_block(result)["rollup"]

    def test_main_model_captured_verbatim_with_output_tokens(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("hi"),
                make_assistant(model="claude-opus-4-6", output_tokens=120),
                make_assistant(model="claude-opus-4-6", output_tokens=80),
                make_assistant(model="claude-sonnet-4-6", output_tokens=40),
            ],
        )
        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["model_usage"], {"claude-opus-4-6": 2, "claude-sonnet-4-6": 1})
        self.assertEqual(r["model_tokens_out"]["claude-opus-4-6"], 200)
        self.assertEqual(r["model_tokens_out"]["claude-sonnet-4-6"], 40)
        self.assertEqual(r["agent_model_usage"], {})

    def test_subagent_model_kept_out_of_main_mix(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [make_user_msg("hi"), make_assistant(model="claude-opus-4-6", output_tokens=10)],
        )
        write_jsonl(
            self.projects / "proj1" / "sessA" / "subagents" / "agent-xyz.jsonl",
            [make_assistant(model="claude-haiku-4-5", output_tokens=5)],
        )
        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["model_usage"], {"claude-opus-4-6": 1})
        self.assertEqual(r["agent_model_usage"], {"claude-haiku-4-5": 1})
        # Subagent output tokens are not attributed to the user's per-model mix.
        self.assertNotIn("claude-haiku-4-5", r["model_tokens_out"])

    def test_prs_opened_counts_create_once_ignores_body_and_near_miss(self):
        heredoc_pr = (
            "gh pr create --title x --body \"$(cat <<'EOF'\n"
            "## Summary\nthis body mentions gh pr create on purpose\nEOF\n)\""
        )
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("ship it"),
                make_tool_call("Bash", {"command": heredoc_pr}),
                make_tool_call("Bash", {"command": "gh pr view 123"}),
                make_tool_call("Bash", {"command": "git push"}),
            ],
        )
        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["prs_opened"], 1)

    def test_prs_opened_matches_whitespace_padded_invocation(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("ship it"),
                make_tool_call("Bash", {"command": "gh   pr   create --fill"}),
            ],
        )
        r = self.rollup(scan(claude_dir=self.tmp))
        self.assertEqual(r["prs_opened"], 1)

    def test_main_only_autonomy_counters_exclude_subagent(self):
        write_jsonl(
            self.projects / "proj1" / "sessA.jsonl",
            [
                make_user_msg("do the thing"),
                make_tool_call("Read"),
                make_tool_call("Edit", {"file_path": "/x/y.ex"}),
            ],
        )
        write_jsonl(
            self.projects / "proj1" / "sessA" / "subagents" / "agent-xyz.jsonl",
            [make_user_msg("(tool result)"), make_tool_call("Grep"), make_tool_call("Read")],
        )
        r = self.rollup(scan(claude_dir=self.tmp))
        # Cross-source counters include the subagent; main-only ones don't.
        self.assertEqual(r["tool_calls"], 4)
        self.assertEqual(r["main_tool_calls"], 2)
        self.assertEqual(r["main_user_messages"], 1)


if __name__ == "__main__":
    unittest.main()
