#!/usr/bin/env python3
"""Tests for daily_update.py — scheduled non-interactive upload wrapper."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class DailyUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp_path)}, clear=False)
        self._env.start()
        if "daily_update" in sys.modules:
            del sys.modules["daily_update"]
        import daily_update

        self.mod = daily_update

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_main_scans_infers_role_and_uploads_without_opening_browser(self):
        fake_metrics = {
            "by_source": {
                "claude_code": {"daily": [{"date": "2026-04-29", "metrics": {"sessions": 1}}]},
                "cowork": {"daily": []},
            },
            "first_messages_sample": ["fix a compile bug"],
        }
        upload_calls = []

        def fake_upload_main(argv):
            upload_calls.append(argv)
            return 0

        with mock.patch.object(self.mod.scan_transcripts, "scan", return_value=fake_metrics), \
             mock.patch.object(self.mod.upload_metrics, "main", side_effect=fake_upload_main), \
             mock.patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self.mod.main([])

        self.assertEqual(rc, 0)
        self.assertEqual(len(upload_calls), 1)
        self.assertIn("--no-open", upload_calls[0])
        self.assertIn("--metrics", upload_calls[0])
        self.assertIn("--role", upload_calls[0])
        self.assertIn("engineer", upload_calls[0])
        metrics_path = Path(upload_calls[0][upload_calls[0].index("--metrics") + 1])
        self.assertFalse(metrics_path.exists())
        self.assertIn("AIQ Rank daily update complete", stdout.getvalue())

    def test_empty_scan_exits_cleanly_without_upload(self):
        fake_metrics = {
            "by_source": {
                "claude_code": {"daily": []},
                "cowork": {"daily": []},
            },
            "first_messages_sample": [],
        }

        with mock.patch.object(self.mod.scan_transcripts, "scan", return_value=fake_metrics), \
             mock.patch.object(self.mod.upload_metrics, "main") as upload_main, \
             mock.patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self.mod.main([])

        self.assertEqual(rc, 0)
        upload_main.assert_not_called()
        self.assertIn("AIQ Rank daily update skipped: no daily metrics", stdout.getvalue())

    def test_config_paths_prefer_host_home_with_existing_device(self):
        sandbox_home = self.tmp_path / "sandbox-home"
        host_home = self.tmp_path / "host-home"
        device_path = host_home / ".config" / "aiqrank" / "device.json"
        device_path.parent.mkdir(parents=True)
        device_path.write_text('{"device_id":"host-device"}\n')

        with mock.patch.object(
            self.mod.scan_transcripts,
            "_host_homes",
            return_value=[sandbox_home, host_home],
        ):
            self.mod._configure_upload_paths(None)

        self.assertEqual(self.mod.upload_metrics.DEVICE_PATH, device_path)
        self.assertEqual(
            self.mod.upload_metrics.LAST_UPLOAD_PATH,
            host_home / ".config" / "aiqrank" / "last_upload_at",
        )


if __name__ == "__main__":
    unittest.main()
