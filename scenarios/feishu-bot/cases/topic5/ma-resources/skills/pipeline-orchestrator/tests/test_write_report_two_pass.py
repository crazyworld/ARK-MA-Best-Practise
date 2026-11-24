"""FIX-44：write_report 两段生成（全文 → 经验总结合成）与四章核心成果表。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
from _util import make_state  # noqa: E402

SUMMARY = ("### 3.1 成功经验\n\n**经验 1：腰部达人**\n数据支撑：2.1.4 显示，条均互动量 75。\n"
           "可复制建议：沿用。\n\n### 3.2 待改进问题\n\n**问题 1：负面集中**\n"
           "根因定位：2.1.3 数据显示，负面 3 条。\n改进方向：优化排队。\n\n"
           "### 3.3 后续行动计划\n\n**立即行动**\n· 增配门店人手\n")


def _body():
    md = "# 瑞幸复盘\n\n### 数据说明\n\n## 整体洞察\n活动相关 2 条。\n\n## 一、品牌传播表现\n"
    for num, title in (("1.1", "传播概况"), ("1.2", "口碑分析"), ("1.3", "内容分析"), ("1.4", "热门内容")):
        md += f"### {num} {title}\n品牌正文。\n"
    md += "## 二、活动传播表现\n### 2.1 活动 A\n"
    for k, title in enumerate(("传播概况", "提及漏斗分析", "口碑分析", "内容分析", "热门内容"), 1):
        md += f"#### 2.1.{k} {title}\n活动正文{k}。\n"
    md += ("### 2.2 活动拉升效果\n以下为品牌整体口径。\n## 三、经验总结\n"
           "### 3.1 成功经验\n占位\n### 3.2 待改进问题\n占位\n### 3.3 后续行动计划\n占位\n"
           "## 四、总结与建议\n- 待改善方向：门店体验。\n")
    return md


class TwoPassTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("stats_bundle", {"brand": {}, "campaigns": {"A": {
            "metrics": {"活动相关条数": 2, "互动量": 150, "条均互动量": 75.0, "NSR": 0.5}}}})
        self.st.write_artifact("chart_urls", {"charts": []})
        self.st.write_artifact("wordcloud_insight", {"campaigns": {}})
        self.st.write_artifact("confirmed_plan", {"brand": "瑞幸", "campaign_goal": ["产品种草"],
                                                  "platforms": ["xiaohongshu"]})

    def _run(self):
        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat", side_effect=[_body(), SUMMARY]) as model:
            out = handlers.write_report({"id": "n16"}, self.st, {})["artifacts"]["report_md"]
        return out, model

    def test_second_pass_gets_body_and_goal(self):
        out, model = self._run()
        self.assertEqual(model.call_count, 2)
        user = model.call_args_list[1].args[1]
        self.assertIn("活动目标：产品种草", user)
        self.assertIn("活动正文3", user)
        self.assertNotIn("占位", user)              # 只喂一、二章洞察
        self.assertIn("**经验 1：腰部达人**", out["markdown"])
        self.assertNotIn("\n占位\n", out["markdown"])

    def test_core_results_table_from_stats(self):
        md = self._run()[0]["markdown"]
        final = md[md.index("## 四、总结与建议"):]
        self.assertIn("| A | 2 | 150 | 75.0 | +50.0% |", final)
        self.assertIn("待改善方向", final)

    def test_goal_not_filled_from_inferred_type(self):
        """Q1：活动目标未填时不用数据推断的活动类型冒充目标。"""
        text = handlers._campaign_goal_text({}, {"propagation_features": {"features": [{"feature": "话题活动"}]}})
        self.assertEqual(text, "未填写（基于数据推断，须在相关表述中注明）")

    def test_summary_missing_section_fails_closed(self):
        bad = SUMMARY.replace("### 3.2 待改进问题", "### 问题")
        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat", side_effect=[_body(), bad]):
            with self.assertRaisesRegex(ValueError, "3.2 待改进问题"):
                handlers.write_report({"id": "n16"}, self.st, {})



class ExperienceScanTest(unittest.TestCase):
    def _issues(self, body):
        import validator
        scan = validator._load_scan()
        assert scan
        return {i["id"] for i in scan._experience_issues(body)}

    def test_vague_action_is_p2(self):
        self.assertIn("P2-vague-action-持续优化", self._issues("· 持续优化品牌形象\n"))

    def test_missing_source_is_p1(self):
        ids = self._issues("**经验 1：达人**\n数据支撑：表现好。\n")
        self.assertTrue(any(i.startswith("P1-experience-source") for i in ids))
        self.assertEqual(self._issues("**经验 1：达人**\n数据支撑：2.1.4 显示 75。\n"), set())



class ReportVersionTest(unittest.TestCase):
    """FIX-45：整篇写作 minor+1/patch 归零；修复只加 patch。"""

    def test_write_then_fix_then_rewrite(self):
        st = make_state(tempfile.mkdtemp())
        st.counters["report_minor"] = 1
        st.counters["report_patch"] = 0
        self.assertEqual(handlers._report_version(st), "1.0")
        st.counters["report_patch"] += 1
        self.assertEqual(handlers._report_version(st), "1.1")
        st.counters["report_minor"] += 1
        st.counters["report_patch"] = 0
        self.assertEqual(handlers._report_version(st), "2.0")

    def test_write_report_sets_minor(self):
        case = TwoPassTest("test_second_pass_gets_body_and_goal")
        case.setUp()
        out, _ = case._run()
        self.assertEqual(out["version"], "1.0")
        self.assertEqual(case.st.counters["report_patch"], 0)


if __name__ == "__main__":
    unittest.main()
