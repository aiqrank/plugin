"""Instruction-edit credit uses paired success evidence, not tool attempts."""

import json
import tempfile
import unittest
from pathlib import Path

import scan_transcripts as scanner
from test_scan_transcripts import make_tool_call_with_id, make_tool_result, write_jsonl


class InstructionWriteTests(unittest.TestCase):
    def events(self, source, tool, path, *, call_id="edit", failed=False,
               completion="2026-04-01T12:01:01Z"):
        if source == "claude":
            return [
                make_tool_call_with_id(tool, {"file_path": path}, call_id,
                                       ts="2026-04-01T12:01:00Z"),
                make_tool_result(call_id, is_error=failed, ts=completion),
            ]
        payload = {"type": "function_call", "name": tool, "call_id": call_id,
                   "arguments": json.dumps({"file_path": path})}
        if tool == "apply_patch":
            payload = {"type": "custom_tool_call", "name": tool, "call_id": call_id,
                       "input": f"*** Begin Patch\n*** Update File: {path}\n@@\n+fixture\n*** End Patch"}
        return [
            {"type": "response_item", "timestamp": "2026-04-01T12:01:00Z", "payload": payload},
            {"type": "response_item", "timestamp": completion,
             "payload": {"type": "custom_tool_call_output" if tool == "apply_patch" else "function_call_output",
                         "call_id": call_id, "output": "Error" if failed else "Done"}},
        ]

    def parse(self, source, events, *, is_main=True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.jsonl"
            write_jsonl(path, events)
            daily = {}
            if source == "claude":
                scanner.process_session(path, daily, [], set(), {}, is_main=is_main)
            else:
                scanner.process_codex_session(path, daily, {})
            return daily, scanner._rollup_from_daily(daily.values())

    def tools(self):
        return [("claude", "Write"), ("claude", "Edit"),
                ("codex", "Write"), ("codex", "Edit"), ("codex", "apply_patch")]

    def test_relative_absolute_and_windows_instruction_paths(self):
        for source, tool in self.tools():
            for path in ("CLAUDE.md", "AGENTS.md", "./CLAUDE.md", "/repo/AGENTS.md",
                         r"C:\repo\CLAUDE.md"):
                with self.subTest(source=source, tool=tool, path=path):
                    _, rollup = self.parse(source, self.events(source, tool, path))
                    self.assertEqual(rollup["claude_md_writes"], 1)
                    if source == "codex":
                        self.assertEqual(rollup["agents_md_writes"], int(path.endswith("AGENTS.md")))

    def test_failed_missing_and_cross_date_results_earn_no_credit(self):
        for source, tool in self.tools():
            for mode in ("failed", "missing", "cross_date"):
                events = self.events(source, tool, "/repo/AGENTS.md", failed=mode == "failed",
                                     completion="2026-04-02T12:01:01Z" if mode == "cross_date" else "2026-04-01T12:01:01Z")
                if mode == "missing":
                    events = events[:1]
                with self.subTest(source=source, tool=tool, mode=mode):
                    _, rollup = self.parse(source, events)
                    self.assertEqual(rollup["claude_md_writes"], 0)
                    self.assertEqual(rollup["agents_md_writes"], 0)

    def test_repeated_successful_edits_count_individually_and_child_edits_still_count(self):
        events = self.events("claude", "Write", "CLAUDE.md", call_id="a")
        events += self.events("claude", "Edit", "CLAUDE.md", call_id="b")
        _, rollup = self.parse("claude", events, is_main=False)
        self.assertEqual(rollup["claude_md_writes"], 2)

    def test_marker_records_measured_zero_on_every_parsed_day(self):
        for source, tool in self.tools():
            daily, rollup = self.parse(source, self.events(source, tool, "/repo/AGENTS.md",
                                                       completion="2026-04-02T12:01:01Z"))
            self.assertEqual(rollup["instruction_writes_measurement_version"], 1)
            for metrics in daily.values():
                self.assertEqual(metrics["instruction_writes_measurement_version"], 1)
            self.assertNotIn("/repo/AGENTS.md", json.dumps(rollup))

    def test_near_miss_names_remain_excluded(self):
        for source, tool in self.tools():
            for path in ("NOT_CLAUDE.md", "claude.md", "/repo/AGENTS.md.bak", "/repo/README.md"):
                _, rollup = self.parse(source, self.events(source, tool, path))
                self.assertEqual(rollup["claude_md_writes"], 0)

    def test_successful_shell_write_still_does_not_count(self):
        events = [make_tool_call_with_id("Bash", {"command": "echo fixture > CLAUDE.md"}, "b",
                                        ts="2026-04-01T12:01:00Z"),
                  make_tool_result("b", ts="2026-04-01T12:01:01Z")]
        _, rollup = self.parse("claude", events)
        self.assertEqual(rollup["claude_md_writes"], 0)

    def test_codex_nested_patch_inherits_enclosing_completion(self):
        patch = "*** Begin Patch\n*** Update File: AGENTS.md\n@@\n+fixture\n*** End Patch"
        for failed in (False, True):
            events = [
                {"type": "response_item", "timestamp": "2026-04-01T12:01:00Z",
                 "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "outer",
                             "input": f"await tools.apply_patch({json.dumps(patch)});"}},
                {"type": "response_item", "timestamp": "2026-04-01T12:01:01Z",
                 "payload": {"type": "custom_tool_call_output", "call_id": "outer",
                             "output": "Error" if failed else "Done"}},
            ]
            _, rollup = self.parse("codex", events)
            self.assertEqual(rollup["claude_md_writes"], int(not failed))
            self.assertEqual(rollup["agents_md_writes"], int(not failed))
