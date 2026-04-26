#!/usr/bin/env python3
"""Tests for upload_metrics.py — mocks urlopen and browser open."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UploadMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp_path)})
        self._env.start()
        if "upload_metrics" in sys.modules:
            del sys.modules["upload_metrics"]
        import upload_metrics
        self.mod = upload_metrics
        self.mod.CONFIG_DIR = self.tmp_path / ".config" / "aiqrank"
        self.mod.DEVICE_PATH = self.mod.CONFIG_DIR / "device.json"
        self.mod.LAST_UPLOAD_PATH = self.mod.CONFIG_DIR / "last_upload_at"

        # Write a minimal metrics file.
        self.metrics_path = self.tmp_path / "metrics.json"
        self.metrics_path.write_text(json.dumps({
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 3}}],
        }))

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _run_with_fake_post(self, response_body: dict, extra_args: list | None = None):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["user_agent"] = req.get_header("User-agent", "")
            return FakeResponse(response_body)

        argv = ["--metrics", str(self.metrics_path), "--role", "engineer"]
        if extra_args:
            argv += extra_args

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(self.mod, "open_in_browser"), \
             mock.patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self.mod.main(argv)
        return rc, captured, stdout.getvalue()

    def test_first_run_posts_without_device_id_and_saves_response_id(self):
        rc, captured, stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=abc",
            "device_id": "dev-new-123",
        })
        self.assertEqual(rc, 0)
        self.assertIn("Opening your rank at https://aiqrank.com/teaser?s=abc", stdout)
        # Payload shape
        self.assertIn("daily", captured["body"])
        self.assertEqual(captured["body"]["inferred_role"], "engineer")
        self.assertNotIn("device_id", captured["body"])
        self.assertTrue(captured["user_agent"].startswith("aiqrank-plugin/"))
        # Device.json persisted
        self.assertTrue(self.mod.DEVICE_PATH.exists())
        saved = json.loads(self.mod.DEVICE_PATH.read_text())
        self.assertEqual(saved["device_id"], "dev-new-123")
        # last_upload_at persisted
        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists())

    def test_returning_device_sends_device_id(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "existing-dev"}))
        rc, captured, stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=xyz",
            "device_id": "existing-dev",
        })
        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["device_id"], "existing-dev")

    def test_network_failure_prints_stderr_and_exits_1(self):
        import urllib.error

        def fake_urlopen(req, timeout=30):
            raise urllib.error.URLError("unreachable")

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(self.mod, "open_in_browser"), \
             mock.patch("sys.stdout", new=io.StringIO()) as stdout, \
             mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = self.mod.main(["--metrics", str(self.metrics_path), "--role", "engineer"])

        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("AIQ Rank upload failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
