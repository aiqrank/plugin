#!/usr/bin/env python3
"""
MyAIScore scoring engine.

Takes the output of scan_transcripts.py, computes:
  * activity_breakdown — percentages per category, sums to 100
  * dimension_values   — log-normalized 0-100 per dimension (independent)
  * overall_score      — weighted average → 0-1000
  * tier               — S / A / B / C / D / F
  * inferred_role      — engineer / product / gtm / research / devops / other
  * top_strength       — label of highest dimension

Pure stdlib. Formula constants live at the top and are tunable without
touching logic (plan R22: calibrate so typical active users land in B-C
once we see real data).
"""

from __future__ import annotations

import json
import math
import sys


# -- Scoring weights (base, shared across roles) --

DIMENSION_WEIGHTS = {
    "custom_creation": 5.0,
    "agent_orchestration": 4.0,
    "context_leverage": 3.5,
    "skills_mcps": 3.0,
    "tool_diversity": 2.0,
    "conversation_depth": 1.5,
    "basic_chat": 1.0,
}

# -- Role-specific weight adjustments (multiplicative) --

ROLE_WEIGHT_MULTIPLIERS = {
    "engineer": {
        "agent_orchestration": 1.25,
        "custom_creation": 1.25,
    },
    "product": {
        "context_leverage": 1.25,
        "conversation_depth": 1.20,
    },
    "gtm": {
        "tool_diversity": 1.20,
        "skills_mcps": 1.20,
        "agent_orchestration": 0.90,
    },
    "research": {
        "conversation_depth": 1.25,
        "context_leverage": 1.20,
    },
    "devops": {
        "agent_orchestration": 1.20,
        "tool_diversity": 1.20,
    },
}

# Reference values for log-normalization. Each raw metric that reaches this
# value maps to dimension_value = 100. Below, log curve produces fast growth
# at low values (so a user who just started gets meaningful score movement).
DIMENSION_REFERENCE_MAX = {
    "custom_creation": 10,  # distinct SKILL.md files authored
    "agent_orchestration": 50,  # # of sessions using agents (breadth sub-signal)
    "context_leverage": 80,  # # of sessions using context tools
    "skills_mcps": 100,  # combined distinct skills + MCP servers
    "tool_diversity": 40,  # # of unique tool names
    "conversation_depth": 300,  # avg messages per session (capped high)
    "basic_chat": 20,  # # of tool-less sessions
}

# `agent_orchestration` blends three sub-signals: breadth (# sessions),
# parallelism depth (# turns dispatching >=2 agents), and peak (max parallel
# agents in one turn). Weights sum to 1.0 so the dimension stays in [0, 100].
ORCHESTRATION_BREADTH_WEIGHT = 0.5
ORCHESTRATION_DEPTH_WEIGHT = 0.3
ORCHESTRATION_PEAK_WEIGHT = 0.2
ORCHESTRATION_DEPTH_REF_MAX = 100  # turns with parallel fan-out
ORCHESTRATION_PEAK_REF_MAX = 10  # parallel agents in a single turn

TIER_RANGES = [
    (850, "S"),
    (700, "A"),
    (500, "B"),
    (300, "C"),
    (150, "D"),
    (0, "F"),
]

DEFAULT_ROLE = "engineer"
ROLE_CONFIDENCE_THRESHOLD = 1.5

ROLE_KEYWORDS = {
    "engineer": [
        "code",
        "function",
        "bug",
        "refactor",
        "compile",
        "migration",
        "schema",
        "commit",
        "branch",
    ],
    "product": [
        "spec",
        "requirement",
        "user story",
        "roadmap",
        "feature",
        "prioritize",
        "stakeholder",
        "prd",
    ],
    "gtm": [
        "marketing",
        "campaign",
        "copy",
        "landing page",
        "seo",
        "email",
        "social",
        "content",
        "audience",
    ],
    "research": [
        "analyze",
        "data",
        "investigate",
        "summarize",
        "compare",
        "hypothesis",
    ],
    "devops": [
        "deploy",
        "pipeline",
        "docker",
        "ci",
        "monitoring",
        "infrastructure",
        "kubernetes",
        "terraform",
    ],
}


# -- Public API --


def score(metrics: dict) -> dict:
    """Compute the full score payload from scanner metrics."""
    inferred_role = classify_role(metrics.get("first_messages_sample", []))
    dimension_values = compute_dimensions(metrics)
    activity_breakdown = compute_activity_breakdown(metrics)
    overall = compute_overall_score(dimension_values, inferred_role)
    tier = tier_for_score(overall)
    top_strength = top_strength_label(dimension_values)
    stats = compute_stats(metrics)

    return {
        "overall_score": overall,
        "tier": tier,
        "inferred_role": inferred_role,
        "activity_breakdown": round_map(activity_breakdown, 2),
        "dimension_values": round_map(dimension_values, 1),
        "top_strength": top_strength,
        # `stats` is local-only: shown in the skill preview so the user sees
        # the raw counts behind their tier. `submit_score.py` strips this
        # block before POSTing so the server only receives score payload.
        "stats": stats,
    }


