"""FIX-55：词云 call-brief（原版 wordcloud_call_brief.md v1.4）与语义复核。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wordcloud_adapter as adapter  # noqa: E402


class CallBriefTest(unittest.TestCase):
    def test_business_question_and_goal_carried(self):
        brief = adapter.build_call_brief(
            {"campaign_goal": ["品牌年轻化"]},
            {"raw_request": "想知道联名对品牌年轻化的作用", "business_questions": ["联名是否带动年轻人讨论"]},
            "A", "overall", "all")
        self.assertIn("联名是否带动年轻人讨论", brief["analysis_questions"])
        self.assertIn("活动讨论是否回应「品牌年轻化」这一目标", brief["analysis_questions"])
        self.assertEqual(brief["user_focus"], "想知道联名对品牌年轻化的作用")
        self.assertEqual(brief["platform"], "综合已选平台")

    def test_requirement_refs_sha256(self):
        req = {"raw_request": "x"}
        brief = adapter.build_call_brief({}, req, "A", "overall", "all")
        ref = next(r for r in brief["requirement_refs"] if r["source"] == "raw_request")
        self.assertEqual(ref["sha256"], adapter._sha("x"))

    def test_brand_level_default_question(self):
        brief = adapter.build_call_brief({"brand": "瑞幸"}, {}, "brand", "overall", "xiaohongshu")
        self.assertEqual(brief["analysis_level"], "brand")
        self.assertEqual(brief["analysis_questions"], [adapter.DEFAULT_QUESTIONS["brand"]])
        self.assertLessEqual(len(adapter.build_call_brief(
            {}, {"business_questions": list("abcde")}, "A", "product", "all")["analysis_questions"]), 3)

    def test_scope_mismatch_rejected(self):
        brief = adapter.build_call_brief({}, {}, "A", "overall", "all")
        with self.assertRaisesRegex(RuntimeError, "dimension"):
            adapter.check_brief_scope(brief, {"campaign": "A", "dimension": "brand", "platform": "all"},
                                      {"platform": "综合已选平台（小红书）"})
        adapter.check_brief_scope(brief, {"campaign": "A", "dimension": "overall", "platform": "all"},
                                  {"platform": "综合已选平台（小红书）"})

    def test_review_must_answer_or_explain(self):
        brief = adapter.build_call_brief({}, {}, "A", "overall", "all")
        with self.assertRaises(ValueError):
            adapter.validate_brief_review({"brief_review": {"answered_questions": ["别的问题"]}}, brief)
        adapter.validate_brief_review({"brief_review": {"answered_questions": [], "unanswered_reason": "词表无相关表达"}}, brief)


if __name__ == "__main__":
    unittest.main()
