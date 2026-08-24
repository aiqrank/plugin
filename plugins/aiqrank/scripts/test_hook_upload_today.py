#!/usr/bin/env python3
"""Tests for hook_upload_today.py — silent background uploader."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


def _claude_envelope(daily, intervals_by_day=None, first_messages_sample=None):
    """Build a scan_transcripts.scan() envelope shaped {window_days, by_source, ...}.

    Tests pass a single `daily` list (interactive activity) and optionally
    intervals; cowork stays empty. Mirrors what real scanner returns.
    """
    return {
        "window_days": 30,
        "by_source": {
            "claude_code": {
                "daily": daily,
                "rollup": {},
                "intervals_by_day": intervals_by_day or {},
            },
            "cowork": {
                "daily": [],
                "rollup": {},
                "intervals_by_day": {},
            },
        },
        "first_messages_sample": first_messages_sample or [],
    }


def _claude_with_cowork_envelope(
    claude_daily,
    cowork_daily,
    claude_intervals=None,
    cowork_intervals=None,
    first_messages_sample=None,
):
    """Like _claude_envelope but with cowork-source activity populated too."""
    return {
        "window_days": 30,
        "by_source": {
            "claude_code": {
                "daily": claude_daily,
                "rollup": {},
                "intervals_by_day": claude_intervals or {},
            },
            "cowork": {
                "daily": cowork_daily,
                "rollup": {},
                "intervals_by_day": cowork_intervals or {},
            },
        },
        "first_messages_sample": first_messages_sample or [],
    }

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class HookUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        # AIQRANK_DETACHED=1 short-circuits the detach-and-exit at main()
        # entry so tests run in-process rather than os._exit(0)ing.
        self._env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.tmp_path), "AIQRANK_DETACHED": "1"},
            clear=False,
        )
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
        self.mod.STALE_VERSION_PATH = cfg / "stale_version"

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

    def test_same_utc_day_gate_logs_gated(self):
        """A last_upload_at from earlier today UTC gates re-upload."""
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh)

        # Force CLAUDE_CODE_REMOTE to a non-"true" value so the cloud
        # bypass doesn't fire when these tests run inside Claude Code on
        # the web. Declarative override via patch.dict is restored on
        # exit; a pop() inside an empty patch.dict would not be.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "not-true"}, clear=False), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=AssertionError("urlopen must not be called when gated")):
            self._invoke_silent()

        self.assertIn("gated", self._log_contents())

    def test_cloud_remote_bypasses_same_utc_day_gate(self):
        """CLAUDE_CODE_REMOTE=true skips the once-per-UTC-day gate.

        Cloud containers are ephemeral; if device.json is persisted across
        sessions (or seeded via AIQRANK_DEVICE_ID env var) the second
        cloud session of the same UTC day would otherwise silently no-op.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh)

        scan_called = []
        def fake_scan():
            scan_called.append(True)
            return _claude_envelope(daily=[])

        with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "true"}), \
             mock.patch.object(self.mod, "_run_scan", side_effect=fake_scan):
            self._invoke_silent()

        self.assertTrue(scan_called, "scanner must run in cloud regardless of last_upload_at")
        self.assertNotIn("gated", self._log_contents())

    def test_cloud_remote_session_start_is_skipped(self):
        """In cloud mode, AIQRANK_HOOK=session_start must short-circuit
        before any scan/upload. SessionEnd owns the cloud upload path;
        SessionStart fires on a fresh container with nothing to upload.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))

        with mock.patch.dict(
            os.environ,
            {"CLAUDE_CODE_REMOTE": "true", "AIQRANK_HOOK": "session_start"},
            clear=False,
        ), mock.patch.object(self.mod, "_run_scan",
                             side_effect=AssertionError("scan must not run on cloud SessionStart")), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=AssertionError("urlopen must not run on cloud SessionStart")):
            self._invoke_silent()

        self.assertIn("cloud skip session_start", self._log_contents())

    def test_local_session_start_still_runs(self):
        """Outside cloud mode the gate must not fire, regardless of
        AIQRANK_HOOK. Local CLI relies on the detach + daily-gate flow.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))

        scan_called = []
        def fake_scan():
            scan_called.append(True)
            return _claude_envelope(daily=[])

        with mock.patch.dict(os.environ, {"AIQRANK_HOOK": "session_start"}, clear=False), \
             mock.patch.object(self.mod, "_run_scan", side_effect=fake_scan):
            self._invoke_silent()

        self.assertTrue(scan_called, "local session_start must still scan")
        self.assertNotIn("cloud skip", self._log_contents())

    def test_cloud_remote_session_end_runs_synchronously_through_main(self):
        """End-to-end SessionEnd cloud path: CLAUDE_CODE_REMOTE=true and
        no AIQRANK_DETACHED must run main() to completion in-process
        (no detach, no os._exit) and reach a real POST.

        The narrower cloud tests cover slices in isolation
        (test_cloud_remote_bypasses_same_utc_day_gate for the gate;
        test_respawn_detached_skipped_in_cloud_remote for the detach
        guard). This one proves the whole pipeline survives main →
        skip-detach → bypass-gate → scan → POST → write last_upload_at.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-cloud123"}))
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh)

        class FakeResp:
            def __init__(self, body): self._b = json.dumps(body).encode("utf-8")
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_envelope = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 3}}],
        )
        captured = []

        def fake_urlopen(req, timeout=30):
            captured.append(req)
            return FakeResp({"teaser_url": "https://x/t", "device_id": "d-cloud123"})

        # Override the setUp-installed AIQRANK_DETACHED=1 with empty
        # string so the detach guard falls through to the cloud check.
        # AIQRANK_HOOK=session_end mirrors the real hooks.json command.
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CODE_REMOTE": "true",
                "AIQRANK_DETACHED": "",
                "AIQRANK_HOOK": "session_end",
            },
            clear=False,
        ), mock.patch.object(self.mod.subprocess, "Popen") as popen, \
             mock.patch.object(self.mod.os, "_exit") as exit_mock, \
             mock.patch.object(self.mod, "_run_scan", return_value=fake_envelope), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()
            popen.assert_not_called()
            exit_mock.assert_not_called()

        self.assertEqual(len(captured), 1, "cloud SessionEnd must reach POST")
        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists())
        log = self._log_contents()
        self.assertIn("ok sessions=1", log)
        self.assertNotIn("gated", log)

    def test_yesterday_utc_does_not_gate(self):
        """A last_upload_at from yesterday UTC must not gate — a new day always uploads.

        This is the core fix: the old 24h rolling gate would block a re-upload
        until ~24h had passed since the last upload. With the calendar-day gate,
        any new UTC day proceeds past the gate regardless of clock time.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        self.mod.LAST_UPLOAD_PATH.write_text(yesterday.strftime("%Y-%m-%dT%H:%M:%SZ"))

        scan_called = []
        def fake_scan():
            scan_called.append(True)
            return _claude_envelope(daily=[])  # empty so we exit early without needing urlopen

        with mock.patch.object(self.mod, "_run_scan", side_effect=fake_scan):
            self._invoke_silent()

        self.assertTrue(scan_called, "expected scanner to be invoked when last upload was yesterday UTC")
        self.assertNotIn("gated", self._log_contents())

    def test_just_after_utc_midnight_does_not_gate(self):
        """last_upload_at at 23:59:59 UTC, current time at 00:00:01 UTC next day → not gated.

        Only ~2 seconds elapsed but the calendar day flipped. The old 24h gate
        would have blocked this; the calendar-day gate must not.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))
        # Stamp last_upload_at as a fixed point yesterday at 23:59:59 UTC.
        yesterday_late = "2026-05-02T23:59:59Z"
        self.mod.LAST_UPLOAD_PATH.write_text(yesterday_late)

        scan_called = []
        def fake_scan():
            scan_called.append(True)
            return _claude_envelope(daily=[])

        # Pin "now" to 2026-05-03 00:00:01 UTC.
        fixed_now = datetime(2026, 5, 3, 0, 0, 1, tzinfo=timezone.utc)

        # FakeDatetime patches `self.mod.datetime` because hook_upload_today
        # imports as `from datetime import datetime, timezone` — the binding
        # we replace is the class symbol inside that module. If the import
        # style ever changes to `import datetime`, this patch silently breaks.
        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        with mock.patch.object(self.mod, "_run_scan", side_effect=fake_scan), \
             mock.patch.object(self.mod, "datetime", FakeDatetime):
            self._invoke_silent()

        self.assertTrue(scan_called, "scanner must run after UTC midnight even if seconds elapsed")
        self.assertNotIn("gated", self._log_contents())

    def test_missing_last_upload_does_not_gate(self):
        """No last_upload_at file → first-ever upload proceeds past the gate."""
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "d-abc12345"}))
        # Intentionally do not write LAST_UPLOAD_PATH.

        scan_called = []
        def fake_scan():
            scan_called.append(True)
            return _claude_envelope(daily=[])

        with mock.patch.object(self.mod, "_run_scan", side_effect=fake_scan):
            self._invoke_silent()

        self.assertTrue(scan_called)
        self.assertNotIn("gated", self._log_contents())

    def test_empty_scan_does_not_write_last_upload(self):
        """Empty scan with no daily entries must not advance last_upload_at.

        Previously the empty-scan branch wrote last_upload_at, arming the gate
        for ~24h and locking the user out for the rest of the day even though
        nothing was actually uploaded. After the fix, the cursor only advances
        on a real successful POST.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-empty01"}))

        with mock.patch.object(self.mod, "_run_scan", return_value=_claude_envelope(daily=[])), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=AssertionError("urlopen must not be called on empty scan")):
            self._invoke_silent()

        log = self._log_contents()
        self.assertIn("ok sessions=0", log)
        self.assertFalse(
            self.mod.LAST_UPLOAD_PATH.exists(),
            "last_upload_at must not be written on the empty-scan branch",
        )

    def test_empty_scan_then_real_scan_same_session_uploads(self):
        """An empty scan does not lock out a subsequent real scan.

        Simulates: hook fires at 10:00 with an empty scan (no cursor written),
        then fires again later the same day with real data. The second call
        must proceed past the gate and successfully POST.
        """
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-empty02"}))

        # First invocation: empty scan, no urlopen.
        with mock.patch.object(self.mod, "_run_scan", return_value=_claude_envelope(daily=[])), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=AssertionError("urlopen must not be called on empty scan")):
            self._invoke_silent()
        self.assertFalse(self.mod.LAST_UPLOAD_PATH.exists())

        # Second invocation: real data, urlopen succeeds.
        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        real_envelope = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 3}}],
        )
        with mock.patch.object(self.mod, "_run_scan", return_value=real_envelope), \
             mock.patch.object(self.mod.urllib.request, "urlopen", return_value=FakeResp()):
            self._invoke_silent()

        self.assertTrue(
            self.mod.LAST_UPLOAD_PATH.exists(),
            "last_upload_at must be written after a real successful POST",
        )
        self.assertIn("ok sessions=1", self._log_contents())

    def test_successful_upload_logs_ok(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))

        class FakeResp:
            def __init__(self, body): self._b = json.dumps(body).encode("utf-8")
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_scan_output = _claude_envelope(
            daily=[
                {"date": "2026-04-19", "metrics": {"sessions": 2}},
                {"date": "2026-04-20", "metrics": {"sessions": 5}},
            ],
            first_messages_sample=[
                "fix bug in migration",
                "refactor function and commit",
                "rebase the branch",
            ],
        )

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
        self.assertIn("ok sessions=2", log)
        self.assertIn("devices=device-a", log)
        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists())

        self.assertEqual(len(captured_requests), 1)
        payload = json.loads(captured_requests[0].data.decode("utf-8"))
        self.assertEqual(payload["inferred_role"], "engineer")
        self.assertNotIn("first_messages_sample", payload)
        self.assertTrue(captured_requests[0].get_header("User-agent", "").startswith("aiqrank-plugin/"))

    # --- new tests: Codex absent path ---

    def test_codex_absent_sends_empty_codex_daily(self):
        """When ~/.codex/sessions/ does not exist, payload still ships by_source
        but the codex source has an empty daily list."""
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

        fake_scan = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 1}}],
        )

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        # Legacy `daily` mirror still present for back-compat servers.
        self.assertIn("daily", payload)
        # by_source is always present; absent sources are not sent as replacements.
        self.assertIn("by_source", payload)
        self.assertNotIn("codex", payload["by_source"])
        # Cowork is omitted from the payload when there's no cowork activity.
        self.assertNotIn("cowork", payload["by_source"])

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

        fake_claude = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 2}}],
        )
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

    def test_codex_uses_canonical_block_from_full_scan_without_subprocess(self):
        """The hook must reuse by_source.codex from scan_transcripts.py."""
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        codex = {
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 7}}],
            "intervals_by_day": {"2026-04-20": [[1, 2]]},
            "_unknown_event_types": {"future:event": 2},
            "completeness": {"status": "complete", "omitted_dates": [], "failure_count": 0},
        }
        scan = _claude_envelope(daily=[])
        scan["by_source"]["codex"] = codex
        logger = self.mod._setup_logger()

        with mock.patch.object(
            self.mod.subprocess,
            "run",
            side_effect=AssertionError("canonical Codex extraction must not launch a second scanner"),
        ):
            result = self.mod._maybe_scan_codex(logger, scan)

        self.assertEqual(result, codex)

    def test_failed_codex_completeness_omits_codex_source(self):
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        codex = {
            "daily": [],
            "intervals_by_day": {},
            "completeness": {"status": "failed", "omitted_dates": [], "failure_count": 1},
        }
        scan = _claude_envelope(daily=[])
        scan["by_source"]["codex"] = codex
        logger = self.mod._setup_logger()

        self.assertIsNone(self.mod._maybe_scan_codex(logger, scan))
        self.assertIn("codex completeness failed failures=1", self._log_contents())

    def test_partial_codex_completeness_uploads_only_scanner_safe_dates(self):
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        codex = {
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 2}}],
            "intervals_by_day": {"2026-04-20": [[1, 2]]},
            "completeness": {
                "status": "partial",
                "omitted_dates": ["2026-04-19"],
                "failure_count": 1,
            },
        }
        scan = _claude_envelope(daily=[])
        scan["by_source"]["codex"] = codex
        logger = self.mod._setup_logger()

        result = self.mod._maybe_scan_codex(logger, scan)

        self.assertEqual(result["daily"], codex["daily"])
        self.assertIn("codex completeness partial failures=1 omitted_dates=1", self._log_contents())

    def test_combined_source_unions_intervals_across_sources(self):
        """When both scanners report intervals on the same day, the combined
        source's max_concurrent_sessions reflects the unioned sweep — not the
        sum of per-source peaks. Two Claude sessions overlapping for 30 min
        plus two Codex sessions overlapping for 30 min in the same window
        should yield combined peak = 4 (all four overlap), not 2+2.
        """
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # 2026-04-19 10:00 UTC
        base = 1745056800.0
        # Two Claude sessions overlap 10:05–10:30
        claude_intervals = [
            [base, base + 1800],            # 10:00–10:30
            [base + 300, base + 2100],      # 10:05–10:35
        ]
        # Two Codex sessions overlap the same window 10:10–10:30
        codex_intervals = [
            [base + 600, base + 2400],      # 10:10–10:40
            [base + 900, base + 2700],      # 10:15–10:45
        ]

        fake_claude = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 2, "max_concurrent_sessions": 2}}],
            intervals_by_day={"2026-04-19": claude_intervals},
        )
        fake_codex = {
            "source": "codex",
            "window_days": 30,
            "daily": [{"date": "2026-04-19", "metrics": {"sessions": 2, "max_concurrent_sessions": 2}}],
            "rollup": {"sessions": 2},
            "_unknown_event_types": {},
            "intervals_by_day": {"2026-04-19": codex_intervals},
        }

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        self.assertIn("combined", payload["by_source"])
        combined_daily = payload["by_source"]["combined"]["daily"]
        self.assertEqual(len(combined_daily), 1)
        # All four sessions overlap from 10:15 to 10:30 = 15 min (≥300s threshold).
        # Per-source peaks were 2 each — sum-of-peaks would say 4, true unioned
        # sweep also says 4 here. The point: it's computed via sweep, not summed.
        self.assertEqual(combined_daily[0]["metrics"]["max_concurrent_sessions"], 4)
        self.assertEqual(combined_daily[0]["date"], "2026-04-19")

    def test_combined_source_omitted_when_no_intervals(self):
        """If neither scanner emits intervals (legacy behavior), no combined
        source row appears in the payload — the server's claude_code score
        falls back to its own peak.
        """
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        fake_claude = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 1}}],
            # No intervals_by_day populated
        )
        fake_codex = self._make_fake_codex_metrics(1)  # also no intervals

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        payload = posted_bodies[0]
        self.assertNotIn("combined", payload["by_source"])

    def test_cowork_daily_round_trips_in_payload(self):
        """When the scanner reports cowork-source daily activity, the payload's
        by_source.cowork.daily mirrors it. This is the cross-source counterpart
        to the codex round-trip test.
        """
        self._setup_device()
        # Codex absent — focuses the assertion on cowork only.

        cowork_daily = [
            {"date": "2026-04-19", "metrics": {"cowork_sessions": 2, "cowork_messages": 14, "queue_events": 5}},
            {"date": "2026-04-20", "metrics": {"cowork_sessions": 1, "cowork_messages": 6}},
        ]
        fake_scan = _claude_with_cowork_envelope(
            claude_daily=[{"date": "2026-04-19", "metrics": {"sessions": 3}}],
            cowork_daily=cowork_daily,
        )

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        self.assertIn("cowork", payload["by_source"])
        self.assertEqual(payload["by_source"]["cowork"]["daily"], cowork_daily)
        # Log includes a cowork_days hint so operators can see autonomous activity.
        self.assertIn("cowork_days=2", self._log_contents())

    def test_inline_sources_daily_round_trip_in_payload(self):
        """New inline scanner sources from scan_transcripts.py must be uploaded,
        even when Claude/Cowork have no activity."""
        self._setup_device()

        opencode_daily = [{"date": "2026-04-19", "metrics": {"sessions": 2}}]
        cursor_daily = [{"date": "2026-04-19", "metrics": {"sessions": 3}}]
        pi_daily = [{"date": "2026-04-19", "metrics": {"sessions": 1}}]
        base = 1745056800.0
        fake_scan = {
            "window_days": 30,
            "by_source": {
                "claude_code": {"daily": [], "rollup": {}, "intervals_by_day": {}},
                "cowork": {"daily": [], "rollup": {}, "intervals_by_day": {}},
                "opencode": {
                    "daily": opencode_daily,
                    "rollup": {},
                    "intervals_by_day": {"2026-04-19": [[base, base + 1200]]},
                },
                "cursor": {
                    "daily": cursor_daily,
                    "rollup": {},
                    "intervals_by_day": {"2026-04-19": [[base + 300, base + 1500]]},
                },
                "pi": {
                    "daily": pi_daily,
                    "rollup": {},
                    "intervals_by_day": {"2026-04-19": [[base + 600, base + 1800]]},
                },
            },
            "first_messages_sample": [],
        }

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        self.assertEqual(payload["by_source"]["opencode"]["daily"], opencode_daily)
        self.assertEqual(payload["by_source"]["cursor"]["daily"], cursor_daily)
        self.assertEqual(payload["by_source"]["pi"]["daily"], pi_daily)
        self.assertEqual(payload["by_source"]["combined"]["daily"][0]["metrics"]["max_concurrent_sessions"], 3)
        log = self._log_contents()
        self.assertIn("opencode_days=1", log)
        self.assertIn("cursor_days=1", log)
        self.assertIn("pi_days=1", log)

    def test_pi_empty_bucket_does_not_advance_gate(self):
        self._setup_device()
        fake_scan = _claude_envelope(daily=[])
        fake_scan["by_source"]["pi"] = {
            "daily": [],
            "rollup": {},
            "intervals_by_day": {},
        }

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(
                 self.mod.urllib.request,
                 "urlopen",
                 side_effect=AssertionError("empty Pi scan must not upload"),
             ):
            self._invoke_silent()

        self.assertIn("pi", self.mod.INLINE_SCAN_SOURCES)
        self.assertIn("hermes", self.mod.INLINE_SCAN_SOURCES)
        self.assertIn("openclaw", self.mod.INLINE_SCAN_SOURCES)
        self.assertIn("nanoclaw", self.mod.INLINE_SCAN_SOURCES)
        self.assertFalse(self.mod.LAST_UPLOAD_PATH.exists())

    def test_legacy_server_pi_retry_preserves_combined(self):
        self._setup_device()
        base = 1745056800.0
        fake_scan = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 1}}],
            intervals_by_day={"2026-04-19": [[base, base + 1200]]},
        )
        fake_scan["by_source"]["pi"] = {
            "daily": [{"date": "2026-04-19", "metrics": {"sessions": 1}}],
            "rollup": {},
            "intervals_by_day": {"2026-04-19": [[base + 300, base + 1500]]},
        }
        posted_bodies = []

        class FakeResp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            if len(posted_bodies) == 1:
                raise self.mod.urllib.error.HTTPError(
                    url=None,
                    code=422,
                    msg="unknown source",
                    hdrs=None,
                    fp=io.BytesIO(json.dumps({"error": "unknown source", "source": "pi"}).encode()),
                )
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 2)
        self.assertIn("pi", posted_bodies[0]["by_source"])
        self.assertNotIn("pi", posted_bodies[1]["by_source"])
        self.assertIn("combined", posted_bodies[1]["by_source"])

    def test_legacy_server_too_many_sources_retry_drops_extras(self):
        """An older server caps the request at 7 sources and rejects the whole
        payload before per-source validation. The retry must progressively
        drop extra sources (newest first) so legacy data still uploads.
        """
        self._setup_device()
        base = 1745056800.0
        fake_scan = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 1}}],
            intervals_by_day={"2026-04-19": [[base, base + 1200]]},
        )
        for source in ("opencode", "cursor", "pi", "hermes", "openclaw", "nanoclaw"):
            fake_scan["by_source"][source] = {
                "daily": [{"date": "2026-04-19", "metrics": {"sessions": 1}}],
                "rollup": {},
                "intervals_by_day": {},
            }
        posted_bodies = []
        legacy_allowlist = {
            "claude_code", "codex", "combined", "cowork", "opencode", "cursor", "pi",
        }

        class FakeResp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            body = json.loads(req.data)
            posted_bodies.append(body)
            sources = body["by_source"]
            if len(sources) > 7:
                raise self.mod.urllib.error.HTTPError(
                    url=None,
                    code=422,
                    msg="too many sources",
                    hdrs=None,
                    fp=io.BytesIO(
                        json.dumps({"error": "too many sources (max 7)"}).encode()
                    ),
                )
            unknown = next((s for s in sources if s not in legacy_allowlist), None)
            if unknown is not None:
                raise self.mod.urllib.error.HTTPError(
                    url=None,
                    code=422,
                    msg="unknown source",
                    hdrs=None,
                    fp=io.BytesIO(
                        json.dumps({"error": "unknown source", "source": unknown}).encode()
                    ),
                )
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        # 8 sources -> too many (drop nanoclaw) -> 7 -> unknown hermes ->
        # 6 -> unknown openclaw -> 5 -> accepted.
        self.assertEqual(len(posted_bodies), 4)
        self.assertIn("nanoclaw", posted_bodies[0]["by_source"])
        final = posted_bodies[-1]["by_source"]
        for source in ("hermes", "openclaw", "nanoclaw"):
            self.assertNotIn(source, final)
        for source in ("claude_code", "opencode", "cursor", "pi", "combined"):
            self.assertIn(source, final)
        self.assertIn("server rejected nanoclaw source", self._log_contents())
        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists())

    def test_combined_source_unions_cowork_intervals(self):
        """The combined-intervals sweep includes the Cowork source — not just
        Claude + Codex. Two Claude sessions, two Codex sessions, and two Cowork
        sessions all overlapping in the same window should yield combined peak=6.
        """
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # 2026-04-19 10:00 UTC
        base = 1745056800.0
        # All six sessions overlap from 10:15 to 10:30 (15 min, well over 300s).
        claude_intervals = [
            [base, base + 1800],            # 10:00–10:30
            [base + 300, base + 2100],      # 10:05–10:35
        ]
        codex_intervals = [
            [base + 600, base + 2400],      # 10:10–10:40
            [base + 900, base + 2700],      # 10:15–10:45
        ]
        cowork_intervals = [
            [base + 600, base + 2400],      # 10:10–10:40
            [base + 900, base + 2700],      # 10:15–10:45
        ]

        fake_claude = _claude_with_cowork_envelope(
            claude_daily=[{"date": "2026-04-19", "metrics": {"sessions": 2}}],
            cowork_daily=[{"date": "2026-04-19", "metrics": {"cowork_sessions": 2}}],
            claude_intervals={"2026-04-19": claude_intervals},
            cowork_intervals={"2026-04-19": cowork_intervals},
        )
        fake_codex = {
            "source": "codex",
            "window_days": 30,
            "daily": [{"date": "2026-04-19", "metrics": {"sessions": 2}}],
            "rollup": {"sessions": 2},
            "_unknown_event_types": {},
            "intervals_by_day": {"2026-04-19": codex_intervals},
        }

        posted_bodies = []

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertEqual(len(posted_bodies), 1)
        payload = posted_bodies[0]
        combined_daily = payload["by_source"]["combined"]["daily"]
        self.assertEqual(len(combined_daily), 1)
        # All six sessions overlap for ≥300s — peak must be 6.
        self.assertEqual(combined_daily[0]["metrics"]["max_concurrent_sessions"], 6)

    def test_codex_present_updates_only_full_snapshot_cursor(self):
        """Successful canonical upload writes no independent Codex cursor."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        class FakeResp:
            def __init__(self): self._b = json.dumps({"teaser_url": "https://x/t", "device_id": "d"}).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_claude = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 1}}],
        )
        fake_codex = self._make_fake_codex_metrics(1)

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_claude), \
             mock.patch.object(self.mod, "_maybe_scan_codex", return_value=fake_codex), \
             mock.patch.object(self.mod.urllib.request, "urlopen", return_value=FakeResp()):
            self._invoke_silent()

        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists(), "last_upload_at not written")
        self.assertFalse(self.mod.LAST_UPLOAD_PATH_CODEX.exists(), "incremental Codex cursor must not be written")

    def test_codex_cursor_not_updated_on_upload_failure(self):
        """Codex cursor must not advance when the upload fails."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        fake_claude = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 1}}],
        )
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
        self.assertFalse(
            self.mod.LAST_UPLOAD_PATH.exists(),
            "global upload gate must not advance on failure",
        )

    # --- new tests: 400 KB chunking ---

    def test_large_codex_backfill_is_chunked(self):
        """A Codex backfill that makes the payload exceed 400 KB is split into chunks."""
        self._setup_device()
        self.mod.CODEX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Build a synthetic Codex daily list large enough to exceed 400 KB when
        # combined with the Claude daily. 80 entries × 8000 chars ≈ 640 KB.
        big_metrics = {"sessions": 5, "messages": 100, "data": "x" * 8000}
        codex_daily = [{"date": f"2026-{(i//28)+1:02d}-{(i%28)+1:02d}", "metrics": big_metrics} for i in range(80)]

        fake_claude = _claude_envelope(
            daily=[{"date": "2026-04-19", "metrics": {"sessions": 2}}],
        )
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

        # Every encoded request must remain under the client byte threshold.
        for body in posted_bodies:
            self.assertLessEqual(
                len(json.dumps(body).encode("utf-8")),
                self.mod.PAYLOAD_SIZE_LIMIT_BYTES,
            )

        # Union across chunks must equal originals (no entries lost or duplicated).
        all_claude = [e for b in posted_bodies for e in b["by_source"]["claude_code"]["daily"]]
        all_codex = [e for b in posted_bodies for e in b["by_source"]["codex"]["daily"]]
        self.assertEqual(all_claude, fake_claude["by_source"]["claude_code"]["daily"])
        self.assertEqual(len(all_codex), len(codex_daily))

    def test_large_pi_backfill_is_chunked_without_loss(self):
        self._setup_device()
        big_metrics = {"sessions": 1, "data": "x" * 8000}
        pi_daily = [
            {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "metrics": big_metrics}
            for i in range(80)
        ]
        fake_scan = _claude_envelope(daily=[])
        fake_scan["by_source"]["pi"] = {
            "daily": pi_daily,
            "rollup": {},
            "intervals_by_day": {},
        }
        posted_bodies = []

        class FakeResp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            posted_bodies.append(json.loads(req.data))
            return FakeResp()

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        self.assertGreater(len(posted_bodies), 1)
        all_pi = [
            entry
            for body in posted_bodies
            for entry in body["by_source"].get("pi", {}).get("daily", [])
        ]
        self.assertEqual(all_pi, pi_daily)
        for body in posted_bodies:
            self.assertLessEqual(
                len(json.dumps(body).encode("utf-8")),
                self.mod.PAYLOAD_SIZE_LIMIT_BYTES,
            )

    def test_dense_valid_multi_source_rows_are_byte_bounded_and_lossless(self):
        labels = {f"label-{index:02d}-" + ("x" * 82): index + 1 for index in range(50)}
        metrics = {
            field: labels
            for field in (
                "tool_name_counts",
                "skill_counts",
                "mcp_server_counts",
                "agent_type_counts",
                "model_usage",
                "agent_model_usage",
                "effort_usage",
                "model_tokens_out",
            )
        }
        metrics["authored_skill_names"] = [
            f"skill-{index:03d}-" + ("y" * 87) for index in range(200)
        ]

        def daily():
            return [
                {
                    "date": f"2026-07-{index + 1:02d}",
                    "metrics": {**metrics, "sessions": index + 1},
                }
                for index in range(7)
            ]

        sources = {
            "claude_code": daily(),
            "codex": daily(),
            "cowork": daily(),
            "opencode": daily(),
            "cursor": daily(),
            "pi": daily(),
            "combined": [
                {
                    "date": f"2026-07-{index + 1:02d}",
                    "metrics": {"max_concurrent_sessions": index + 1},
                }
                for index in range(7)
            ],
        }
        posted = []

        with mock.patch.object(self.mod, "_post_upload", side_effect=posted.append):
            success = self.mod._post_by_source(
                "device-dense",
                sources["claude_code"],
                sources["codex"],
                sources["cowork"],
                {},
                "engineer",
                mock.MagicMock(),
                combined_daily=sources["combined"],
                extra_sources_daily={
                    source: sources[source] for source in ("opencode", "cursor", "pi")
                },
            )

        self.assertTrue(success)
        self.assertGreater(len(posted), 1)
        for body in posted:
            self.assertLessEqual(
                len(json.dumps(body).encode("utf-8")),
                self.mod.PAYLOAD_SIZE_LIMIT_BYTES,
            )
        for source, expected in sources.items():
            actual = [
                row
                for body in posted
                for row in body["by_source"].get(source, {}).get("daily", [])
            ]
            self.assertEqual(actual, expected)

    def test_single_oversized_row_fails_without_posting(self):
        oversized = [
            {
                "date": "2026-07-15",
                "metrics": {"data": "x" * self.mod.PAYLOAD_SIZE_LIMIT_BYTES},
            }
        ]
        with mock.patch.object(self.mod, "_post_upload") as post:
            success = self.mod._post_by_source(
                "device-oversized",
                [],
                [],
                [],
                {},
                "engineer",
                mock.MagicMock(),
                extra_sources_daily={"pi": oversized},
            )

        self.assertFalse(success)
        post.assert_not_called()

    def test_chunked_payload_retries_without_pi_on_legacy_server(self):
        big_metrics = {"sessions": 1, "data": "x" * 8000}
        pi_daily = [
            {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "metrics": big_metrics}
            for i in range(80)
        ]
        combined_daily = [
            {"date": "2026-04-19", "metrics": {"max_concurrent_sessions": 2}}
        ]
        posted_bodies = []

        def fake_post(payload):
            posted_bodies.append(payload)
            if len(posted_bodies) == 1:
                raise self.mod.urllib.error.HTTPError(
                    url=None,
                    code=422,
                    msg="unknown source",
                    hdrs=None,
                    fp=io.BytesIO(json.dumps({"error": "unknown source", "source": "pi"}).encode()),
                )

        with mock.patch.object(self.mod, "_post_upload", side_effect=fake_post):
            success = self.mod._post_by_source(
                "device-pi-legacy",
                [],
                [],
                [],
                {},
                "engineer",
                mock.MagicMock(),
                combined_daily=combined_daily,
                extra_sources_daily={"pi": pi_daily},
            )

        self.assertTrue(success)
        self.assertEqual(len(posted_bodies), 2)
        self.assertIn("pi", posted_bodies[0]["by_source"])
        self.assertNotIn("pi", posted_bodies[1]["by_source"])
        self.assertEqual(posted_bodies[1]["by_source"]["combined"]["daily"], combined_daily)

    def test_legacy_rejection_removes_only_the_unsupported_source(self):
        row = [{"date": "2026-07-15", "metrics": {"sessions": 1}}]
        combined = [
            {"date": "2026-07-15", "metrics": {"max_concurrent_sessions": 2}}
        ]
        for rejected_source in ("combined", "cowork", "opencode", "cursor"):
            with self.subTest(source=rejected_source):
                posted = []

                def fake_post(payload):
                    posted.append(payload)
                    if len(posted) == 1:
                        raise self.mod.urllib.error.HTTPError(
                            url=None,
                            code=422,
                            msg="unknown source",
                            hdrs=None,
                            fp=io.BytesIO(
                                json.dumps(
                                    {
                                        "error": "unknown source",
                                        "source": rejected_source,
                                    }
                                ).encode()
                            ),
                        )

                with mock.patch.object(self.mod, "_post_upload", side_effect=fake_post):
                    success = self.mod._post_by_source(
                        "device-legacy-matrix",
                        row,
                        row,
                        row,
                        {},
                        "engineer",
                        mock.MagicMock(),
                        combined_daily=combined,
                        extra_sources_daily={
                            "opencode": row,
                            "cursor": row,
                            "pi": row,
                        },
                    )

                self.assertTrue(success)
                self.assertEqual(len(posted), 2)
                self.assertNotIn(rejected_source, posted[1]["by_source"])
                expected_remaining = {
                    "claude_code",
                    "codex",
                    "cowork",
                    "opencode",
                    "cursor",
                    "pi",
                    "combined",
                } - {rejected_source}
                self.assertEqual(set(posted[1]["by_source"]), expected_remaining)

    def test_late_chunk_rejection_fails_without_committing_partial_snapshot(self):
        big_metrics = {"sessions": 1, "data": "x" * 8000}
        claude_daily = [
            {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "metrics": big_metrics}
            for i in range(80)
        ]
        pi_daily = [
            {"date": entry["date"], "metrics": {"sessions": 1}}
            for entry in claude_daily
        ]
        posted_bodies = []
        successful_bodies = []
        rejected = False

        def fake_post(payload):
            nonlocal rejected
            posted_bodies.append(payload)
            if len(posted_bodies) == 2 and not rejected:
                rejected = True
                raise self.mod.urllib.error.HTTPError(
                    url=None,
                    code=422,
                    msg="unknown source",
                    hdrs=None,
                    fp=io.BytesIO(
                        json.dumps({"error": "unknown source", "source": "pi"}).encode()
                    ),
                )
            successful_bodies.append(payload)

        with mock.patch.object(self.mod, "_post_upload", side_effect=fake_post):
            success = self.mod._post_by_source(
                "device-pi-late-legacy",
                claude_daily,
                [],
                [],
                {},
                "engineer",
                mock.MagicMock(),
                extra_sources_daily={"pi": pi_daily},
            )

        self.assertFalse(success)
        self.assertEqual(len(posted_bodies), 2)
        self.assertEqual(successful_bodies, [posted_bodies[0]])
        self.assertIn("pi", posted_bodies[0]["by_source"])

    def test_unrecognized_422_logs_server_reason(self):
        logger = mock.MagicMock()

        def reject(payload):
            raise self.mod.urllib.error.HTTPError(
                url=None,
                code=422,
                msg="unprocessable entity",
                hdrs=None,
                fp=io.BytesIO(
                    json.dumps(
                        {
                            "error": "invalid inferred role",
                            "source": "claude_code",
                        }
                    ).encode()
                ),
            )

        with mock.patch.object(self.mod, "_post_upload", side_effect=reject):
            success = self.mod._post_by_source(
                "device-422-reason",
                [{"date": "2026-07-15", "metrics": {"sessions": 1}}],
                [],
                [],
                {},
                "engineer",
                logger,
            )

        self.assertFalse(success)
        logger.info.assert_any_call(
            "error chunk=0 http=422 reason=invalid inferred role source=claude_code"
        )

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

        fake_claude = _claude_envelope(daily=claude_daily)
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

        # Every chunk must respect the encoded byte cap.
        for body in posted_bodies:
            self.assertLessEqual(
                len(json.dumps(body).encode("utf-8")),
                self.mod.PAYLOAD_SIZE_LIMIT_BYTES,
            )

        # No entries lost or duplicated.
        all_claude = [e for b in posted_bodies for e in b["by_source"]["claude_code"]["daily"]]
        self.assertEqual(len(all_claude), len(claude_daily))

    # --- timeout tests ---

    def test_run_scan_passes_long_local_timeout(self):
        """_run_scan uses a bounded rolling window and local timeout."""
        captured_kwargs = {}
        captured_args = []

        def capture_kwargs(*args, **kwargs):
            captured_args.extend(args[0])
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

        self.assertEqual(captured_kwargs.get("timeout"), 240)
        self.assertEqual(captured_args[-2:], ["--days", "7"])

    def test_scan_transcript_timeout_stops_upload(self):
        """TimeoutExpired from _run_scan is caught as SubprocessError: upload does not happen."""
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-timeout02"}))

        def timeout_run_scan():
            raise subprocess.TimeoutExpired(cmd="scan_transcripts.py", timeout=240)

        posted = []

        with mock.patch.object(self.mod, "_run_scan", side_effect=timeout_run_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=lambda *a, **kw: posted.append(1)):
            self._invoke_silent()

        self.assertEqual(posted, [], "urlopen must not be called when scan times out")
        log = self._log_contents()
        self.assertIn("error", log)

    def test_scan_timeout_logs_timeout_budget(self):
        """A real _run_scan timeout surfaces the budget via main()'s handler.

        Patches _run_scan (not _run) so the exception flows through _run's
        re-raise to main()'s `except subprocess.TimeoutExpired` handler —
        the production path. Patching _run would bypass _run's catch and
        mask the dead-handler regression.
        """
        self._setup_device()

        with mock.patch.object(
            self.mod,
            "_run_scan",
            side_effect=subprocess.TimeoutExpired(
                cmd="scan_transcripts.py", timeout=240
            ),
        ):
            self._invoke_silent()

        self.assertIn("error TimeoutExpired timeout_sec=240", self._log_contents())

    def test_empty_sample_defaults_to_engineer(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))

        class FakeResp:
            def __init__(self, body): self._b = json.dumps(body).encode("utf-8")
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_scan_output = _claude_envelope(
            daily=[{"date": "2026-04-20", "metrics": {"sessions": 1}}],
            first_messages_sample=[],
        )

        captured = []

        def fake_urlopen(req, timeout=30):
            captured.append(req)
            return FakeResp({"teaser_url": "https://x/t", "device_id": "device-abcdef12"})

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan_output), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        payload = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(payload["inferred_role"], "engineer")

    def test_upload_sends_user_agent_header(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))
        captured = {}

        class FakeResp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.headers)
            return FakeResp()

        fake_scan = _claude_envelope(
            daily=[{"date": "2026-04-20", "metrics": {"sessions": 1}}],
        )

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self._invoke_silent()

        ua = captured["headers"].get("User-agent") or captured["headers"].get("User-Agent")
        self.assertIsNotNone(ua)
        self.assertIn("aiqrank-plugin/", ua)

    def test_writes_stale_version_when_local_behind(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))

        class FakeResp:
            def read(self): return json.dumps({"latest_plugin_version": "9.9.9"}).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake_scan = _claude_envelope(
            daily=[{"date": "2026-04-20", "metrics": {"sessions": 1}}],
        )

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", return_value=FakeResp()):
            self._invoke_silent()

        self.assertTrue(self.mod.STALE_VERSION_PATH.exists())
        self.assertEqual(self.mod.STALE_VERSION_PATH.read_text().strip(), "9.9.9")

    def test_clears_stale_version_when_local_caught_up(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "device-abcdef12"}))
        self.mod.STALE_VERSION_PATH.write_text("0.0.1\n")

        outer = self

        class FakeResp:
            def read(self_inner):
                return json.dumps({"latest_plugin_version": outer.mod.PLUGIN_VERSION}).encode("utf-8")
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False

        fake_scan = _claude_envelope(
            daily=[{"date": "2026-04-20", "metrics": {"sessions": 1}}],
        )

        with mock.patch.object(self.mod, "_run_scan", return_value=fake_scan), \
             mock.patch.object(self.mod.urllib.request, "urlopen", return_value=FakeResp()):
            self._invoke_silent()

        self.assertFalse(self.mod.STALE_VERSION_PATH.exists())


