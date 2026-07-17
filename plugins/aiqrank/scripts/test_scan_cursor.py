#!/usr/bin/env python3
"""Tests for scan_cursor.py."""

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

from scan_cursor import main, scan  # noqa: E402


def _create_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE cursorDiskKV (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    return conn


def _put(conn: sqlite3.Connection, key: str, value) -> None:
    if not isinstance(value, str):
        value = json.dumps(value)
    conn.execute(
        "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
        (key, value),
    )


def _ms(year, month, day, hour=12, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


class ScanCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "state.vscdb"

        # Isolated fake home so ~/.claude/skills doesn't leak into tests.
        self.fake_home = self.tmp / "home"
        (self.fake_home / ".claude").mkdir(parents=True)
        self.cursor_home = self.fake_home / ".cursor"
        self.cursor_home.mkdir()
        self.workspace_storage = self.tmp / "workspaceStorage"
        self.workspace_storage.mkdir()

        # Pin "now" to a fixed reference inside our window.
        self.now_ts = datetime(2026, 4, 28, 18, 0).timestamp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _scan(self, **kwargs):
        defaults = dict(
            cursor_db_path=self.db,
            cursor_home=self.cursor_home,
            workspace_storage_dir=self.workspace_storage,
            home=self.fake_home,
            now_ts=self.now_ts,
        )
        defaults.update(kwargs)
        return scan(**defaults)

    # ── happy paths ───────────────────────────────────────────────────────

    def test_two_composers_emit_two_day_envelope(self):
        conn = _create_db(self.db)
        # Composer 1 — Apr 26 noon.
        c1_start = _ms(2026, 4, 26, 12, 0)
        c1_end = _ms(2026, 4, 26, 12, 30)
        _put(conn, "composerData:c1", {
            "createdAt": c1_start,
            "lastUpdatedAt": c1_end,
            "fullConversationHeadersOnly": [{}, {}, {}],
        })
        # Composer 2 — Apr 27 noon.
        c2_start = _ms(2026, 4, 27, 12, 0)
        c2_end = _ms(2026, 4, 27, 12, 15)
        _put(conn, "composerData:c2", {
            "createdAt": c2_start,
            "lastUpdatedAt": c2_end,
            "fullConversationHeadersOnly": [{}, {}],
        })
        conn.commit()
        conn.close()

        result = self._scan()
        self.assertEqual(result["source"], "cursor")
        dates = [d["date"] for d in result["daily"]]
        self.assertEqual(dates, ["2026-04-26", "2026-04-27"])

        rollup = result["rollup"]
        self.assertEqual(rollup["sessions"], 2)
        self.assertEqual(rollup["main_sessions"], 2)
        self.assertEqual(rollup["messages"], 5)  # 3 + 2
        self.assertEqual(rollup["max_messages_in_session"], 3)

    def test_is_agentic_increments_orchestration(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:agent1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "isAgentic": True,
            "fullConversationHeadersOnly": [{}],
        })
        _put(conn, "composerData:plain1", {
            "createdAt": ts + 1000,
            "lastUpdatedAt": ts + 61_000,
            "fullConversationHeadersOnly": [{}],
        })
        conn.commit()
        conn.close()

        result = self._scan()
        self.assertEqual(result["rollup"]["sessions_with_orchestration"], 1)

    def test_thinking_field_increments_reasoning_blocks(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:c1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [{}, {}],
        })
        _put(conn, "bubbleId:c1:b1", {"thinking": "step 1: think"})
        _put(conn, "bubbleId:c1:b2", {"thinking": "   "})  # whitespace only — skip
        _put(conn, "bubbleId:c1:b3", {})                   # missing — skip
        _put(conn, "bubbleId:c1:b4", {"thinking": "another"})
        conn.commit()
        conn.close()

        result = self._scan()
        self.assertEqual(result["rollup"]["reasoning_blocks"], 2)

    def test_global_mcp_json_three_servers(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:c1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [],
        })
        conn.commit()
        conn.close()

        mcp = self.cursor_home / "mcp.json"
        mcp.write_text(json.dumps({
            "mcpServers": {
                "alpha": {"command": "alpha-bin"},
                "beta": {"command": "beta-bin"},
                "gamma": {"command": "gamma-bin"},
            }
        }))

        result = self._scan()
        rollup_mcp = result["rollup"]["mcp_server_counts"]
        # One active day × three servers = 3 total.
        self.assertEqual(sum(rollup_mcp.values()), 3)
        self.assertEqual(set(rollup_mcp.keys()), {"alpha", "beta", "gamma"})

    def test_project_mcp_json_only_counts_when_recent(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:c1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [],
        })
        conn.commit()
        conn.close()

        recent_project = self.tmp / "recent_project"
        stale_project = self.tmp / "stale_project"
        (recent_project / ".cursor").mkdir(parents=True)
        (stale_project / ".cursor").mkdir(parents=True)
        recent_mcp = recent_project / ".cursor" / "mcp.json"
        stale_mcp = stale_project / ".cursor" / "mcp.json"
        recent_mcp.write_text(json.dumps({"mcpServers": {"recent": {}}}))
        stale_mcp.write_text(json.dumps({"mcpServers": {"stale": {}}}))

        old_ts = datetime(2025, 1, 1, 12, 0).timestamp()
        os.utime(stale_mcp, (old_ts, old_ts))

        for idx, project in enumerate([recent_project, stale_project], start=1):
            ws = self.workspace_storage / f"ws{idx}"
            ws.mkdir()
            (ws / "workspace.json").write_text(
                json.dumps({"folder": f"file://{project}"})
            )

        result = self._scan()
        rollup_mcp = result["rollup"]["mcp_server_counts"]
        self.assertEqual(rollup_mcp.get("recent"), 1)
        self.assertNotIn("stale", rollup_mcp)
        self.assertEqual(result["rollup"]["custom_mcp_config_writes"], 1)

    def test_tool_former_data_counts_tool_calls(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:c1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        _put(conn, "bubbleId:c1:b1", {
            "toolFormerData": {"toolName": "edit_file"}
        })
        _put(conn, "bubbleId:c1:b2", {
            "toolFormerData": [
                {"toolName": "read_file"},
                {"name": "grep_search"},
            ]
        })
        _put(conn, "bubbleId:c1:b3", {"toolFormerData": "garbage-string"})
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["tool_calls"], 3)
        self.assertEqual(rollup["sessions_with_tools"], 1)
        self.assertEqual(rollup["tool_name_counts"]["edit_file"], 1)
        self.assertEqual(rollup["tool_name_counts"]["read_file"], 1)
        self.assertEqual(rollup["tool_name_counts"]["grep_search"], 1)

    def test_structured_token_count(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:c1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        _put(conn, "bubbleId:c1:b1", {
            "tokenCount": {
                "input": 100, "output": 50,
                "cacheRead": 30, "cacheCreation": 10,
                "total": 190,
            }
        })
        _put(conn, "bubbleId:c1:b2", {"tokenCount": 25})  # plain int
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["tokens_input"], 100)
        self.assertEqual(rollup["tokens_output"], 50)
        self.assertEqual(rollup["tokens_cache_read"], 30)
        self.assertEqual(rollup["tokens_cache_creation"], 10)
        # 190 (structured total) + 25 (plain int) = 215.
        self.assertEqual(rollup["tokens_total"], 215)

    # ── edge cases ────────────────────────────────────────────────────────

    def test_missing_state_vscdb_returns_empty_envelope(self):
        # No db file written.
        captured_err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured_err
        try:
            result = self._scan()
        finally:
            sys.stderr = old_stderr

        self.assertEqual(result["source"], "cursor")
        self.assertEqual(result["daily"], [])
        # Rollup should still be a dict with all-zero fields.
        self.assertEqual(result["rollup"]["sessions"], 0)
        # No exception was raised.
        self.assertIn("not found", captured_err.getvalue())

    def test_malformed_json_value_is_skipped(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:bad", "this is not json {{{")
        _put(conn, "composerData:good", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        conn.commit()
        conn.close()

        result = self._scan()
        # Bad row skipped, good row counted.
        self.assertEqual(result["rollup"]["sessions"], 1)

    def test_bubble_missing_token_count_is_zero(self):
        conn = _create_db(self.db)
        ts = _ms(2026, 4, 27, 10, 0)
        _put(conn, "composerData:c1", {
            "createdAt": ts,
            "lastUpdatedAt": ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        _put(conn, "bubbleId:c1:b1", {"thinking": "x"})  # no tokenCount
        conn.commit()
        conn.close()

        result = self._scan()
        rollup = result["rollup"]
        self.assertEqual(rollup["tokens_total"], 0)
        self.assertEqual(rollup["reasoning_blocks"], 1)

    def test_composer_outside_window_is_excluded(self):
        conn = _create_db(self.db)
        # Composer from way before the 30-day window.
        old_ts = _ms(2025, 1, 1, 12, 0)
        _put(conn, "composerData:old", {
            "createdAt": old_ts,
            "lastUpdatedAt": old_ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        # Composer in window.
        new_ts = _ms(2026, 4, 27, 12, 0)
        _put(conn, "composerData:new", {
            "createdAt": new_ts,
            "lastUpdatedAt": new_ts + 60_000,
            "fullConversationHeadersOnly": [{}, {}],
        })
        conn.commit()
        conn.close()

        result = self._scan()
        self.assertEqual(result["rollup"]["sessions"], 1)
        self.assertEqual(result["rollup"]["messages"], 2)

    def test_composer_crossing_midnight_buckets_into_both_days(self):
        conn = _create_db(self.db)
        start = _ms(2026, 4, 26, 23, 30)  # Apr 26 23:30
        end = _ms(2026, 4, 27, 0, 30)     # Apr 27 00:30
        _put(conn, "composerData:cross", {
            "createdAt": start,
            "lastUpdatedAt": end,
            "fullConversationHeadersOnly": [{}, {}],
        })
        conn.commit()
        conn.close()

        result = self._scan()
        dates = [d["date"] for d in result["daily"]]
        self.assertEqual(dates, ["2026-04-26", "2026-04-27"])
        # Sessions should appear on BOTH days from the split.
        per_day = {d["date"]: d["metrics"] for d in result["daily"]}
        self.assertEqual(per_day["2026-04-26"]["sessions"], 1)
        self.assertEqual(per_day["2026-04-27"]["sessions"], 1)
        self.assertEqual(per_day["2026-04-26"]["messages"], 2)
        self.assertEqual(per_day["2026-04-27"]["messages"], 0)
        self.assertEqual(result["rollup"]["sessions"], 2)
        self.assertEqual(result["rollup"]["messages"], 2)

    def test_mtime_after_flag_honored(self):
        conn = _create_db(self.db)
        # Composer 5 days ago — within default window, but excluded by mtime_after.
        old_ts = _ms(2026, 4, 23, 12, 0)
        _put(conn, "composerData:old", {
            "createdAt": old_ts,
            "lastUpdatedAt": old_ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        # Composer today — should pass.
        new_ts = _ms(2026, 4, 27, 12, 0)
        _put(conn, "composerData:new", {
            "createdAt": new_ts,
            "lastUpdatedAt": new_ts + 60_000,
            "fullConversationHeadersOnly": [{}],
        })
        conn.commit()
        conn.close()

        cutoff = datetime(2026, 4, 26, 0, 0).timestamp()
        result = self._scan(mtime_after_ts=cutoff)
        self.assertEqual(result["rollup"]["sessions"], 1)

    def test_main_emits_json_to_stdout(self):
        # End-to-end smoke through main(), with a missing db so we don't
        # need to monkey-patch defaults — just verify it returns 0 and
        # writes a parseable JSON envelope.
        captured = io.StringIO()
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = captured
        sys.stderr = io.StringIO()
        try:
            rc = main(["--days", "30"])
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        self.assertEqual(rc, 0)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["source"], "cursor")
        self.assertEqual(payload["window_days"], 30)


if __name__ == "__main__":
    unittest.main()
