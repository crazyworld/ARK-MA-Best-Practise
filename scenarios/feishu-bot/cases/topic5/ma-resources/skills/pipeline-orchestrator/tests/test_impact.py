"""FIX-60：补证/数据修订影响矩阵（KS_Report_Scope_Contract §5）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hc_bridge  # noqa: E402
import impact  # noqa: E402
from _util import make_state  # noqa: E402


class MatrixTest(unittest.TestCase):
    def test_relevance_excludes_brand_pulls(self):
        plan = impact.plan_revision([{"row_id": "r1", "field": "relevance", "old": "是", "new": "否"}])
        self.assertNotIn("brand_stats", plan["invalidate"])
        self.assertNotIn("wordcloud_data", plan["invalidate"])
        self.assertIn("denoised_data", plan["invalidate"])
        self.assertEqual(plan["rerun_rows"]["round1"], ["r1"])
        self.assertEqual(impact.entry_node(plan["invalidate"]), "n07b_noise_review")

    def test_opinion_only_touches_round2(self):
        plan = impact.plan_revision([{"row_id": "r9", "field": "opinion", "new": "包装好看"}])
        self.assertNotIn("annotated_round1", plan["invalidate"])
        self.assertEqual(impact.entry_node(plan["invalidate"]), "n11_annotate_r2")

    def test_display_only_keeps_annotations(self):
        plan = impact.plan_revision([{"row_id": "r1", "field": "display"}])
        self.assertEqual(impact.entry_node(plan["invalidate"]), "n12_aggregate")
        self.assertNotIn("annotated_data", plan["invalidate"])

    def test_unknown_type_full_invalidate(self):
        plan = impact.plan_revision([{"row_id": "r1", "field": "mystery"}])
        self.assertTrue(plan["full"])
        self.assertEqual(plan["invalidate"], impact.FULL_INVALIDATE)

    def test_date_change_requires_authorization(self):
        plan = impact.plan_revision([{"field": "date_range", "new": "2026-10-01~2026-10-31"}])
        self.assertTrue(plan["requires_authorization"])
        self.assertIn("activity_detail", plan["invalidate"])


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("report_md", {"markdown": "x", "version": "1.0"})
        self.st.write_artifact("delivery_draft", {"revision_id": 3, "readback": {}})
        self.st.write_artifact("qc_report", {"has_p0": False, "issues": []})

    def test_g4_content_with_changes_routes_by_matrix(self):
        self.st.human = {"node": "n21_hc_user", "expect": ["user_confirmation"]}
        note = json.dumps({"change_type": "content", "note": "这条不是活动内容",
                           "changes": [{"row_id": "r1", "field": "relevance", "old": "是", "new": "否"}]})
        out = hc_bridge.build_human_outputs(self.st, "reject", note)
        self.assertEqual(out["_control"]["next"], "n07b_noise_review")
        self.assertEqual(out["annotation_rework_plan"]["rerun_ids"], ["r1"])
        self.assertNotIn("brand_stats", out["_control"]["invalidate_artifacts"])

    def test_hc7_redo_date_change_back_to_plan(self):
        self.st.human = {"node": "n_hc7_escalate", "expect": ["escalation_decision"]}
        note = json.dumps({"changes": [{"field": "date_range", "new": "x"}]})
        out = hc_bridge.build_human_outputs(self.st, "redo_with_data", note)
        self.assertEqual(out["_control"]["next"], "n02_plan")
        self.assertTrue(out["revision_plan"]["requires_authorization"])


if __name__ == "__main__":
    unittest.main()
