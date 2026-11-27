"""FIX-06：高点证据 + Validator 噪音回退 Step5.5。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import noise_review as NR  # noqa: E402
from _util import make_state  # noqa: E402

_QC = {"has_p0": True, "issues": [{"id": "P0-peak-noise", "severity": "P0",
                                    "noise_ids": ["x1", "x2"], "return_to": "Step5.5"}]}


class PeakEvidenceTest(unittest.TestCase):
    def test_contributors_cover_80_percent(self):
        rows = [{"id": f"a{i}", "_publish_ymd": "20261001", "interaction_cnt": v}
                for i, v in enumerate((70, 20, 5, 5))]
        rows += [{"id": "b", "_publish_ymd": "20261002", "interaction_cnt": 1}]
        peak = NR.peak_evidence(rows)["interaction_peaks"][0]
        self.assertEqual(peak["date"], "20261001")
        self.assertEqual([p["id"] for p in peak["contributors"]], ["a0", "a1"])
        self.assertEqual(peak["covered_share"], 0.9)


class RollbackTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("qc_report", _QC)

    def test_route_noise_found_then_maxed(self):
        # 噪音帖必须仍在保留集才回去噪；已剔除的按报告层 P0 处理（KS_Report_Scope_Contract:55）
        self.st.write_artifact("denoised_data", {"rows": [{"id": "x1"}, {"id": "x2"}]})
        self.assertEqual(handlers.qc_decision({}, self.st, {})["route"], "noise_found")
        self.st.counters["noise_rollbacks"] = handlers.NOISE_ROLLBACK_MAX
        self.assertEqual(handlers.qc_decision({}, self.st, {})["route"], "has_p0_maxed")

    def test_plain_p0_still_retries(self):
        self.st.write_artifact("qc_report", {"has_p0": True, "issues": [{"id": "p", "severity": "P0"}]})
        self.assertEqual(handlers.qc_decision({}, self.st, {})["route"], "has_p0_retry")

    def test_rollback_invalidates_and_bumps_epoch(self):
        self.st.write_artifact("denoised_data", {"rows": [{"id": "x1"}, {"id": "x2"}]})
        self.st.write_artifact("stats_bundle", {"x": 1})
        out = handlers.noise_rollback({}, self.st, {})["artifacts"]["noise_rollback_record"]
        self.assertEqual(out["noise_ids"], ["x1", "x2"])
        self.assertEqual(self.st.params["qc_noise_ids"], ["x1", "x2"])
        self.assertEqual(self.st.counters["rerun_epoch"], 1)
        self.assertIsNone(self.st.read_artifact("denoised_data"))
        self.assertIsNone(self.st.read_artifact("stats_bundle"))
        # 契约：只重做受影响部分——类别与已确认标签不因去噪被作废
        self.assertNotIn("confirmed_opinion_labels", out["invalidated"])
        key = self.st.render_idempotency({"idempotency": "annot:r1:{task_id}:e{rerun_epoch}"})
        self.assertTrue(key.endswith(":e1"))

    def test_rollback_without_ids_fails_closed(self):
        self.st.write_artifact("qc_report", {"has_p0": True, "issues": []})
        with self.assertRaises(RuntimeError):
            handlers.noise_rollback({}, self.st, {})


if __name__ == "__main__":
    unittest.main()
