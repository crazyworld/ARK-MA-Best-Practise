"""FIX-35：HC7 放宽并留痕 / 补数据重做 / 终止项目。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hc_bridge  # noqa: E402
import report_contract as RC  # noqa: E402
from _util import make_state  # noqa: E402

_QC = {"has_p0": True, "p0_count": 2, "issues": [
    {"id": "P0-a", "severity": "P0", "title": "缺品牌高点原帖"},
    {"id": "P0-b", "severity": "P0", "title": "竞品对比缺失"},
    {"id": "P0-postpublish-owner", "severity": "P0", "title": "所有权"}]}


class Hc7Test(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("qc_report", json.loads(json.dumps(_QC)))
        self.st.human = {"node": "n_hc7_escalate", "expect": ["escalation_decision"]}

    def _relax(self, ids):
        return hc_bridge.build_human_outputs(
            self.st, "relax", json.dumps({"issue_ids": ids, "reason": "数据源缺失，客户接受"}))

    def test_partial_relax_returns_to_fix(self):
        self.st.write_artifact("qc_report", {**_QC, "issues": _QC["issues"][:2]})
        out = self._relax(["P0-a"])
        self.assertEqual(out["_control"]["next"], "n19_fix")
        self.assertTrue(out["qc_report"]["has_p0"])

    def test_full_relax_goes_to_delivery_with_waivers(self):
        self.st.write_artifact("qc_report", {**_QC, "issues": _QC["issues"][:2]})
        out = self._relax(["P0-a", "P0-b"])
        self.assertEqual(out["_control"]["next"], "n20_delivery_draft")
        self.assertFalse(out["qc_report"]["has_p0"])
        self.assertEqual(len(out["qc_report"]["waivers"]), 2)

    def test_external_permission_cannot_relax(self):
        with self.assertRaisesRegex(ValueError, "外部权限"):
            self._relax(["P0-postpublish-owner"])

    def test_relax_requires_reason(self):
        with self.assertRaises(ValueError):
            hc_bridge.build_human_outputs(self.st, "relax", json.dumps({"issue_ids": ["P0-a"]}))

    def test_redo_resets_rounds(self):
        self.st.counters["qc_rounds"] = 3
        out = hc_bridge.build_human_outputs(self.st, "redo_with_data", "baseline_days=60")
        self.assertEqual(out["_control"]["next"], "n02_plan")
        self.assertEqual(self.st.counters["qc_rounds"], 0)
        self.assertEqual(self.st.counters["rerun_epoch"], 1)
        self.assertIn("denoised_data", out["_control"]["invalidate_artifacts"])

    def test_terminate(self):
        out = hc_bridge.build_human_outputs(self.st, "terminate", "客户取消")
        self.assertTrue(out["escalation_decision"]["terminated"])
        self.assertIsNone(out["_control"]["next"])
        self.assertTrue(out["_control"]["terminate"])

    def test_pass_keeps_retry_semantics(self):
        self.st.write_artifact("qc_report", {**_QC, "issues": _QC["issues"][:2]})
        self.assertEqual(hc_bridge.build_human_outputs(self.st, "pass", "")["_control"]["next"], "n19_fix")


class WaiverNoteTest(unittest.TestCase):
    def test_after_description_idempotent(self):
        md = "# 标题\n\n### 数据说明\n\n| a | b |\n\n## 一、品牌传播表现\n"
        out = RC.waiver_note(md, [{"issue_id": "P0-a", "title": "缺原帖", "reason": "r"}])
        self.assertLess(out.index("经用户确认放宽"), out.index("## 一、"))
        self.assertGreater(out.index("经用户确认放宽"), out.index("| a | b |"))
        self.assertEqual(out, RC.waiver_note(out, [{"issue_id": "P0-a", "reason": "r"}]))
        self.assertEqual(RC.waiver_note(md, []), md)


if __name__ == "__main__":
    unittest.main()
