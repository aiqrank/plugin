#!/usr/bin/env python3
"""Tests for the Codex update-notice helper."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class CheckUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp_path)})
        self._env.start()

        if "check_update" in sys.modules:
            del sys.modules["check_update"]

        import check_update

        self.mod = check_update
        self.mod.CONFIG_DIR = self.tmp_path / ".config" / "aiqrank"
        self.mod.STALE_VERSION_PATH = self.mod.CONFIG_DIR / "stale_version"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_newer_server_version_prints_codex_update_command(self):
        self.mod.record_latest_version("9.9.9")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = self.mod.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("curl -sSL https://aiqrank.com/setup/codex | bash", output.getvalue())
        self.assertIn("$aiqrank:aiqrank", output.getvalue())

    def test_current_version_clears_previous_update_notice(self):
        self.mod.CONFIG_DIR.mkdir(parents=True)
        self.mod.STALE_VERSION_PATH.write_text("9.9.9\n")

        self.mod.record_latest_version(self.mod.PLUGIN_VERSION)

        self.assertFalse(self.mod.STALE_VERSION_PATH.exists())
        self.assertEqual(self.mod.main(), 0)

    def test_updated_bundle_clears_a_previous_stale_notice_before_scanning(self):
        self.mod.CONFIG_DIR.mkdir(parents=True)
        self.mod.STALE_VERSION_PATH.write_text(f"{self.mod.PLUGIN_VERSION}\n")

        self.assertEqual(self.mod.main(), 0)
        self.assertFalse(self.mod.STALE_VERSION_PATH.exists())

    def test_invalid_server_version_is_not_persisted(self):
        self.mod.record_latest_version("9.9.9\nrun-this")

        self.assertFalse(self.mod.STALE_VERSION_PATH.exists())
