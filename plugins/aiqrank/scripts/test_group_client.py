#!/usr/bin/env python3
"""Unit tests for group_client.py — pure stdlib + unittest."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import group_client  # noqa: E402


class ConfirmTests(unittest.TestCase):
    def test_force_yes_short_circuits(self):
        self.assertTrue(group_client.confirm("Continue?", force_yes=True))

    def test_yes_input(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            with mock.patch("builtins.input", return_value="y"):
                self.assertTrue(group_client.confirm("?", False))

    def test_no_input(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            with mock.patch("builtins.input", return_value=""):
                self.assertFalse(group_client.confirm("?", False))

    def test_non_tty_without_yes_exits(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit):
                group_client.confirm("?", force_yes=False)


class RedeemErrorOutputTests(unittest.TestCase):
    def test_token_expired_message(self):
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            group_client.print_error_for_redeem(
                group_client.HttpError(422, {"error": "token_expired"})
            )
        self.assertIn("expired", captured.getvalue().lower())

    def test_token_revoked_message(self):
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            group_client.print_error_for_redeem(
                group_client.HttpError(422, {"error": "token_revoked"})
            )
        self.assertIn("revoked", captured.getvalue().lower())

    def test_token_max_uses_message(self):
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            group_client.print_error_for_redeem(
                group_client.HttpError(422, {"error": "token_max_uses"})
            )
        self.assertIn("maximum", captured.getvalue().lower())

    def test_device_not_found_includes_pairing_url(self):
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            group_client.print_error_for_redeem(
                group_client.HttpError(
                    401,
                    {
                        "error": "device_not_found",
                        "teaser_url": "https://aiqrank.com/me",
                    },
                )
            )
        self.assertIn("pair", captured.getvalue().lower())
        self.assertIn("aiqrank.com", captured.getvalue())


class HttpRequestTests(unittest.TestCase):
    def test_post_payload_serialized_as_json(self):
        captured_payload = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode()

        def fake_urlopen(req, timeout):
            captured_payload["body"] = req.data
            captured_payload["method"] = req.get_method()
            captured_payload["headers"] = dict(req.headers)
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = group_client.http_request(
                "https://example.com/x", method="POST", payload={"a": 1}
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(json.loads(captured_payload["body"]), {"a": 1})
        self.assertEqual(captured_payload["method"], "POST")


if __name__ == "__main__":
    unittest.main()
