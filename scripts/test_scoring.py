#!/usr/bin/env python3
"""Tests for scoring.py"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scoring import (  # noqa: E402
    classify_role,
    compute_activity_breakdown,
    compute_dimensions,
    compute_overall_score,
    score,
    tier_for_score,
    top_strength_label,
)


class TierBoundariesTests(unittest.TestCase):
    def test_all_boundaries(self):
        pairs = [
            (0, "F"),
            (149, "F"),
            (150, "D"),
            (299, "D"),
            (300, "C"),
            (499, "C"),
            (500, "B"),
            (699, "B"),
            (700, "A"),
            (849, "A"),
            (850, "S"),
            (1000, "S"),
        ]
        for value, expected in pairs:
            self.assertEqual(tier_for_score(value), expected, msg=f"score={value}")


class RoleClassificationTests(unittest.TestCase):
    def test_defaults_to_engineer_for_empty_input(self):
        self.assertEqual(classify_role([]), "engineer")

    def test_classifies_engineering_heavy_transcript(self):
        messages = [
            "fix the bug in the migration",
            "refactor this function and recompile",
            "commit the changes to the branch",
        ]
        self.assertEqual(classify_role(messages), "engineer")

    def test_classifies_gtm_heavy_transcript(self):
        messages = [
            "write landing page copy for the campaign",
            "improve SEO and email subject lines",
            "draft social content for the audience",
        ]
        self.assertEqual(classify_role(messages), "gtm")

    def test_classifies_devops_transcript(self):
        messages = [
            "deploy the docker image",
            "set up the pipeline in CI",
            "monitor the kubernetes cluster",
        ]
        self.assertEqual(classify_role(messages), "devops")

    def test_falls_back_to_engineer_on_low_confidence(self):
        # Balanced keywords — no role clearly dominates
        messages = ["code and marketing and deploy and data"]
        self.assertEqual(classify_role(messages), "engineer")

    def test_no_matches_falls_back_to_engineer(self):
        messages = ["today is tuesday and the weather is great"]
        self.assertEqual(classify_role(messages), "engineer")


class ActivityBreakdownTests(unittest.TestCase):
    def test_all_basic_chat_when_no_tools(self):
        result = compute_activity_breakdown({"tool_name_counts": {}})
        self.assertEqual(result["basic_chat"], 100.0)

    def test_categorizes_agent_as_orchestration(self):
        result = compute_activity_breakdown({"tool_name_counts": {"Agent": 10}})
        self.assertEqual(result["agent_orchestration"], 100.0)

    def test_categorizes_skill_and_mcp_together(self):
        result = compute_activity_breakdown(
            {
                "tool_name_counts": {
                    "Skill": 5,
                    "mcp__pencil__batch_design": 5,
                }
            }
        )
        self.assertEqual(result["skills_mcps"], 100.0)

    def test_categorizes_context_leverage(self):
        result = compute_activity_breakdown(
            {"tool_name_counts": {"TaskCreate": 2, "ScheduleWakeup": 3, "CronCreate": 5}}
        )
        self.assertEqual(result["context_leverage"], 100.0)

    def test_categorizes_generic_tools_as_tool_diversity(self):
        result = compute_activity_breakdown(
            {"tool_name_counts": {"Bash": 5, "Read": 3, "Write": 2}}
        )
        self.assertEqual(result["tool_diversity"], 100.0)

    def test_sums_to_100_across_mix(self):
        result = compute_activity_breakdown(
            {
                "tool_name_counts": {
                    "Agent": 25,
                    "Skill": 25,
                    "TaskCreate": 25,
                    "Bash": 25,
                }
            }
        )
        total = sum(result.values())
        self.assertAlmostEqual(total, 100.0, places=5)


class DimensionsTests(unittest.TestCase):
    def test_all_zero_for_empty_metrics(self):
        dims = compute_dimensions({})
        for dim, value in dims.items():
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)

    def test_all_clamped_to_100_max(self):
        dims = compute_dimensions(
            {
                "custom_skill_files_written": 10_000,
                "custom_mcp_config_writes": 10_000,
                "sessions_with_orchestration": 10_000,
                "sessions_with_context_leverage": 10_000,
                "skill_counts": {str(i): 1 for i in range(1000)},
                "mcp_server_counts": {str(i): 1 for i in range(1000)},
                "tool_name_counts": {str(i): 1 for i in range(1000)},
                "sessions": 10_000,
                "messages": 10_000_000,
                "sessions_with_tools": 10_000,
            }
        )
        for value in dims.values():
            self.assertLessEqual(value, 100)


class OverallScoreTests(unittest.TestCase):
    def test_zero_dimensions_is_zero_score(self):
        dims = {k: 0.0 for k in ["custom_creation", "agent_orchestration", "context_leverage", "skills_mcps", "tool_diversity", "conversation_depth", "basic_chat"]}
        self.assertEqual(compute_overall_score(dims, "engineer"), 0)

    def test_all_100_dimensions_is_1000_score(self):
        dims = {k: 100.0 for k in ["custom_creation", "agent_orchestration", "context_leverage", "skills_mcps", "tool_diversity", "conversation_depth", "basic_chat"]}
        self.assertEqual(compute_overall_score(dims, "engineer"), 1000)

    def test_role_changes_weighting(self):
        # Scenario: lots of orchestration, nothing else. Engineer weights boost
        # agent_orchestration 1.25x, GTM reduces it 0.9x.
        dims = {k: 0.0 for k in ["custom_creation", "agent_orchestration", "context_leverage", "skills_mcps", "tool_diversity", "conversation_depth", "basic_chat"]}
        dims["agent_orchestration"] = 100.0

        eng_score = compute_overall_score(dims, "engineer")
        gtm_score = compute_overall_score(dims, "gtm")

        self.assertGreater(eng_score, gtm_score)


class TopStrengthTests(unittest.TestCase):
    def test_returns_humanized_label(self):
        dims = {"tool_mastery": 20, "agent_orchestration": 90, "custom_creation": 50}
        self.assertEqual(top_strength_label(dims), "Agent Orchestration")

    def test_deterministic_tie_break(self):
        dims = {"beta": 50, "alpha": 50, "gamma": 50}
        self.assertEqual(top_strength_label(dims), "Alpha")

    def test_empty_input(self):
        self.assertEqual(top_strength_label({}), "")


class EndToEndTests(unittest.TestCase):
    def test_user_with_zero_sessions_gets_f_tier(self):
        result = score({})
        self.assertEqual(result["tier"], "F")
        self.assertEqual(result["overall_score"], 0)

    def test_full_payload_shape(self):
        metrics = {
            "sessions": 20,
            "messages": 300,
            "tool_calls": 100,
            "sessions_with_tools": 18,
            "sessions_with_orchestration": 8,
            "sessions_with_context_leverage": 10,
            "tool_name_counts": {
                "Bash": 30,
                "Read": 25,
                "Edit": 20,
                "Write": 10,
                "Agent": 5,
                "Skill": 5,
                "TaskCreate": 5,
            },
            "skill_counts": {"commit": 3, "review": 2},
            "mcp_server_counts": {"pencil": 4},
            "agent_type_counts": {"general-purpose": 5},
            "custom_skill_files_written": 1,
            "custom_mcp_config_writes": 0,
            "first_messages_sample": ["fix this bug", "refactor the migration"],
        }

        result = score(metrics)

        self.assertIn(result["tier"], ["S", "A", "B", "C", "D", "F"])
        self.assertEqual(result["inferred_role"], "engineer")
        self.assertGreaterEqual(result["overall_score"], 0)
        self.assertLessEqual(result["overall_score"], 1000)

        # activity_breakdown sums to ~100
        total = sum(result["activity_breakdown"].values())
        self.assertAlmostEqual(total, 100.0, places=1)

        # All dimension values in [0, 100]
        for v in result["dimension_values"].values():
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 100)

        self.assertIsInstance(result["top_strength"], str)

    def test_stats_block_exposes_rich_local_signals(self):
        metrics = {
            "window_days": 60,
            "sessions": 100,
            "messages": 5000,
            "tool_calls": 800,
            "sessions_with_tools": 85,
            "sessions_with_orchestration": 12,
            "sessions_with_context_leverage": 20,
            "tool_name_counts": {"Bash": 300, "Read": 200, "Edit": 100, "Agent": 40, "Skill": 10},
            "skill_counts": {"commit": 3, "review": 2},
            "mcp_server_counts": {"pencil": 4, "playwright": 2},
            "agent_type_counts": {"general-purpose": 20, "Explore": 15, "Plan": 5},
            "custom_skill_files_written": 2,
            "custom_mcp_config_writes": 0,
            "max_parallel_agents": 5,
            "parallel_agent_turns": 7,
            "max_messages_in_session": 420,
            "first_messages_sample": [],
        }

        stats = score(metrics)["stats"]

        self.assertEqual(stats["sessions"], 100)
        self.assertEqual(stats["basic_chat_sessions"], 15)  # 100 - 85
        self.assertEqual(stats["distinct_tools"], 5)
        self.assertEqual(stats["distinct_skills"], 2)
        self.assertEqual(stats["distinct_mcps"], 2)
        self.assertEqual(stats["distinct_agent_types"], 3)
        self.assertEqual(stats["max_parallel_agents"], 5)
        self.assertEqual(stats["parallel_agent_turns"], 7)
        self.assertEqual(stats["total_agent_calls"], 40)  # sum of agent_type_counts
        self.assertEqual(stats["max_messages_in_session"], 420)
        self.assertEqual(stats["avg_messages_per_session"], 50.0)

        # top_tools is sorted desc and capped
        self.assertEqual(stats["top_tools"][0], ["Bash", 300])
        self.assertEqual(stats["top_tools"][1], ["Read", 200])
        self.assertLessEqual(len(stats["top_tools"]), 10)

    def test_stats_block_handles_empty_metrics(self):
        stats = score({})["stats"]

        self.assertEqual(stats["sessions"], 0)
        self.assertEqual(stats["avg_messages_per_session"], 0.0)
        self.assertEqual(stats["top_tools"], [])
        self.assertEqual(stats["max_parallel_agents"], 0)
        self.assertEqual(stats["user_messages"], 0)
        self.assertEqual(stats["correction_rate_pct"], 0.0)

    def test_correction_rate_computed_from_raw_counts(self):
        stats = score({"user_messages": 200, "user_corrections": 15})["stats"]
        self.assertEqual(stats["user_messages"], 200)
        self.assertEqual(stats["user_corrections"], 15)
        self.assertEqual(stats["correction_rate_pct"], 7.5)

    def test_agent_orchestration_rewards_parallel_dispatch(self):
        # Two users with identical breadth (10 orchestration sessions) but
        # one fans out in parallel — that user should score higher on the
        # agent_orchestration dimension.
        base = {
            "sessions": 50,
            "sessions_with_tools": 40,
            "sessions_with_orchestration": 10,
        }
        solo = score(base)["dimension_values"]["agent_orchestration"]
        parallel = score(
            {**base, "parallel_agent_turns": 40, "max_parallel_agents": 6}
        )["dimension_values"]["agent_orchestration"]

        self.assertGreater(parallel, solo)


if __name__ == "__main__":
    unittest.main()
