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
from test_scan_transcripts import (  # noqa: E402
    make_tool_call_with_id,
    make_tool_result,
    write_jsonl,
)


_FIXTURE_ROOTS = (
    Path(__file__).resolve().parents[4],  # server repository layout
    Path(__file__).resolve().parents[3],  # standalone plugin repository layout
)
FIXTURES = next(
    (root / "test" / "fixtures" / "codex" for root in _FIXTURE_ROOTS if (root / "test" / "fixtures" / "codex").is_dir()),
    None,
)
if FIXTURES is None:
    raise RuntimeError("Codex test fixtures are missing from the repository")

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


class CodexStructuralPlanningTests(unittest.TestCase):
    """Codex structural planning outcomes and plugin skill authorship mirror
    the canonical Claude semantics: signals qualify only with a later
    successful mutation on the same date, plan-artifact writes qualify alone,
    and authorship requires a successful skills/<name>/SKILL.md mutation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._time_patch = mock.patch("time.time", return_value=_FIXTURE_NOW)
        self._time_patch.start()

    def tearDown(self):
        self._time_patch.stop()
        shutil.rmtree(self.tmp)

    def _write_rollout(self, name: str, events: list[dict]) -> Path:
        sessions = self.tmp / "sessions" / "2026" / "04" / "20"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"rollout-{name}.jsonl"
        with path.open("w") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        os.utime(path, (_FIXTURE_NOW, _FIXTURE_NOW))
        return path

    @staticmethod
    def _event(
        payload: dict,
        ts: str = "2026-04-20T12:00:00.000Z",
        event_type: str = "response_item",
    ) -> dict:
        return {"timestamp": ts, "type": event_type, "payload": payload}

    def _update_plan(self, ts: str = "2026-04-20T12:00:00.000Z") -> dict:
        return self._event(
            {"type": "function_call", "name": "update_plan", "call_id": "plan-1", "arguments": "{}"},
            ts,
        )

    def _patch(
        self, call_id: str, file_path: str, ts: str = "2026-04-20T12:01:00.000Z"
    ) -> dict:
        return self._event(
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": call_id,
                "input": f"*** Begin Patch\n*** Update File: {file_path}\n@@\n+x\n*** End Patch",
            },
            ts,
        )

    def _output(
        self, call_id: str, text: str = "Done", ts: str = "2026-04-20T12:01:01.000Z"
    ) -> dict:
        return self._event(
            {"type": "custom_tool_call_output", "call_id": call_id, "output": text},
            ts,
        )

    def test_update_plan_with_successful_patch_qualifies_once(self):
        self._write_rollout(
            "qualify",
            [
                self._update_plan(),
                self._patch("p1", "lib/foo.ex"),
                self._output("p1"),
                self._patch("p2", "lib/bar.ex", ts="2026-04-20T12:02:00.000Z"),
                self._output("p2", ts="2026-04-20T12:02:01.000Z"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 1)
        self.assertEqual(rollup["plan_mode_invocations"], 1)

    def test_activation_alone_and_failed_patch_earn_no_credit(self):
        self._write_rollout("activation-only", [self._update_plan()])
        self._write_rollout(
            "failed-patch",
            [
                self._update_plan(),
                self._patch("p1", "lib/foo.ex"),
                self._output("p1", text="Error: command failed"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)
        self.assertEqual(rollup["plan_mode_invocations"], 2)

    def test_missing_patch_output_fails_closed(self):
        self._write_rollout(
            "unpaired",
            [self._update_plan(), self._patch("p1", "lib/foo.ex")],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)

    def test_shell_only_follow_through_earns_no_credit(self):
        self._write_rollout(
            "shell-only",
            [
                self._update_plan(),
                self._event(
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "sh-1",
                        "arguments": json.dumps({"cmd": "mix test"}),
                    },
                    ts="2026-04-20T12:01:00.000Z",
                ),
                self._event(
                    {"type": "exec_command_end", "call_id": "sh-1", "exit_code": 0},
                    ts="2026-04-20T12:01:01.000Z",
                    event_type="event_msg",
                ),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)

    def test_plan_artifact_patch_qualifies_without_signal(self):
        self._write_rollout(
            "artifact",
            [
                self._patch("p1", "docs/plans/2026-07-29-001-fix-example-plan.md"),
                self._output("p1"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 1)
        self.assertEqual(rollup["plan_mode_invocations"], 0)

    def test_generic_patch_targets_earn_no_artifact_credit(self):
        self._write_rollout(
            "generic",
            [
                self._patch("p1", "README.md"),
                self._output("p1"),
                self._patch("p2", "spec.md", ts="2026-04-20T12:02:00.000Z"),
                self._output("p2", ts="2026-04-20T12:02:01.000Z"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)

    def test_cross_midnight_follow_through_does_not_qualify(self):
        self._write_rollout(
            "cross-midnight",
            [
                self._update_plan(ts="2026-04-19T08:00:00.000Z"),
                self._patch("p1", "lib/foo.ex", ts="2026-04-20T14:00:00.000Z"),
                self._output("p1", ts="2026-04-20T14:00:01.000Z"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)

    def test_completion_after_midnight_fails_closed(self):
        # The patch is invoked on day one but its successful output lands the
        # next day — same-date completion is not proven.
        self._write_rollout(
            "late-completion",
            [
                self._update_plan(ts="2026-04-19T08:00:00.000Z"),
                self._patch("p1", "lib/foo.ex", ts="2026-04-19T09:00:00.000Z"),
                self._output("p1", ts="2026-04-20T15:00:00.000Z"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)

    def test_ambiguous_or_failure_marker_outputs_fail_closed(self):
        for name, output in [
            ("denied", "permission denied while applying patch"),
            ("patchfail", "patch failed to apply"),
            ("neutral", "xyzzy text with no recognizable marker"),
        ]:
            self._write_rollout(
                name,
                [
                    self._update_plan(),
                    self._patch("p1", "lib/foo.ex"),
                    self._output("p1", text=output),
                ],
            )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)

    def test_embedded_exit_code_decides_mutation_success(self):
        self._write_rollout(
            "exit-zero",
            [
                self._update_plan(),
                self._patch("p1", "lib/foo.ex"),
                self._output("p1", text='{"output":"","metadata":{"exit_code":0}}'),
            ],
        )
        self._write_rollout(
            "exit-one",
            [
                self._update_plan(ts="2026-04-20T13:00:00.000Z"),
                self._patch("p2", "lib/bar.ex", ts="2026-04-20T13:01:00.000Z"),
                self._output(
                    "p2",
                    text='{"output":"success","metadata":{"exit_code":1}}',
                    ts="2026-04-20T13:01:01.000Z",
                ),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        # exit_code 0 qualifies its session; exit_code 1 overrides the
        # "success" text and fails closed.
        self.assertEqual(rollup["sessions_with_plan_mode"], 1)

    def test_nested_apply_patch_inherits_outer_success(self):
        code = (
            'await tools.apply_patch("*** Begin Patch\\n'
            '*** Update File: docs/plans/2026-07-29-002-nested-plan.md\\n'
            '*** End Patch");\n'
            'await tools.apply_patch("*** Begin Patch\\n'
            '*** Update File: plugin/skills/nested-skill/SKILL.md\\n'
            '*** End Patch");'
        )
        self._write_rollout(
            "nested-success",
            [
                self._event(
                    {"type": "custom_tool_call", "name": "exec", "call_id": "outer-1", "input": code}
                ),
                self._event(
                    {"type": "custom_tool_call_output", "call_id": "outer-1", "output": "Done"},
                    ts="2026-04-20T12:00:01.000Z",
                ),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 1)
        self.assertEqual(rollup["authored_skill_names"], ["nested-skill"])

    def test_nested_apply_patch_with_failed_outer_earns_nothing(self):
        code = (
            'await tools.apply_patch("*** Begin Patch\\n'
            '*** Update File: docs/plans/2026-07-29-002-nested-plan.md\\n'
            '*** End Patch");\n'
            'await tools.apply_patch("*** Begin Patch\\n'
            '*** Update File: plugin/skills/nested-skill/SKILL.md\\n'
            '*** End Patch");'
        )
        self._write_rollout(
            "nested-failed",
            [
                self._event(
                    {"type": "custom_tool_call", "name": "exec", "call_id": "outer-1", "input": code}
                ),
                self._event(
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "outer-1",
                        "output": "Error: command failed",
                    },
                    ts="2026-04-20T12:00:01.000Z",
                ),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["sessions_with_plan_mode"], 0)
        self.assertEqual(rollup["authored_skill_names"], [])

    def test_write_tool_to_plugin_skill_md_establishes_authorship(self):
        self._write_rollout(
            "plugin-author",
            [
                self._event(
                    {
                        "type": "function_call",
                        "name": "Write",
                        "call_id": "w1",
                        "arguments": json.dumps(
                            {"file_path": "/Users/me/dev/scott-cc/skills/acceptance-criteria/SKILL.md"}
                        ),
                    }
                ),
                self._event(
                    {"type": "function_call_output", "call_id": "w1", "output": "ok"},
                    ts="2026-04-20T12:00:01.000Z",
                ),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["authored_skill_names"], ["acceptance-criteria"])

    def test_apply_patch_to_plugin_skill_md_establishes_authorship(self):
        self._write_rollout(
            "patch-author",
            [
                self._patch("p1", "plugin/skills/review-helper/SKILL.md"),
                self._output("p1"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["authored_skill_names"], ["review-helper"])

    def test_failed_and_self_skill_mutations_never_author(self):
        self._write_rollout(
            "non-author",
            [
                self._patch("p1", "plugin/skills/aiqrank/SKILL.md"),
                self._output("p1"),
                self._patch("p2", "plugin/skills/broken/SKILL.md", ts="2026-04-20T12:02:00.000Z"),
                self._output("p2", text="Error: command failed", ts="2026-04-20T12:02:01.000Z"),
                self._patch("p3", "plugin/skills/unpaired/SKILL.md", ts="2026-04-20T12:03:00.000Z"),
            ],
        )

        rollup = scan(codex_dir=self.tmp)["rollup"]
        self.assertEqual(rollup["authored_skill_names"], [])

    def test_daily_rows_and_rollup_carry_planning_measurement_version(self):
        self._write_rollout(
            "version",
            [
                self._event(
                    {"type": "user_message", "message": "hello"},
                    event_type="event_msg",
                )
            ],
        )

        result = scan(codex_dir=self.tmp)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(
            result["daily"][0]["metrics"]["planning_measurement_version"], 2
        )
        self.assertEqual(result["rollup"]["planning_measurement_version"], 2)

    def test_claude_and_codex_parity_for_equivalent_fixtures(self):
        # Equivalent structural fixtures: a planning signal followed by a
        # successful mutation of a plugin skill file. Both parsers must
        # produce identical structural outcomes.
        skill_path = "/Users/me/dev/scott-cc/skills/parity-skill/SKILL.md"
        claude_dir = self.tmp / "claude"
        (claude_dir / "projects").mkdir(parents=True)
        write_jsonl(
            claude_dir / "projects" / "proj1" / "parity.jsonl",
            [
                make_tool_call_with_id(
                    "ExitPlanMode", {"plan": "..."}, "sig-1", ts="2026-04-20T12:00:00Z"
                ),
                make_tool_call_with_id(
                    "Write", {"file_path": skill_path}, "w1", ts="2026-04-20T12:01:00Z"
                ),
                make_tool_result("w1", ts="2026-04-20T12:01:01Z"),
            ],
        )
        self._write_rollout(
            "parity",
            [
                self._update_plan(),
                self._event(
                    {
                        "type": "function_call",
                        "name": "Write",
                        "call_id": "w1",
                        "arguments": json.dumps({"file_path": skill_path}),
                    },
                    ts="2026-04-20T12:01:00.000Z",
                ),
                self._event(
                    {"type": "function_call_output", "call_id": "w1", "output": "ok"},
                    ts="2026-04-20T12:01:01.000Z",
                ),
            ],
        )

        result = scan_all_sources(
            claude_dir=claude_dir, codex_dir=self.tmp, now_ts=_FIXTURE_NOW
        )
        claude_rollup = result["by_source"]["claude_code"]["rollup"]
        codex_rollup = result["by_source"]["codex"]["rollup"]

        for field in (
            "sessions_with_plan_mode",
            "plan_mode_invocations",
            "authored_skill_names",
            "planning_measurement_version",
        ):
            self.assertEqual(claude_rollup[field], codex_rollup[field], field)
        self.assertEqual(claude_rollup["sessions_with_plan_mode"], 1)
        self.assertEqual(claude_rollup["authored_skill_names"], ["parity-skill"])


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
