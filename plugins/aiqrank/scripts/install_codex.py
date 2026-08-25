#!/usr/bin/env python3
"""Install and safely upgrade AIQ Rank's managed Codex artifacts.

Unknown files are treated as user-owned. Managed files are replaced only when
their hash matches the local manifest, a known prior release, or the current
bundle. Every replacement uses an atomic rename and the manifest advances only
after the complete artifact transaction succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BUNDLED_VERSION = "0.3.25"
MANIFEST_NAME = "managed_artifacts.json"

# Hashes from 0.3.10, the release immediately preceding managed upgrades.
# Manifest hashes cover every subsequent release; this table bootstraps users
# whose existing files predate the local manifest.
KNOWN_BUNDLED_HASHES = {
    "scripts/scan_codex.py": {
        "caa1cf3b7e474f71b025352d30a45b873630bf4a641d195559fab9d4264442dc",  # 0.3.5
        "193ec38e0e5481bebc5b518e738d0e37b7b2a54d0541807a69325db7685684d4",  # 0.3.9-10
    },
    "scripts/scan_transcripts.py": {
        "0c9c27b0bbd932251286d81e2cfe359334d87c5335b142356fb8e36091e759f0",  # 0.3.5
        "18e7e5ac48fbadb0434b85364191d0f903783597fb86b81c4769b378221a9c43",  # 0.3.9
        "41d1301fc1e39e5415ce7c3255f833b0466bb12103c0559577b7b3fa4cbe8f47",  # 0.3.10
    },
    "scripts/upload_metrics.py": {
        "da4cd4c15538b1d103089dc668fff531746daabaae7099c0a1d58e5929b97dcf",  # 0.3.5
        "c5af315abf8b2b4b9adca0ef4796b89942fc599ea9a7a293c52119a6a3bed933",  # 0.3.9-10
    },
    "scripts/_version.py": {
        "d2f5a5c13045ed8f1a84a441dd1f998d844ed7562d4d32da00655fe01c9e9e6a",  # 0.3.5
        "6b5438b4602672b5e16c645fb43a055cd11b71a4520f49849a56e5ba3cd969cb",  # 0.3.9
        "852397272035efb9467800360386584b456fbb35e798aeef270d925abd898b43",  # 0.3.10
    },
    "prompts/aiqrank.md": {
        "98f1ca9997bcc53587cf199faa59a7a0ba189bd92a7ad6ed36723aa1979da20c",  # 0.3.5
        "519ed28448ebeaa5ce97074cd8e2495f3964cda257b6f7e3e9ed71f2c5b24443",  # 0.3.9-10
    },
}

SCRIPT_NAMES = (
    "scan_codex.py",
    "scan_transcripts.py",
    "scan_pi.py",
    "scan_agent_runtimes.py",
    "upload_metrics.py",
    "check_update.py",
    "self_update.py",
    "_version.py",
)


@dataclass(frozen=True)
class Artifact:
    destination: Path
    content: bytes
    mode: int


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_hash(manifest: dict, key: str) -> str | None:
    artifacts = manifest.get("artifacts")
    entry = artifacts.get(key) if isinstance(artifacts, dict) else None
    value = entry.get("sha256") if isinstance(entry, dict) else None
    return value if isinstance(value, str) else None


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)


def install_artifacts(
    artifacts: dict[str, Artifact],
    manifest_path: Path,
    *,
    version: str,
    warn: Callable[[str], None] = print,
) -> bool:
    """Install a complete artifact set, preserving unknown modified files.

    Returns true only when all artifacts are managed and the manifest advanced.
    If any artifact is user-modified, the whole update is preserved so every
    installed hash remains described by the prior manifest.
    """
    manifest = _read_manifest(manifest_path)
    eligible: dict[str, Artifact] = {}
    current_states: dict[Path, tuple[bytes, int] | None] = {}
    preserved = []

    for key, artifact in artifacts.items():
        new_hash = _sha256(artifact.content)
        try:
            current = artifact.destination.read_bytes()
            current_mode = stat.S_IMODE(artifact.destination.stat().st_mode)
        except FileNotFoundError:
            current_states[artifact.destination] = None
            eligible[key] = artifact
            continue
        except OSError as exc:
            warn(f"AIQ Rank: could not inspect {key} ({type(exc).__name__})")
            return False

        current_states[artifact.destination] = (current, current_mode)
        current_hash = _sha256(current)
        allowed_hashes = set(KNOWN_BUNDLED_HASHES.get(key, set()))
        previous_hash = _manifest_hash(manifest, key)
        if previous_hash:
            allowed_hashes.add(previous_hash)
        allowed_hashes.add(new_hash)

        if current_hash not in allowed_hashes:
            preserved.append(key)
            warn(f"AIQ Rank: preserving modified {key}; update it manually")
        elif current_hash != new_hash or current_mode != artifact.mode:
            eligible[key] = artifact

    backups: dict[Path, tuple[bytes, int] | None] = {}
    replaced: list[Path] = []
    keys_by_destination = {
        artifact.destination: key for key, artifact in artifacts.items()
    }

    if preserved:
        return False

    def rollback() -> list[str]:
        failures: list[str] = []
        for destination in reversed(replaced):
            backup = backups[destination]
            try:
                if backup is None:
                    destination.unlink(missing_ok=True)
                else:
                    content, mode = backup
                    _atomic_write(destination, content, mode)
            except OSError:
                failures.append(keys_by_destination[destination])
        return failures

    def warn_rollback(prefix: str, exc: OSError) -> None:
        failures = rollback()
        if failures:
            warn(
                f"AIQ Rank: {prefix} ({type(exc).__name__}); rollback incomplete for "
                + ", ".join(failures)
            )
        else:
            warn(f"AIQ Rank: {prefix} ({type(exc).__name__}); rolled back")

    try:
        for artifact in eligible.values():
            destination = artifact.destination
            backups[destination] = current_states[destination]
            _atomic_write(destination, artifact.content, artifact.mode)
            replaced.append(destination)
    except OSError as exc:
        warn_rollback("managed artifact update failed", exc)
        return False

    manifest_body = {
        "version": version,
        "artifacts": {
            key: {"sha256": _sha256(artifact.content), "mode": oct(artifact.mode)}
            for key, artifact in sorted(artifacts.items())
        },
    }
    manifest_content = (json.dumps(manifest_body, indent=2, sort_keys=True) + "\n").encode()
    try:
        manifest_current = manifest_path.read_bytes()
        manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
    except FileNotFoundError:
        manifest_current = None
        manifest_mode = None
    except OSError as exc:
        warn_rollback("manifest update failed", exc)
        return False

    if manifest_current == manifest_content and manifest_mode == 0o600:
        return True

    try:
        _atomic_write(manifest_path, manifest_content, 0o600)
    except OSError as exc:
        warn_rollback("manifest update failed", exc)
        return False
    return True


def _build_artifacts(
    home: Path, loader: Callable[[str], bytes]
) -> dict[str, Artifact]:
    artifacts = {
        f"scripts/{name}": Artifact(
            destination=home / ".aiqrank" / "scripts" / name,
            content=loader(f"scripts/{name}"),
            mode=0o700,
        )
        for name in SCRIPT_NAMES
    }
    artifacts["prompts/aiqrank.md"] = Artifact(
        destination=home / ".codex" / "prompts" / "aiqrank.md",
        content=loader("codex_prompts/aiqrank.md"),
        mode=0o600,
    )
    return artifacts


def bundled_artifacts(plugin_root: Path, home: Path) -> dict[str, Artifact]:
    return _build_artifacts(home, lambda relative: (plugin_root / relative).read_bytes())


def install_bundled(
    plugin_root: Path,
    *,
    home: Path | None = None,
    warn: Callable[[str], None] = print,
) -> bool:
    home = home or Path.home()
    try:
        artifacts = bundled_artifacts(plugin_root, home)
    except OSError as exc:
        warn(f"AIQ Rank: could not read bundled Codex artifacts ({type(exc).__name__})")
        return False
    return install_artifacts(
        artifacts,
        home / ".aiqrank" / MANIFEST_NAME,
        version=BUNDLED_VERSION,
        warn=warn,
    )


def download_artifacts(base_url: str, home: Path) -> dict[str, Artifact]:
    def download(relative: str) -> bytes:
        with urllib.request.urlopen(f"{base_url}/{relative}", timeout=30) as response:
            return response.read()

    return _build_artifacts(home, download)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    home = Path.home()
    try:
        artifacts = download_artifacts(args.base.rstrip("/"), home)
    except OSError as exc:
        print(f"AIQ Rank setup failed: download error ({type(exc).__name__})", file=sys.stderr)
        return 1
    complete = install_artifacts(
        artifacts,
        home / ".aiqrank" / MANIFEST_NAME,
        version=BUNDLED_VERSION,
        warn=lambda message: print(message, file=sys.stderr),
    )
    if not complete:
        print("AIQ Rank setup preserved one or more modified files; review the warnings above.", file=sys.stderr)
        return 1
    print("AIQ Rank is set up for Codex.")
    print("Run $aiqrank:aiqrank in a new Codex session to see your score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
