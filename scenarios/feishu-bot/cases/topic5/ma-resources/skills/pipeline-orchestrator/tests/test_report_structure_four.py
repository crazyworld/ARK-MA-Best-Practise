"""FIX-42：固定四章结构（KS_Report_Template §一；检查清单 1.5 / 5.7 / 5.8 / 5.12）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import report_contract as RC  # noqa: E402
import validator  # noqa: E402


def _report(campaigns=("A", "B"), lift=True, retired=None, next_advice=False):
    md = "# 报告\n\n### 数据说明\nx\n\n## 整体洞察\ny\n\n## 一、品牌传播表现\n"
    for num, title in (("1.1", "传播概况"), ("1.2", "口碑分析"), ("1.3", "内容分析"), ("1.4", "热门内容")):
        md += f"### {num} {title}\nz\n"
    md += "## 二、活动传播表现\n"
    for i, cid in enumerate(campaigns, 1):
        md += f"### 2.{i} 活动 {cid}\n"
        for k, title in enumerate(("传播概况", "提及漏斗分析", "口碑分析", "内容分析", "热门内容"), 1):
            md += f"#### 2.{i}.{k} {title}\nz\n"
    if lift:
        md += f"### 2.{len(campaigns) + 1} 活动拉升效果\n以下为品牌整体口径。\n"
    md += ("## 三、经验总结\n### 3.1 成功经验\na\n### 3.2 待改进问题\nb\n### 3.3 后续行动计划\nc\n"
           "## 四、总结与建议\n核心成果表。\n")
    if next_advice:
        md += "下次活动建议：加大投放。\n"
    if retired:
        md += f"\n## {retired}\n"
    return md


def _ids(md, campaigns=("A", "B")):
    scan = validator._load_scan()
    assert scan
    return {i["id"] for i in scan._structure_issues(md, list(campaigns))}


class FourChapterTest(unittest.TestCase):
    def test_customer_sample_two_campaigns_passes(self):
        self.assertEqual(_ids(_report()), set())
        handlers._assert_report_structure(_report(), ["A", "B"])

    def test_missing_lift_section_is_p0(self):
        self.assertIn("P0-missing-lift-section", _ids(_report(lift=False)))
        with self.assertRaises(ValueError):
            handlers._assert_report_structure(_report(lift=False), ["A", "B"])

    def test_lift_number_follows_campaign_count(self):
        self.assertEqual(_ids(_report(campaigns=("A",)), campaigns=("A",)), set())

    def test_retired_chapters_flagged(self):
        for title in ("03 综合表现定位", "2.1.2 活动声量渗透分析"):
            with self.subTest(title=title):
                self.assertTrue(any(i.startswith("P0-retired-section") for i in _ids(_report(retired=title))))

    def test_five_chapter_sample_fails(self):
        five = _report().replace("## 一、品牌传播表现", "## 01 品牌传播表现")
        self.assertIn("P0-missing-chapter-一、品牌传播表现", _ids(five))

    def test_next_activity_advice_in_chapter_four_is_p2(self):
        self.assertIn("P2-summary-duplicates-action-plan", _ids(_report(next_advice=True)))

    def test_headings_recognise_chinese_chapter_and_numbered_section(self):
        titles = [h[3] for h in RC._headings(_report())]
        self.assertIn("一、品牌传播表现", titles)
        self.assertIn("2.1.2 提及漏斗分析", titles)
        self.assertIsNotNone(RC.section(_report(), "2.3"))

    def test_fallback_template_is_four_chapters(self):
        md = handlers._fallback_report_md({"campaigns": {"A": {}}}, {}, {}, "four", "0.0")
        self.assertEqual(_ids(md, campaigns=("A",)), set())

    def test_prompt_rule_rendered_from_policy(self):
        rule = handlers._report_structure_rule(2)
        self.assertIn("2.3 活动拉升效果", rule)
        self.assertIn("2.N.2 提及漏斗分析", rule)



class DataDescriptionTest(unittest.TestCase):
    """FIX-43：KS_Report_Template §零 用户定稿版。"""
    PLAN = {"brand": "瑞幸", "brand_query": "瑞幸", "brand_filter": "无",
            "activity_name": "瑞幸×暹罗厘普联名活动", "activity_query": "瑞幸+(暹罗厘普)",
            "platforms": ["xiaohongshu"], "period_start": "2026-09-28", "period_end": "2026-10-07"}

    def test_heading_and_fields(self):
        text = RC.data_description(self.PLAN, {})
        self.assertTrue(text.startswith("### 数据说明"))
        self.assertIn("时间周期：2026.9.28-2026.10.7", text)
        self.assertIn("| 瑞幸×暹罗厘普联名活动 | 瑞幸+(暹罗厘普) |  |", text)
        for banned in ("聚合", "抽样", "清洗", "口径", "无 |"):
            self.assertNotIn(banned, text)

    def test_sampling_notice_after_insight(self):
        md = RC.assemble("# 报告\n\n### 数据说明\n\n## 整体洞察\n洞察。\n\n## 一、品牌传播表现\n\n"
                         "### 1.1 传播概况\nx\n\n### 1.4 热门内容\n", plan=self.PLAN,
                         stats={"brand": {}, "clean_record": {"sampling": {
                             "applied": True, "population_size": 900, "sample_size": 500}}},
                         charts={}, insight={})
        self.assertLess(md.index("整体洞察"), md.index("[!warn]"))
        self.assertLess(md.index("[!warn]"), md.index("一、品牌传播表现"))

    def test_muted_marker_renders_grey_in_feishu(self):
        import feishu
        run = feishu._inline_elements("*{{muted}}注：说明。*")[0]["text_run"]
        self.assertEqual(run["content"], "注：说明。")
        self.assertEqual(run["text_element_style"]["text_color"], feishu.MUTED_TEXT_COLOR)


if __name__ == "__main__":
    unittest.main()
