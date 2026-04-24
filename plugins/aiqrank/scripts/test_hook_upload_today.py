#!/usr/bin/env python3
"""Tests for hook_upload_today.py — silent background uploader."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class HookUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp_path)}, clear=False)
        self._env.start()
        if "hook_upload_today" in sys.modules:
            del sys.modules["hook_upload_today"]
        import hook_upload_today
        self.mod = hook_upload_today
        cfg = self.tmp_path / ".config" / "aiqrank"
        self.mod.CONFIG_DIR = cfg
        self.mod.DEVICE_PATH = cfg / "device.json"
        self.mod.LAST_UPLOAD_PATH = cfg / "last_upload_at"
        self.mod.LOCK_PATH = cfg / "upload.lock"
        self.mod.DISABLED_FLAG = cfg / "disabled"
        self.mod.LOG_PATH = cfg / "hook.log"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _invoke_silent(self):
        # Assert no stdout/stderr output ever.
        with mock.patch("sys.stdout", new=io.StringIO()) as out, \
             mock.patch("sys.stderr", new=io.StringIO()) as err:
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def _log_contents(self) -> str:
        if not self.mod.LOG_PATH.exists():
            return ""
        return self.mod.LOG_PATH.read_text()

    def test_no_device_logs_and_exits(self):
        self._invoke_silent()
        self.assertIn("no device", self._log_contents())

    def test_disabled_flag_skips_upload(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-1"}))
        self.mod.DISABLED_FLAG.write_text("")
        self._invoke_silent()
        self.assertIn("disabled", self._log_contents())

    def test_24h_gate_logs_gated(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh)

        # If we reach urlopen, the gate broke.
        with mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=AssertionError("urlopen must not be called when gated")):
            self._invoke_silent()

        self.assertIn("gated", self._log_contents())

    def test_successful_upload_logs_ok(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))
        # No last_upload_at → not gated.

        class FakeResp:
            def __init__(self, body): self._b = json.dumps(body).encode("utf-8")
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_scan_output = {
            "daily": [
                {"date": "2026-04-19", "metrics": {"sessions": 2}},
                {"date": "2026-04-20", "metrics": {"sessions": 5}},
            ],
            "first_messages_sample": [
                "fix bug in migration",
                "refactor function and commit",
                "rebase the branch",
            ],
        }

        def fake_run_scan():
            return fake_scan_output

        captured_requests = []

        def fake_urlopen(req, timeout=30):
            captured_requests.append(req)
            return FakeResp({"teaser_url": "https://x/t", "device_id": "device-abcdef12"})

        with mock.patch.object(self.mod, "_run_scan", side_effect=fake_run_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        log = self._log_contents()
        self.assertIn("ok sessions=2 devices=device-a", log)
        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists())

        # Payload should carry a server-valid role inferred from the sample
        # (never the old "other" fallback).
        self.assertEqual(len(captured_requests), 1)
        payload = json.loads(captured_requests[0].data.decode("utf-8"))
        self.assertEqual(payload["inferred_role"], "engineer")
        self.assertNotIn("first_messages_sample", payload)

    def test_empty_sample_defaults_to_engineer(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))

        class FakeResp:
            def __init__(self, body): self._b = json.dumps(body).encode("utf-8")
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_scan_output = {
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 1}}],
            "first_messages_sample": [],
        }

        captured = []

        def fake_urlopen(req, timeout=30):
            captured.append(req)
            return FakeResp({"teaser_url": "https://x/t", "device_id": "device-abcdef12"})

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan_output), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        payload = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(payload["inferred_role"], "engineer")


if __name__ == "__main__":
    unittest.main()
