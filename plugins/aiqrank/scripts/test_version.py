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
from install_codex import BUNDLED_VERSION, SCRIPT_NAMES


class VersionParityTests(unittest.TestCase):
    def test_automatic_update_guidance_prepares_the_0_3_22_release(self):
        self.assertEqual(PLUGIN_VERSION, "0.3.22")

    def test_plugin_version_matches_plugin_json(self):
        plugin_json = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
        manifest = json.loads(plugin_json.read_text())
        self.assertEqual(PLUGIN_VERSION, manifest["version"])

    def test_plugin_version_matches_marketplace_json(self):
        marketplace_json = Path(__file__).resolve().parent.parent.parent.parent / ".claude-plugin" / "marketplace.json"
        marketplace = json.loads(marketplace_json.read_text())
        entry = next(p for p in marketplace["plugins"] if p["name"] == "aiqrank")
        self.assertEqual(PLUGIN_VERSION, entry["version"])

    def test_user_agent_well_formed(self):
        self.assertTrue(USER_AGENT.startswith("aiqrank-plugin/"))
        self.assertIn(PLUGIN_VERSION, USER_AGENT)

    def test_codex_installer_version_matches_plugin(self):
        self.assertEqual(PLUGIN_VERSION, BUNDLED_VERSION)

    def test_codex_installer_bundles_pi_scanner_dependency(self):
        self.assertIn("scan_pi.py", SCRIPT_NAMES)

    def test_codex_installer_bundles_the_update_notice_helper(self):
        self.assertIn("check_update.py", SCRIPT_NAMES)

    def test_codex_installer_bundles_the_self_updater(self):
        """Without this, a Codex install could never reach a newer release."""
        self.assertIn("self_update.py", SCRIPT_NAMES)

    def test_codex_prompt_uses_canonical_scan_upload_path(self):
        prompt = (Path(__file__).resolve().parent.parent / "codex_prompts" / "aiqrank.md").read_text()
        self.assertIn("upload_metrics.py --scan", prompt)
        self.assertNotIn("scan_codex.py --days", prompt)

    def test_installed_agent_instructions_disclose_pi_scan_scope(self):
        plugin_root = Path(__file__).resolve().parent.parent
        instructions = [
            (plugin_root / "codex_prompts" / "aiqrank.md").read_text(),
            (plugin_root / "skills" / "aiqrank" / "SKILL.md").read_text(),
        ]

        for content in instructions:
            self.assertIn("including Pi when present", content)
            self.assertIn("only aggregate metrics", content)

    def test_codex_instructions_explain_how_to_refresh_the_plugin(self):
        plugin_root = Path(__file__).resolve().parent.parent
        instructions = [
            (plugin_root / "codex_prompts" / "aiqrank.md").read_text(),
            (plugin_root / "skills" / "aiqrank" / "SKILL.md").read_text(),
        ]

        for content in instructions:
            self.assertIn("$aiqrank:aiqrank", content)
            self.assertIn("$aiqrank:aiqrank", content)
            self.assertIn("self_update.py", content)


if __name__ == "__main__":
    unittest.main()
