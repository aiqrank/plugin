#!/usr/bin/env python3
"""Tests for hook_nudge_if_stale.py."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class HookNudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patcher = mock.patch.dict(os.environ, {"HOME": str(self.tmp_path)})
        self._patcher.start()
        # Re-import module with patched HOME so LAST_UPLOAD_PATH resolves fresh.
        if "hook_nudge_if_stale" in sys.modules:
            del sys.modules["hook_nudge_if_stale"]
        import hook_nudge_if_stale  # noqa: F401
        self.mod = hook_nudge_if_stale
        # Force the module-level paths to our tmp home so the hook-fired
        # log line writes into the test sandbox, not the real ~/.config.
        cfg = self.tmp_path / ".config" / "aiqrank"
        self.mod.CONFIG_DIR = cfg
        self.mod.LAST_UPLOAD_PATH = cfg / "last_upload_at"
        self.mod.LOG_PATH = cfg / "hook.log"

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def _run(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_missing_file_prints_nudge(self):
        output = self._run()
        self.assertIn("AIQ Rank:", output)
        self.assertIn("30 days", output)

    def test_fresh_timestamp_silent(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh + "\n")
        output = self._run()
        self.assertEqual(output, "")

    def test_old_timestamp_prints_nudge(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text("2020-01-01T00:00:00Z\n")
        output = self._run()
        self.assertIn("AIQ Rank:", output)

    def test_malformed_timestamp_prints_nudge(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text("garbage\n")
        output = self._run()
        self.assertIn("AIQ Rank:", output)

    def test_empty_file_prints_nudge(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text("")
        output = self._run()
        self.assertIn("AIQ Rank:", output)

    def test_hook_fired_log_line_written(self):
        # Diagnostic: every invocation appends a single hook-fired line to
        # the shared log file so customer triage can prove the hook ran.
        self._run()
        log = self.mod.LOG_PATH.read_text()
        self.assertIn("hook fired script=nudge_if_stale", log)
        self.assertIn(f"platform={sys.platform}", log)

    def test_stale_version_file_prints_update_nudge(self):
        self.mod.STALE_VERSION_PATH = self.tmp_path / ".config" / "aiqrank" / "stale_version"
        self.mod.STALE_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.STALE_VERSION_PATH.write_text("0.9.1\n")
        # Fresh upload so the 30-day nudge is silent.
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh + "\n")

        output = self._run()
        self.assertIn("plugin update available", output)
        self.assertIn("0.9.1", output)
        # Claude Code uses its slash command, which updates itself rather than
        # asking the user to run update commands by hand.
        self.assertIn("/aiqrank", output)
        self.assertNotIn("curl -sSL", output)
        self.assertNotIn("30 days", output)

    def test_codex_stale_version_uses_namespaced_skill_command(self):
        self.mod.STALE_VERSION_PATH = self.tmp_path / ".config" / "aiqrank" / "stale_version"
        self.mod.STALE_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.STALE_VERSION_PATH.write_text("0.9.1\n")
        self.mod.LAST_UPLOAD_PATH.write_text(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n"
        )

        with mock.patch.dict(os.environ, {"CODEX_PLUGIN_ROOT": "/plugin"}):
            output = self._run()

        self.assertIn("$aiqrank:aiqrank", output)
        self.assertNotIn("run /aiqrank", output)

    def test_codex_stale_rank_uses_namespaced_skill_command(self):
        with mock.patch.dict(os.environ, {"CODEX_PLUGIN_ROOT": "/plugin"}):
            output = self._run()

        self.assertIn("$aiqrank:aiqrank", output)
        self.assertNotIn("run /aiqrank", output)

    def test_no_stale_version_file_silent_when_fresh(self):
        self.mod.STALE_VERSION_PATH = self.tmp_path / ".config" / "aiqrank" / "stale_version"
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh + "\n")

        output = self._run()
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
