#!/usr/bin/env python3
"""Tests for the automatic update path."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import self_update


def _make_cache(root: Path, versions: list[str], *, with_scripts: bool = True) -> Path:
    for version in versions:
        target = root / version
        target.mkdir(parents=True, exist_ok=True)
        if with_scripts:
            (target / "scripts").mkdir(exist_ok=True)
    return root


class DetectEngineTest(unittest.TestCase):
    def test_prefers_injected_claude_root(self):
        with mock.patch.dict("os.environ", {"CLAUDE_PLUGIN_ROOT": "/x"}, clear=True):
            self.assertEqual(self_update.detect_engine(), "claude")

    def test_prefers_injected_codex_root(self):
        with mock.patch.dict("os.environ", {"CODEX_PLUGIN_ROOT": "/x"}, clear=True):
            self.assertEqual(self_update.detect_engine(), "codex")

    def test_explicit_codex_root_wins_when_both_are_injected(self):
        env = {"CLAUDE_PLUGIN_ROOT": "/a", "CODEX_PLUGIN_ROOT": "/b"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(self_update.detect_engine(), "codex")

    def test_detects_codex_from_claude_compatible_cache_root(self):
        root = "/Users/test/.codex/plugins/cache/aiqrank/aiqrank/0.3.21"
        with mock.patch.dict(
            "os.environ",
            {"CLAUDE_PLUGIN_ROOT": root, "CODEX_PLUGIN_ROOT": ""},
            clear=True,
        ):
            self.assertEqual(self_update.detect_engine(), "codex")

    def test_keeps_claude_cache_root_as_claude(self):
        root = "/Users/test/.claude/plugins/cache/aiqrank/aiqrank/0.3.21"
        with mock.patch.dict(
            "os.environ",
            {"CLAUDE_PLUGIN_ROOT": root, "CODEX_PLUGIN_ROOT": ""},
            clear=True,
        ):
            self.assertEqual(self_update.detect_engine(), "claude")

    def test_falls_back_to_codex_managed_install(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(Path, "is_dir", return_value=True):
                self.assertEqual(self_update.detect_engine(), "codex")


class PendingVersionTest(unittest.TestCase):
    def test_none_when_no_signal_recorded(self):
        with mock.patch.object(self_update, "_read_stale_version", return_value=None):
            self.assertIsNone(self_update.pending_version())

    def test_none_when_recorded_version_is_not_newer(self):
        with mock.patch.object(self_update, "PLUGIN_VERSION", "0.3.16"):
            with mock.patch.object(
                self_update, "_read_stale_version", return_value="0.3.15"
            ):
                with mock.patch.object(self_update, "_clear_stale_signal") as clear:
                    self.assertIsNone(self_update.pending_version())
        clear.assert_called_once()

    def test_returns_version_when_newer(self):
        with mock.patch.object(self_update, "PLUGIN_VERSION", "0.3.15"):
            with mock.patch.object(
                self_update, "_read_stale_version", return_value="0.3.16"
            ):
                self.assertEqual(self_update.pending_version(), "0.3.16")


class ResolveRootTest(unittest.TestCase):
    def test_picks_highest_semver_not_lexicographic(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                cache = _make_cache(Path(tmp), ["0.3.9", "0.3.10", "0.3.16"])
                with mock.patch.object(self_update, "CLAUDE_CACHE", cache):
                    self.assertEqual(
                        self_update.resolve_root("claude").name, "0.3.16"
                    )

    def test_ignores_cache_entries_without_scripts(self):
        import tempfile

        with mock.patch.dict("os.environ", {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                _make_cache(cache, ["0.3.15"])
                _make_cache(cache, ["0.3.16"], with_scripts=False)
                with mock.patch.object(self_update, "CLAUDE_CACHE", cache):
                    self.assertEqual(
                        self_update.resolve_root("claude").name, "0.3.15"
                    )

    def test_newer_cache_supersedes_injected_session_root(self):
        """The whole point: an in-session update must win over the stale env var."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp), ["0.3.15", "0.3.16"])
            env = {"CLAUDE_PLUGIN_ROOT": str(cache / "0.3.15")}
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch.object(self_update, "CLAUDE_CACHE", cache):
                    self.assertEqual(
                        self_update.resolve_root("claude").name, "0.3.16"
                    )

    def test_newer_codex_cache_supersedes_older_managed_root(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            managed = base / "managed"
            managed_scripts = managed / "scripts"
            managed_scripts.mkdir(parents=True)
            (managed_scripts / "_version.py").write_text(
                'PLUGIN_VERSION = "0.3.14"\n'
            )
            cache = _make_cache(base / "cache", ["0.3.21"])
            with mock.patch.dict("os.environ", {}, clear=True):
                with mock.patch.object(self_update, "CODEX_MANAGED_ROOT", managed):
                    with mock.patch.object(self_update, "CODEX_CACHE", cache):
                        self.assertEqual(
                            self_update.resolve_root("codex"), cache / "0.3.21"
                        )

    def test_falls_back_to_injected_root_when_cache_is_absent(self):
        env = {"CLAUDE_PLUGIN_ROOT": "/injected"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch.object(self_update, "CLAUDE_CACHE", Path("/nonexistent")):
                self.assertEqual(
                    self_update.resolve_root("claude"), Path("/injected")
                )


class UpdateCodexTest(unittest.TestCase):
    def test_refreshes_codex_plugin_and_downloads_matching_tag(self):
        seen = {}

        def fake_fetch(url):
            seen["url"] = url
            return b"print('installer')"

        with mock.patch.object(self_update.shutil, "which", return_value="/bin/codex"):
            with mock.patch.object(self_update, "_fetch", fake_fetch):
                with mock.patch.object(self_update, "_run", return_value=True) as run:
                    self.assertTrue(self_update._update_codex("0.3.16"))

        commands = [call[0][0] for call in run.call_args_list]
        self.assertEqual(commands[0][1:], ["plugin", "marketplace", "upgrade", "aiqrank"])
        self.assertEqual(commands[1][1:], ["plugin", "add", "aiqrank@aiqrank"])
        self.assertEqual(
            commands[2][1:],
            ["-", "--base", "https://raw.githubusercontent.com/aiqrank/plugin/v0.3.16/plugins/aiqrank"],
        )

        self.assertEqual(
            seen["url"],
            "https://raw.githubusercontent.com/aiqrank/plugin/v0.3.16"
            "/plugins/aiqrank/scripts/install_codex.py",
        )

    def test_adds_marketplace_when_upgrade_fails(self):
        with mock.patch.object(self_update.shutil, "which", return_value="/bin/codex"):
            with mock.patch.object(self_update, "_fetch", return_value=b"installer"):
                with mock.patch.object(
                    self_update, "_run", side_effect=[False, True, True, True]
                ) as run:
                    self.assertTrue(self_update._update_codex("0.3.16"))

        commands = [call[0][0] for call in run.call_args_list]
        self.assertEqual(commands[1][1:], ["plugin", "marketplace", "add", "aiqrank/plugin"])
        self.assertEqual(commands[2][1:], ["plugin", "add", "aiqrank@aiqrank"])

    def test_reports_failure_when_codex_cli_is_missing(self):
        with mock.patch.object(self_update.shutil, "which", return_value=None):
            with mock.patch.object(self_update, "_fetch") as fetch:
                self.assertFalse(self_update._update_codex("0.3.16"))
        fetch.assert_not_called()

    def test_failed_download_does_not_run_installer(self):
        with mock.patch.object(self_update.shutil, "which", return_value="/bin/codex"):
            with mock.patch.object(self_update, "_fetch", return_value=None):
                with mock.patch.object(self_update, "_run", return_value=True) as run:
                    self.assertFalse(self_update._update_codex("0.3.16"))

        self.assertEqual(run.call_count, 2)

    def test_failed_marketplace_update_does_not_continue_when_add_fails(self):
        with mock.patch.object(self_update.shutil, "which", return_value="/bin/codex"):
            with mock.patch.object(self_update, "_run", side_effect=[False, False]) as run:
                self.assertFalse(self_update._update_codex("0.3.16"))

        self.assertEqual(run.call_count, 2)

    def test_host_is_constant_and_not_server_controlled(self):
        seen = {}

        with mock.patch.object(self_update, "_run", return_value=True) as run:
            with mock.patch.object(self_update.shutil, "which", return_value="/bin/codex"):
                with mock.patch.object(
                    self_update, "_fetch", lambda url: seen.setdefault("url", url) and None
                ):
                    self_update._update_codex("0.3.16")

        self.assertTrue(
            seen["url"].startswith("https://raw.githubusercontent.com/aiqrank/plugin/")
        )

class UpdateClaudeTest(unittest.TestCase):
    def test_runs_marketplace_then_plugin_update(self):
        with mock.patch.object(self_update.shutil, "which", return_value="/bin/claude"):
            with mock.patch.object(self_update, "_run", return_value=True) as run:
                self.assertTrue(self_update._update_claude())

        commands = [call[0][0] for call in run.call_args_list]
        self.assertEqual(commands[0][1:], ["plugin", "marketplace", "update", "aiqrank"])
        self.assertEqual(commands[1][1:], ["plugin", "update", "aiqrank@aiqrank"])

    def test_stops_when_marketplace_update_fails(self):
        with mock.patch.object(self_update.shutil, "which", return_value="/bin/claude"):
            with mock.patch.object(self_update, "_run", return_value=False) as run:
                self.assertFalse(self_update._update_claude())
        self.assertEqual(run.call_count, 1)

    def test_reports_failure_when_cli_is_missing(self):
        with mock.patch.object(self_update.shutil, "which", return_value=None):
            self.assertFalse(self_update._update_claude())


class MainTest(unittest.TestCase):
    def test_always_prints_a_plugin_root(self):
        with mock.patch.object(self_update, "pending_version", return_value=None):
            with mock.patch.object(
                self_update, "resolve_root", return_value=Path("/root")
            ):
                with mock.patch("builtins.print") as printed:
                    self.assertEqual(self_update.main(), 0)
        printed.assert_called_once_with("PLUGIN_ROOT=/root")

    def test_failed_update_still_lets_the_scan_continue(self):
        with mock.patch.object(self_update, "pending_version", return_value="0.3.16"):
            with mock.patch.object(self_update, "update", return_value=False):
                with mock.patch.object(
                    self_update, "resolve_root", return_value=Path("/root")
                ):
                    with mock.patch("builtins.print") as printed:
                        self.assertEqual(self_update.main(), 0)

        output = " ".join(call[0][0] for call in printed.call_args_list)
        self.assertIn("did not complete", output)
        self.assertIn("PLUGIN_ROOT=/root", output)

    def test_codex_compat_session_clears_old_marker_and_uses_newest_cache(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            marker = base / "stale_version"
            marker.write_text("0.3.20\n")

            managed = base / "managed"
            managed_scripts = managed / "scripts"
            managed_scripts.mkdir(parents=True)
            (managed_scripts / "_version.py").write_text(
                'PLUGIN_VERSION = "0.3.14"\n'
            )
            cache = _make_cache(base / "cache", ["0.3.21"])
            plugin_root_path = base / "plugin_root"
            plugin_root_path.parent.mkdir(parents=True, exist_ok=True)
            compat_root = base / ".codex" / "plugins" / "cache" / "aiqrank"

            with (
                mock.patch.object(self_update, "PLUGIN_VERSION", "0.3.22"),
                mock.patch.object(
                    self_update, "_read_stale_version", return_value="0.3.20"
                ),
                mock.patch.object(self_update, "STALE_VERSION_PATH", marker),
                mock.patch.object(self_update, "CODEX_MANAGED_ROOT", managed),
                mock.patch.object(self_update, "CODEX_CACHE", cache),
                mock.patch.object(
                    self_update, "PLUGIN_ROOT_PATH", plugin_root_path
                ),
                mock.patch.dict(
                    "os.environ",
                    {
                        "CLAUDE_PLUGIN_ROOT": str(compat_root),
                        "CODEX_PLUGIN_ROOT": "",
                    },
                    clear=True,
                ),
                mock.patch.object(self_update, "update") as update,
                mock.patch("builtins.print") as printed,
            ):
                self.assertEqual(self_update.main(), 0)
                update.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual(
                    plugin_root_path.read_text(),
                    str(cache / "0.3.21") + "\n",
                )
                printed.assert_called_once_with(
                    f"PLUGIN_ROOT={cache / '0.3.21'}"
                )

    def test_successful_update_clears_the_stale_signal(self):
        with mock.patch.object(self_update, "pending_version", return_value="0.3.16"):
            with mock.patch.object(self_update, "update", return_value=True):
                with mock.patch.object(
                    self_update, "resolve_root", return_value=Path("/root")
                ):
                    with mock.patch.object(self_update, "_clear_stale_signal") as clear:
                        with mock.patch("builtins.print"):
                            self_update.main()
        clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
