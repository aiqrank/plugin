#!/usr/bin/env python3
"""Locks PLUGIN_VERSION in _version.py to the version field in plugin.json
so the User-Agent string can't drift from the plugin loader's view of the version.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _version import PLUGIN_VERSION, USER_AGENT


class VersionParityTests(unittest.TestCase):
    def test_plugin_version_matches_plugin_json(self):
        plugin_json = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
        manifest = json.loads(plugin_json.read_text())
        self.assertEqual(PLUGIN_VERSION, manifest["version"])

    def test_user_agent_well_formed(self):
        self.assertTrue(USER_AGENT.startswith("aiqrank-plugin/"))
        self.assertIn(PLUGIN_VERSION, USER_AGENT)


if __name__ == "__main__":
    unittest.main()
