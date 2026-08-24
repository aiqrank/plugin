#!/usr/bin/env python3
"""Tests for hook_nudge_if_stale.py."""

from __future__ import annotations

import importlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class HookNudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patcher = mock.patch.dict(
            os.environ,
            # Empty values read as unset to the module, which checks
            # truthiness -- this keeps a developer's own overrides out.
            {
                "HOME": str(self.tmp_path),
                "PATH": "",
                "CLAUDE_CONFIG_DIR": "",
                "AIQRANK_HOST_HOME": "",
            },
        )
        self._patcher.start()
        # Re-import module with patched HOME so LAST_UPLOAD_PATH resolves fresh.
        if "hook_nudge_if_stale" in sys.modules:
            del sys.modules["hook_nudge_if_stale"]
        import hook_nudge_if_stale  # noqa: F401
        self.mod = hook_nudge_if_stale
        # Capture what the module computed from the patched HOME before the
        # rebinding below hides it. test_module_paths_are_built_from_home
        # asserts against this, so a typo in a path constant cannot pass.
        self.imported = {
            "config_dir": self.mod.CLAUDE_CONFIG_DIR,
            "registry": self.mod.CLAUDE_PLUGIN_REGISTRY_PATH,
            "marketplaces": self.mod.CLAUDE_MARKETPLACES_PATH,
            "nudge_marker": self.mod.CLI_INSTALL_NUDGE_PATH,
        }
        # Force the module-level paths to our tmp home so the hook-fired
        # log line writes into the test sandbox, not the real ~/.config.
        cfg = self.tmp_path / ".config" / "aiqrank"
        self.mod.CONFIG_DIR = cfg
        self.mod.LAST_UPLOAD_PATH = cfg / "last_upload_at"
        self.mod.LOG_PATH = cfg / "hook.log"
        self.mod.STALE_VERSION_PATH = cfg / "stale_version"
        self.mod.CLI_INSTALL_NUDGE_PATH = cfg / "cli_install_nudge_at"
        claude_plugins = self.tmp_path / ".claude" / "plugins"
        claude_plugins.mkdir(parents=True, exist_ok=True)
        self.mod.CLAUDE_CONFIG_DIR = self.tmp_path / ".claude"
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH = claude_plugins / "installed_plugins.json"
        self.mod.CLAUDE_MARKETPLACES_PATH = claude_plugins / "known_marketplaces.json"
        # Keep CLI discovery hermetic: without this a real /usr/local/bin/claude
        # on the developer's machine would decide these tests.
        self.cli_path = self.tmp_path / ".local" / "bin" / "claude"
        self.mod.CLAUDE_CLI_FALLBACK_PATHS = (self.cli_path,)

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def _install_fake_cli(self) -> None:
        """Put a real executable where the CLI would be.

        PATH is empty in these tests, so this exercises the fallback scan
        in _claude_cli_present rather than stubbing the detection out.
        """
        self.cli_path.parent.mkdir(parents=True, exist_ok=True)
        self.cli_path.write_text("#!/bin/sh\n")
        self.cli_path.chmod(0o755)

    def _assert_nudged(self, output: str) -> None:
        """The full marketplace-setup variant appeared verbatim."""
        self.assertIn(self.mod.CLI_INSTALL_NUDGE_WITH_MARKETPLACE, output)

    def _reimport(self, **env):
        """Re-import the module with extra environment set at import time."""
        with mock.patch.dict(os.environ, env):
            del sys.modules["hook_nudge_if_stale"]
            return importlib.import_module("hook_nudge_if_stale")

    def _run(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _write_fresh_upload(self) -> None:
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh + "\n")

    def _write_cli_plugins(self, plugins: dict[str, object]) -> None:
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH.write_text(
            json.dumps({"version": 2, "plugins": plugins})
        )

    def _write_cli_marketplaces(self, marketplaces: dict[str, object]) -> None:
        self.mod.CLAUDE_MARKETPLACES_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLAUDE_MARKETPLACES_PATH.write_text(json.dumps(marketplaces))

    def test_missing_file_prints_nudge(self):
        output = self._run()
        self.assertIn("AIQ Rank:", output)
        self.assertIn("30 days", output)

    def test_fresh_timestamp_silent(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh + "\n")
        output = self._run()
        self.assertEqual(output, "")

    def test_old_timestamp_prints_nudge(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text("2020-01-01T00:00:00Z\n")
        output = self._run()
        self.assertIn("AIQ Rank:", output)

    def test_malformed_timestamp_prints_nudge(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text("garbage\n")
        output = self._run()
        self.assertIn("AIQ Rank:", output)

    def test_empty_file_prints_nudge(self):
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text("")
        output = self._run()
        self.assertIn("AIQ Rank:", output)

    def test_hook_fired_log_line_written(self):
        # Diagnostic: every invocation appends a single hook-fired line to
        # the shared log file so customer triage can prove the hook ran.
        self._run()
        log = self.mod.LOG_PATH.read_text()
        self.assertIn("hook fired script=nudge_if_stale", log)
        self.assertIn(f"platform={sys.platform}", log)

    def test_stale_version_file_prints_update_nudge(self):
        self.mod.STALE_VERSION_PATH = self.tmp_path / ".config" / "aiqrank" / "stale_version"
        self.mod.STALE_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.STALE_VERSION_PATH.write_text("0.9.1\n")
        # Fresh upload so the 30-day nudge is silent.
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mod.LAST_UPLOAD_PATH.write_text(fresh + "\n")

        output = self._run()
        self.assertIn("plugin update available", output)
        self.assertIn("0.9.1", output)
        # Claude Code uses its slash command, which updates itself rather than
        # asking the user to run update commands by hand.
        self.assertIn("/aiqrank", output)
        self.assertNotIn("curl -sSL", output)
        self.assertNotIn("30 days", output)

    def test_older_stale_version_is_cleared_and_silent(self):
        self.mod.STALE_VERSION_PATH = self.tmp_path / ".config" / "aiqrank" / "stale_version"
        self.mod.STALE_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.STALE_VERSION_PATH.write_text("0.3.20\n")
        self.mod.LAST_UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.LAST_UPLOAD_PATH.write_text(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n"
        )

        with mock.patch.object(self.mod, "PLUGIN_VERSION", "0.3.22"):
            output = self._run()

        self.assertEqual(output, "")
        self.assertFalse(self.mod.STALE_VERSION_PATH.exists())

    def test_codex_stale_version_uses_namespaced_skill_command(self):
        self.mod.STALE_VERSION_PATH = self.tmp_path / ".config" / "aiqrank" / "stale_version"
        self.mod.STALE_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.STALE_VERSION_PATH.write_text("0.9.1\n")
        self.mod.LAST_UPLOAD_PATH.write_text(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n"
        )

        with mock.patch.dict(os.environ, {"CODEX_PLUGIN_ROOT": "/plugin"}):
            output = self._run()

        self.assertIn("$aiqrank:aiqrank", output)
        self.assertNotIn("run /aiqrank", output)
        self.assertIn("updates the Codex plugin automatically", output)

    def test_codex_stale_rank_uses_namespaced_skill_command(self):
        with mock.patch.dict(os.environ, {"CODEX_PLUGIN_ROOT": "/plugin"}):
            output = self._run()

        self.assertIn("$aiqrank:aiqrank", output)
        self.assertNotIn("run /aiqrank", output)

    def test_codex_cache_root_uses_namespaced_skill_command(self):
        codex_root = self.tmp_path / ".codex" / "plugins" / "cache" / "aiqrank" / "aiqrank" / "0.3.20"
        with mock.patch.dict(
            os.environ,
            {"CODEX_PLUGIN_ROOT": "", "CLAUDE_PLUGIN_ROOT": str(codex_root)},
        ):
            output = self._run()

        self.assertIn("$aiqrank:aiqrank", output)
        self.assertNotIn("run /aiqrank", output)

    def test_claude_cache_root_keeps_slash_command(self):
        claude_root = self.tmp_path / ".claude" / "plugins" / "cache" / "aiqrank" / "aiqrank" / "0.3.20"
        with mock.patch.dict(
            os.environ,
            {"CODEX_PLUGIN_ROOT": "", "CLAUDE_PLUGIN_ROOT": str(claude_root)},
        ):
            output = self._run()

        self.assertIn("run /aiqrank", output)
        self.assertNotIn("$aiqrank:aiqrank", output)

    def test_no_stale_version_file_silent_when_fresh(self):
        self._write_fresh_upload()

        output = self._run()
        self.assertEqual(output, "")

    def test_module_paths_are_built_from_home(self):
        # setUp rebinds these constants, so nothing else in this file would
        # notice a typo in the module's own path construction.
        claude = self.tmp_path / ".claude"
        self.assertEqual(self.imported["config_dir"], claude)
        self.assertEqual(
            self.imported["registry"], claude / "plugins" / "installed_plugins.json"
        )
        self.assertEqual(
            self.imported["marketplaces"], claude / "plugins" / "known_marketplaces.json"
        )
        self.assertEqual(
            self.imported["nudge_marker"],
            self.tmp_path / ".config" / "aiqrank" / "cli_install_nudge_at",
        )

    def test_claude_config_dir_env_overrides_the_probe_location(self):
        relocated = self.tmp_path / "elsewhere"
        mod = self._reimport(CLAUDE_CONFIG_DIR=str(relocated))
        self.assertEqual(mod.CLAUDE_CONFIG_DIR, relocated)
        self.assertEqual(
            mod.CLAUDE_PLUGIN_REGISTRY_PATH,
            relocated / "plugins" / "installed_plugins.json",
        )

    def test_host_home_env_overrides_the_probe_location(self):
        host = self.tmp_path / "mounted-home"
        mod = self._reimport(AIQRANK_HOST_HOME=str(host))
        self.assertEqual(mod.CLAUDE_CONFIG_DIR, host / ".claude")

    def test_cli_found_only_on_path_is_detected(self):
        bindir = self.tmp_path / "somewhere" / "bin"
        bindir.mkdir(parents=True)
        cli = bindir / "claude"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        self.mod.CLAUDE_CLI_FALLBACK_PATHS = ()

        with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
            self.assertTrue(self.mod._claude_cli_present())

    def test_cli_missing_from_path_is_found_at_its_install_location(self):
        # The case a GUI host hits: narrow PATH, CLI installed anyway.
        self._install_fake_cli()
        self.assertTrue(self.mod._claude_cli_present())

    def test_cli_install_copy_reads_as_a_message_not_a_command(self):
        # This line lands in an agent's SessionStart context. An imperative
        # shell recipe there invites the agent to run it and mutate the
        # user's CLI plugin registry unasked.
        for text in (
            self.mod.CLI_INSTALL_NUDGE,
            self.mod.CLI_INSTALL_NUDGE_WITH_MARKETPLACE,
        ):
            self.assertIn("do not execute this", text)
            self.assertNotIn("`", text)
            self.assertNotIn("run ", text)

    def test_first_missing_cli_plugin_nudge_includes_marketplace_setup(self):
        self._write_fresh_upload()
        self._install_fake_cli()

        output = self._run()

        self._assert_nudged(output)
        self.assertIn("claude plugin marketplace add aiqrank/plugin", output)
        self.assertIn("claude plugin install aiqrank@aiqrank", output)
        self.assertTrue(self.mod.CLI_INSTALL_NUDGE_PATH.exists())
        mode = stat.S_IMODE(self.mod.CLI_INSTALL_NUDGE_PATH.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_cli_plugin_nudge_is_silent_for_thirty_days(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        now = time.time()

        with mock.patch.object(self.mod.time, "time", return_value=now):
            first_output = self._run()
            second_output = self._run()

        self._assert_nudged(first_output)
        self.assertEqual(second_output, "")

    def test_missing_cli_plugin_nudge_reappears_after_thirty_days(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        now = time.time()
        # Literal 30 days, not the module constant, so a change to
        # CLI_INSTALL_NUDGE_SECONDS fails this test instead of moving with it.
        self.assertEqual(self.mod.CLI_INSTALL_NUDGE_SECONDS, 30 * 24 * 60 * 60)
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text(
            self.mod._iso_timestamp(now - 30 * 24 * 60 * 60 - 1) + "\n"
        )

        with mock.patch.object(self.mod.time, "time", return_value=now):
            output = self._run()

        self._assert_nudged(output)
        self.assertEqual(
            self.mod.CLI_INSTALL_NUDGE_PATH.read_text().strip(),
            self.mod._iso_timestamp(now),
        )

    def test_thirty_day_boundary_is_inclusive(self):
        # Whole seconds, so the microsecond rounding in _iso_timestamp cannot
        # decide the result -- the end-to-end test above stays a second clear
        # of the boundary for exactly that reason.
        now = 1_800_000_000.0
        day30 = 30 * 24 * 60 * 60
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)

        self.mod.CLI_INSTALL_NUDGE_PATH.write_text(
            self.mod._iso_timestamp(now - day30) + "\n"
        )
        self.assertIs(self.mod._cli_install_nudge_due(now), True)

        self.mod.CLI_INSTALL_NUDGE_PATH.write_text(
            self.mod._iso_timestamp(now - day30 + 1) + "\n"
        )
        self.assertIs(self.mod._cli_install_nudge_due(now), False)

    def test_nudge_stays_silent_just_before_thirty_days(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        now = time.time()
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text(
            self.mod._iso_timestamp(now - 30 * 24 * 60 * 60 + 60) + "\n"
        )

        with mock.patch.object(self.mod.time, "time", return_value=now):
            output = self._run()

        self.assertEqual(output, "")

    def test_existing_marketplace_uses_install_only_nudge(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        self._write_cli_marketplaces(
            {
                "aiqrank": {
                    "source": {"source": "github", "repo": "aiqrank/plugin"}
                }
            }
        )

        output = self._run()

        self.assertIn(self.mod.CLI_INSTALL_NUDGE, output)
        self.assertNotIn("marketplace add", output)

    def test_installed_cli_plugin_is_silent_and_clears_nudge_marker(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        self._write_cli_plugins(
            {
                "aiqrank@aiqrank": [
                    {"scope": "user", "version": "0.3.24", "installPath": "/plugin"}
                ]
            }
        )
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text("2026-01-01T00:00:00Z\n")

        output = self._run()

        self.assertEqual(output, "")
        self.assertFalse(self.mod.CLI_INSTALL_NUDGE_PATH.exists())

    def test_legacy_object_shaped_install_entry_is_silent(self):
        # Older Claude Code manifests used one object per installed plugin.
        self._write_fresh_upload()
        self._install_fake_cli()
        self._write_cli_plugins(
            {"aiqrank@aiqrank": {"scope": "user", "version": "0.3.24"}}
        )

        self.assertIs(self.mod._cli_plugin_installed(), True)
        self.assertEqual(self._run(), "")

    def test_empty_install_list_counts_as_not_installed(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        self._write_cli_plugins({"aiqrank@aiqrank": []})

        self.assertIs(self.mod._cli_plugin_installed(), False)
        self._assert_nudged(self._run())

    def test_absent_cli_is_silent_and_clears_nudge_marker(self):
        self._write_fresh_upload()
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text("2026-01-01T00:00:00Z\n")

        output = self._run()

        self.assertEqual(output, "")
        self.assertFalse(self.mod.CLI_INSTALL_NUDGE_PATH.exists())

    def test_uninstall_after_detection_gets_an_immediate_nudge(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        self._write_cli_plugins(
            {
                "aiqrank@aiqrank": [
                    {"scope": "user", "version": "0.3.24", "installPath": "/plugin"}
                ]
            }
        )
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text("2026-01-01T00:00:00Z\n")

        self.assertEqual(self._run(), "")
        self._write_cli_plugins({})
        output = self._run()

        self._assert_nudged(output)

    def test_missing_claude_config_dir_is_unknown_not_absent(self):
        # A relocated CLAUDE_CONFIG_DIR leaves no tree here. Claiming "not
        # installed" would nag forever, since installing could never clear it.
        self._write_fresh_upload()
        self._install_fake_cli()
        nowhere = self.tmp_path / "nowhere"
        self.mod.CLAUDE_CONFIG_DIR = nowhere
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH = (
            nowhere / "plugins" / "installed_plugins.json"
        )

        self.assertIsNone(self.mod._cli_plugin_installed())
        self.assertEqual(self._run(), "")

    def test_malformed_cli_registry_fails_silently(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH.write_text("not-json")

        output = self._run()

        self.assertEqual(output, "")
        self.assertFalse(self.mod.CLI_INSTALL_NUDGE_PATH.exists())

    def test_unreadable_cli_registry_returns_unknown(self):
        # Assert on the probe, not on main(): main()'s blanket except would
        # swallow an unhandled OSError and make this pass either way.
        # A directory at the registry path raises IsADirectoryError, which is
        # an OSError but not FileNotFoundError, whatever the process euid is.
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH.mkdir(parents=True)

        self.assertIsNone(self.mod._cli_plugin_installed())

    def test_unreadable_marketplaces_file_returns_unknown(self):
        self.mod.CLAUDE_MARKETPLACES_PATH.mkdir(parents=True)

        self.assertIsNone(self.mod._cli_marketplace_installed())

    def test_non_object_registry_payload_returns_unknown(self):
        self.mod.CLAUDE_PLUGIN_REGISTRY_PATH.write_text("[1, 2, 3]")

        self.assertIsNone(self.mod._cli_plugin_installed())

    def test_malformed_nudge_marker_heals_and_nudges(self):
        # A truncated write must not silence the reminder forever.
        self._write_fresh_upload()
        self._install_fake_cli()
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text("2026-08-21T18:1")

        output = self._run()

        self._assert_nudged(output)

    def test_future_dated_nudge_marker_heals_and_nudges(self):
        self._write_fresh_upload()
        self._install_fake_cli()
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text("2099-01-01T00:00:00Z\n")

        output = self._run()

        self._assert_nudged(output)

    def test_marker_write_failure_returns_false_and_writes_no_marker(self):
        # Assert on _record_cli_install_nudge itself. Going through main()
        # cannot tell a handled OSError from an unhandled one.
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.mod.CLI_INSTALL_NUDGE_PATH.parent / (
            self.mod.CLI_INSTALL_NUDGE_PATH.name + ".tmp"
        )
        tmp.mkdir()

        self.assertFalse(self.mod._record_cli_install_nudge(time.time()))
        self.assertFalse(self.mod.CLI_INSTALL_NUDGE_PATH.exists())

    def test_failed_write_keeps_the_previous_marker(self):
        # The rename is atomic, so a failed write leaves the old cooldown
        # intact instead of resetting it.
        self.mod.CLI_INSTALL_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.CLI_INSTALL_NUDGE_PATH.write_text("2026-01-01T00:00:00Z\n")
        tmp = self.mod.CLI_INSTALL_NUDGE_PATH.parent / (
            self.mod.CLI_INSTALL_NUDGE_PATH.name + ".tmp"
        )
        tmp.mkdir()

        self.assertFalse(self.mod._record_cli_install_nudge(time.time()))
        self.assertEqual(
            self.mod.CLI_INSTALL_NUDGE_PATH.read_text().strip(),
            "2026-01-01T00:00:00Z",
        )

    def test_unwritable_config_dir_prints_no_nudge_at_all(self):
        # The cooldown is recorded after the reminder is printed, so the
        # ability to record is checked before printing. Without that check an
        # unwritable config dir would repeat the same reminder every session.
        self._write_fresh_upload()
        self._install_fake_cli()
        blocked = self.tmp_path / "blocked"
        blocked.write_text("")  # a file where the marker's directory must be
        self.mod.CLI_INSTALL_NUDGE_PATH = blocked / "cli_install_nudge_at"

        self.assertFalse(self.mod._cli_install_nudge_recordable())
        self.assertEqual(self._run(), "")

    def test_reminder_repeats_when_the_cooldown_cannot_be_recorded(self):
        # The deliberate failure direction: if the process dies between the
        # print and the record, the user sees the reminder again next session
        # rather than losing it silently for 30 days.
        self._write_fresh_upload()
        self._install_fake_cli()

        with mock.patch.object(
            self.mod, "_record_cli_install_nudge", return_value=False
        ):
            first = self._run()
            second = self._run()

        self._assert_nudged(first)
        self._assert_nudged(second)

    def test_codex_host_skips_cli_install_nudge(self):
        self._write_fresh_upload()
        self._install_fake_cli()

        with mock.patch.dict(os.environ, {"CODEX_PLUGIN_ROOT": "/plugin"}):
            output = self._run()

        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
