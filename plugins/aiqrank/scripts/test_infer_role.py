#!/usr/bin/env python3
"""Tests for infer_role.py"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from infer_role import (  # noqa: E402
    DEFAULT_ROLE,
    ROLE_CONFIDENCE_THRESHOLD,
    classify_role,
)


class ClassifyRoleTests(unittest.TestCase):
    def test_empty_messages_returns_default_role(self):
        result = classify_role([])
        self.assertEqual(result["inferred_role"], DEFAULT_ROLE)

    def test_strongly_engineer_messages_classify_as_engineer(self):
        messages = [
            "let me refactor this function and commit",
            "bug in the schema migration",
            "compile errors on the branch",
        ]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], "engineer")

    def test_strongly_product_messages_classify_as_product(self):
        messages = [
            "draft a prd for the new feature",
            "what's the spec requirement for this user story",
            "roadmap and stakeholder alignment",
        ]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], "product")

    def test_strongly_marketing_messages_classify_as_marketing(self):
        messages = [
            "marketing copy for the landing page",
            "seo audit for the campaign",
            "social content for the audience",
        ]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], "marketing")

    def test_strongly_sales_messages_classify_as_sales(self):
        messages = [
            "sales pipeline update for the deal",
            "outbound prospect follow-up",
            "discovery call notes and proposal",
        ]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], "sales")

    def test_strongly_founder_messages_classify_as_founder(self):
        messages = [
            "pitch deck for the investor meeting",
            "fundraise update and cap table",
            "hiring plan for the seed round",
        ]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], "founder")

    def test_ambiguous_messages_fall_back_to_default(self):
        # A mix that doesn't clearly favor any role.
        messages = ["hello", "how are you", "what time is it"]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], DEFAULT_ROLE)

    def test_below_confidence_threshold_falls_back(self):
        # Close tie (engineer vs product at ~1:1) — threshold requires 1.5x.
        messages = [
            "refactor the spec",
            "commit the prd",
        ]
        result = classify_role(messages)
        self.assertEqual(result["inferred_role"], DEFAULT_ROLE)

    def test_scores_dict_always_present(self):
        result = classify_role(["compile this code"])
        self.assertIn("scores", result)
        self.assertIn("engineer", result["scores"])
        self.assertIsInstance(result["scores"]["engineer"], float)

    def test_threshold_constant_is_above_unity(self):
        # Guard against accidentally loosening the threshold to <=1.
        self.assertGreater(ROLE_CONFIDENCE_THRESHOLD, 1.0)


if __name__ == "__main__":
    unittest.main()
