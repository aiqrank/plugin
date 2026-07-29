import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_pi as scan_pi_module  # noqa: E402
from scan_pi import PiScanIncomplete, resolve_session_dir, scan  # noqa: E402


class PiScannerTests(unittest.TestCase):
    NOW = datetime(2026, 7, 15, 12, 0, 0).timestamp()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def write_jsonl(self, path: Path, entries) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(entry if isinstance(entry, str) else json.dumps(entry) for entry in entries)
            + "\n"
        )
        return path

    def ts(self, day=15, hour=10, minute=0):
        return datetime(2026, 7, day, hour, minute, 0).isoformat()

    def session(self, *, timestamp=None, cwd=None, parent_session=None):
        entry = {
            "type": "session",
            "id": "PRIVATE-SESSION-ID",
            "timestamp": timestamp or self.ts(),
            "cwd": str(cwd or self.cwd),
            "version": "9.9.9-future",
        }
        if parent_session:
            entry["parentSession"] = parent_session
        return entry

    def message(self, role, *, timestamp=None, **attrs):
        message = {"role": role, "timestamp": timestamp or self.ts()}
        message.update(attrs)
        return {
            "type": "message",
            "id": f"PRIVATE-{role}-ID",
            "timestamp": timestamp or self.ts(),
            "message": message,
        }

    def assistant(self, content, *, timestamp=None, usage=None, model=None):
        attrs = {"content": content}
        if usage is not None:
            attrs["usage"] = usage
        if model is not None:
            attrs["model"] = model
        return self.message("assistant", timestamp=timestamp, **attrs)

    def tool_result(self, call_id, tool_name, *, timestamp=None, error=False, details=None):
        attrs = {
            "toolCallId": call_id,
            "toolName": tool_name,
            "isError": error,
            "content": [{"type": "text", "text": "PRIVATE-TOOL-OUTPUT"}],
        }
        if details is not None:
            attrs["details"] = details
        return self.message("toolResult", timestamp=timestamp, **attrs)

    def pi(self, **kwargs):
        return scan(
            session_dir=self.sessions,
            home=self.home,
            now_ts=self.NOW,
            **kwargs,
        )

    def test_resolve_session_dir_precedence(self):
        explicit = self.root / "explicit"
        agent = self.root / "agent"
        default_home = self.root / "default-home"
        with patch.dict(
            os.environ,
            {
                "PI_CODING_AGENT_SESSION_DIR": str(explicit),
                "PI_CODING_AGENT_DIR": str(agent),
            },
            clear=False,
        ):
            self.assertEqual(resolve_session_dir(home=default_home), explicit)

        with patch.dict(
            os.environ,
            {"PI_CODING_AGENT_DIR": str(agent)},
            clear=True,
        ):
            self.assertEqual(resolve_session_dir(home=default_home), agent / "sessions")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_session_dir(home=default_home),
                default_home / ".pi" / "agent" / "sessions",
            )

    def test_main_session_maps_messages_tokens_thinking_tools_models_and_effort(self):
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(),
                {
                    "type": "model_change",
                    "timestamp": self.ts(hour=10),
                    "modelId": "pi-model-active",
                },
                {
                    "type": "thinking_level_change",
                    "timestamp": self.ts(hour=10),
                    "thinkingLevel": "high",
                },
                self.message(
                    "user",
                    timestamp=self.ts(hour=10, minute=1),
                    content=[{"type": "text", "text": "PRIVATE-USER-PROMPT"}],
                ),
                self.assistant(
                    [
                        {"type": "thinking", "thinking": "PRIVATE-REASONING-ONE"},
                        {"type": "thinking", "thinking": "PRIVATE-REASONING-TWO"},
                        {
                            "type": "toolCall",
                            "id": "call-object",
                            "name": "bash",
                            "arguments": {"command": "PRIVATE-COMMAND"},
                        },
                        {
                            "type": "toolCall",
                            "id": "call-string",
                            "name": "mcp__github__search",
                            "arguments": json.dumps({"query": "PRIVATE-TASK"}),
                        },
                    ],
                    timestamp=self.ts(hour=10, minute=2),
                    model="pi-model-explicit",
                    usage={
                        "input": 100,
                        "output": 40,
                        "reasoning": 999,
                        "cacheRead": 20,
                        "cacheWrite": 10,
                        "totalTokens": 150,
                        "cost": 123.45,
                    },
                ),
                self.message(
                    "bashExecution",
                    timestamp=self.ts(hour=10, minute=3),
                    command="PRIVATE-DIRECT-COMMAND",
                    output="PRIVATE-DIRECT-OUTPUT",
                ),
                self.tool_result("call-object", "bash", timestamp=self.ts(hour=10, minute=4)),
            ],
        )

        result = self.pi()
        rollup = result["rollup"]
        self.assertEqual(result["source"], "pi")
        self.assertEqual(rollup["sessions"], 1)
        self.assertEqual(rollup["main_sessions"], 1)
        self.assertEqual(rollup["messages"], 2)
        self.assertEqual(rollup["user_messages"], 1)
        self.assertEqual(rollup["tool_calls"], 3)
        self.assertEqual(rollup["reasoning_blocks"], 2)
        self.assertEqual(rollup["tokens_input"], 100)
        self.assertEqual(rollup["tokens_output"], 40)
        self.assertEqual(rollup["tokens_cache_read"], 20)
        self.assertEqual(rollup["tokens_cache_creation"], 10)
        self.assertEqual(rollup["tokens_total"], 150)
        self.assertEqual(rollup["model_usage"], {"pi-model-explicit": 1})
        self.assertEqual(rollup["agent_model_usage"], {})
        self.assertEqual(rollup["effort_usage"], {"high": 1})
        self.assertEqual(rollup["mcp_server_counts"], {"github": 1})
        self.assertEqual(rollup["tool_name_counts"]["bashExecution"], 1)

        serialized = json.dumps(result)
        for secret in (
            "PRIVATE-SESSION-ID",
            "PRIVATE-USER-PROMPT",
            "PRIVATE-REASONING-ONE",
            "PRIVATE-COMMAND",
            "PRIVATE-DIRECT-COMMAND",
            "PRIVATE-DIRECT-OUTPUT",
            "PRIVATE-TOOL-OUTPUT",
            "PRIVATE-TASK",
            "123.45",
            str(self.root),
        ):
            self.assertNotIn(secret, serialized)

    def test_reasoning_token_count_never_creates_reasoning_block(self):
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(),
                self.assistant(
                    [{"type": "text", "text": "PRIVATE-RESPONSE"}],
                    usage={"input": 1, "output": 2, "reasoning": 300, "totalTokens": 303},
                ),
            ],
        )
        self.assertEqual(self.pi()["rollup"]["reasoning_blocks"], 0)

    def test_high_cardinality_metric_labels_are_compacted_for_server_bounds(self):
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(),
                self.assistant(
                    [
                        {
                            "type": "toolCall",
                            "id": f"tool-{index}",
                            "name": f"custom_tool_{index:02d}",
                            "arguments": {},
                        }
                        for index in range(51)
                    ]
                ),
            ],
        )

        tool_counts = self.pi()["rollup"]["tool_name_counts"]
        self.assertEqual(len(tool_counts), 50)
        self.assertEqual(tool_counts["__other__"], 2)

    def test_model_change_is_used_when_assistant_message_omits_model(self):
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(),
                {
                    "type": "model_change",
                    "timestamp": self.ts(hour=10),
                    "modelId": "pi-model-active",
                },
                self.assistant(
                    [],
                    timestamp=self.ts(hour=10, minute=1),
                    usage={"output": 17, "totalTokens": 17},
                ),
                self.assistant(
                    [],
                    timestamp=self.ts(hour=10, minute=2),
                    model="pi-model-explicit",
                    usage={"output": 23, "totalTokens": 23},
                ),
            ],
        )

        rollup = self.pi()["rollup"]
        self.assertEqual(
            rollup["model_usage"],
            {"pi-model-active": 1, "pi-model-explicit": 1},
        )
        self.assertEqual(
            rollup["model_tokens_out"],
            {"pi-model-active": 17, "pi-model-explicit": 23},
        )

    def test_child_session_uses_agent_model_usage_and_contributes_concurrency(self):
        main = self.sessions / "project" / "main.jsonl"
        child = self.sessions / "project" / "main" / "run-1" / "session.jsonl"
        self.write_jsonl(
            main,
            [
                self.session(timestamp=self.ts(hour=9)),
                self.message("user", timestamp=self.ts(hour=9, minute=1), content=[]),
                self.assistant([], timestamp=self.ts(hour=11), model="main-model"),
            ],
        )
        self.write_jsonl(
            child,
            [
                self.session(timestamp=self.ts(hour=9, minute=30), parent_session="PRIVATE-PARENT-ID"),
                self.assistant([], timestamp=self.ts(hour=9, minute=31), model="child-model"),
                self.assistant([], timestamp=self.ts(hour=10, minute=30), model="child-model"),
            ],
        )
        result = self.pi()
        self.assertEqual(result["rollup"]["sessions"], 2)
        self.assertEqual(result["rollup"]["main_sessions"], 1)
        self.assertEqual(result["rollup"]["model_usage"], {"main-model": 1})
        self.assertEqual(result["rollup"]["agent_model_usage"], {"child-model": 2})
        self.assertGreaterEqual(result["rollup"]["max_concurrent_sessions"], 2)
        self.assertEqual(result["rollup"]["sessions_with_orchestration"], 1)
        self.assertEqual(result["rollup"]["max_parallel_agents"], 1)

    def test_enabled_project_and_package_skill_reads_are_counted(self):
        project_skill = self.cwd / ".pi" / "configured" / "project" / "SKILL.md"
        project_skill.parent.mkdir(parents=True)
        project_skill.write_text("project")
        global_skill = self.home / ".pi" / "agent" / "configured" / "global" / "SKILL.md"
        global_skill.parent.mkdir(parents=True)
        global_skill.write_text("global")

        local_package = self.cwd / ".pi" / "packages" / "local"
        local_skill = local_package / "custom-skills" / "local-skill" / "SKILL.md"
        blocked_local_skill = (
            local_package / "custom-skills" / "blocked" / "SKILL.md"
        )
        local_skill.parent.mkdir(parents=True)
        blocked_local_skill.parent.mkdir(parents=True)
        local_skill.write_text("local")
        blocked_local_skill.write_text("blocked")
        (local_package / "package.json").write_text(
            json.dumps(
                {
                    "name": "local-package",
                    "pi": {
                        "skills": [
                            "./custom-skills",
                            "!./custom-skills/blocked/**",
                        ]
                    },
                }
            )
        )
        disabled_package = self.cwd / ".pi" / "packages" / "disabled"
        disabled_skill = (
            disabled_package / "skills" / "disabled-skill" / "SKILL.md"
        )
        disabled_skill.parent.mkdir(parents=True)
        disabled_skill.write_text("disabled")
        (disabled_package / "package.json").write_text(
            json.dumps({"name": "disabled-package", "pi": {"skills": []}})
        )

        npm_package = self.cwd / ".pi" / "npm" / "node_modules" / "@org" / "pkg"
        npm_allowed = npm_package / "skills" / "npm-allowed" / "SKILL.md"
        npm_blocked = npm_package / "skills" / "npm-blocked" / "SKILL.md"
        npm_allowed.parent.mkdir(parents=True)
        npm_blocked.parent.mkdir(parents=True)
        npm_allowed.write_text("npm allowed")
        npm_blocked.write_text("npm blocked")
        (npm_package / "package.json").write_text(json.dumps({"name": "@org/pkg"}))
        stale_package = self.cwd / ".pi" / "npm" / "node_modules" / "stale"
        stale_skill = stale_package / "skills" / "stale-skill" / "SKILL.md"
        stale_skill.parent.mkdir(parents=True)
        stale_skill.write_text("stale")
        (stale_package / "package.json").write_text(json.dumps({"name": "stale"}))

        git_package = (
            self.home
            / ".pi"
            / "agent"
            / "git"
            / "github.com"
            / "user"
            / "repo"
        )
        git_skill = git_package / "skills" / "git-skill" / "SKILL.md"
        git_skill.parent.mkdir(parents=True)
        git_skill.write_text("git")
        global_npm_package = (
            self.home
            / ".nvm"
            / "versions"
            / "node"
            / "v1"
            / "lib"
            / "node_modules"
            / "global-pkg"
        )
        global_npm_skill = (
            global_npm_package / "skills" / "global-npm" / "SKILL.md"
        )
        global_npm_skill.parent.mkdir(parents=True)
        global_npm_skill.write_text("global npm")
        (global_npm_package / "package.json").write_text(
            json.dumps({"name": "global-pkg"})
        )

        project_settings = self.cwd / ".pi" / "settings.json"
        project_settings.parent.mkdir(parents=True, exist_ok=True)
        project_settings.write_text(
            json.dumps(
                {
                    "skills": ["./configured/project"],
                    "packages": [
                        "./packages/local",
                        "./packages/disabled",
                        {"source": "npm:@org/pkg", "skills": ["npm-allowed"]},
                    ],
                }
            )
        )
        global_settings = self.home / ".pi" / "agent" / "settings.json"
        global_settings.parent.mkdir(parents=True, exist_ok=True)
        global_settings.write_text(
            json.dumps(
                {
                    "skills": ["./configured/global"],
                    "packages": ["git:github.com/user/repo@v1", "npm:global-pkg"],
                }
            )
        )

        skill_paths = [
            project_skill,
            global_skill,
            local_skill,
            blocked_local_skill,
            disabled_skill,
            npm_allowed,
            npm_blocked,
            git_skill,
            global_npm_skill,
            stale_skill,
        ]
        calls = [
            {
                "type": "toolCall",
                "id": f"skill-{index}",
                "name": "read",
                "arguments": {"path": str(path)},
            }
            for index, path in enumerate(skill_paths)
        ]
        results = [
            self.tool_result(f"skill-{index}", "read")
            for index in range(len(skill_paths))
        ]
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [self.session(), self.assistant(calls), *results],
        )

        self.assertEqual(
            self.pi()["rollup"]["skill_counts"],
            {
                "git-skill": 1,
                "global": 1,
                "global-npm": 1,
                "local-skill": 1,
                "npm-allowed": 1,
                "project": 1,
            },
        )

    def test_parent_child_launches_deduplicate_and_control_actions_do_not_launch(self):
        main = self.sessions / "project" / "main.jsonl"
        child = self.sessions / "project" / "main" / "run-1" / "session.jsonl"
        missing_child = self.sessions / "project" / "main" / "run-2" / "session.jsonl"
        self.write_jsonl(
            main,
            [
                self.session(),
                self.assistant(
                    [
                        {
                            "type": "toolCall",
                            "id": "launch-call",
                            "name": "subagent",
                            "arguments": {
                                "async": True,
                                "tasks": [
                                    {"agent": "worker-a", "task": "PRIVATE-TASK-A"},
                                    {"agent": "worker-b", "task": "PRIVATE-TASK-B"},
                                ],
                            },
                        },
                        *[
                            {
                                "type": "toolCall",
                                "id": f"control-{action}",
                                "name": "subagent",
                                "arguments": {"action": action},
                            }
                            for action in (
                                "list",
                                "get",
                                "models",
                                "create",
                                "update",
                                "delete",
                                "status",
                                "interrupt",
                                "resume",
                                "append-step",
                                "doctor",
                            )
                        ],
                    ]
                ),
                self.tool_result(
                    "launch-call",
                    "subagent",
                    details={
                        "runId": "PRIVATE-RUN-ID",
                        "mode": "parallel",
                        "totalCost": 999,
                        "results": [
                            {"agent": "worker-a", "sessionFile": str(child), "task": "PRIVATE-A"},
                            {
                                "agent": "worker-b",
                                "sessionFile": str(missing_child),
                                "task": "PRIVATE-B",
                            },
                        ],
                    },
                ),
            ],
        )
        self.write_jsonl(
            child,
            [
                self.session(parent_session="PRIVATE-PARENT-ID"),
                self.assistant([], model="child-model"),
            ],
        )

        rollup = self.pi()["rollup"]
        self.assertEqual(rollup["sessions_with_orchestration"], 1)
        self.assertEqual(rollup["parallel_agent_turns"], 1)
        self.assertEqual(rollup["max_parallel_agents"], 2)
        self.assertEqual(rollup["agent_type_counts"], {"worker-a": 1, "worker-b": 1})

    def test_parent_only_launch_counts_when_no_child_transcript_exists(self):
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(),
                self.assistant(
                    [
                        {
                            "type": "toolCall",
                            "id": "parent-only",
                            "name": "subagent",
                            "arguments": {"agent": "solo", "task": "PRIVATE-TASK"},
                        }
                    ]
                ),
            ],
        )
        rollup = self.pi()["rollup"]
        self.assertEqual(rollup["sessions_with_orchestration"], 1)
        self.assertEqual(rollup["parallel_agent_turns"], 0)
        self.assertEqual(rollup["max_parallel_agents"], 1)
        self.assertEqual(rollup["agent_type_counts"], {"solo": 1})

    def test_skill_reads_and_authored_skills_emit_names_only(self):
        user_skill = self.home / ".pi" / "agent" / "skills" / "user-skill" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text("PRIVATE-SKILL-BODY")
        project_skill = self.cwd / ".pi" / "skills" / "project-skill" / "SKILL.md"
        project_skill.parent.mkdir(parents=True)
        project_skill.write_text("PRIVATE-PROJECT-SKILL-BODY")
        outside = self.root / "outside" / "secret-skill" / "SKILL.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("PRIVATE-OUTSIDE-SKILL")

        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(),
                self.assistant(
                    [
                        {
                            "type": "toolCall",
                            "id": "skill-ok",
                            "name": "read",
                            "arguments": {"path": str(user_skill)},
                        },
                        {
                            "type": "toolCall",
                            "id": "skill-failed",
                            "name": "read",
                            "arguments": {"path": str(project_skill)},
                        },
                        {
                            "type": "toolCall",
                            "id": "skill-outside",
                            "name": "read",
                            "arguments": {"path": str(outside)},
                        },
                    ]
                ),
                self.tool_result("skill-ok", "read"),
                self.tool_result("skill-failed", "read", error=True),
                self.tool_result("skill-outside", "read"),
            ],
        )

        result = self.pi()
        rollup = result["rollup"]
        self.assertEqual(rollup["skill_counts"], {"user-skill": 1})
        self.assertEqual(rollup["custom_skill_files_written"], 0)
        self.assertEqual(rollup["authored_skill_names"], [])
        serialized = json.dumps(result)
        self.assertNotIn(str(user_skill), serialized)
        self.assertNotIn("PRIVATE-SKILL-BODY", serialized)

    def test_malformed_unknown_and_run_history_records_are_ignored(self):
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                "not-json PRIVATE-MALFORMED",
                self.session(),
                {"type": "future-record", "timestamp": self.ts(), "future": "PRIVATE-FUTURE"},
                self.message("user", content=[]),
            ],
        )
        self.write_jsonl(
            self.sessions / "project" / "run-history.jsonl",
            [self.session(), self.message("user", content=[])],
        )
        result = self.pi()
        self.assertEqual(result["rollup"]["sessions"], 1)
        self.assertEqual(result["rollup"]["messages"], 1)
        self.assertNotIn("PRIVATE-MALFORMED", json.dumps(result))
        self.assertNotIn("PRIVATE-FUTURE", json.dumps(result))

    def test_window_boundaries_epoch_milliseconds_and_midnight_splitting(self):
        start = datetime(2026, 7, 14, 23, 59, 30)
        end = datetime(2026, 7, 15, 0, 1, 0)
        old = datetime(2026, 6, 1, 12, 0, 0)
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [
                self.session(timestamp=int(start.timestamp() * 1000)),
                self.message("user", timestamp=int(start.timestamp() * 1000), content=[]),
                self.assistant([], timestamp=int(end.timestamp() * 1000)),
                self.message("user", timestamp=int(old.timestamp() * 1000), content=[]),
            ],
        )
        result = self.pi()
        self.assertEqual([d["date"] for d in result["daily"]], ["2026-07-14", "2026-07-15"])
        self.assertEqual(set(result["intervals_by_day"]), {"2026-07-14", "2026-07-15"})
        self.assertEqual(result["rollup"]["messages"], 2)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_real_path_deduplication(self):
        real = self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [self.session(), self.message("user", content=[])],
        )
        alias = self.sessions / "project" / "alias.jsonl"
        try:
            alias.symlink_to(real)
        except OSError:
            self.skipTest("symlink creation unavailable")
        result = self.pi()
        self.assertEqual(result["rollup"]["sessions"], 1)
        self.assertEqual(result["rollup"]["messages"], 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinked_transcript_outside_session_root_is_ignored(self):
        outside = self.write_jsonl(
            self.root / "outside.jsonl",
            [self.session(), self.message("user", content=[])],
        )
        alias = self.sessions / "project" / "outside-alias.jsonl"
        alias.parent.mkdir(parents=True)
        try:
            alias.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation unavailable")

        result = self.pi()
        self.assertEqual(result["rollup"]["sessions"], 0)
        self.assertEqual(result["rollup"]["messages"], 0)

    def test_unreadable_transcript_fails_the_pi_snapshot(self):
        transcript = self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [self.session(), self.message("user", content=[])],
        ).resolve()
        original_open = Path.open

        def fail_target(path, *args, **kwargs):
            if path == transcript:
                raise PermissionError("private path detail")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_target):
            with self.assertRaisesRegex(PiScanIncomplete, "could not be opened"):
                self.pi()

    def test_unreadable_nested_transcript_directory_fails_the_pi_snapshot(self):
        nested = self.sessions / "project"
        self.write_jsonl(
            nested / "main.jsonl",
            [self.session(), self.message("user", content=[])],
        )
        original_scandir = scan_pi_module.os.scandir

        def fail_nested(path):
            if Path(path) == nested.resolve():
                raise PermissionError("private nested path")
            return original_scandir(path)

        with patch.object(scan_pi_module.os, "scandir", side_effect=fail_nested):
            with self.assertRaisesRegex(PiScanIncomplete, "could not be traversed"):
                self.pi()

    def test_unreadable_skill_directory_no_longer_affects_the_pi_snapshot(self):
        skill_root = self.home / ".pi" / "agent" / "skills"
        nested = skill_root / "private-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("private")
        self.write_jsonl(
            self.sessions / "project" / "main.jsonl",
            [self.session(), self.message("user", content=[])],
        )
        original_scandir = scan_pi_module.os.scandir

        def fail_nested(path):
            if Path(path) == nested.resolve():
                raise PermissionError("private nested path")
            return original_scandir(path)

        with patch.object(scan_pi_module.os, "scandir", side_effect=fail_nested):
            rollup = self.pi()["rollup"]
        self.assertEqual(rollup["authored_skill_names"], [])
        self.assertEqual(rollup["custom_skill_files_written"], 0)

    def test_missing_session_directory_returns_empty_envelope(self):
        result = scan(
            session_dir=self.root / "missing",
            home=self.home,
            now_ts=self.NOW,
        )
        self.assertEqual(result["source"], "pi")
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["intervals_by_day"], {})
        self.assertEqual(result["rollup"]["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
