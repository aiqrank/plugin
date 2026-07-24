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
        self.mod.check_update.CONFIG_DIR = self.mod.CONFIG_DIR
        self.mod.check_update.STALE_VERSION_PATH = self.mod.CONFIG_DIR / "stale_version"

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
        self.assertNotIn("by_source", captured["body"])
        self.assertNotIn("device_id", captured["body"])
        self.assertTrue(captured["user_agent"].startswith("aiqrank-plugin/"))
        # Device.json persisted
        self.assertTrue(self.mod.DEVICE_PATH.exists())
        saved = json.loads(self.mod.DEVICE_PATH.read_text())
        self.assertEqual(saved["device_id"], "dev-new-123")
        # last_upload_at persisted
        self.assertTrue(self.mod.LAST_UPLOAD_PATH.exists())

    def test_records_server_update_for_the_next_codex_prompt(self):
        rc, _captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=update",
            "device_id": "dev-update",
            "latest_plugin_version": "9.9.9",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(
            self.mod.check_update.STALE_VERSION_PATH.read_text(), "9.9.9\n"
        )

    def test_returning_device_sends_device_id(self):
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": "existing-dev"}))
        rc, captured, stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=xyz",
            "device_id": "existing-dev",
        })
        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["device_id"], "existing-dev")

    def test_no_open_skips_browser_launch(self):
        response_body = {
            "teaser_url": "https://aiqrank.com/teaser?s=noopen",
            "device_id": "dev-new-789",
        }

        def fake_urlopen(req, timeout=30):
            return FakeResponse(response_body)

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(self.mod, "open_in_browser") as open_in_browser, \
             mock.patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self.mod.main([
                "--metrics",
                str(self.metrics_path),
                "--role",
                "engineer",
                "--no-open",
            ])

        self.assertEqual(rc, 0)
        self.assertIn("Rank updated at https://aiqrank.com/teaser?s=noopen", stdout.getvalue())
        open_in_browser.assert_not_called()

    def test_by_source_metrics_payload_is_preserved(self):
        metrics = {
            "window_days": 30,
            "by_source": {
                "claude_code": {"daily": []},
                "opencode": {"daily": [{"date": "2026-04-20", "metrics": {"sessions": 2}}]},
                "cursor": {"daily": [{"date": "2026-04-20", "metrics": {"sessions": 3}}]},
                "pi": {"daily": [{"date": "2026-04-20", "metrics": {"sessions": 4}}]},
            },
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=multi",
            "device_id": "dev-new-456",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["daily"], [])
        self.assertEqual(captured["body"]["by_source"]["opencode"]["daily"][0]["metrics"]["sessions"], 2)
        self.assertEqual(captured["body"]["by_source"]["cursor"]["daily"][0]["metrics"]["sessions"], 3)
        self.assertEqual(captured["body"]["by_source"]["pi"]["daily"][0]["metrics"]["sessions"], 4)

    def test_by_source_payload_strips_local_only_data_and_failed_codex(self):
        metrics = {
            "window_days": 30,
            "by_source": {
                "claude_code": {
                    "daily": [{"date": "2026-04-20", "metrics": {"sessions": 1}}],
                    "rollup": {"sessions": 1},
                    "intervals_by_day": {"2026-04-20": [[1, 2]]},
                },
                "codex": {
                    "daily": [{"date": "2026-04-20", "metrics": {"sessions": 99}}],
                    "rollup": {"sessions": 99},
                    "completeness": {
                        "status": "failed", "omitted_dates": [], "failure_count": 1,
                    },
                },
            },
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=safe",
            "device_id": "dev-safe",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(set(captured["body"]["by_source"]), {"claude_code"})
        self.assertEqual(
            set(captured["body"]["by_source"]["claude_code"]), {"daily"}
        )

    def test_partial_codex_payload_uploads_only_already_filtered_dates(self):
        safe_daily = [{"date": "2026-04-20", "metrics": {"sessions": 2}}]
        metrics = {
            "by_source": {
                "codex": {
                    "daily": safe_daily,
                    "completeness": {
                        "status": "partial",
                        "omitted_dates": ["2026-04-19"],
                        "failure_count": 1,
                    },
                }
            }
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=partial",
            "device_id": "dev-partial",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["by_source"]["codex"], {"daily": safe_daily})

    def test_top_level_codex_source_payload_is_wrapped_as_by_source(self):
        metrics = {
            "source": "codex",
            "window_days": 30,
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 4}}],
            "rollup": {"sessions": 4},
            "intervals_by_day": {"2026-04-20": [[1, 2]]},
            "metadata": {"scanner": "scan_codex.py"},
            "_unknown_event_types": {"response_item:new_event": 2},
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=codex",
            "device_id": "dev-new-codex",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["daily"], [])
        self.assertEqual(captured["body"]["by_source"]["codex"]["daily"], metrics["daily"])
        self.assertEqual(
            captured["body"]["by_source"]["codex"]["_unknown_event_types"],
            metrics["_unknown_event_types"],
        )
        self.assertNotIn("rollup", captured["body"]["by_source"]["codex"])
        self.assertNotIn("intervals_by_day", captured["body"]["by_source"]["codex"])
        self.assertNotIn("metadata", captured["body"]["by_source"]["codex"])

    def test_top_level_codex_source_ignores_non_dict_unknown_event_types(self):
        metrics = {
            "source": "codex",
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 4}}],
            "_unknown_event_types": ["not", "a", "dict"],
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=codex",
            "device_id": "dev-new-codex",
        })

        self.assertEqual(rc, 0)
        self.assertNotIn("_unknown_event_types", captured["body"]["by_source"]["codex"])

    def test_top_level_pi_source_payload_is_wrapped_as_by_source(self):
        metrics = {
            "source": "pi",
            "window_days": 30,
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 4}}],
            "rollup": {"sessions": 4},
            "intervals_by_day": {"2026-04-20": [[1, 2]]},
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=pi",
            "device_id": "dev-new-pi",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["daily"], [])
        self.assertEqual(captured["body"]["by_source"]["pi"], {"daily": metrics["daily"]})

    def test_unhashable_top_level_source_falls_back_to_legacy_daily(self):
        metrics = {
            "source": ["codex"],
            "daily": [{"date": "2026-04-20", "metrics": {"sessions": 4}}],
        }
        self.metrics_path.write_text(json.dumps(metrics))

        rc, captured, _stdout = self._run_with_fake_post({
            "teaser_url": "https://aiqrank.com/teaser?s=legacy",
            "device_id": "dev-new-legacy",
        })

        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["daily"], metrics["daily"])
        self.assertNotIn("by_source", captured["body"])

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
