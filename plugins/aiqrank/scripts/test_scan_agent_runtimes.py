#!/usr/bin/env python3
"""Tests for Hermes, OpenClaw, and NanoClaw aggregate scanners."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_agent_runtimes import (  # noqa: E402
    AgentRuntimeScanIncomplete,
    _max_overlapping,
    scan_hermes,
    scan_nanoclaw,
    scan_openclaw,
)
from scan_transcripts import scan as scan_all  # noqa: E402


NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc).timestamp()


class AgentRuntimeScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_hermes_separates_interactive_cron_and_child_sessions(self):
        path = self.root / "state.db"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE sessions (
              id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
              message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
              reasoning_tokens INTEGER, model TEXT, model_config TEXT, end_reason TEXT
            );
            CREATE TABLE messages (
              session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
              tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
              timestamp REAL, reasoning_content TEXT, active INTEGER DEFAULT 1
            );
            """
        )
        rows = [
            ("human", "cli", None, NOW - 300, NOW - 100, 4, 9, 10, 20, 2, 1, 4, "model-a", "{}", None),
            ("cron", "cron", None, NOW - 200, NOW - 150, 2, 1, 3, 4, 0, 0, 0, "model-a", "{}", "cron_complete"),
            ("cron-orphan", "cron", None, NOW - 210, NOW - 160, 2, 0, 3, 4, 0, 0, 0, "model-a", "{}", "ws_orphan_reap"),
            ("webhook", "webhook", None, NOW - 205, NOW - 155, 0, 0, 0, 0, 0, 0, 0, "model-a", "{}", "webhook_complete"),
            ("child-a", "subagent", "human", NOW - 180, NOW - 140, 2, 1, 1, 2, 0, 0, 0, "model-b", "{}", None),
            ("child-b", "subagent", "human", NOW - 170, NOW - 130, 2, 1, 1, 2, 0, 0, 0, "model-b", "{}", None),
            ("continued", "cli", "human", NOW - 120, NOW - 80, 2, 0, 1, 1, 0, 0, 0, "model-a", "{}", None),
            ("branch", "cli", "human", NOW - 110, NOW - 70, 2, 0, 1, 1, 0, 0, 0, "model-a", '{"_branched_from":"human"}', None),
            ("branched-parent", "cli", None, NOW - 115, NOW - 75, 0, 0, 0, 0, 0, 0, 0, "model-a", "{}", "branched"),
            ("branch-alt", "cli", "branched-parent", NOW - 105, NOW - 65, 1, 0, 0, 0, 0, 0, 0, "model-a", "{}", None),
            ("old-active", "cli", None, NOW - 40 * 86400, None, 2, 0, 99, 99, 0, 0, 0, "model-a", "{}", None),
            ("empty", "acp", None, NOW - 50, NOW - 40, 0, 0, 0, 0, 0, 0, 0, "model-a", "{}", None),
        ]
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("ALTER TABLE sessions ADD COLUMN rewind_count INTEGER DEFAULT 0")
        db.execute("UPDATE sessions SET rewind_count = 2 WHERE id = 'human'")

        def call(call_id, name, arguments=None):
            function = {"name": name}
            if arguments is not None:
                function["arguments"] = json.dumps(arguments)
            return {"id": call_id, "function": function}

        human_calls = [
            call("mcp", "mcp__demo__search"),
            call("skill-ok", "skill_view", {"name": "review"}),
            call("skill-failed", "skill_view", {"name": "ignored"}),
            call("manage", "skill_manage", {"action": "create", "name": "authored"}),
            call("manage-local", "skill_manage", {"action": "patch", "name": "local-authored"}),
            call("manage-external", "skill_manage", {"action": "patch", "name": "bundled"}),
            call("manage-staged", "skill_manage", {"action": "create", "name": "not-created"}),
            call("authored-1", "skill_view", {"name": "authored"}),
            call("authored-2", "skill_view", {"name": "authored"}),
            call("config", "write_file", {"path": "/tmp/.mcp.json"}),
        ]
        db.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("human", "user", "fabricated request", None, None, None, None, NOW - 290, None, 1),
                ("human", "assistant", None, None, json.dumps(human_calls), None, None, NOW - 280, "fabricated reasoning", 1),
                ("human", "tool", '{"success":true}', "mcp", None, "mcp__demo__search", None, NOW - 279, None, 1),
                ("human", "tool", '{"success":true}', "skill-ok", None, "skill_view", None, NOW - 278, None, 1),
                ("human", "tool", "private non-json failure", "skill-failed", None, "skill_view", None, NOW - 277, None, 1),
                ("human", "tool", '{"success":true,"path":"authored"}', "manage", None, "skill_manage", None, NOW - 276, None, 1),
                ("human", "tool", json.dumps({"success": True, "path": str(self.root / "skills" / "local-authored" / "SKILL.md")}), "manage-local", None, "skill_manage", None, NOW - 275.8, None, 1),
                ("human", "tool", '{"success":true,"path":"/bundled/skills/bundled/SKILL.md"}', "manage-external", None, "skill_manage", None, NOW - 275.6, None, 1),
                ("human", "tool", '{"success":true,"staged":true}', "manage-staged", None, "skill_manage", None, NOW - 275.5, None, 1),
                ("human", "tool", '{"success":true}', "authored-1", None, "skill_view", None, NOW - 275, None, 1),
                ("human", "tool", '{"success":true}', "authored-2", None, "skill_view", None, NOW - 274, None, 1),
                ("human", "tool", '{"success":true,"private":"never returned"}', "config", None, "write_file", None, NOW - 273, None, 1),
                ("cron", "user", None, None, None, None, None, NOW - 190, None, 1),
                ("cron", "assistant", None, None, json.dumps([call("cron-mcp", "mcp__cron__run")]), None, None, NOW - 185, "automated reasoning", 1),
                ("cron", "tool", '{"success":true}', "cron-mcp", None, "mcp__cron__run", None, NOW - 184, None, 1),
                ("child-a", "assistant", None, None, json.dumps([
                    call("skill-ok", "skill_view", {"name": "wrong-session"}),
                    call("child-skill", "skill_view", {"name": "child-review"}),
                ]), None, None, NOW - 170, "child reasoning", 1),
                ("child-a", "tool", '{"success":false}', "skill-ok", None, "skill_view", None, NOW - 169.5, None, 1),
                ("child-a", "tool", '{"success":true}', "child-skill", None, "skill_view", None, NOW - 169, None, 1),
                ("continued", "user", None, None, None, None, None, NOW - 100, None, 1),
                ("branch", "user", None, None, None, None, None, NOW - 250, None, 1),
                ("branch", "user", None, None, None, None, None, NOW - 90, None, 1),
                ("branch-alt", "user", None, None, None, None, None, NOW - 65, None, 1),
                ("old-active", "user", None, None, None, None, None, NOW - 60, None, 1),
                ("human", "user", "inactive private request", None, None, None, None, NOW - 59, None, 0),
                ("human", "assistant", None, None, json.dumps([call("inactive", "skill_view", {"name": "inactive"})]), None, None, NOW - 58, "inactive reasoning", 0),
                ("human", "tool", '{"success":true}', "inactive", None, "skill_view", None, NOW - 57, None, 0),
            ],
        )
        db.commit()
        db.close()

        metrics = scan_hermes(path, now_ts=NOW)["rollup"]

        self.assertEqual(metrics["sessions"], 4)
        self.assertEqual(metrics["scheduled_task_runs"], 2)
        self.assertEqual(metrics["queue_events"], 3)
        self.assertEqual(metrics["sessions_with_orchestration"], 1)
        self.assertEqual(metrics["parallel_agent_turns"], 1)
        self.assertEqual(metrics["max_parallel_agents"], 2)
        self.assertEqual(metrics["mcp_server_counts"], {"demo": 1})
        self.assertEqual(metrics["tool_calls"], 13)
        self.assertEqual(
            metrics["skill_counts"],
            {"authored": 2, "child-review": 1, "review": 1},
        )
        self.assertEqual(metrics["authored_skill_names"], ["authored", "local-authored"])
        self.assertEqual(metrics["custom_mcp_config_writes"], 1)
        self.assertEqual(metrics["reasoning_blocks"], 1)
        self.assertEqual(metrics["user_corrections"], 2)
        self.assertEqual(metrics["user_messages"], 5)
        self.assertLess(metrics["tokens_input"], 99)
        self.assertNotIn("private", json.dumps(metrics))

        incremental = scan_hermes(path, now_ts=NOW, mtime_after_ts=NOW - 95)["rollup"]
        self.assertEqual(incremental["sessions"], 3)
        self.assertEqual(incremental["user_messages"], 3)
        self.assertEqual(incremental["skill_counts"], {})

    def test_parallelism_counts_overlap_not_total_children(self):
        self.assertEqual(_max_overlapping([(0, 5), (5, 10)]), 1)
        self.assertEqual(_max_overlapping([(0, 6), (5, 10)]), 2)

    def test_hermes_rejects_oversized_tool_events(self):
        path = self.root / "state.db"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE sessions (
              id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
              message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
              reasoning_tokens INTEGER, model TEXT, model_config TEXT, end_reason TEXT
            );
            CREATE TABLE messages (
              session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
              tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
              timestamp REAL, active INTEGER DEFAULT 1
            );
            """
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("human", "cli", None, NOW - 60, NOW, 2, 1, 0, 0, 0, 0, 0, "model", "{}", None),
        )
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            ("human", "tool", "x" * 20, "call", None, "tool", None, NOW - 30, 1),
        )
        db.commit()
        db.close()

        with patch("scan_agent_runtimes.MAX_EVENT_JSON_BYTES", 10):
            with self.assertRaises(AgentRuntimeScanIncomplete):
                scan_hermes(path, now_ts=NOW)

        with patch("scan_agent_runtimes.MAX_SQLITE_TEXT_BYTES", 3):
            with self.assertRaises(AgentRuntimeScanIncomplete):
                scan_hermes(path, now_ts=NOW)

    def test_openclaw_uses_typed_provenance_without_returning_event_content(self):
        path = self.root / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE session_nodes (
              session_key TEXT, current_session_id TEXT, entry_json TEXT, updated_at INTEGER,
              created_via TEXT, created_actor_type TEXT, parent_session_key TEXT
            );
            CREATE TABLE session_windows (
              session_id TEXT, session_key TEXT, started_at INTEGER, ended_at INTEGER,
              created_at INTEGER, updated_at INTEGER, parent_session_key TEXT, spawned_by TEXT
            );
            CREATE TABLE transcript_events (
              session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER
            );
            CREATE TABLE session_transcript_active_events (
              session_id TEXT, event_seq INTEGER, active_position INTEGER
            );
            CREATE TABLE session_transcript_index_state (
              session_id TEXT, needs_rebuild INTEGER
            );
            """
        )
        for key, via, actor, parent, offset in (
            ("agent:main:direct", "channel", "human", None, 400),
            ("agent:main:cron:daily", "cron", "system", None, 300),
            ("agent:main:subagent:a", "spawn", "agent", "agent:main:direct", 200),
        ):
            start = int((NOW - offset) * 1000)
            session_id = key.rsplit(":", 1)[-1]
            db.execute("INSERT INTO session_nodes VALUES (?,?,?,?,?,?,?)", (key, session_id, "{}", start, via, actor, parent))
            db.execute("INSERT INTO session_windows VALUES (?,?,?,?,?,?,?,?)", (session_id, key, start, start + 60_000, start, start + 60_000, parent, parent))
        event = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "mcp__browser__open", "input": {"private": "never returned"}}],
                "usage": {"input": 5, "output": 7},
            },
        }
        db.execute("INSERT INTO transcript_events VALUES (?,?,?,?)", ("direct", 1, json.dumps(event), int((NOW - 390) * 1000)))
        db.execute("INSERT INTO session_transcript_active_events VALUES (?,?,?)", ("direct", 1, 1))
        db.execute("INSERT INTO session_transcript_index_state VALUES (?,?)", ("direct", 0))
        inactive = {**event, "message": {**event["message"], "content": [{"type": "tool_use", "name": "inactive_tool"}]}}
        db.execute("INSERT INTO transcript_events VALUES (?,?,?,?)", ("direct", 2, json.dumps(inactive), int((NOW - 380) * 1000)))
        db.commit()
        db.close()

        result = scan_openclaw(self.root, now_ts=NOW)
        metrics = result["rollup"]

        self.assertEqual(metrics["sessions"], 1)
        self.assertEqual(metrics["scheduled_task_runs"], 1)
        self.assertEqual(metrics["sessions_with_orchestration"], 1)
        self.assertEqual(metrics["tool_name_counts"], {"mcp__browser__open": 1})
        self.assertNotIn("private", json.dumps(result))

        db = sqlite3.connect(path)
        db.execute(
            "UPDATE session_transcript_index_state SET needs_rebuild = 1 WHERE session_id = ?",
            ("direct",),
        )
        db.commit()
        db.close()
        with self.assertRaises(AgentRuntimeScanIncomplete):
            scan_openclaw(self.root, now_ts=NOW)

    def test_openclaw_scheduled_completion_credit_requires_terminal_window(self):
        path = self.root / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE session_nodes (
              session_key TEXT, current_session_id TEXT, entry_json TEXT, updated_at INTEGER,
              created_via TEXT, created_actor_type TEXT, parent_session_key TEXT
            );
            CREATE TABLE session_windows (
              session_id TEXT, session_key TEXT, started_at INTEGER, ended_at INTEGER,
              created_at INTEGER, updated_at INTEGER, parent_session_key TEXT, spawned_by TEXT
            );
            CREATE TABLE transcript_events (
              session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER
            );
            CREATE TABLE session_transcript_active_events (
              session_id TEXT, event_seq INTEGER, active_position INTEGER
            );
            CREATE TABLE session_transcript_index_state (
              session_id TEXT, needs_rebuild INTEGER
            );
            """
        )
        # One cron window with a recorded terminal end and one still in
        # flight (ended_at NULL): both are attempts, only the terminal one
        # earns completion credit.
        for key, ended_offset, offset in (
            ("agent:main:cron:done", 60_000, 400),
            ("agent:main:cron:inflight", None, 300),
        ):
            start = int((NOW - offset) * 1000)
            ended = start + ended_offset if ended_offset is not None else None
            session_id = key.rsplit(":", 1)[-1]
            db.execute("INSERT INTO session_nodes VALUES (?,?,?,?,?,?,?)", (key, session_id, "{}", start, "cron", "system", None))
            db.execute("INSERT INTO session_windows VALUES (?,?,?,?,?,?,?,?)", (session_id, key, start, ended, start, start + 60_000, None, None))
        db.commit()
        db.close()

        metrics = scan_openclaw(self.root, now_ts=NOW)["rollup"]

        self.assertEqual(metrics["queue_events"], 2)
        self.assertEqual(metrics["scheduled_task_runs"], 1)

    def test_nanoclaw_counts_channel_sessions_and_task_messages_separately(self):
        central = self.root / "data" / "v2.db"
        central.parent.mkdir(parents=True)
        db = sqlite3.connect(central)
        db.execute(
            """
            CREATE TABLE sessions (
              id TEXT, agent_group_id TEXT, messaging_group_id TEXT, thread_id TEXT,
              created_at TEXT, last_active TEXT
            )
            """
        )
        stamp = datetime.fromtimestamp(NOW - 300, tz=timezone.utc).isoformat()
        old_stamp = datetime.fromtimestamp(NOW - 40 * 86400, tz=timezone.utc).isoformat()
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", ("chat", "group", "channel", "thread", stamp, stamp))
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", ("tasks", "group", None, "system:tasks", stamp, stamp))
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            ("chat-old", "group", "channel", "thread-old", old_stamp, stamp),
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            ("agent-shared", "group", None, None, stamp, stamp),
        )
        db.commit()
        db.close()

        for session_id, kind, series in (
            ("chat", "chat", "chat-series"),
            ("tasks", "task", "daily"),
            ("chat-old", "chat", None),
            ("agent-shared", "chat", "shared-series"),
        ):
            folder = self.root / "data" / "v2-sessions" / "group" / session_id
            folder.mkdir(parents=True)
            inbound = sqlite3.connect(folder / "inbound.db")
            inbound.execute(
                """
                CREATE TABLE messages_in (
                  kind TEXT, timestamp TEXT, status TEXT, series_id TEXT,
                  source_session_id TEXT
                )
                """
            )
            inbound.execute(
                "INSERT INTO messages_in VALUES (?,?,?,?,?)",
                (kind, stamp, "completed", series, None),
            )
            if session_id == "tasks":
                inbound.execute(
                    "INSERT INTO messages_in VALUES (?,?,?,?,?)",
                    ("task", stamp, "pending", "later", None),
                )
            if session_id == "chat":
                inbound.execute(
                    "INSERT INTO messages_in VALUES (?,?,?,?,?)",
                    ("chat", stamp, "completed", None, "upstream-session"),
                )
            inbound.commit()
            inbound.close()
            outbound = sqlite3.connect(folder / "outbound.db")
            outbound.execute("CREATE TABLE messages_out (timestamp TEXT)")
            outbound.execute("INSERT INTO messages_out VALUES (?)", (stamp,))
            outbound.commit()
            outbound.close()

        transcript = (
            self.root
            / "data"
            / "v2-sessions"
            / "group"
            / ".claude-shared"
            / "projects"
            / "-workspace-agent"
            / "provider.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        event_time = datetime.fromtimestamp(NOW - 250, tz=timezone.utc).isoformat()
        events = [
            {"type": "user", "timestamp": event_time, "message": {"content": "fabricated"}},
            {
                "type": "assistant",
                "timestamp": event_time,
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "mcp__search__query", "input": {}}
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            },
        ]
        transcript.write_text("".join(json.dumps(event) + "\n" for event in events))

        metrics = scan_nanoclaw([self.root], now_ts=NOW)["rollup"]

        self.assertEqual(metrics["sessions"], 3)
        self.assertEqual(metrics["scheduled_task_runs"], 1)
        self.assertEqual(metrics["messages"], 10)
        self.assertEqual(metrics["user_messages"], 3)
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["mcp_server_counts"], {"search": 1})

    def test_nanoclaw_rejects_session_path_traversal(self):
        central = self.root / "data" / "v2.db"
        central.parent.mkdir(parents=True)
        db = sqlite3.connect(central)
        db.execute(
            """
            CREATE TABLE sessions (
              id TEXT, agent_group_id TEXT, messaging_group_id TEXT, thread_id TEXT,
              created_at TEXT, last_active TEXT
            )
            """
        )
        stamp = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            ("session", "../outside", "channel", "thread", stamp, stamp),
        )
        db.commit()
        db.close()

        with self.assertRaises(AgentRuntimeScanIncomplete):
            scan_nanoclaw([self.root], now_ts=NOW)

    def test_combined_scanner_emits_complete_runtime_sources(self):
        path = self.root / "state.db"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE sessions (
              id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
              message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
              reasoning_tokens INTEGER, model TEXT, model_config TEXT, end_reason TEXT
            );
            CREATE TABLE messages (
              session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
              tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
              timestamp REAL, active INTEGER DEFAULT 1
            );
            """
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("human", "cli", None, NOW - 60, NOW, 2, 0, 1, 1, 0, 0, 0, "model", "{}", None),
        )
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            ("human", "user", "fabricated", None, None, None, None, NOW - 30, 1),
        )
        db.commit()
        db.close()

        result = scan_all(
            claude_dir=self.root / "claude",
            hermes_db=path,
            openclaw_dir=self.root / "missing-openclaw",
            nanoclaw_roots=[],
            now_ts=NOW,
        )

        self.assertEqual(result["by_source"]["hermes"]["rollup"]["sessions"], 1)

    def test_combined_scanner_omits_detected_but_incomplete_runtime_store(self):
        path = self.root / "broken.db"
        path.write_text("not sqlite")

        result = scan_all(
            claude_dir=self.root / "claude",
            hermes_db=path,
            openclaw_dir=self.root / "missing-openclaw",
            nanoclaw_roots=[],
            now_ts=NOW,
        )

        self.assertNotIn("hermes", result["by_source"])
        self.assertIn("claude_code", result["by_source"])

    def test_combined_scanner_survives_broken_runtime_scanner_module(self):
        path = self.root / "state.db"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE sessions (
              id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
              message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
              reasoning_tokens INTEGER, model TEXT, model_config TEXT, end_reason TEXT
            );
            CREATE TABLE messages (
              session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
              tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
              timestamp REAL, active INTEGER DEFAULT 1
            );
            """
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("human", "cli", None, NOW - 60, NOW, 2, 0, 1, 1, 0, 0, 0, "model", "{}", None),
        )
        db.commit()
        db.close()

        # None in sys.modules makes `from scan_agent_runtimes import ...`
        # raise ImportError — simulating a broken or missing scanner module.
        with patch.dict(sys.modules, {"scan_agent_runtimes": None}):
            result = scan_all(
                claude_dir=self.root / "claude",
                hermes_db=path,
                openclaw_dir=self.root / "missing-openclaw",
                nanoclaw_roots=[],
                now_ts=NOW,
            )

        # Identical to "runtime sources not detected": the primary scan
        # still emits and all three runtime sources are cleanly omitted —
        # even though the Hermes store above would emit if the import worked.
        self.assertIn("claude_code", result["by_source"])
        for source in ("hermes", "openclaw", "nanoclaw"):
            self.assertNotIn(source, result["by_source"])

    def test_combined_scanner_treats_explicit_empty_nanoclaw_roots_as_provided(self):
        # An explicit [] means "no roots" — it must never fall through to
        # real-home discovery the way a missing (None) argument does.
        with patch("scan_agent_runtimes.resolve_nanoclaw_roots") as resolver:
            result = scan_all(
                claude_dir=self.root / "claude",
                hermes_db=self.root / "missing.db",
                openclaw_dir=self.root / "missing-openclaw",
                nanoclaw_roots=[],
                now_ts=NOW,
            )

        resolver.assert_not_called()
        self.assertNotIn("nanoclaw", result["by_source"])


if __name__ == "__main__":
    unittest.main()
