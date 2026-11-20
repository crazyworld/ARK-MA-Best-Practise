"""FIX-01 / FIX-02：SSJH 查询语义与检索字段契约测试。

背景（客户原版）：
  - 检索式必须整式提交。「瑞幸+(暹罗厘普|多栋)」的 `+` 是“且”，拆词后逐个查询
    再相加，会把同一条帖子按命中词重复计数，并把“且”降级为“或”。
  - 检索字段统一 `content,title`；封面 OCR / 视频标签不得参与检索。

运行：python3 -m unittest tests.test_ssjh_query
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssjh  # noqa: E402


class KeywordSelectionTest(unittest.TestCase):
    def test_prefers_whole_brand_query_expression(self):
        plan = {
            "brand": "瑞幸",
            "brand_query": "瑞幸+(暹罗厘普|多栋)",
            "brand_words": ["瑞幸", "暹罗厘普", "多栋"],
            "keywords": ["瑞幸+(暹罗厘普|多栋)"],
        }
        self.assertEqual(ssjh._keywords(plan), ["瑞幸+(暹罗厘普|多栋)"])

    def test_brand_query_wins_over_split_keywords(self):
        """老 Run 若残留拆好的 keywords，仍以 brand_query 为准。"""
        plan = {"brand_query": "瑞幸+(暹罗厘普|多栋)", "keywords": ["瑞幸", "暹罗厘普"]}
        self.assertEqual(ssjh._keywords(plan), ["瑞幸+(暹罗厘普|多栋)"])

    def test_falls_back_to_brand(self):
        self.assertEqual(ssjh._keywords({"brand": "立白"}), ["立白"])

    def test_combined_query_single_passthrough(self):
        self.assertEqual(ssjh._combined_query(["A+B"]), "A+B")

    def test_combined_query_ors_without_losing_and(self):
        """多检索式合并成一次查询，各自内部的 `+` 保持原样。"""
        merged = ssjh._combined_query(["瑞幸+(暹罗厘普|多栋)", "瑞幸+流心芝士拿铁"])
        self.assertEqual(merged, "(瑞幸+(暹罗厘普|多栋))|(瑞幸+流心芝士拿铁)")


class PullQueryContractTest(unittest.TestCase):
    """pull() 只发一次请求，且显式带 keyword_fields。"""

    class _FakeClient:
        calls = []

        def run(self, **kwargs):
            type(self).calls.append(kwargs)
            return {"articles": {}, "cloud_word": {}, "meta": {},
                    "total_volume": 0, "total_interaction": 0}

    def setUp(self):
        self._FakeClient.calls = []
        self.plan = {
            "period_start": "2026-10-01", "period_end": "2026-10-20",
            "brand": "瑞幸",
            "brand_query": "瑞幸+(暹罗厘普|多栋)",
            "brand_words": ["瑞幸", "暹罗厘普", "多栋"],
            "keywords": ["瑞幸+(暹罗厘普|多栋)"],
            "platforms": ["xiaohongshu"],
        }

    def test_single_request_with_whole_expression_and_text_fields(self):
        with patch.object(ssjh, "_load_skill", return_value=self._FakeClient), \
                patch.object(ssjh, "api_available", return_value=True):
            out = ssjh.pull(self.plan)
        self.assertEqual(len(self._FakeClient.calls), 1, "整式必须只查一次")
        call = self._FakeClient.calls[0]
        self.assertEqual(call["keyword"], "瑞幸+(暹罗厘普|多栋)")
        self.assertEqual(call["keyword_fields"], "content,title")
        self.assertEqual(out["meta"]["keyword_fields"], "content,title")
        self.assertEqual(out["meta"]["query"], "瑞幸+(暹罗厘普|多栋)")

    def test_plan_can_override_keyword_fields(self):
        plan = dict(self.plan, ssjh_keyword_fields="content,title,cover_ocr_content")
        with patch.object(ssjh, "_load_skill", return_value=self._FakeClient), \
                patch.object(ssjh, "api_available", return_value=True):
            ssjh.pull(plan)
        self.assertEqual(
            self._FakeClient.calls[0]["keyword_fields"],
            "content,title,cover_ocr_content",
        )

    def test_no_merge_of_per_word_results(self):
        """回归：不得再对每个词各查一次再相加。"""
        with patch.object(ssjh, "_load_skill", return_value=self._FakeClient), \
                patch.object(ssjh, "api_available", return_value=True):
            ssjh.pull(self.plan)
        self.assertEqual(len(self._FakeClient.calls), 1)
        keywords = [c["keyword"] for c in self._FakeClient.calls]
        self.assertNotIn("瑞幸", keywords)
        self.assertNotIn("暹罗厘普", keywords)


class MatchFieldsDefaultTest(unittest.TestCase):
    def test_skill_default_keeps_ocr_pipeline_overrides_to_content_title(self):
        """FIX-02：skill 层 MATCH_FIELDS 保留原版默认值（含 OCR），
        Topic5 在 pipeline 层（ssjh.py DEFAULT_KEYWORD_FIELDS）显式传
        content,title 覆盖。原版行为是「覆盖安装 Skill 包含 OCR 的默认值」，
        不改共享 skill 的默认值——本文件被多个 topic 共用。"""
        import importlib.util
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "SSJH-Social-Stat", "skill.py",
        )
        spec = importlib.util.spec_from_file_location("ssjh_skill_under_test", path)
        assert spec is not None and spec.loader is not None, f"无法加载 {path}"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 1) skill 默认值保留 OCR 字段（原版行为，不改）
        self.assertIn("cover_ocr_content", mod.MATCH_FIELDS["weibo"])
        self.assertIn("video_labels", mod.MATCH_FIELDS["video"])
        # 2) pipeline 层的默认覆盖值是 content,title
        import ssjh
        self.assertEqual(ssjh.DEFAULT_KEYWORD_FIELDS, "content,title")


if __name__ == "__main__":
    unittest.main()