def compute_stats(metrics: dict) -> dict:
    """
    Human-readable activity summary for the skill preview. Pure counts and
    top-N lists — nothing derived from the scoring curves. These exist to
    tell users what they actually did, not how it mapped to dimensions.
    """
    tool_counts = metrics.get("tool_name_counts") or {}
    skill_counts = metrics.get("skill_counts") or {}
    mcp_counts = metrics.get("mcp_server_counts") or {}
    agent_counts = metrics.get("agent_type_counts") or {}

    sessions = int(metrics.get("sessions", 0))
    messages = int(metrics.get("messages", 0))
    sessions_with_tools = int(metrics.get("sessions_with_tools", 0))
    basic_chat_sessions = max(sessions - sessions_with_tools, 0)
    user_messages = int(metrics.get("user_messages", 0))
    user_corrections = int(metrics.get("user_corrections", 0))
    correction_rate_pct = (
        round(100.0 * user_corrections / user_messages, 1) if user_messages else 0.0
    )

    return {
        "window_days": int(metrics.get("window_days", 0)),
        "sessions": sessions,
        "messages": messages,
        "tool_calls": int(metrics.get("tool_calls", 0)),
        "basic_chat_sessions": basic_chat_sessions,
        "user_messages": user_messages,
        "user_corrections": user_corrections,
        "correction_rate_pct": correction_rate_pct,
        "sessions_with_orchestration": int(metrics.get("sessions_with_orchestration", 0)),
        "sessions_with_context_leverage": int(metrics.get("sessions_with_context_leverage", 0)),
        "distinct_tools": len(tool_counts),
        "distinct_skills": len(skill_counts),
        "distinct_mcps": len(mcp_counts),
        "distinct_agent_types": len(agent_counts),
        "max_parallel_agents": int(metrics.get("max_parallel_agents", 0)),
        "parallel_agent_turns": int(metrics.get("parallel_agent_turns", 0)),
        "total_agent_calls": sum(agent_counts.values()),
        "custom_skills_written": int(metrics.get("custom_skill_files_written", 0)),
        "custom_mcp_writes": int(metrics.get("custom_mcp_config_writes", 0)),
        "avg_messages_per_session": round(messages / sessions, 1) if sessions else 0.0,
        "max_messages_in_session": int(metrics.get("max_messages_in_session", 0)),
        "top_tools": top_n(tool_counts, 10),
        "top_skills": top_n(skill_counts, 10),
        "top_mcps": top_n(mcp_counts, 10),
        "top_agents": top_n(agent_counts, 10),
    }


