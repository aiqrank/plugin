#!/usr/bin/env python3
"""Tests for submit_score.py — the pieces that don't hit the network."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from submit_score import build_payload, filter_after_cursor  # noqa: E402


class FilterAfterCursorTests(unittest.TestCase):
    def test_filters_to_on_or_after_cursor(self):
        # `>=` semantics: entries on the cursor day are KEPT so a same-day
        # re-run can replace the partial day's data on the server.
        daily = [
            {"date": "2026-04-15", "metrics": {"sessions": 1}},
            {"date": "2026-04-16", "metrics": {"sessions": 2}},
            {"date": "2026-04-17", "metrics": {"sessions": 3}},
        ]

        out = filter_after_cursor(daily, date(2026, 4, 16))
        kept_dates = [entry["date"] for entry in out]
        self.assertEqual(kept_dates, ["2026-04-16", "2026-04-17"])

    def test_none_cursor_falls_back_to_today_minus_30(self):
        # All entries within last 30 days are kept; older ones are dropped.
        today = date.today()
        daily = [
            {"date": (today - timedelta(days=40)).isoformat(), "metrics": {}},
            {"date": (today - timedelta(days=10)).isoformat(), "metrics": {}},
            {"date": today.isoformat(), "metrics": {}},
        ]

        out = filter_after_cursor(daily, None)
        # 40-days-ago is outside the backfill; 10-days-ago and today survive
        self.assertEqual(len(out), 2)
        kept_dates = {entry["date"] for entry in out}
        self.assertNotIn((today - timedelta(days=40)).isoformat(), kept_dates)

    def test_all_entries_strictly_before_cursor_returns_empty(self):
        daily = [
            {"date": "2026-04-15", "metrics": {}},
            {"date": "2026-04-16", "metrics": {}},
        ]

        self.assertEqual(filter_after_cursor(daily, date(2026, 4, 17)), [])
        self.assertEqual(filter_after_cursor(daily, date(2030, 1, 1)), [])

    def test_same_day_cursor_keeps_today_for_replacement(self):
        # The bug we're guarding against: server says latest_date=today,
        # plugin runs again, today's fresher data must still upload.
        daily = [{"date": "2026-04-17", "metrics": {"sessions": 5}}]
        out = filter_after_cursor(daily, date(2026, 4, 17))
        self.assertEqual(len(out), 1)

    def test_skips_entries_with_missing_or_malformed_date(self):
        daily = [
            {"date": "2026-04-17", "metrics": {}},
            {"metrics": {}},
            {"date": "not-a-date", "metrics": {}},
            "not even a dict",
        ]

        out = filter_after_cursor(daily, date(2026, 4, 1))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], "2026-04-17")


class BuildPayloadTests(unittest.TestCase):
    def test_strips_unknown_keys_from_each_day(self):
        daily = [
            {
                "date": "2026-04-17",
                "metrics": {
                    "sessions": 5,
                    "tool_calls": 100,
                    "evil_field": "bad",
                    "first_messages_sample": ["leak"],
                },
            }
        ]

        payload = build_payload(daily, "engineer")

        self.assertEqual(payload["inferred_role"], "engineer")
        self.assertIn("computed_at", payload)
        [day] = payload["daily"]
        self.assertEqual(day["date"], "2026-04-17")
        self.assertEqual(day["metrics"], {"sessions": 5, "tool_calls": 100})
        self.assertNotIn("evil_field", day["metrics"])
        self.assertNotIn("first_messages_sample", day["metrics"])

    def test_passes_through_dict_metric_fields(self):
        daily = [
            {
                "date": "2026-04-17",
                "metrics": {
                    "tool_name_counts": {"Bash": 50, "Read": 30},
                    "skill_counts": {"commit": 2},
                },
            }
        ]

        payload = build_payload(daily, "engineer")
        [day] = payload["daily"]
        self.assertEqual(day["metrics"]["tool_name_counts"], {"Bash": 50, "Read": 30})
        self.assertEqual(day["metrics"]["skill_counts"], {"commit": 2})


if __name__ == "__main__":
    unittest.main()
