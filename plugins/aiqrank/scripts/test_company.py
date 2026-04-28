#!/usr/bin/env python3
"""Tests for company.py — /aiqrank:company subcommands."""

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


class CompanyCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp_path)}, clear=False)
        self._env.start()
        if "company" in sys.modules:
            del sys.modules["company"]
        import company
        self.mod = company
        cfg = self.tmp_path / ".config" / "aiqrank"
        self.mod.CONFIG_DIR = cfg
        self.mod.DEVICE_PATH = cfg / "device.json"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _pair(self, device_id: str = "device-abcdef12") -> None:
        self.mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.DEVICE_PATH.write_text(json.dumps({"device_id": device_id}))

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=err):
            rc = self.mod.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _fake_response(self, body: object, status: int = 200):
        encoded = json.dumps(body).encode("utf-8") if body is not None else b""

        class FakeResp:
            status = 0  # set below
            def read(self_inner): return encoded
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False

        FakeResp.status = status
        return FakeResp()

    # --- unpaired ---

    def test_unpaired_device_prompts_signin(self):
        rc, out, err = self._run(["list"])
        self.assertEqual(rc, 1)
        self.assertIn("Sign in first", err)

    # --- list ---

    def test_list_empty(self):
        self._pair()

        def fake_urlopen(req, timeout=30):
            self.assertEqual(req.method, "GET")
            self.assertIn("/api/plugin/companies/list", req.full_url)
            return self._fake_response({"companies": []})

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            rc, out, err = self._run(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("not a member", out)

    def test_list_with_companies(self):
        self._pair()
        body = {"companies": [
            {"slug": "alpha", "name": "Alpha Co", "role": "admin"},
            {"slug": "beta", "name": "Beta", "role": "member"},
        ]}

        def fake_urlopen(req, timeout=30):
            return self._fake_response(body)

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            rc, out, err = self._run(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("alpha", out)
        self.assertIn("Alpha Co", out)
        self.assertIn("admin", out)
        self.assertIn("beta", out)

    # --- join ---

    def test_join_fetches_consent_then_redeems(self):
        self._pair()
        seen_paths = []

        def fake_urlopen(req, timeout=30):
            seen_paths.append(req.full_url)
            if "/consent" in req.full_url:
                return self._fake_response({"version": "v1", "text": "Joining means..."})
            if "/redeem" in req.full_url:
                payload = json.loads(req.data)
                self.assertEqual(payload["consent_text_version"], "v1")
                self.assertIn("consent_shown_at", payload)
                self.assertEqual(payload["token"], "tok123")
                return self._fake_response({
                    "ok": True,
                    "already_member": False,
                    "company": {"slug": "alpha", "name": "Alpha", "role": "member"},
                })
            self.fail(f"unexpected url {req.full_url}")

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch("builtins.input", return_value="yes"):
            rc, out, err = self._run(["join", "tok123"])

        self.assertEqual(rc, 0)
        self.assertIn("Joined Alpha", out)
        self.assertTrue(any("/consent" in p for p in seen_paths))
        self.assertTrue(any("/redeem" in p for p in seen_paths))

    def test_join_user_declines_consent(self):
        self._pair()

        def fake_urlopen(req, timeout=30):
            self.assertIn("/consent", req.full_url)  # never reaches /redeem
            return self._fake_response({"version": "v1", "text": "..."})

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch("builtins.input", return_value="no"):
            rc, out, err = self._run(["join", "tok123"])

        self.assertEqual(rc, 0)
        self.assertIn("Cancelled", out)

    def test_join_token_expired_friendly_message(self):
        self._pair()

        def fake_urlopen(req, timeout=30):
            if "/consent" in req.full_url:
                return self._fake_response({"version": "v1", "text": "..."})
            # /redeem returns 422 with token_expired
            raise self.mod.urllib.error.HTTPError(
                req.full_url, 422, "Unprocessable", {},
                io.BytesIO(json.dumps({"error": "token_expired"}).encode("utf-8")),
            )

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch("builtins.input", return_value="yes"):
            rc, out, err = self._run(["join", "tok123"])

        self.assertEqual(rc, 1)
        self.assertIn("expired", err)

    # --- leave ---

    def test_leave_confirms_then_calls_endpoint(self):
        self._pair()

        def fake_urlopen(req, timeout=30):
            self.assertIn("/leave", req.full_url)
            payload = json.loads(req.data)
            self.assertEqual(payload, {"device_id": "device-abcdef12", "slug": "alpha"})
            return self._fake_response({"ok": True})

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch("builtins.input", return_value="yes"):
            rc, out, err = self._run(["leave", "alpha"])

        self.assertEqual(rc, 0)
        self.assertIn("Left alpha", out)

    def test_leave_user_cancels(self):
        self._pair()

        def fake_urlopen(req, timeout=30):
            self.fail("urlopen must not be called when user cancels")

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch("builtins.input", return_value=""):
            rc, out, err = self._run(["leave", "alpha"])

        self.assertEqual(rc, 0)
        self.assertIn("Cancelled", out)

    def test_leave_not_a_member(self):
        self._pair()

        def fake_urlopen(req, timeout=30):
            raise self.mod.urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {},
                io.BytesIO(json.dumps({"error": "not_a_member"}).encode("utf-8")),
            )

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch("builtins.input", return_value="yes"):
            rc, out, err = self._run(["leave", "alpha"])

        self.assertEqual(rc, 1)
        self.assertIn("not a member", err)


if __name__ == "__main__":
    unittest.main()
