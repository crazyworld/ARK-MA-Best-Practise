"""FIX-15：活动拉升按日均口径。

原版依据：`blueai-stats-aggregation-skill/scripts/lift_compare.py` 的 compute()
与 `KS_Exception_Handling.md:56-95`。
客户真实案例：总量算 -74%，按日均算是 -22.8% —— 总量口径会把结论方向放大或颠倒。

运行：python3 -m unittest tests.test_lift
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
sys.path.insert(0, ORCH)

import lift  # noqa: E402


def _bundle(start, end, volume, interaction=0, pos=0, neg=0):
    return {"meta": {"start_date": start, "end_date": end},
            "total_volume": volume, "total_interaction": interaction,
            "sentiment_dist": {"positive": pos, "negative": neg}}


class DailyFirstTest(unittest.TestCase):
    def test_equal_daily_volume_reports_zero_not_total_drop(self):
        """原版样例：7 天 700 条 / 30 天 3000 条 → 日均均 100，变化 0%。"""
        out = lift.from_bundles(
            _bundle("2026-09-28", "2026-10-04", 700, 7000),
            _bundle("2026-08-29", "2026-09-27", 3000, 30000))
        self.assertEqual(out["days"], {"period": 7, "base": 30})
        self.assertEqual(out["daily"]["period"]["volume"], 100.0)
        self.assertEqual(out["daily"]["base"]["volume"], 100.0)
        self.assertEqual(out["daily_volume_change_pct"], 0.0)
        # 总量差 -2300 仍在，但只能作辅助列示
        self.assertEqual(out["volume_change"], -2300)

    def test_days_include_both_ends(self):
        self.assertEqual(lift.days_inclusive("2026-09-28", "2026-09-28"), 1)
        self.assertEqual(lift.days_inclusive("2026-09-28", "2026-10-07"), 10)

    def test_accepts_compact_and_epoch_dates(self):
        self.assertEqual(lift.days_inclusive("20260928", "20261007"), 10)
        self.assertEqual(lift.days_inclusive("2026/09/28", "2026-10-07"), 10)

    def test_unequal_days_noted(self):
        out = lift.from_bundles(_bundle("2026-09-28", "2026-10-04", 700),
                                _bundle("2026-09-08", "2026-10-07", 3000))
        self.assertTrue(any("窗口不等长" in n for n in out["notes"]))

    def test_days_ratio_flag(self):
        out = lift.from_bundles(_bundle("2026-10-01", "2026-10-07", 700),
                                _bundle("2026-09-01", "2026-10-01", 5000))
        self.assertTrue(out["days_ratio_flag"])
        self.assertTrue(any("仅供参考" in n for n in out["notes"]))

    def test_invalid_period(self):
        out = lift.compare({"start": "2026-10-07", "end": "2026-10-01"},
                           {"start": "2026-09-01", "end": "2026-09-30"})
        self.assertEqual(out["status"], "invalid_period")


class ThresholdTest(unittest.TestCase):
    def test_base_volume_at_or_below_100_suppresses_rate(self):
        for volume in (100, 90, 0):
            with self.subTest(volume=volume):
                out = lift.from_bundles(
                    _bundle("2026-09-28", "2026-10-07", 500),
                    _bundle("2026-08-29", "2026-09-27", volume))
                self.assertTrue(out["base_insufficient"])
                self.assertIsNone(out["daily_volume_change_pct"])
                self.assertTrue(any("延长基准期" in n for n in out["notes"]))

    def test_nsr_change_in_percentage_points(self):
        out = lift.from_bundles(
            _bundle("2026-09-28", "2026-10-07", 500, pos=60, neg=40),
            _bundle("2026-08-29", "2026-09-27", 500, pos=50, neg=50))
        self.assertEqual(out["nsr"]["period"], 20.0)
        self.assertEqual(out["nsr"]["base"], 0.0)
        self.assertEqual(out["nsr_change_pp"], 20.0)

    def test_nsr_sample_below_30_insufficient(self):
        out = lift.from_bundles(
            _bundle("2026-09-28", "2026-10-07", 500, pos=15, neg=5),
            _bundle("2026-08-29", "2026-09-27", 500, pos=50, neg=50))
        self.assertTrue(out["nsr_insufficient"])
        self.assertIsNone(out["nsr_change_pp"])
        self.assertIn("活动期", out["notes"][-1])

    def test_nsr_denominator_excludes_neutral(self):
        out = lift.from_bundles(
            _bundle("2026-09-28", "2026-10-07", 1000, pos=30, neg=10),
            _bundle("2026-08-29", "2026-09-27", 1000, pos=30, neg=10))
        self.assertEqual(out["nsr"]["sample"]["period"], 40)  # 30+10，不含中性


class HolidayTest(unittest.TestCase):
    def test_base_in_national_day_flags(self):
        out = lift.from_bundles(
            _bundle("2026-10-08", "2026-10-14", 500),
            _bundle("2026-09-08", "2026-10-07", 2000))
        self.assertEqual(out["holiday_flags"], ["国庆"])
        self.assertEqual(out["holiday_check"], "ok")
        self.assertIn("受国庆影响", out["holiday_text"])
        self.assertIn("日均声量作为参考", out["holiday_text"])

    def test_no_flag_when_gap_within_20_percent(self):
        out = lift.from_bundles(
            _bundle("2026-10-08", "2026-10-14", 1000),
            _bundle("2026-09-08", "2026-10-07", 1100))
        self.assertEqual(out["holiday_flags"], ["国庆"])
        self.assertIsNone(out["holiday_text"])

    def test_base_outside_holiday_has_no_flag(self):
        out = lift.from_bundles(
            _bundle("2026-08-01", "2026-08-07", 500),
            _bundle("2026-07-01", "2026-07-30", 2000))
        self.assertEqual(out["holiday_flags"], [])
        self.assertIsNone(out["holiday_text"])

    def test_unknown_year_disclosed(self):
        out = lift.from_bundles(
            _bundle("2030-03-01", "2030-03-07", 500),
            _bundle("2030-01-28", "2030-02-26", 2000))
        self.assertEqual(out["holiday_check"], "unknown")
        self.assertTrue(any("超出已登记节假日年份" in n for n in out["notes"]))


class ReportWordingTest(unittest.TestCase):
    """报告不得出现「较基准期下降 X%」这类总量结论。"""

    def test_holiday_text_never_says_decline(self):
        out = lift.from_bundles(
            _bundle("2026-10-08", "2026-10-14", 500),
            _bundle("2026-09-08", "2026-10-07", 2000))
        for banned in ("下降", "增长", "提升", "拉低"):
            self.assertNotIn(banned, out["holiday_text"] or "")


if __name__ == "__main__":
    unittest.main()
