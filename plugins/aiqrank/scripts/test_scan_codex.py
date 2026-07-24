#!/usr/bin/env python3
"""Tests for scan_codex.py."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_codex import scan, _shell_verb, _patch_touches_agents_md  # noqa: E402
from scan_transcripts import scan as scan_all_sources  # noqa: E402


FIXTURES = Path(__file__).resolve().parents[4] / "test" / "fixtures" / "codex"

# The codex fixtures carry explicit 2026-04-20 event timestamps. The scanner now
# drops day-buckets older than `now - window_days`, so the fixture-based tests
# pin the wall clock just after the fixture date. Patching time.time keeps the
# scanner's window, the staged-file mtimes, and the mtime-filter test all
# consistent regardless of when the suite runs.
_FIXTURE_NOW = datetime(2026, 4, 21, tzinfo=timezone.utc).timestamp()


def _staged_session_root(tmp: Path, *fixture_names: str) -> Path:
    """Lay out a temp ~/.codex/sessions tree with the named fixtures."""
    sessions = tmp / "sessions" / "2026" / "04" / "20"
    sessions.mkdir(parents=True, exist_ok=True)
    for name in fixture_names:
        src = FIXTURES / name
        dest = sessions / f"rollout-{name}"
        shutil.copy(src, dest)
    # Bump mtime to "now" so the default 30-day window includes the fixtures.
    now = time.time()
    for p in sessions.iterdir():
        os.utime(p, (now, now))
    return tmp


class ScanCodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._time_patch = mock.patch("time.time", return_value=_FIXTURE_NOW)
        self._time_patch.start()

    def tearDown(self):
        self._time_patch.stop()
        shutil.rmtree(self.tmp)

    def test_normal_session_extracts_expected_counts(self):
        _staged_session_root(self.tmp, "fixture_normal.jsonl")
        result = scan(codex_dir=self.tmp)

        self.assertEqual(result["source"], "codex")
        rollup = result["rollup"]

        # 1 session
        self.assertEqual(rollup["sessions"], 1)
        self.assertEqual(rollup["main_sessions"], 1)

        # 2 user messages + 1 agent message = 3 messages
        self.assertEqual(rollup["user_messages"], 2)
        self.assertEqual(rollup["messages"], 3)

        # Correction regex hits "no" and "revert" in the second user msg
        self.assertEqual(rollup["user_corrections"], 1)

        # 3 shell calls + 1 MCP call + 1 web search + 1 exec_command = 6 tool calls
        self.assertEqual(rollup["tool_calls"], 6)
        self.assertEqual(rollup["sessions_with_tools"], 1)

        # Shell verb diversity: ls, git, pwd (git status + git diff collapse to "git")
        self.assertEqual(rollup["command_diversity"], 3)

        # MCP server counted
        self.assertEqual(rollup["mcp_server_counts"].get("pencil"), 1)

        # Tokens summed from last_token_usage across 2 token_count events
        self.assertEqual(rollup["tokens_input"], 150)
        self.assertEqual(rollup["tokens_output"], 25)
        self.assertEqual(rollup["tokens_cache_read"], 60)
        self.assertEqual(rollup["tokens_total"], 235)

        # 1 reasoning block
        self.assertEqual(rollup["reasoning_blocks"], 1)

        # No unknown event types in the normal fixture
        self.assertEqual(result["_unknown_event_types"], {})

    def test_apply_patch_counts_file_changes_and_agents_md(self):
        _staged_session_root(self.tmp, "fixture_apply_patch.jsonl")
        result = scan(codex_dir=self.tmp)
        rollup = result["rollup"]

        # 3 apply_patch calls = 3 file_changes + 3 tool_calls
        self.assertEqual(rollup["file_changes"], 3)
        self.assertEqual(rollup["tool_calls"], 3)
        self.assertEqual(rollup["tool_name_counts"].get("apply_patch"), 3)

        # Only the first patch touched AGENTS.md
        self.assertEqual(rollup["agents_md_writes"], 1)
        self.assertEqual(rollup["claude_md_writes"], 1)

    def test_edge_cases_unknown_types_and_malformed_lines(self):
        _staged_session_root(self.tmp, "fixture_edge_cases.jsonl")
        result = scan(codex_dir=self.tmp)

        # An unlocalizable malformed record makes this source unsafe to upload.
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["completeness"]["status"], "failed")

        # Unknown event_msg payload and unknown top-level both surface
        unknown = result["_unknown_event_types"]
        self.assertIn("event_msg:unknown_future_thing", unknown)
        self.assertIn("brand_new_top_level:whatever", unknown)

    def test_all_fixtures_together(self):
        _staged_session_root(
            self.tmp,
            "fixture_normal.jsonl",
            "fixture_apply_patch.jsonl",
        )
        result = scan(codex_dir=self.tmp)

        # 2 sessions, one day
        self.assertEqual(result["rollup"]["sessions"], 2)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["daily"][0]["date"], "2026-04-20")

    def test_drops_event_buckets_older_than_window(self):
        # A recently-touched (resumed) rollout that passes the mtime filter but
        # carries events from before the window. Out-of-window day-buckets must
        # not leak into the emitted daily list / intervals.
        sessions = self.tmp / "sessions" / "2026" / "04" / "20"
        sessions.mkdir(parents=True, exist_ok=True)
        events = [
            {"timestamp": "2026-04-20T12:00:00.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "recent"}},
            {"timestamp": "2026-01-15T12:00:00.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "old"}},
            {"timestamp": "2025-12-01T12:00:00.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "older"}},
        ]
        path = sessions / "rollout-resumed.jsonl"
        with path.open("w") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
        now = time.time()
        os.utime(path, (now, now))

        result = scan(codex_dir=self.tmp)
        dates = {row["date"] for row in result["daily"]}
        self.assertEqual(dates, {"2026-04-20"})
        # The in-window day's interval survives (guards against an off-by-one
        # that drops the in-window bucket along with the out-of-window ones).
        self.assertIn("2026-04-20", result["intervals_by_day"])
        self.assertNotIn("2026-01-15", result["intervals_by_day"])
        self.assertNotIn("2025-12-01", result["intervals_by_day"])

    def test_privacy_no_raw_text_in_output(self):
        _staged_session_root(
            self.tmp, "fixture_normal.jsonl", "fixture_apply_patch.jsonl"
        )
        result = scan(codex_dir=self.tmp)
        blob = json.dumps(result)

        # Exact user-message text must not appear.
        self.assertNotIn("please add a readme", blob)
        self.assertNotIn("actually, no — revert that change", blob)
        # Raw patch bodies must not appear.
        self.assertNotIn("*** Begin Patch", blob)
        self.assertNotIn("# Repository Guidelines", blob)
        # Raw cwd must not appear.
        self.assertNotIn("/Users/test/proj", blob)
        self.assertNotIn("/Users/test/other", blob)

    def test_mtime_filter_excludes_old_files(self):
        _staged_session_root(self.tmp, "fixture_normal.jsonl")
        # Push file mtime back beyond the default 30-day window
        sessions = self.tmp / "sessions" / "2026" / "04" / "20"
        for p in sessions.iterdir():
            old = time.time() - (60 * 86400)
            os.utime(p, (old, old))

        result = scan(codex_dir=self.tmp)
        self.assertEqual(result["rollup"]["sessions"], 0)
        self.assertEqual(result["daily"], [])

    def test_empty_codex_dir(self):
        result = scan(codex_dir=self.tmp)
        self.assertEqual(result["rollup"]["sessions"], 0)
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["_unknown_event_types"], {})
        self.assertEqual(result["completeness"]["status"], "complete")

    def test_scan_is_a_compatibility_delegate(self):
        canonical = {
            "daily": [], "rollup": {"sessions": 0}, "intervals_by_day": {},
            "_unknown_event_types": {},
            "completeness": {"status": "complete", "omitted_dates": [], "failure_count": 0},
        }
        with mock.patch("scan_codex._canonical_scan_codex", return_value=canonical) as delegated:
            result = scan(codex_dir=self.tmp, window_days=17, now_ts=123.0, mtime_after_ts=45.0)

        delegated.assert_called_once_with(
            self.tmp, window_days=17, now_ts=123.0, mtime_after_ts=45.0
        )
        self.assertEqual(result["source"], "codex")
        self.assertEqual(result["daily"], [])

    def test_standalone_and_integrated_codex_blocks_are_identical(self):
        _staged_session_root(self.tmp, "fixture_normal.jsonl", "fixture_apply_patch.jsonl")
        claude_dir = self.tmp / "claude"
        (claude_dir / "projects").mkdir(parents=True)

        standalone = scan(codex_dir=self.tmp, now_ts=_FIXTURE_NOW)
        integrated = scan_all_sources(
            claude_dir=claude_dir,
            codex_dir=self.tmp,
            now_ts=_FIXTURE_NOW,
        )["by_source"]["codex"]

        for key in (
            "daily", "rollup", "intervals_by_day", "_unknown_event_types", "completeness"
        ):
            self.assertEqual(standalone[key], integrated[key])

    def test_shell_verb_extraction(self):
        # Old `shell` array form
        self.assertEqual(
            _shell_verb('{"command":["bash","-lc","git status"]}'), "git"
        )
        self.assertEqual(
            _shell_verb('{"command":["bash","-lc","ls -la"]}'), "ls"
        )
        self.assertEqual(
            _shell_verb('{"command":["bash","-lc","FOO=bar git diff"]}'), "git"
        )
        self.assertEqual(
            _shell_verb('{"command":["python3","script.py"]}'), "python3"
        )
        # New `exec_command` string form
        self.assertEqual(
            _shell_verb('{"cmd":"pwd","workdir":"/tmp"}'), "pwd"
        )
        self.assertEqual(
            _shell_verb('{"cmd":"git log --oneline","workdir":"/x"}'), "git"
        )
        self.assertEqual(
            _shell_verb('{"cmd":"FOO=bar git status","workdir":"/x"}'), "git"
        )
        self.assertIsNone(_shell_verb("not-json"))
        self.assertIsNone(_shell_verb(""))
        self.assertIsNone(_shell_verb('{"command":[]}'))
        self.assertIsNone(_shell_verb('{"cmd":""}'))

    def test_worktree_spawns_from_shell_and_exec_command(self):
        sessions = self.tmp / "sessions" / "2026" / "04" / "20"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / "rollout-worktrees.jsonl"
        events = [
            {
                "timestamp": "2026-04-20T12:00:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": json.dumps(
                        {
                            "command": [
                                "bash",
                                "-lc",
                                "git worktree add ../shell-worktree",
                            ]
                        }
                    ),
                    "call_id": "shell-worktree",
                },
            },
            {
                "timestamp": "2026-04-20T12:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {
                            "cmd": "FOO=bar git -C repo worktree add ../exec-worktree",
                            "workdir": "/tmp",
                        }
                    ),
                    "call_id": "exec-worktree",
                },
            },
            {
                "timestamp": "2026-04-20T12:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "git worktree list"}),
                    "call_id": "worktree-list",
                },
            },
        ]
        with path.open("w") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        now = time.time()
        os.utime(path, (now, now))

        self.assertEqual(scan(codex_dir=self.tmp)["rollup"]["worktree_spawns"], 2)

    def test_patch_touches_agents_md(self):
        self.assertTrue(
            _patch_touches_agents_md(
                "*** Begin Patch\n*** Add File: AGENTS.md\n+content"
            )
        )
        self.assertTrue(
            _patch_touches_agents_md(
                "*** Begin Patch\n*** Update File: AGENTS.md\n@@"
            )
        )
        self.assertFalse(
            _patch_touches_agents_md(
                "*** Begin Patch\n*** Update File: README.md\n@@"
            )
        )
        # Body reference alone doesn't count — must be in a header line
        self.assertFalse(
            _patch_touches_agents_md(
                "*** Begin Patch\n*** Update File: notes.md\n+ see AGENTS.md"
            )
        )


class CodexModelEffortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._time_patch = mock.patch("time.time", return_value=_FIXTURE_NOW)
        self._time_patch.start()

    def tearDown(self):
        self._time_patch.stop()
        shutil.rmtree(self.tmp)

    def test_captures_model_from_turn_context(self):
        _staged_session_root(self.tmp, "fixture_normal.jsonl")
        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertGreaterEqual(rollup["model_usage"].get("gpt-5-codex", 0), 1)
        self.assertIsInstance(rollup["effort_usage"], dict)

    def test_captures_model_and_effort_histograms(self):
        sessions = self.tmp / "sessions" / "2026" / "04" / "20"
        sessions.mkdir(parents=True, exist_ok=True)
        ts = "2026-04-20T12:00:00.000Z"
        events = [
            {"timestamp": ts, "type": "turn_context",
             "payload": {"model": "gpt-5.5", "effort": "high"}},
            {"timestamp": ts, "type": "turn_context",
             "payload": {"model": "gpt-5.5", "effort": "high"}},
            {"timestamp": ts, "type": "turn_context",
             "payload": {"model": "gpt-5-codex", "effort": "medium"}},
        ]
        path = sessions / "rollout-effort.jsonl"
        with path.open("w") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
        now = time.time()
        os.utime(path, (now, now))

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["model_usage"], {"gpt-5.5": 2, "gpt-5-codex": 1})
        self.assertEqual(rollup["effort_usage"], {"high": 2, "medium": 1})


if __name__ == "__main__":
    unittest.main()
