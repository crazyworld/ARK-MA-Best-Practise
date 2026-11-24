"""FIX-50：观点双栏、UGC 不适用固定措辞、40-60 字代表原文（KS_Report_Template 2.N.3/2.N.4②）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feishu  # noqa: E402
import report_contract as RC  # noqa: E402


def _dir(n, label="价格优惠"):
    return {"sample_size": n, "top5": [{"label": label, "share": 0.5, "count": n // 2,
                                        "representatives": [{"content": "很划算", "url": "https://x/1"}]}]}


class OpinionLayoutTest(unittest.TestCase):
    def test_small_negative_not_extracted(self):
        text = RC.opinion_text({"opinion_analysis": {"positive": _dir(20), "negative": _dir(8)}})
        self.assertIn("负面内容仅 8 条，不做观点提取", text)
        self.assertNotIn("方向性参考", text)

    def test_grid_markers_wrap_two_columns(self):
        md = "## x\n\n" + RC.opinion_text({"opinion_analysis": {"positive": _dir(20), "negative": _dir(12)}})
        blocks = feishu.layout_blocks(feishu.md_to_blocks(md))
        grids = [b for b in blocks if b["block_type"] == 24]
        self.assertEqual(len(grids), 1)
        left, right = grids[0]["_columns"]
        self.assertIn("正面观点", feishu._block_text(left[0]))
        self.assertIn("负面观点", feishu._block_text(right[0]))
        self.assertFalse(any(b.get("_grid_marker") for b in blocks))

    def test_broken_grid_marker_fails(self):
        with self.assertRaises(ValueError):
            feishu.layout_blocks(feishu.md_to_blocks("<!-- grid:50:50 -->\n\nx\n"))


class UgcLayoutTest(unittest.TestCase):
    def test_pgc_scope_fixed_wording(self):
        self.assertIn("本次仅分析 PGC 内容，不适用 UGC 参与方式分析", RC.ugc_text({}, "pgc"))

    def test_zero_ugc(self):
        self.assertIn("本活动未采集到活动相关 UGC", RC.ugc_text({"ugc_analysis": {"sample_size": 0}}))

    def test_quote_length_40_to_60(self):
        long = "今天去门店打卡了联名活动。" * 8
        quote = RC.ugc_quote({"content": long})
        self.assertTrue(40 <= len(quote) <= 61, len(quote))
        self.assertEqual(RC.ugc_quote({"content": "短句"}), "短句")

    def test_small_category_flag_and_no_table(self):
        bundle = {"ugc_analysis": {"sample_size": 20, "categories": [
            {"label": "晒单分享", "count": 15, "share": 0.75, "representatives": []},
            {"label": "创意改编", "count": 5, "share": 0.25, "representatives": []}]}}
        text = RC.ugc_text(bundle)
        self.assertIn("创意改编**  25.0%（5 条），样本偏少，仅供参考", text)
        self.assertNotIn("| ---", text)


if __name__ == "__main__":
    unittest.main()
