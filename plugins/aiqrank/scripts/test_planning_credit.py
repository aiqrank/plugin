"""Fabricated evidence for plan-path parity and local-only explanations."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import scan_codex
import scan_transcripts as scanner
from test_scan_transcripts import make_tool_call_with_id, make_tool_result, write_jsonl


class PlanningCreditTests(unittest.TestCase):
    def parse(self, events, *, codex=False, is_main=True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            write_jsonl(path, events)
            daily, diagnostics = {}, {}
            if codex:
                scanner.process_codex_session(path, daily, {}, planning_diagnostics=diagnostics)
            else:
                scanner.process_session(
                    path, daily, [], set(), {}, is_main=is_main,
                    planning_diagnostics=diagnostics,
                )
            return daily, diagnostics

    def mutation(self, path, *, codex=False, call_id="write", failed=False,
                 result_ts="2026-04-01T12:01:01Z"):
        if codex:
            return [
                {"type": "response_item", "timestamp": "2026-04-01T12:01:00Z",
                 "payload": {"type": "custom_tool_call", "name": "apply_patch",
                             "call_id": call_id,
                             "input": f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch"}},
                {"type": "response_item", "timestamp": result_ts,
                 "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                             "output": "Error" if failed else "Done"}},
            ]
        return [
            make_tool_call_with_id("Write", {"file_path": path}, call_id,
                                   ts="2026-04-01T12:01:00Z"),
            make_tool_result(call_id, is_error=failed, ts=result_ts),
        ]

    def test_case_insensitive_paths_agree_across_tools(self):
        cases = {
            "/repo/docs/Plans/DESIGN.MD": True,
            "/repo/plan.md": True,
            "/repo/feature-Plan.Md": True,
            r"C:\repo\PLANS\notes.md": True,
            "/repo/plans/Interview-Plan-and-Rubric.md": True,
            "/repo/Interview-Plan-and-Rubric.md": False,
            "/repo/DESIGN.md": False,
            "/repo/16_plans-roadmap.md": False,
            "/repo/plan/notes.md": False,
            "/repo/planning/notes.md": False,
            "/repo/replan.md": False,
            "/repo/PLANS/data.txt": False,
        }
        for codex in (False, True):
            for path, expected in cases.items():
                with self.subTest(codex=codex, path=path):
                    daily, _ = self.parse(self.mutation(path, codex=codex), codex=codex)
                    self.assertEqual(sum(m["sessions_with_plan_mode"] for m in daily.values()), int(expected))

    def test_failure_cross_date_and_unpaired_evidence_explained_for_both_tools(self):
        for codex in (False, True):
            for mode, reason in (
                ("failed", "missing_or_failed_completion"),
                ("unpaired", "missing_or_failed_completion"),
                ("late", "completion_on_other_date"),
            ):
                events = self.mutation("/repo/Plans/private.MD", codex=codex,
                                       failed=mode == "failed",
                                       result_ts="2026-04-02T12:01:01Z" if mode == "late" else "2026-04-01T12:01:01Z")
                if mode == "unpaired":
                    events = events[:1]
                with self.subTest(codex=codex, mode=mode):
                    daily, diagnostics = self.parse(events, codex=codex)
                    self.assertEqual(sum(m["sessions_with_plan_mode"] for m in daily.values()), 0)
                    days = next(iter(diagnostics.values()))
                    self.assertEqual(sum(counts.get(reason, 0) for counts in days.values()), 1)

    def test_reasons_dedupe_and_can_coexist_with_credit(self):
        for codex in (False, True):
            events = self.mutation("/repo/secret.md", codex=codex, call_id="a")
            events += self.mutation("/repo/secret.md", codex=codex, call_id="b")
            events += self.mutation("/repo/PLAN.md", codex=codex, call_id="c")
            daily, diagnostics = self.parse(events, codex=codex)
            counts = next(iter(next(iter(diagnostics.values())).values()))
            self.assertEqual(counts["unrecognized_path_without_prior_signal"], 1)
            self.assertEqual(counts["credited_main_session_days"], 1)
            self.assertEqual(sum(m["sessions_with_plan_mode"] for m in daily.values()), 1)
            self.assertNotIn("secret", str(diagnostics))
            self.assertNotIn("unrecognized_path", str(daily))

    def test_claude_shell_and_child_exclusions(self):
        events = [make_tool_call_with_id("Bash", {"command": "cat > plans/private.md"}, "b",
                                         ts="2026-04-01T12:00:00Z")]
        events += self.mutation("/repo/Plans/private.md")
        daily, diagnostics = self.parse(events, is_main=False)
        counts = next(iter(diagnostics["claude_code"].values()))
        self.assertEqual(counts["child_session_days"], 1)
        self.assertEqual(counts["child_mutation_excluded"], 1)
        self.assertEqual(counts["shell_not_mutation_evidence"], 1)
        self.assertEqual(sum(m["sessions_with_plan_mode"] for m in daily.values()), 0)

    def test_codex_shell_does_not_qualify_and_generic_mutation_after_signal_does(self):
        shell = {"type": "response_item", "timestamp": "2026-04-01T12:00:00Z",
                 "payload": {"type": "function_call", "name": "exec_command", "call_id": "shell",
                             "arguments": json.dumps({"cmd": "cat > Plans/private.MD"})}}
        daily, diagnostics = self.parse([shell], codex=True)
        self.assertEqual(sum(m["sessions_with_plan_mode"] for m in daily.values()), 0)
        self.assertEqual(next(iter(diagnostics["codex"].values()))["shell_not_mutation_evidence"], 1)
        for codex in (False, True):
            signal = (
                {"type": "response_item", "timestamp": "2026-04-01T12:00:00Z",
                 "payload": {"type": "function_call", "name": "update_plan", "call_id": "plan",
                             "arguments": "{}"}}
                if codex else make_tool_call_with_id("ExitPlanMode", {}, "plan", ts="2026-04-01T12:00:00Z")
            )
            daily, diagnostics = self.parse([signal] + self.mutation("/repo/DESIGN.md", codex=codex), codex=codex)
            self.assertEqual(sum(m["sessions_with_plan_mode"] for m in daily.values()), 1)
            self.assertNotIn("unrecognized_path_without_prior_signal", str(diagnostics))

    def test_cli_explanation_never_changes_stdout_and_omits_unemitted_dates(self):
        block = {"daily": [{"date": "2026-04-01", "metrics": {}}], "rollup": {}}
        for module, envelope in (
            (scanner, {"by_source": {"codex": block}}), (scan_codex, block),
        ):
            def fake_scan(**kwargs):
                diagnostics = kwargs.get("planning_diagnostics")
                if diagnostics is not None:
                    diagnostics["codex"] = {
                        date(2026, 4, 1): {"credited_main_session_days": 1},
                        date(2026, 3, 1): {"uncredited_main_session_days": 99},
                    }
                return envelope

            outputs = []
            for args in ([], ["--explain-planning"]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(module, "scan", side_effect=fake_scan), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(module.main(args), 0)
                outputs.append(stdout.getvalue())
                self.assertEqual(json.loads(stdout.getvalue()), envelope)
                if args:
                    report = json.loads(stderr.getvalue())["planning_diagnostics"]["codex"]
                    self.assertEqual(len(report), 1)
                    self.assertEqual(report[0]["counts"]["credited_main_session_days"], 1)
                else:
                    self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(outputs[0], outputs[1])

    def test_integrated_scan_explanations_match_credit_without_entering_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = root / "claude", root / "codex"
            for source_root, codex_events in ((claude, False), (codex, True)):
                path = (source_root / "sessions" / "rollout-private.jsonl" if codex_events
                        else source_root / "projects" / "fixture" / "private.jsonl")
                write_jsonl(path, self.mutation("/repo/Plans/private-sentinel.MD", codex=codex_events))
            diagnostics = {}
            kwargs = {"claude_dir": claude, "codex_dir": codex,
                      "now_ts": datetime(2026, 4, 2, tzinfo=timezone.utc).timestamp()}
            with mock.patch.object(scanner, "_host_homes", return_value=[root]):
                plain = scanner.scan(**kwargs)
                explained = scanner.scan(**kwargs, planning_diagnostics=diagnostics)
            self.assertEqual(plain, explained)
            report = scanner._planning_diagnostic_report(diagnostics, explained)
            for source in ("claude_code", "codex"):
                self.assertEqual(explained["by_source"][source]["rollup"]["sessions_with_plan_mode"], 1)
                self.assertEqual(report["planning_diagnostics"][source][0]["counts"]["credited_main_session_days"], 1)
            for serialized in (json.dumps(explained), json.dumps(report)):
                self.assertNotIn("private-sentinel", serialized)
                self.assertNotIn("/repo/Plans", serialized)
            self.assertNotIn("planning_diagnostics", explained)
