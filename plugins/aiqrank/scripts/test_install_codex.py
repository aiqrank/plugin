#!/usr/bin/env python3
"""Tests for safe, managed Codex artifact installation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install_codex


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InstallCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.messages: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _artifacts(self, script: bytes = b"new script\n", prompt: bytes = b"new prompt\n"):
        return {
            "scripts/example.py": install_codex.Artifact(
                destination=self.home / ".aiqrank" / "scripts" / "example.py",
                content=script,
                mode=0o700,
            ),
            "prompts/aiqrank.md": install_codex.Artifact(
                destination=self.home / ".codex" / "prompts" / "aiqrank.md",
                content=prompt,
                mode=0o600,
            ),
        }

    def test_known_manifest_hash_upgrades_all_files_and_manifest_last(self):
        old_script = b"old script\n"
        old_prompt = b"old prompt\n"
        artifacts = self._artifacts()
        for key, old in (("scripts/example.py", old_script), ("prompts/aiqrank.md", old_prompt)):
            dest = artifacts[key].destination
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old)

        manifest = self.home / ".aiqrank" / "managed_artifacts.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": "0.3.10",
                    "artifacts": {
                        "scripts/example.py": {"sha256": _sha(old_script)},
                        "prompts/aiqrank.md": {"sha256": _sha(old_prompt)},
                    },
                }
            )
        )

        result = install_codex.install_artifacts(
            artifacts,
            manifest,
            version="0.3.11",
            warn=self.messages.append,
        )

        self.assertTrue(result)
        self.assertEqual(artifacts["scripts/example.py"].destination.read_bytes(), b"new script\n")
        self.assertEqual(artifacts["prompts/aiqrank.md"].destination.read_bytes(), b"new prompt\n")
        self.assertEqual(artifacts["scripts/example.py"].destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual(artifacts["prompts/aiqrank.md"].destination.stat().st_mode & 0o777, 0o600)
        saved = json.loads(manifest.read_text())
        self.assertEqual(saved["version"], "0.3.11")
        self.assertEqual(saved["artifacts"]["scripts/example.py"]["sha256"], _sha(b"new script\n"))
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)

    def test_unknown_modified_file_is_preserved_and_manifest_does_not_advance(self):
        artifacts = self._artifacts()
        destination = artifacts["scripts/example.py"].destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"my custom script\n")
        manifest = self.home / ".aiqrank" / "managed_artifacts.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "version": "0.3.10",
                    "artifacts": {"scripts/example.py": {"sha256": _sha(b"old bundled script\n")}},
                }
            )
        )
        before_manifest = manifest.read_bytes()

        result = install_codex.install_artifacts(
            artifacts,
            manifest,
            version="0.3.11",
            warn=self.messages.append,
        )

        self.assertFalse(result)
        self.assertEqual(destination.read_bytes(), b"my custom script\n")
        self.assertFalse(artifacts["prompts/aiqrank.md"].destination.exists())
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertTrue(any("preserving modified" in message for message in self.messages))

    def test_known_pre_manifest_hash_is_upgraded(self):
        artifacts = self._artifacts()
        destination = artifacts["scripts/example.py"].destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        legacy_content = b"legacy bundled script\n"
        destination.write_bytes(legacy_content)
        manifest = self.home / ".aiqrank" / "managed_artifacts.json"

        known_hashes = dict(install_codex.KNOWN_BUNDLED_HASHES)
        known_hashes["scripts/example.py"] = {_sha(legacy_content)}
        with mock.patch.object(install_codex, "KNOWN_BUNDLED_HASHES", known_hashes):
            result = install_codex.install_artifacts(
                artifacts, manifest, version="0.3.11", warn=self.messages.append
            )

        self.assertTrue(result)
        self.assertEqual(destination.read_bytes(), b"new script\n")
        self.assertEqual(json.loads(manifest.read_text())["version"], "0.3.11")

    def test_replace_failure_rolls_back_every_artifact_and_manifest(self):
        old_script = b"old script\n"
        old_prompt = b"old prompt\n"
        artifacts = self._artifacts()
        for key, old in (("scripts/example.py", old_script), ("prompts/aiqrank.md", old_prompt)):
            dest = artifacts[key].destination
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old)
        manifest = self.home / ".aiqrank" / "managed_artifacts.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": "0.3.10",
                    "artifacts": {
                        "scripts/example.py": {"sha256": _sha(old_script)},
                        "prompts/aiqrank.md": {"sha256": _sha(old_prompt)},
                    },
                }
            )
        )
        before_manifest = manifest.read_bytes()
        real_replace = os.replace
        artifact_replaces = 0

        def fail_second_artifact(source, destination):
            nonlocal artifact_replaces
            if str(destination) != str(manifest):
                artifact_replaces += 1
                if artifact_replaces == 2:
                    raise OSError("simulated replacement failure")
            return real_replace(source, destination)

        with mock.patch.object(install_codex.os, "replace", side_effect=fail_second_artifact):
            result = install_codex.install_artifacts(
                artifacts,
                manifest,
                version="0.3.11",
                warn=self.messages.append,
            )

        self.assertFalse(result)
        self.assertEqual(artifacts["scripts/example.py"].destination.read_bytes(), old_script)
        self.assertEqual(artifacts["prompts/aiqrank.md"].destination.read_bytes(), old_prompt)
        self.assertEqual(manifest.read_bytes(), before_manifest)

    def test_rollback_failure_names_incomplete_artifact_without_leaking_home(self):
        old_script = b"old script\n"
        old_prompt = b"old prompt\n"
        artifacts = self._artifacts()
        for key, old in (("scripts/example.py", old_script), ("prompts/aiqrank.md", old_prompt)):
            destination = artifacts[key].destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(old)
        manifest = self.home / ".aiqrank" / "managed_artifacts.json"
        manifest.write_text(json.dumps({
            "version": "0.3.10",
            "artifacts": {
                "scripts/example.py": {"sha256": _sha(old_script)},
                "prompts/aiqrank.md": {"sha256": _sha(old_prompt)},
            },
        }))
        real_atomic_write = install_codex._atomic_write
        script_writes = 0

        def fail_update_and_rollback(path, content, mode):
            nonlocal script_writes
            if path == artifacts["scripts/example.py"].destination:
                script_writes += 1
                if script_writes == 2:
                    raise OSError("simulated rollback failure")
            if path == artifacts["prompts/aiqrank.md"].destination:
                raise OSError("simulated update failure")
            return real_atomic_write(path, content, mode)

        with mock.patch.object(
            install_codex, "_atomic_write", side_effect=fail_update_and_rollback
        ):
            result = install_codex.install_artifacts(
                artifacts, manifest, version="0.3.11", warn=self.messages.append
            )

        self.assertFalse(result)
        warning = next(message for message in self.messages if "rollback incomplete" in message)
        self.assertIn("scripts/example.py", warning)
        self.assertNotIn(str(self.home), warning)
        self.assertEqual(artifacts["scripts/example.py"].destination.read_bytes(), b"new script\n")
        self.assertEqual(artifacts["prompts/aiqrank.md"].destination.read_bytes(), old_prompt)

    def test_current_artifacts_and_manifest_are_not_rewritten(self):
        artifacts = self._artifacts()
        manifest = self.home / ".aiqrank" / "managed_artifacts.json"
        self.assertTrue(
            install_codex.install_artifacts(
                artifacts, manifest, version="0.3.11", warn=self.messages.append
            )
        )

        with mock.patch.object(install_codex.os, "replace") as replace:
            result = install_codex.install_artifacts(
                artifacts, manifest, version="0.3.11", warn=self.messages.append
            )

        self.assertTrue(result)
        replace.assert_not_called()

    def test_manifest_write_failure_rolls_back_artifacts(self):
        old_script = b"old script\n"
        old_prompt = b"old prompt\n"
        artifacts = self._artifacts()
        for key, old in (("scripts/example.py", old_script), ("prompts/aiqrank.md", old_prompt)):
            destination = artifacts[key].destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(old)
        manifest = self.home / ".aiqrank" / "managed_artifacts.json"
        manifest.write_text(json.dumps({
            "version": "0.3.10",
            "artifacts": {
                "scripts/example.py": {"sha256": _sha(old_script)},
                "prompts/aiqrank.md": {"sha256": _sha(old_prompt)},
            },
        }))
        before_manifest = manifest.read_bytes()
        real_atomic_write = install_codex._atomic_write

        def fail_manifest_write(path, content, mode):
            if path == manifest:
                raise OSError("simulated manifest failure")
            return real_atomic_write(path, content, mode)

        with mock.patch.object(install_codex, "_atomic_write", side_effect=fail_manifest_write):
            result = install_codex.install_artifacts(
                artifacts, manifest, version="0.3.11", warn=self.messages.append
            )

        self.assertFalse(result)
        self.assertEqual(artifacts["scripts/example.py"].destination.read_bytes(), old_script)
        self.assertEqual(artifacts["prompts/aiqrank.md"].destination.read_bytes(), old_prompt)
        self.assertEqual(manifest.read_bytes(), before_manifest)


if __name__ == "__main__":
    unittest.main()
