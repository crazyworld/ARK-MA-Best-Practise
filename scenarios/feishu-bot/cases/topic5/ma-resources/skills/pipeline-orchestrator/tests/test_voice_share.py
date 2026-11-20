"""FIX-19：声量贡献参考（活动相关声量 / 品牌同期总声量）。

原版依据：
  - KS_Metrics_Calculation v0.15：分母（品牌同期总声量）≥ 分子（活动相关声量），
    不满足时不输出该指标、不得输出 > 100% 的数值（LM #46）
  - KS_Report_Template:491：分母一律取自合并品牌数据第一章同一份 brand_stats，
    按该活动自己的活动期对逐日声量汇总，不得为单个活动另行取数
  - 检查清单 4.2：把该指标称为“渗透率”并作为漏斗主指标 → P0

运行：python3 -m unittest tests.test_voice_share
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers         # noqa: E402
import report_contract  # noqa: E402


BRAND = {"volume_trend": {
    "xiaohongshu": {"20260928": 400, "20260930": 300},
    "douyin": {"2026-10-01": 300, "20261008": 999},   # 10-08 在活动窗口外
}}


class VoiceShareCalculationTest(unittest.TestCase):
    def test_divides_window_sum(self):
        got = handlers._voice_share(120, BRAND, "2026-09-28", "2026-10-07", [])
        self.assertEqual(got["value"], 0.12)
        self.assertEqual(got["numerator"], 120)
        self.assertEqual(got["denominator"], 1000)   # 400+300+300，10-08 的 999 不算
        self.assertIsNone(got["reason"])

    def test_denominator_smaller_than_numerator_is_not_output(self):
        got = handlers._voice_share(1200, BRAND, "2026-09-28", "2026-10-07", [])
        self.assertIsNone(got["value"])
        self.assertEqual(got["denominator"], 1000)
        self.assertIn("分母小于分子", got["reason"])

    def test_zero_activity_volume_is_not_output(self):
        got = handlers._voice_share(0, BRAND, "2026-09-28", "2026-10-07", [])
        self.assertIsNone(got["value"])
        self.assertIn("活动相关声量为 0", got["reason"])

    def test_missing_brand_trend_is_not_output(self):
        got = handlers._voice_share(120, {}, "2026-09-28", "2026-10-07", [])
        self.assertIsNone(got["value"])
        self.assertIsNone(got["denominator"])
        self.assertIn("缺失", got["reason"])

    def test_coverage_warnings_block_output(self):
        """FIX-08 覆盖告警时品牌口径不覆盖活动口径，不得输出。"""
        got = handlers._voice_share(120, BRAND, "2026-09-28", "2026-10-07",
                                    ["活动检索式含品牌未覆盖的分支"])
        self.assertIsNone(got["value"])
        self.assertIn("未覆盖", got["reason"])

    def test_never_exceeds_one(self):
        for n in (1, 100, 500, 1000, 5000):
            got = handlers._voice_share(n, BRAND, "2026-09-28", "2026-10-07", [])
            if got["value"] is not None:
                self.assertLessEqual(got["value"], 1.0, n)


class VoiceShareRenderTest(unittest.TestCase):
    def test_render_includes_numbers(self):
        bundle = {"voice_share": {"value": 0.12, "numerator": 120, "denominator": 1000}}
        text = report_contract._voice_share_line(bundle)
        self.assertIn("声量贡献参考", text)
        self.assertIn("+12.0%", text)
        self.assertIn("活动相关 120", text)
        self.assertIn("品牌同期 1000", text)

    def test_render_explains_when_not_output(self):
        bundle = {"voice_share": {"value": None, "numerator": 1200,
                                  "denominator": 1000, "reason": "分母小于分子"}}
        text = report_contract._voice_share_line(bundle)
        self.assertIn("暂不输出", text)
        self.assertIn("分母小于分子", text)
        self.assertNotIn("%", text)

    def test_render_without_voice_share(self):
        self.assertIn("暂不输出", report_contract._voice_share_line({}))


class MetricWhitelistTest(unittest.TestCase):
    def test_whitelist_contains_all_customer_metrics(self):
        """KS_Metrics_Calculation v0.12 表格的 7 个指标名。"""
        for name in ("条均互动量", "条均互动倍数", "品牌提及率", "品牌主导讨论占比",
                     "产品提及占比", "声量贡献参考", "NSR"):
            self.assertIn(name, handlers.METRIC_WHITELIST)


if __name__ == "__main__":
    unittest.main()
