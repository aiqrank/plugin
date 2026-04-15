#!/usr/bin/env python3
"""
MyAIScore role inference.

Reads a scanner metrics JSON (from `scan_transcripts.py`) and classifies
the user's role using keyword matching over `first_messages_sample` —
the short text of each session's first user message. Emits JSON:

    {"inferred_role": "engineer", "scores": {...}}

The role is the ONLY thing derived from message text. This happens
locally on the user's machine; the raw sample text is never transmitted
(the skill strips `first_messages_sample` before calling submit_score.py).

Keeping this separate from scoring keeps the skill flow honest: no tier,
no overall score, and no dimensions are ever computed on the client.
Those come from the server after submission.

Pure stdlib. Constants are tunable in place.
"""

from __future__ import annotations

import json
import sys


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


def classify_role(messages: list) -> dict:
    """
    Returns `{"inferred_role": role, "scores": {role: normalized_score, ...}}`.

    Normalizes per-role match counts by vocabulary size and requires the
    top role to be at least ROLE_CONFIDENCE_THRESHOLD x the runner-up.
    Otherwise falls back to DEFAULT_ROLE.
    """
    if not messages:
        return {"inferred_role": DEFAULT_ROLE, "scores": {}}

    corpus = " ".join(m.lower() for m in messages if isinstance(m, str))

    scores = {}
    for role, vocab in ROLE_KEYWORDS.items():
        matches = sum(corpus.count(kw) for kw in vocab)
        # Normalize so roles with more keywords don't win structurally.
        scores[role] = matches / len(vocab) if vocab else 0.0

    if not scores or all(v == 0 for v in scores.values()):
        return {"inferred_role": DEFAULT_ROLE, "scores": scores}

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_role, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    if runner_up_score == 0 or top_score / runner_up_score >= ROLE_CONFIDENCE_THRESHOLD:
        return {"inferred_role": top_role, "scores": scores}

    return {"inferred_role": DEFAULT_ROLE, "scores": scores}


def main(argv: list[str]) -> int:
    """
    Reads scanner metrics from stdin (or --from FILE), emits role to stdout.
    """
    if "--from" in argv:
        idx = argv.index("--from")
        with open(argv[idx + 1], "r") as fh:
            metrics = json.load(fh)
    else:
        metrics = json.load(sys.stdin)

    sample = metrics.get("first_messages_sample", [])
    result = classify_role(sample)

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
