"""FIX-39 返修数字核查 / 图表视觉自检 / 封面 fail-closed；FIX-40 出口关键数字核对。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charts  # noqa: E402
import feishu  # noqa: E402
import hc_bridge  # noqa: E402
import report_contract as RC  # noqa: E402

_STATS = {"campaigns": {"A": {"metrics": {"活动相关条数": 1234, "NSR": 0.2},
                              "sentiment_dist": {"positive": 6, "neutral": 2, "negative": 2}}},
          "baseline_lift": {"status": "available", "daily_volume_change_pct": 25.0}}


class KeyNumbersTest(unittest.TestCase):
    def test_numbers_registered_and_checked(self):
        nums = {n["label"]: n["value_text"] for n in RC.key_numbers(_STATS)}
        self.assertEqual(nums["A·NSR"], "+20.0%")
        self.assertEqual(nums["A·正面占比"], "60.0%")
        self.assertEqual(nums["品牌日均声量变化"], "+25.0%")
        text = "活动相关 1234 条 正面 60.0% 中性 20.0% 负面 20.0% NSR +20.0%"
        result = feishu.numbers_check(text, RC.key_numbers(_STATS))
        self.assertFalse(result["ok"])
        self.assertEqual([m["value_text"] for m in result["missing"]], ["+25.0%"])
        self.assertTrue(feishu.numbers_check(text + " +25.0%", RC.key_numbers(_STATS))["ok"])

    def test_g4_checklist_single_platform(self):
        self.assertIn("传播概况单平台不分栏", hc_bridge.delivery_checklist({"platforms": ["xiaohongshu"]}))
        self.assertIn("传播概况：平台分布与内容类型两图并排",
                      hc_bridge.delivery_checklist({"platforms": ["xiaohongshu", "douyin"]}))


class NumberDiffTest(unittest.TestCase):
    OLD = "## 2.1.3 口碑分析\n\nNSR +20.0%，相关 1234 条。\n"

    def test_fabricated_number_flagged(self):
        diff = RC.number_diff(self.OLD, self.OLD.replace("+20.0%", "+27.0%"), _STATS, {})
        self.assertEqual(diff["unknown"], ["2.1.3:+27.0%"])

    def test_stats_value_recorded_with_chart_flag(self):
        new = self.OLD.replace("1234 条", "1234 条，正面占 60.0%")
        charts_payload = {"charts": [{"chart_id": "A_sentiment", "title": "2.1.3 情感",
                                      "spec": {"data": [60.0]}}]}
        diff = RC.number_diff(self.OLD, new, _STATS, charts_payload)
        self.assertEqual(diff["unknown"], [])
        self.assertTrue(diff["records"][0]["chart_contains_value"])
        diff = RC.number_diff(self.OLD, new, _STATS, {})
        self.assertEqual(diff["records"][0]["note"], "已核实配图不含该数字")

    def test_small_numbers_not_checked(self):
        self.assertEqual(RC.number_diff(self.OLD, self.OLD + "\nTop 10，共 3 天，2026 年\n", _STATS, {})["records"], [])


class PngSelfCheckTest(unittest.TestCase):
    def _png(self, color, mode="RGB"):
        from PIL import Image
        path = os.path.join(tempfile.mkdtemp(), "x.png")
        Image.new(mode, (100, 50), color).save(path)
        return path

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "需要 Pillow（MA 环境已声明 Pillow>=10）")
    def test_black_and_blank_rejected(self):
        self.assertFalse(charts.png_self_check(self._png((0, 0, 0)))["ok"])
        self.assertFalse(charts.png_self_check(self._png((0, 0, 0, 0), "RGBA"))["ok"])
        self.assertTrue(charts.png_self_check(self._png((250, 250, 250)))["ok"])

    def test_cover_failure_raises(self):
        with patch.object(charts, "renderer_available", return_value=True), \
                patch.object(charts, "build_chart_specs", return_value=[]), \
                patch.object(charts, "_gen_cover", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "封面"):
                charts.render_charts({}, {}, project="p", version="v1.0", report_name="r",
                                     outdir=tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
