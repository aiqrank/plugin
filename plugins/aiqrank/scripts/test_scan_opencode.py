#!/usr/bin/env python3
"""Tests for scan_opencode.py."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scan_opencode  # noqa: E402
from scan_opencode import (  # noqa: E402
    _extract_mcp_server,
    _split_interval_by_day,
    main,
    scan,
)


def _create_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            directory TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        );
        """
    )
    return conn


def _create_current_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            directory TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        """
    )
    return conn


def _insert_session(conn, sid, parent_id, t_created, t_updated, directory=""):
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (sid, parent_id, t_created, t_updated, directory),
    )


def _insert_message(conn, mid, session_id, t_created, data):
    conn.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?)",
        (mid, session_id, t_created, json.dumps(data) if not isinstance(data, str) else data),
    )


def _insert_current_message(conn, mid, session_id, t_created, data):
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            mid,
            session_id,
            t_created,
            t_created,
            json.dumps(data) if not isinstance(data, str) else data,
        ),
    )


def _insert_part(conn, pid, mid, session_id, t_created, data):
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (
            pid,
            mid,
            session_id,
            t_created,
            t_created,
            json.dumps(data) if not isinstance(data, str) else data,
        ),
    )


def _ms_at(year, month, day, hour=12, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


class ScanOpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "opencode.db"
        # Empty home for skill snapshot — keeps tests deterministic.
        self.fake_home = self.tmp / "fake_home_dot_claude"
        self.fake_home.mkdir()
        self.fake_config = self.tmp / "fake_opencode_config"
        self.fake_config.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _scan(self, **overrides):
        kwargs = dict(
            opencode_db=self.db,
            home=self.fake_home,
            opencode_config_root=self.fake_config,
        )
        kwargs.update(overrides)
        return scan(**kwargs)

    # ── happy path ───────────────────────────────────────────────────────
    def test_two_sessions_across_two_days(self):
        # Use "now" timestamps so the default 30-day window picks them up
        # but place them on two different local-calendar days.
        now = time.time()
        t_today = int(now * 1000)
        t_yesterday = int((now - 86400) * 1000)

        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t_yesterday, t_yesterday + 1000)
        _insert_session(conn, "s2", None, t_today, t_today + 1000)
        _insert_message(conn, "m1", "s1", t_yesterday, {"role": "assistant", "parts": []})
        _insert_message(conn, "m2", "s2", t_today, {"role": "assistant", "parts": []})
        conn.commit()
        conn.close()

        result = self._scan()
        self.assertEqual(result["source"], "opencode")
        self.assertEqual(len(result["daily"]), 2)
        self.assertEqual(result["rollup"]["sessions"], 2)
        self.assertEqual(result["rollup"]["main_sessions"], 2)
        self.assertEqual(result["rollup"]["messages"], 2)

    def test_mcp_tool_bucketed_by_server(self):
        now = time.time()
        t = int(now * 1000)
        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t, t + 1000)
        _insert_message(
            conn, "m1", "s1", t,
            {
                "role": "assistant",
                "parts": [
                    {"type": "tool", "tool": "mcp_github_search"},
                    {"type": "tool", "tool": "mcp_github_list"},
                    {"type": "tool", "tool": "mcp__pencil__open_document"},
                    {"type": "tool", "tool": "bash"},
                ],
            },
        )
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["tool_calls"], 4)
        self.assertEqual(rollup["mcp_server_counts"].get("github"), 2)
        self.assertEqual(rollup["mcp_server_counts"].get("pencil"), 1)
        self.assertEqual(rollup["sessions_with_tools"], 1)

    def test_parallel_agents_in_single_message(self):
        now = time.time()
        t = int(now * 1000)
        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t, t + 1000)
        _insert_message(
            conn, "m1", "s1", t,
            {
                "role": "assistant",
                "parts": [
                    {"type": "agent", "name": "researcher"},
                    {"type": "agent", "name": "tester"},
                    {"type": "agent", "name": "writer"},
                ],
            },
        )
        # A second message with a single agent — should NOT bump
        # parallel_agent_turns.
        _insert_message(
            conn, "m2", "s1", t + 1000,
            {"role": "assistant", "parts": [{"type": "agent", "name": "solo"}]},
        )
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["parallel_agent_turns"], 1)
        self.assertEqual(rollup["max_parallel_agents"], 3)
        self.assertEqual(rollup["sessions_with_orchestration"], 1)

    def test_reasoning_blocks_counted(self):
        now = time.time()
        t = int(now * 1000)
        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t, t + 1000)
        _insert_message(
            conn, "m1", "s1", t,
            {
                "role": "assistant",
                "parts": [
                    {"type": "reasoning", "text": "..."},
                    {"type": "reasoning", "text": "..."},
                    {"type": "tool", "tool": "bash"},
                ],
            },
        )
        conn.commit()
        conn.close()

        result = self._scan()
        self.assertEqual(result["rollup"]["reasoning_blocks"], 2)

    def test_current_schema_reads_session_message_and_part_tables(self):
        now = time.time()
        t = int(now * 1000)
        conn = _create_current_db(self.db)
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("s1", None, t, t + 1000, ""),
        )
        _insert_current_message(conn, "m1", "s1", t, {"role": "user"})
        _insert_current_message(conn, "m2", "s1", t + 1000, {"role": "assistant"})
        _insert_part(conn, "p1", "m2", "s1", t + 1000, {"type": "reasoning"})
        _insert_part(conn, "p2", "m2", "s1", t + 1000, {"type": "tool", "tool": "mcp_github_search"})
        _insert_part(conn, "p3", "m2", "s1", t + 1000, {"type": "agent", "name": "a"})
        _insert_part(conn, "p4", "m2", "s1", t + 1000, {"type": "agent", "name": "b"})
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["sessions"], 1)
        self.assertEqual(rollup["messages"], 2)
        self.assertEqual(rollup["user_messages"], 1)
        self.assertEqual(rollup["reasoning_blocks"], 1)
        self.assertEqual(rollup["tool_calls"], 1)
        self.assertEqual(rollup["mcp_server_counts"].get("github"), 1)
        self.assertEqual(rollup["sessions_with_tools"], 1)
        self.assertEqual(rollup["sessions_with_orchestration"], 1)
        self.assertEqual(rollup["parallel_agent_turns"], 1)
        self.assertEqual(rollup["max_parallel_agents"], 2)

    # ── edge cases ───────────────────────────────────────────────────────
    def test_missing_db_returns_empty_envelope(self):
        result = scan(
            opencode_db=self.tmp / "does_not_exist.db",
            home=self.fake_home,
            opencode_config_root=self.fake_config,
        )
        self.assertEqual(result["source"], "opencode")
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["intervals_by_day"], {})
        # Rollup is a zeroed metrics dict.
        self.assertEqual(result["rollup"]["sessions"], 0)

    def test_malformed_json_row_skipped(self):
        now = time.time()
        t = int(now * 1000)
        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t, t + 1000)
        # Insert a row with malformed JSON.
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            ("m_bad", "s1", t, "{not valid json"),
        )
        # Plus a valid row alongside.
        _insert_message(conn, "m_ok", "s1", t, {"role": "assistant", "parts": []})
        conn.commit()
        conn.close()

        result = self._scan()
        # The valid message is counted; the malformed row is skipped without
        # raising.
        self.assertEqual(result["rollup"]["messages"], 1)

    def test_session_crossing_midnight_buckets_into_both_days(self):
        # Session starts at 23:30 yesterday-local, ends at 01:00 today-local.
        today = datetime.now().date()
        yesterday = datetime(today.year, today.month, today.day).timestamp() - 1800  # 23:30 yesterday
        tomorrow_start = datetime(today.year, today.month, today.day).timestamp() + 3600  # 01:00 today
        t_start = int(yesterday * 1000)
        t_end = int(tomorrow_start * 1000)

        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t_start, t_end)
        # One message at 23:35 yesterday, one at 00:30 today.
        _insert_message(conn, "m1", "s1", int((yesterday + 300) * 1000), {"parts": []})
        _insert_message(conn, "m2", "s1", int((tomorrow_start - 1800) * 1000), {"parts": []})
        conn.commit()
        conn.close()

        result = self._scan()
        # Two daily entries (one for yesterday, one for today).
        self.assertEqual(len(result["daily"]), 2)
        # Each day records the session interval.
        self.assertEqual(len(result["intervals_by_day"]), 2)
        # Sessions counted once per day touched.
        self.assertEqual(result["rollup"]["sessions"], 2)

    def test_subagent_session_does_not_increment_main_sessions(self):
        now = time.time()
        t = int(now * 1000)
        conn = _create_db(self.db)
        _insert_session(conn, "s1", None, t, t + 1000)            # main
        _insert_session(conn, "s2", "s1", t, t + 1000)            # subagent
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["sessions"], 2)
        self.assertEqual(rollup["main_sessions"], 1)

    def test_rows_outside_window_excluded(self):
        now = time.time()
        t_recent = int(now * 1000)
        t_old = int((now - 60 * 86400) * 1000)  # 60 days ago

        conn = _create_db(self.db)
        _insert_session(conn, "s_old", None, t_old, t_old + 1000)
        _insert_session(conn, "s_new", None, t_recent, t_recent + 1000)
        _insert_message(conn, "m_old", "s_old", t_old, {"parts": []})
        _insert_message(conn, "m_new", "s_new", t_recent, {"parts": []})
        conn.commit()
        conn.close()

        # Default 30-day window — old rows excluded.
        result = self._scan(window_days=30)
        self.assertEqual(result["rollup"]["sessions"], 1)
        self.assertEqual(result["rollup"]["messages"], 1)

    def test_mtime_after_honored_via_main(self):
        # A row from a few days ago, plus a row from now. Pin --mtime-after to
        # "yesterday" — only the now-row should be picked up.
        now = time.time()
        t_recent = int(now * 1000)
        t_few_days = int((now - 5 * 86400) * 1000)

        conn = _create_db(self.db)
        _insert_session(conn, "s_fewdays", None, t_few_days, t_few_days + 1000)
        _insert_session(conn, "s_now", None, t_recent, t_recent + 1000)
        _insert_message(conn, "m_fewdays", "s_fewdays", t_few_days, {"parts": []})
        _insert_message(conn, "m_now", "s_now", t_recent, {"parts": []})
        conn.commit()
        conn.close()

        cutoff_iso = datetime.fromtimestamp(now - 86400).isoformat()

        # Run the full scan(...) directly with mtime_after set to yesterday.
        result = scan(
            opencode_db=self.db,
            home=self.fake_home,
            opencode_config_root=self.fake_config,
            mtime_after_ts=now - 86400,
        )
        self.assertEqual(result["rollup"]["sessions"], 1)

        # And exercise main() with the CLI flag.
        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            argv = ["--days", "30", "--mtime-after", cutoff_iso]
            # main() uses the real default DB path; it should run without
            # exception even on a machine without OpenCode installed.
            main(argv)
        finally:
            sys.stdout = original_stdout
        out = captured.getvalue().strip()
        # main() always emits a JSON envelope to stdout.
        parsed = json.loads(out)
        self.assertEqual(parsed["source"], "opencode")

    # ── helpers ──────────────────────────────────────────────────────────
    def test_extract_mcp_server(self):
        self.assertEqual(_extract_mcp_server("mcp_github_search"), "github")
        self.assertEqual(_extract_mcp_server("mcp__pencil__open_document"), "pencil")
        self.assertIsNone(_extract_mcp_server("bash"))
        self.assertIsNone(_extract_mcp_server("mcp_"))
        self.assertIsNone(_extract_mcp_server("mcp__"))

    def test_split_interval_by_day_within_one_day(self):
        today = datetime.now().date()
        start = datetime(today.year, today.month, today.day, 10, 0).timestamp()
        end = start + 3600
        out = list(_split_interval_by_day(start, end))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], today)
        self.assertEqual(out[0][1], (start, end))

    def test_help_flag(self):
        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["--help"])
        finally:
            sys.stdout = original_stdout
        self.assertEqual(rc, 0)
        self.assertIn("--days", captured.getvalue())
        self.assertIn("--mtime-after", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
