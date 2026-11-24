"""FIX-46：Callout 显式标记与写作规则（KS §Callout / 检查清单 5.5、5.14、5.15）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feishu  # noqa: E402
import validator  # noqa: E402


def _scan(md):
    scan = validator._load_scan()
    assert scan
    return {i["id"] for i in scan._callout_issues(md)}


INSIGHT = ("> [!insight] 活动期间品牌声量高于活动前，但品牌名出现率不足一成。"
           "活动期日均声量约为活动前的 6 倍。因此声量提升不能直接读作品牌讨论增加。")
HEAD = "# 报告\n\n### 数据说明\n\n## 整体洞察\n\n" + INSIGHT + "\n\n## 一、品牌传播表现\n\n### 1.1 传播概况\n\n"


class FeishuCalloutTest(unittest.TestCase):
    def test_insight_marker_becomes_bar_chart_callout(self):
        blocks = feishu.layout_blocks(feishu.md_to_blocks(HEAD + "> [!tip] 讨论集中在小红书（占 82%）。\n"))
        callouts = [b for b in blocks if b["block_type"] == 19]
        self.assertEqual([c["callout"]["emoji_id"] for c in callouts], ["bar_chart", "bulb"])
        self.assertEqual(callouts[0]["callout"]["background_color"], feishu.CALLOUT_STYLE["insight"]["background_color"])

    def test_plain_first_paragraph_not_converted_when_explicit(self):
        md = HEAD + "普通正文首段。\n"
        blocks = feishu.layout_blocks(feishu.md_to_blocks(md))
        self.assertEqual(sum(1 for b in blocks if b["block_type"] == 19), 1)

    def test_legacy_reports_keep_first_paragraph_fallback(self):
        md = "# 报告\n\n## 整体洞察\n\n旧报告首段。\n"
        blocks = feishu.layout_blocks(feishu.md_to_blocks(md))
        self.assertEqual(sum(1 for b in blocks if b["block_type"] == 19), 1)


class CalloutScanTest(unittest.TestCase):
    def test_three_sentence_insight_passes(self):
        self.assertEqual(_scan(HEAD), set())

    def test_four_sentences_is_p1(self):
        md = HEAD.replace("增加。", "增加。还有第四句。")
        self.assertIn("P1-insight-callout-sentences", _scan(md))

    def test_tip_with_advice_is_p2(self):
        ids = _scan(HEAD + "> [!tip] 建议加大投放。\n")
        self.assertTrue(any(i.startswith("P2-callout-建议") for i in ids))

    def test_tip_starting_with_number_and_degree_word(self):
        ids = _scan(HEAD + "> [!tip] 82% 的讨论非常集中。\n")
        self.assertTrue(any("number-first" in i for i in ids))
        self.assertTrue(any("程度副词" in i for i in ids))

    def test_insight_after_first_chapter_is_p0(self):
        md = "# 报告\n\n## 一、品牌传播表现\n\n" + INSIGHT + "\n"
        self.assertIn("P0-insight-callout-position", _scan(md))

    def test_callout_in_summary_chapter(self):
        md = HEAD + "## 三、经验总结\n\n> [!tip] 一句话。\n"
        self.assertTrue(any(i.startswith("P2-summary-callout") for i in _scan(md)))


if __name__ == "__main__":
    unittest.main()
