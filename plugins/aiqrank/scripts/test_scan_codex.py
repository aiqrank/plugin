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
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_codex import scan, _shell_verb, _patch_touches_agents_md  # noqa: E402


FIXTURES = Path(__file__).resolve().parents[4] / "test" / "fixtures" / "codex"


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

    def tearDown(self):
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

        # Malformed JSON line is skipped silently; session still counted
        self.assertEqual(result["rollup"]["sessions"], 1)

        # Empty user_message is ignored (no count, no regex scan)
        self.assertEqual(result["rollup"]["user_messages"], 0)

        # Unknown event_msg payload and unknown top-level both surface
        unknown = result["_unknown_event_types"]
        self.assertIn("event_msg:unknown_future_thing", unknown)
        self.assertIn("brand_new_top_level:whatever", unknown)

    def test_all_fixtures_together(self):
        _staged_session_root(
            self.tmp,
            "fixture_normal.jsonl",
            "fixture_apply_patch.jsonl",
            "fixture_edge_cases.jsonl",
        )
        result = scan(codex_dir=self.tmp)

        # 3 sessions, one day
        self.assertEqual(result["rollup"]["sessions"], 3)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["daily"][0]["date"], "2026-04-20")

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


if __name__ == "__main__":
    unittest.main()
