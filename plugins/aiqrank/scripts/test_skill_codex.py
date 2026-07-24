#!/usr/bin/env python3
"""Regression tests for the plugin-root commands in SKILL.md.

Step 0 resolves the root from the engine's cache; the later steps read the
root step 0 recorded. Both halves are checked against a Codex-only layout,
because a Claude-only test would not catch a Claude-specific path.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "aiqrank" / "SKILL.md"


def _skill_commands() -> list[str]:
    return re.findall(
        r"^[ \t]*```bash\n(.*?)^[ \t]*```",
        SKILL_PATH.read_text(),
        re.DOTALL | re.MULTILINE,
    )[:4]


def _resolve(command: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    root_resolution = command.split("; PY=", 1)[0]
    return subprocess.run(
        ["bash", "-c", f'{root_resolution}; printf "%s" "$PLUGIN_ROOT"'],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


class CodexSkillTests(unittest.TestCase):
    def test_skill_still_has_a_bootstrap_step_plus_three_actions(self):
        self.assertEqual(len(_skill_commands()), 4)

    def test_bootstrap_command_prefers_the_codex_plugin_cache(self):
        commands = _skill_commands()

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            expected = (
                home / ".codex" / "plugins" / "cache" / "aiqrank" / "aiqrank" / "0.3.16"
            )
            (expected / "scripts").mkdir(parents=True)

            env = os.environ | {
                "HOME": str(home),
                "CLAUDE_PLUGIN_ROOT": "",
                "CODEX_PLUGIN_ROOT": "",
            }

            result = _resolve(commands[0], env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(expected))

    def test_action_commands_use_the_root_recorded_by_step_zero(self):
        """This is what lets an in-session update take effect immediately."""
        commands = _skill_commands()

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            updated_root = home / "cache" / "0.3.17"
            (updated_root / "scripts").mkdir(parents=True)

            config = home / ".config" / "aiqrank"
            config.mkdir(parents=True)
            (config / "plugin_root").write_text(f"{updated_root}\n")

            env = os.environ | {
                "HOME": str(home),
                "CLAUDE_PLUGIN_ROOT": str(home / "stale"),
                "CODEX_PLUGIN_ROOT": "",
            }

            for command in commands[1:]:
                result = _resolve(command, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, str(updated_root))

    def test_action_commands_fail_loudly_when_step_zero_was_skipped(self):
        commands = _skill_commands()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env = os.environ | {
                "HOME": tmp_dir,
                "CLAUDE_PLUGIN_ROOT": "",
                "CODEX_PLUGIN_ROOT": "",
            }

            for command in commands[1:]:
                result = _resolve(command, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("run step 0 first", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