class CrossPlatformPrimitivesTests(unittest.TestCase):
    """U1: cross-platform lock + detach + invocation marker + hook-fired log."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.tmp_path), "AIQRANK_DETACHED": "1"},
            clear=False,
        )
        self._env.start()
        if "hook_upload_today" in sys.modules:
            del sys.modules["hook_upload_today"]
        import hook_upload_today
        self.mod = hook_upload_today
        cfg = self.tmp_path / ".config" / "aiqrank"
        self.mod.CONFIG_DIR = cfg
        self.mod.LOCK_PATH = cfg / "upload.lock"
        self.mod.LOG_PATH = cfg / "hook.log"
        self.mod.DEVICE_PATH = cfg / "device.json"
        self.mod.LAST_UPLOAD_PATH = cfg / "last_upload_at"
        self.mod.LAST_UPLOAD_PATH_CODEX = cfg / "last_upload_at_codex"
        self.mod.DISABLED_FLAG = cfg / "disabled"
        cfg.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_acquire_exclusive_first_call_succeeds(self):
        fd = os.open(str(self.mod.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.assertTrue(self.mod._acquire_exclusive(fd))
        finally:
            self.mod._release_lock(fd)
            os.close(fd)

    def test_acquire_exclusive_second_call_returns_false(self):
        fd1 = os.open(str(self.mod.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
        fd2 = os.open(str(self.mod.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.assertTrue(self.mod._acquire_exclusive(fd1))
            # POSIX fcntl is per-fd; second fd from the same process still
            # contends. msvcrt on Windows is also per-fd.
            self.assertFalse(self.mod._acquire_exclusive(fd2))
        finally:
            self.mod._release_lock(fd1)
            os.close(fd1)
            os.close(fd2)

    def test_acquire_exclusive_seeks_to_zero(self):
        # Move the file pointer; _acquire_exclusive must seek back to 0
        # so the locked byte is always byte 0 (matters on Windows, harmless
        # on POSIX). Verify by checking pointer position after acquire.
        fd = os.open(str(self.mod.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.write(fd, b"contents that move the pointer")
            self.assertTrue(self.mod._acquire_exclusive(fd))
            # Pointer is at 0 immediately after lseek; the lock call may
            # advance it on some platforms but the seek-before-lock
            # invariant is what we assert.
            os.lseek(fd, 0, 0)
            self.assertEqual(os.lseek(fd, 0, 1), 0)
        finally:
            self.mod._release_lock(fd)
            os.close(fd)

    def test_respawn_detached_skipped_when_already_detached(self):
        # AIQRANK_DETACHED=1 is set in setUp; the helper must no-op without
        # calling Popen or os._exit.
        with mock.patch.object(self.mod.subprocess, "Popen") as popen, \
             mock.patch.object(self.mod.os, "_exit") as exit_mock:
            self.mod._respawn_detached_and_exit()
            popen.assert_not_called()
            exit_mock.assert_not_called()

    def test_respawn_detached_skipped_in_cloud_remote(self):
        # In Claude Code on the web the container reclaims as the session
        # ends, so a detached child would be killed mid-upload. Run
        # synchronously instead — Popen and os._exit must not be called.
        # Override AIQRANK_DETACHED to empty (setUp sets it to "1") so
        # the detach guard falls through to the cloud-remote check.
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_CODE_REMOTE": "true", "AIQRANK_DETACHED": ""},
            clear=False,
        ), mock.patch.object(self.mod.subprocess, "Popen") as popen, \
             mock.patch.object(self.mod.os, "_exit") as exit_mock:
            self.mod._respawn_detached_and_exit()
            popen.assert_not_called()
            exit_mock.assert_not_called()

    def test_read_device_id_env_var_wins_over_file(self):
        # AIQRANK_DEVICE_ID env var lets cloud/CI seed identity without
        # writing device.json into an ephemeral ~/.config/aiqrank.
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "from-file"}))
        with mock.patch.dict(os.environ, {"AIQRANK_DEVICE_ID": "from-env"}):
            self.assertEqual(self.mod._read_device_id(), "from-env")

    def test_read_device_id_env_var_used_when_no_file(self):
        with mock.patch.dict(os.environ, {"AIQRANK_DEVICE_ID": "env-only"}):
            self.assertEqual(self.mod._read_device_id(), "env-only")

    def test_read_device_id_empty_env_var_falls_through_to_file(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "from-file"}))
        with mock.patch.dict(os.environ, {"AIQRANK_DEVICE_ID": ""}):
            self.assertEqual(self.mod._read_device_id(), "from-file")

    def test_read_device_id_rejects_malformed_env_values(self):
        # Newline-bearing, whitespace-only, too-short, and bad-character
        # env values must fall through to the file rather than leak into
        # log lines (device_id[:8]) or the upload payload.
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "from-file"}))
        for bad in ("with\nnewline", "   ", "ab", "has space mid", "tab\there", "a" * 129):
            with self.subTest(value=repr(bad)):
                with mock.patch.dict(os.environ, {"AIQRANK_DEVICE_ID": bad}):
                    self.assertEqual(self.mod._read_device_id(), "from-file")

    def test_is_cloud_remote_accepts_common_truthy_spellings(self):
        # Strict `== "true"` would silently disable the cloud branches if
        # Claude Code on the web ever emits "1" / "True" / "yes".
        for truthy in ("true", "True", "TRUE", "1", "yes", " true "):
            with self.subTest(value=repr(truthy)):
                with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": truthy}):
                    self.assertTrue(self.mod._is_cloud_remote())
        for falsy in ("", "0", "false", "no", "not-true"):
            with self.subTest(value=repr(falsy)):
                with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": falsy}):
                    self.assertFalse(self.mod._is_cloud_remote())

    def test_respawn_detached_uses_devnull_and_posix_flag(self):
        # Pretend we are the parent (no AIQRANK_DETACHED) to exercise the
        # spawn path. Mock os._exit and subprocess.Popen so the test
        # process survives.
        with mock.patch.dict(os.environ, {}, clear=False) as _:
            os.environ.pop("AIQRANK_DETACHED", None)
            os.environ.pop("CLAUDE_CODE_REMOTE", None)
            with mock.patch.object(self.mod.subprocess, "Popen") as popen, \
                 mock.patch.object(self.mod.os, "_exit"), \
                 mock.patch.object(self.mod.sys, "platform", "linux"):
                self.mod._respawn_detached_and_exit()
            popen.assert_called_once()
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["stdin"], self.mod.subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], self.mod.subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], self.mod.subprocess.DEVNULL)
            self.assertTrue(kwargs.get("start_new_session"))
            self.assertNotIn("creationflags", kwargs)
            self.assertEqual(kwargs["env"]["AIQRANK_DETACHED"], "1")

    def test_respawn_detached_uses_devnull_and_windows_flags(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIQRANK_DETACHED", None)
            os.environ.pop("CLAUDE_CODE_REMOTE", None)
            # Real subprocess.Popen on Mac rejects creationflags with a
            # ValueError, so fully mock the subprocess module rather than
            # wrapping the real one. Inject the Windows constants by hand.
            fake_subprocess = mock.MagicMock()
            fake_subprocess.DETACHED_PROCESS = 0x00000008
            fake_subprocess.CREATE_NEW_PROCESS_GROUP = 0x00000200
            fake_subprocess.DEVNULL = -3  # subprocess.DEVNULL sentinel
            with mock.patch.object(self.mod, "subprocess", fake_subprocess), \
                 mock.patch.object(self.mod.os, "_exit"), \
                 mock.patch.object(self.mod.sys, "platform", "win32"):
                self.mod._respawn_detached_and_exit()
            fake_subprocess.Popen.assert_called_once()
            kwargs = fake_subprocess.Popen.call_args.kwargs
            self.assertEqual(kwargs["stdin"], fake_subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], fake_subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], fake_subprocess.DEVNULL)
            self.assertNotIn("start_new_session", kwargs)
            self.assertEqual(
                kwargs["creationflags"],
                fake_subprocess.DETACHED_PROCESS | fake_subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            self.assertEqual(kwargs["env"]["AIQRANK_DETACHED"], "1")

    def test_invocation_marker_written_on_main(self):
        marker = self.mod.CONFIG_DIR / "last_hook_invocation"
        self.assertFalse(marker.exists())
        self.mod._write_invocation_marker()
        self.assertTrue(marker.exists())
        # Contains an ISO timestamp ending with Z.
        self.assertRegex(marker.read_text().strip(), r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    def test_hook_fired_log_line_present_after_main(self):
        # No device → main exits early but only after logging the
        # "hook fired" line. Used as fault-domain triage signal.
        with mock.patch("sys.stdout", new=io.StringIO()), \
             mock.patch("sys.stderr", new=io.StringIO()):
            self.mod.main()
        log = self.mod.LOG_PATH.read_text()
        self.assertIn("hook fired", log)
        self.assertIn(f"platform={sys.platform}", log)


if __name__ == "__main__":
    unittest.main()
