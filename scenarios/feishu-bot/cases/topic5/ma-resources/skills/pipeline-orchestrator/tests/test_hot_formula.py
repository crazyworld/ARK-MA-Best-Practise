"""FIX-49：热帖成功配方（KS_Content_Analysis_Prompt 热帖解构 / 2.N.4④）。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hot_formula  # noqa: E402
import report_contract as RC  # noqa: E402

ROWS = [{"platform": "小红书", "content_mode": "UGC", "title": f"t{i}", "content": "c",
         "interaction_cnt": 100 * i, "url": f"https://x/{i}"} for i in range(1, 6)]
GOOD = {"callout": "高互动内容集中在开箱测评类笔记。", "insights": ["前五条中 3 条为开箱。", "情感以种草为主。"],
        "rows": [{"form": "图文", "theme": "开箱", "tone": "种草", "rep_id": "post-1"},
                 {"form": "图文", "theme": "测评", "tone": "测评", "rep_id": "post-2"},
                 {"form": "视频", "theme": "打卡", "tone": "趣味", "rep_id": "post-5"}]}


class HotFormulaTest(unittest.TestCase):
    def test_rep_interaction_backfilled_from_input(self):
        model_says = {**GOOD, "rows": [{**r, "rep_interaction": 999999} for r in GOOD["rows"]]}
        with patch("semantic.chat_json", return_value=model_says):
            out = hot_formula.analyze("A", ROWS, ("人均",))
        self.assertEqual([r["rep_interaction"] for r in out["rows"]], [100, 200, 500])

    def test_unknown_rep_id_retried_then_accepted(self):
        bad = {**GOOD, "rows": GOOD["rows"][:2] + [{**GOOD["rows"][2], "rep_id": "post-99"}]}
        with patch("semantic.chat_json", side_effect=[bad, GOOD]) as model:
            hot_formula.analyze("A", ROWS, ())
        self.assertEqual(model.call_count, 2)
        self.assertIn("post-99", model.call_args_list[1].args[1])

    def test_three_failures_raise(self):
        with patch("semantic.chat_json", return_value={"rows": []}):
            with self.assertRaises(RuntimeError):
                hot_formula.analyze("A", ROWS, ())

    def test_forbidden_terms_rejected(self):
        bad = {**GOOD, "callout": "人均互动最高的是开箱。"}
        with patch("semantic.chat_json", side_effect=[bad, GOOD]) as model:
            hot_formula.analyze("A", ROWS, ("人均",))
        self.assertEqual(model.call_count, 2)

    def test_too_few_rows_not_applicable(self):
        self.assertTrue(hot_formula.analyze("A", ROWS[:2], ())["not_applicable"])

    def test_render_four_column_table_in_2n4(self):
        with patch("semantic.chat_json", return_value=GOOD):
            result = hot_formula.analyze("A", ROWS, ())
        md = RC.assemble("# r\n\n### 数据说明\n\n## 整体洞察\nx\n\n## 一、品牌传播表现\n\n### 1.1 传播概况\n\n"
                         "### 1.4 热门内容\n\n## 二、活动传播表现\n\n### 2.1 活动 A\n\n#### 2.1.3 口碑分析\n\n"
                         "#### 2.1.4 内容分析\n\n#### 2.1.5 热门内容\n",
                         plan={}, stats={"brand": {}, "campaigns": {"A": {}}}, charts={},
                         insight={"hot_post_formula": {"A": result}})
        body = md[RC.section(md, "2.1.4")[1]:RC.section(md, "2.1.5")[0]]
        self.assertIn("④ 热帖成功配方", body)
        self.assertIn("| 内容形式 | 核心主题 | 情感基调 | 代表帖互动量 |", body)
        self.assertIn("> [!tip]", body)


if __name__ == "__main__":
    unittest.main()
