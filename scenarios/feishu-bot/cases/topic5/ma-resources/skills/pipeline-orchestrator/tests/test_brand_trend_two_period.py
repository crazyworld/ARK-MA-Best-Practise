"""FIX-48：1.1 品牌趋势连续展示基准期 + 活动期（KS_Chart_Spec 1.1）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charts  # noqa: E402

RENDER = Path(__file__).resolve().parents[2] / "blueai-yuqing-echarts-skill-new/scripts/render_from_spec.js"


def _bundle(start, end, volume, total=None):
    out = {"meta": {"start_date": start, "end_date": end},
           "volume_trend": {"xiaohongshu": volume},
           "interaction_trend": {"xiaohongshu": {d: v * 10 for d, v in volume.items()}}}
    if total is not None:
        out["total_volume"] = total
    return out


class TwoPeriodTest(unittest.TestCase):
    def test_thirty_plus_seven_days_continuous(self):
        from datetime import date, timedelta
        base_days = [(date(2026, 9, 1) + timedelta(i)).strftime("%Y%m%d") for i in range(30)]
        act_days = [(date(2026, 10, 1) + timedelta(i)).strftime("%Y%m%d") for i in range(7)]
        two = charts.two_period_trend(
            _bundle("2026-10-01", "2026-10-07", {d: 2 for d in act_days}, total=14),
            _bundle("2026-09-01", "2026-09-30", {d: 1 for d in base_days}))
        self.assertEqual(len(two["dates"]), 37)
        self.assertEqual([p["days"] for p in two["periods"]], [30, 7])
        self.assertTrue(two["checks"]["totals_match"])
        self.assertFalse(two["checks"]["overlap"])

    def test_missing_day_is_null_not_zero(self):
        two = charts.two_period_trend(
            _bundle("2026-10-01", "2026-10-03", {"20261001": 3, "20261003": 4}), None)
        self.assertEqual(two["volume"], [3, None, 4])
        self.assertEqual(two["checks"]["missing_days"], ["20261002"])
        self.assertTrue(two["base_unavailable"])

    def test_totals_mismatch_recorded_not_raised(self):
        two = charts.two_period_trend(_bundle("2026-10-01", "2026-10-02", {"20261001": 1, "20261002": 1},
                                              total=5), None)
        self.assertFalse(two["checks"]["totals_match"])

    def test_spec_has_areas_and_peak(self):
        two = charts.two_period_trend(
            _bundle("2026-10-01", "2026-10-02", {"20261001": 1, "20261002": 2}),
            _bundle("2026-09-29", "2026-09-30", {"20260929": 1, "20260930": 1}))
        specs = {s["chart_id"]: s for s in charts.build_chart_specs(
            {"brand": {"trend_two_period": two}}, {})}
        spec = specs["brand_trend"]
        self.assertEqual(len(spec["mark_areas"]), 2)
        self.assertEqual(spec["mark_points"], ["max"])
        self.assertFalse(spec["connect_nulls"])

    @unittest.skipUnless(shutil.which("node") and RENDER.exists(), "需要 node")
    def test_renderer_emits_mark_area_and_point(self):
        two = charts.two_period_trend(
            _bundle("2026-10-01", "2026-10-03", {"20261001": 1, "20261003": 2}),
            _bundle("2026-09-30", "2026-09-30", {"20260930": 1}))
        work = Path(tempfile.mkdtemp())
        (work / "spec.json").write_text(json.dumps(charts._brand_trend_spec(two), ensure_ascii=False))
        subprocess.run(["node", str(RENDER), "--spec", str(work / "spec.json"),
                        "--js-out", str(work / "option.js")], check=True, capture_output=True)
        probe = ("const fs=require('fs');const echarts={graphic:{LinearGradient:function(){}}};"
                 f"eval(fs.readFileSync({json.dumps(str(work / 'option.js'))},'utf8')+';globalThis.o=option;');"
                 "const s=globalThis.o.series[0];"
                 "console.log(JSON.stringify([s.markArea.data.length,s.markPoint.data[0].type,s.connectNulls,s.data]));")
        out = json.loads(subprocess.run(["node", "-e", probe], check=True, capture_output=True,
                                        text=True).stdout)
        self.assertEqual(out, [2, "max", False, [1, 1, None, 2]])
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
