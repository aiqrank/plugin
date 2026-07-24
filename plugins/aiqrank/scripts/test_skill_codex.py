#!/usr/bin/env python3
"""Regression tests for the Codex plugin-root commands in SKILL.md."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class CodexSkillTests(unittest.TestCase):
    def test_each_skill_command_prefers_the_codex_plugin_cache(self):
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "aiqrank" / "SKILL.md"
        commands = re.findall(
            r"^[ \t]*```bash\n(.*?)^[ \t]*```",
            skill_path.read_text(),
            re.DOTALL | re.MULTILINE,
        )[:3]

        self.assertEqual(len(commands), 3)

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            expected_root = home / ".codex" / "plugins" / "cache" / "aiqrank" / "aiqrank" / "0.3.15"
            (expected_root / "scripts").mkdir(parents=True)

            env = os.environ | {
                "HOME": str(home),
                "CLAUDE_PLUGIN_ROOT": "",
                "CODEX_PLUGIN_ROOT": "",
            }

            for command in commands:
                root_resolution = command.split('; PY=', 1)[0]
                result = subprocess.run(
                    ["bash", "-c", f'{root_resolution}; printf "%s" "$PLUGIN_ROOT"'],
                    check=False,
                    capture_output=True,
                    env=env,
                    text=True,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, str(expected_root))