def top_n(counts: dict, n: int) -> list:
    """Return top-N items as [[name, count], ...], sorted desc by count."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[name, int(count)] for name, count in ranked[:n]]


def compute_dimensions(metrics: dict) -> dict:
    """Log-normalize each raw metric onto a 0-100 scale, independently."""
    custom_skills = int(metrics.get("custom_skill_files_written", 0)) + int(
        metrics.get("custom_mcp_config_writes", 0)
    )

    sessions_with_orchestration = int(metrics.get("sessions_with_orchestration", 0))
    parallel_agent_turns = int(metrics.get("parallel_agent_turns", 0))
    max_parallel_agents = int(metrics.get("max_parallel_agents", 0))
    sessions_with_context = int(metrics.get("sessions_with_context_leverage", 0))

    skills = len(metrics.get("skill_counts") or {})
    mcps = len(metrics.get("mcp_server_counts") or {})
    skills_mcps_total = skills + mcps

    unique_tools = len(metrics.get("tool_name_counts") or {})

    sessions = max(int(metrics.get("sessions", 0)), 1)
    messages = int(metrics.get("messages", 0))
    avg_msgs = messages / sessions

    basic_chat_sessions = max(
        int(metrics.get("sessions", 0)) - int(metrics.get("sessions_with_tools", 0)),
        0,
    )

    # Blended agent_orchestration: breadth (how often agents appear) +
    # parallel depth (how often you fan out) + peak (how wide you go).
    orchestration_breadth = log_normalize(
        sessions_with_orchestration, DIMENSION_REFERENCE_MAX["agent_orchestration"]
    )
    orchestration_depth = log_normalize(parallel_agent_turns, ORCHESTRATION_DEPTH_REF_MAX)
    orchestration_peak = log_normalize(max_parallel_agents, ORCHESTRATION_PEAK_REF_MAX)
    orchestration = (
        ORCHESTRATION_BREADTH_WEIGHT * orchestration_breadth
        + ORCHESTRATION_DEPTH_WEIGHT * orchestration_depth
        + ORCHESTRATION_PEAK_WEIGHT * orchestration_peak
    )

    return {
        "custom_creation": log_normalize(custom_skills, DIMENSION_REFERENCE_MAX["custom_creation"]),
        "agent_orchestration": orchestration,
        "context_leverage": log_normalize(
            sessions_with_context, DIMENSION_REFERENCE_MAX["context_leverage"]
        ),
        "skills_mcps": log_normalize(skills_mcps_total, DIMENSION_REFERENCE_MAX["skills_mcps"]),
        "tool_diversity": log_normalize(unique_tools, DIMENSION_REFERENCE_MAX["tool_diversity"]),
        "conversation_depth": log_normalize(
            avg_msgs, DIMENSION_REFERENCE_MAX["conversation_depth"]
        ),
        "basic_chat": log_normalize(basic_chat_sessions, DIMENSION_REFERENCE_MAX["basic_chat"]),
    }


def compute_activity_breakdown(metrics: dict) -> dict:
    """
    Split tool calls across categories. Sums to 100 (unless no tools in which
    case it's all basic_chat).
    """
    tools = metrics.get("tool_name_counts") or {}
    total_calls = sum(tools.values())

    buckets = {
        "custom_creation": 0,
        "agent_orchestration": 0,
        "context_leverage": 0,
        "skills_mcps": 0,
        "tool_diversity": 0,
        "basic_chat": 0,
    }

    if total_calls == 0:
        # No tools used — 100% basic chat
        buckets["basic_chat"] = 100.0
        return buckets

    for name, count in tools.items():
        bucket = categorize_tool(name)
        buckets[bucket] += count

    return {k: (v * 100.0 / total_calls) for k, v in buckets.items()}


def categorize_tool(name: str) -> str:
    if name == "Agent":
        return "agent_orchestration"

    if name == "Skill" or name.startswith("mcp__"):
        return "skills_mcps"

    if name in {
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "ScheduleWakeup",
        "CronCreate",
        "CronList",
        "CronDelete",
        "RemoteTrigger",
        "Monitor",
    }:
        return "context_leverage"

    return "tool_diversity"


def compute_overall_score(dimension_values: dict, role: str) -> int:
    """Weighted average: (sum of dim * weight) / sum(weight) * 10 → 0-1000."""
    multipliers = ROLE_WEIGHT_MULTIPLIERS.get(role, {})

    weighted_sum = 0.0
    weight_sum = 0.0

    for dim, base_weight in DIMENSION_WEIGHTS.items():
        adjusted = base_weight * multipliers.get(dim, 1.0)
        weight_sum += adjusted
        weighted_sum += dimension_values.get(dim, 0.0) * adjusted

    if weight_sum == 0:
        return 0

    return int(round(weighted_sum / weight_sum * 10))


def tier_for_score(score_value: int) -> str:
    for threshold, letter in TIER_RANGES:
        if score_value >= threshold:
            return letter

    return "F"


def classify_role(messages: list) -> str:
    """
    Keyword-based classification.

    Normalizes per-role match counts by vocabulary size, requires the top
    role to be at least ROLE_CONFIDENCE_THRESHOLD x the runner-up to win.
    Otherwise falls back to DEFAULT_ROLE.
    """
    if not messages:
        return DEFAULT_ROLE

    corpus = " ".join(m.lower() for m in messages if isinstance(m, str))

    scores = {}
    for role, vocab in ROLE_KEYWORDS.items():
        matches = sum(corpus.count(kw) for kw in vocab)
        # Normalize by vocabulary size so roles with more keywords don't win structurally
        scores[role] = matches / len(vocab) if vocab else 0

    if not scores or all(v == 0 for v in scores.values()):
        return DEFAULT_ROLE

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_role, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    if runner_up_score == 0 or top_score / runner_up_score >= ROLE_CONFIDENCE_THRESHOLD:
        return top_role

    return DEFAULT_ROLE


def top_strength_label(dimension_values: dict) -> str:
    if not dimension_values:
        return ""

    # Deterministic tie-break: highest value, then alphabetical
    items = sorted(dimension_values.items(), key=lambda kv: (-kv[1], kv[0]))
    key = items[0][0]
    return key.replace("_", " ").title()


# -- Helpers --


def log_normalize(raw, reference_max):
    """Log curve: fast growth at low values, diminishing returns at high."""
    if raw <= 0:
        return 0.0
    if reference_max <= 0:
        return 0.0

    value = 100 * math.log(1 + raw) / math.log(1 + reference_max)
    return max(0.0, min(100.0, value))


def round_map(d: dict, places: int) -> dict:
    return {k: round(v, places) for k, v in d.items()}


# -- CLI entry --


def main(argv: list[str]) -> int:
    # Reads scanner JSON from stdin or from --from FILE
    if "--from" in argv:
        idx = argv.index("--from")
        with open(argv[idx + 1], "r") as fh:
            metrics = json.load(fh)
    else:
        metrics = json.load(sys.stdin)

    result = score(metrics)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
