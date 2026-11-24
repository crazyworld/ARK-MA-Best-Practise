"""缺陷清单 Q3：品牌基准期——用户确认日期优先，未指定默认参照开始日前30天，写入需求确认单。"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
sys.path.insert(0, ORCH)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(ORCH))))

import ssjh  # noqa: E402
import topic5_intake as intake  # noqa: E402


class BaselineTest(unittest.TestCase):
    def test_default_is_30_days_before_window_start(self):
        out = intake.resolve_baseline("", "2026-10-01", "2026-10-07")
        self.assertEqual(out, {"baseline_start": "2026-09-01", "baseline_end": "2026-09-30",
                               "baseline_source": "default"})

    def test_user_dates_take_priority(self):
        out = intake.resolve_baseline("2026-08-01 ~ 2026-08-31", "2026-10-01", "2026-10-07")
        self.assertEqual((out["baseline_start"], out["baseline_end"], out["baseline_source"]),
                         ("2026-08-01", "2026-08-31", "user"))

    def test_overlap_and_half_filled_rejected(self):
        with self.assertRaises(intake.IntakeValidationError):
            intake.resolve_baseline("2026-09-20 ~ 2026-10-02", "2026-10-01", "2026-10-07")
        with self.assertRaises(intake.IntakeValidationError):
            intake.resolve_baseline("2026-08-01", "2026-10-01", "2026-10-07")

    def test_pull_reads_confirmed_dates(self):
        plan = {"period_start": "2026-10-01", "period_end": "2026-10-07",
                "baseline_start": "2026-08-01", "baseline_end": "2026-08-31"}
        self.assertEqual(ssjh._plan_window(plan, baseline=True), ("2026-08-01", "2026-08-31"))


if __name__ == "__main__":
    unittest.main()
