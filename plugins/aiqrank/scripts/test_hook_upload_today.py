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
        self.mod.LAST_UPLOAD_PATH_CODEX = cfg / "last_upload_at_codex"
        self.mod.LOCK_PATH = cfg / "upload.lock"
        self.mod.DISABLED_FLAG = cfg / "disabled"
        self.mod.LOG_PATH = cfg / "hook.log"
        self.mod.CODEX_SESSIONS_DIR = self.tmp_path / ".codex" / "sessions"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _invoke_silent(self):
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

    # --- existing tests ---

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

        with mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=AssertionError("urlopen must not be called when gated")):
            self._invoke_silent()

        self.assertIn("gated", self._log_contents())

    def test_successful_upload_logs_ok(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))

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

        self.assertEqual(len(captured_requests), 1)
        payload = json.loads(captured_requests[0].data.decode("utf-8"))
        self.assertEqual(payload["inferred_role"], "engineer")
        self.assertNotIn("first_messages_sample", payload)
        self.assertTrue(captured_requests[0].get_header("User-agent", "").startswith("aiqrank-plugin/"))

    # --- new tests: Codex absent path ---

    def test_codex_absent_uses_legacy_payload(self):
        """When ~/.codex/sessions/ does not exist, payload has no by_source."""
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-codextest"}))
        # CODEX_SESSIONS_DIR is already set to a non-existent path in setUp.

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        fake_scan = {"daily": [{"date": "2026-04-19", "metrics": {"sessions": 1}}], "inferred_role": "engineer"}

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        # Legacy shape: has daily, no by_source
        self.assertIn("daily", payload)
        self.assertNotIn("by_source", payload)

    # --- new tests: Codex present path ---

    def _setup_device(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-codextest1"}))

    def _make_fake_codex_metrics(self, n_days=2):
        return {
            "source": "codex",
            "window_days": 30,
            "daily": [{"date": f"2026-04-{18+i:02d}", "metrics": {"sessions": 1}} for i in range(n_days)],
            "rollup": {"sessions": n_days},
            "_unknown_event_types": {},
        }

    def test_codex_present_sends_by_source_payload(self):
        """When ~/.codex/sessions/ exists, payload includes by_source."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        fake_claude = {"daily": [{"date": "2026-04-19", "metrics": {"sessions": 2}}], "inferred_role": "engineer"}
        fake_codex = self._make_fake_codex_metrics(2)

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        self.assertIn("by_source", payload)
        self.assertIn("claude_code", payload["by_source"])
        self.assertIn("codex", payload["by_source"])
        # Legacy daily still present for back-compat
        self.assertIn("daily", payload)

    def test_codex_present_updates_both_cursors(self):
        """Both last_upload_at and last_upload_at_codex are written on success."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_claude = {"daily": [{"date": "2026-04-19", "metrics": {"sessions": 1}}], "inferred_role": "engineer"}
        fake_codex = self._make_fake_codex_metrics(1)

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", return_value=FakeResp()):
            self._invoke_silent()

        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists(), "last_upload_at not written")
        self.assertTrue(self.mod.LAST_UPLOAD_PATH_CODEX.exists(), "last_upload_at_codex not written")

    def test_codex_cursor_not_updated_on_upload_failure(self):
        """Codex cursor must not advance when the upload fails."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        fake_claude = {"daily": [{"date": "2026-04-19", "metrics": {"sessions": 1}}], "inferred_role": "engineer"}
        fake_codex = self._make_fake_codex_metrics(1)

        import urllib.error
        def fake_urlopen(req, timeout=30):
            # Provide a real BytesIO fp so urllib's cleanup doesn't emit a
            # ResourceWarning that pollutes the test's stderr assertion.
            raise urllib.error.HTTPError(
                url=None, code=500, msg="server error", hdrs=None, fp=io.BytesIO(b"")
            )

        import warnings
        with warnings.catch_warnings(), \
             mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            # HTTPError instances trigger a ResourceWarning on garbage collection
            # in Python 3.12+; that's noise here, not a real leak.
            warnings.simplefilter("ignore", ResourceWarning)
            self._invoke_silent()

        self.assertFalse(self.mod.LAST_UPLOAD_PATH_CODEX.exists(), "codex cursor must not be written on failure")

    # --- new tests: 400 KB chunking ---

    def test_large_codex_backfill_is_chunked(self):
        """A Codex backfill that makes the payload exceed 400 KB is split into chunks."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Build a synthetic Codex daily list large enough to exceed 400 KB when
        # combined with the Claude daily. 80 entries × 8000 chars ≈ 640 KB.
        big_metrics = {"sessions": 5, "messages": 100, "data": "x" * 8000}
        codex_daily = [{"date": f"2026-{(i//28)+1:02d}-{(i%28)+1:02d}", "metrics": big_metrics} for i in range(80)]

        fake_claude = {"daily": [{"date": "2026-04-19", "metrics": {"sessions": 2}}], "inferred_role": "engineer"}
        fake_codex = {
            "source": "codex",
            "window_days": 30,
            "daily": codex_daily,
            "rollup": {},
            "_unknown_event_types": {},
        }

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            body = json.loads(req.data)
            posted_bodies.append(body)
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        # Must have posted more than one chunk.
        self.assertGreater(len(posted_bodies), 1, "expected multiple chunks for large Codex backfill")

        # Every chunk must have by_source.
        for body in posted_bodies:
            self.assertIn("by_source", body)

        # Each chunk's Claude AND Codex daily must be <= CODEX_CHUNK_DAYS entries.
        for body in posted_bodies:
            claude_chunk = body["by_source"]["claude_code"]["daily"]
            codex_chunk = body["by_source"]["codex"]["daily"]
            self.assertLessEqual(len(claude_chunk), self.mod.CODEX_CHUNK_DAYS)
            self.assertLessEqual(len(codex_chunk), self.mod.CODEX_CHUNK_DAYS)

        # Union across chunks must equal originals (no entries lost or duplicated).
        all_claude = [e for b in posted_bodies for e in b["by_source"]["claude_code"]["daily"]]
        all_codex = [e for b in posted_bodies for e in b["by_source"]["codex"]["daily"]]
        self.assertEqual(all_claude, fake_claude["daily"])
        self.assertEqual(len(all_codex), len(codex_daily))

    def test_large_claude_daily_alone_is_chunked(self):
        """A heavy Claude (cowork) backfill that exceeds 400 KB is split, even
        when Codex is small or absent. Pre-cowork the claude side never grew
        large; post-cowork it can.
        """
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        big_metrics = {
            "sessions": 5,
            "messages": 100,
            "cowork_sessions": 4,
            "cowork_messages": 80,
            "queue_events": 200,
            "data": "x" * 8000,
        }
        claude_daily = [
            {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "metrics": big_metrics}
            for i in range(80)
        ]

        fake_claude = {"daily": claude_daily, "inferred_role": "engineer"}
        fake_codex = self._make_fake_codex_metrics(1)

        posted_bodies = []

        class FakeResp:
            def __init__(self):
                self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            body = json.loads(req.data)
            posted_bodies.append(body)
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), mock.patch.object(
            self.mod, "_maybe_scan_codex", return_value=fake_codex
        ), mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertGreater(len(posted_bodies), 1, "expected multiple chunks for large Claude backfill")

        # Every chunk must respect the per-source size cap.
        for body in posted_bodies:
            claude_chunk = body["by_source"]["claude_code"]["daily"]
            self.assertLessEqual(len(claude_chunk), self.mod.CODEX_CHUNK_DAYS)

        # No entries lost or duplicated.
        all_claude = [e for b in posted_bodies for e in b["by_source"]["claude_code"]["daily"]]
        self.assertEqual(len(all_claude), len(claude_daily))

    def test_chunk_daily_helper(self):
        """_chunk_daily splits a list into sublists of at most chunk_size."""
        daily = [{"date": f"2026-04-{i:02d}"} for i in range(1, 16)]
        chunks = self.mod._chunk_daily(daily, 7)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 7)
        self.assertEqual(len(chunks[1]), 7)
        self.assertEqual(len(chunks[2]), 1)

    def test_chunk_daily_empty(self):
        """_chunk_daily on empty list returns a single empty chunk."""
        chunks = self.mod._chunk_daily([], 7)
        self.assertEqual(chunks, [[]])

    # --- timeout tests ---

    def test_maybe_scan_codex_timeout_returns_none(self):
        """subprocess.TimeoutExpired inside _maybe_scan_codex returns None and logs.

        Verifies that timeout=60 is passed to subprocess.run and that TimeoutExpired
        is caught so the hook can fall back to the legacy upload path.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        logger = self.mod._setup_logger()

        # Simulate subprocess.run raising TimeoutExpired (the timeout=60 path).
        def timeout_subprocess_run(*args, **kwargs):
            raise self.mod.subprocess.TimeoutExpired(cmd=args[0], timeout=60)

        with mock.patch.object(self.mod.subprocess, "run", side_effect=timeout_subprocess_run):
            result = self.mod._maybe_scan_codex(logger)

        self.assertIsNone(result)
        log = self._log_contents()
        self.assertIn("TimeoutExpired", log)

    def test_run_scan_passes_timeout_60(self):
        """_run_scan passes timeout=60 to subprocess.run."""
        captured_kwargs = {}

        def capture_kwargs(*args, **kwargs):
            captured_kwargs.update(kwargs)
            # Return a valid result to avoid JSON parse errors
            import json as _json
            result = mock.MagicMock()
            result.stdout = _json.dumps({"daily": [], "inferred_role": "other"})
            return result

        with mock.patch.object(self.mod.subprocess, "run", side_effect=capture_kwargs):
            try:
                self.mod._run_scan()
            except Exception:
                pass

        self.assertEqual(captured_kwargs.get("timeout"), 60)

    def test_scan_transcript_timeout_stops_upload(self):
        """TimeoutExpired from _run_scan is caught as SubprocessError: upload does not happen."""
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-timeout02"}))

        def timeout_run_scan():
            raise subprocess.TimeoutExpired(cmd="scan_transcripts.py", timeout=60)

        posted = []

        with mock.patch.object(self.mod, "_run_scan", side_effect=timeout_run_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=lambda *a, **kw: posted.append(1)):
            self._invoke_silent()

        self.assertEqual(posted, [], "urlopen must not be called when scan times out")
        log = self._log_contents()
        self.assertIn("error", log)

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
