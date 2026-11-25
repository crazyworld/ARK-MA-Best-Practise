"""FIX-36：G4 返工区分版式/内容，返工轮次独立计数。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hc_bridge  # noqa: E402
from _util import make_state  # noqa: E402


class G4ReworkTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("report_md", {"markdown": "x", "version": "1.0"})
        self.st.write_artifact("delivery_draft", {"revision_id": 3, "readback": {"readback_text": "飞书正文"}})
        self.st.write_artifact("qc_report", {"has_p0": False, "issues": []})
        self.st.counters["qc_rounds"] = 1
        self.st.human = {"node": "n21_hc_user", "expect": ["user_confirmation"]}

    def _reject(self, change):
        return hc_bridge.build_human_outputs(
            self.st, "reject", json.dumps({"change_type": change, "note": "改一下"}))

    def test_layout_rework_applies_note_without_consuming_qc_rounds(self):
        # 版式说明必须经 n19 落到报告上（原样重发不会有任何变化）；不计 G3 返修轮次。
        out = self._reject("layout")
        self.assertEqual(out["_control"]["next"], "n19_fix")
        self.assertNotIn("report_md", out["_control"]["invalidate_artifacts"])
        self.assertEqual(out["qc_report"]["issues"][-1]["change_type"], "layout")
        self.assertEqual(self.st.counters["user_rework_rounds"], 1)
        self.assertEqual(self.st.counters["qc_rounds"], 1)

    def test_content_goes_fix_with_readback(self):
        out = self._reject("content")
        self.assertEqual(out["_control"]["next"], "n19_fix")
        self.assertEqual(out["user_rework"]["readback_text"], "飞书正文")
        self.assertTrue(out["qc_report"]["has_p0"])

    def test_plain_note_defaults_to_content(self):
        out = hc_bridge.build_human_outputs(self.st, "reject", "不确定")
        self.assertEqual(out["user_confirmation"]["change_type"], "content")

    def test_exhausted_escalates(self):
        self.st.counters["user_rework_rounds"] = hc_bridge.USER_REWORK_MAX
        out = self._reject("layout")
        self.assertEqual(out["_control"]["next"], "n_user_escalate")

    def test_layout_rework_renders_new_idempotency_key(self):
        node = {"idempotency": "feishu:{task_id}:u{user_rework_rounds}"}
        before = self.st.render_idempotency(node)
        self._reject("layout")
        self.assertNotEqual(before, self.st.render_idempotency(node))

    def test_escalate_requirement_change_back_to_intake(self):
        self.st.human = {"node": "n_user_escalate", "expect": ["escalation_decision"]}
        out = hc_bridge.build_human_outputs(self.st, "requirement_change", "")
        self.assertEqual(out["_control"]["next"], "n00_intake")
        self.st.counters["user_rework_rounds"] = 3
        out = hc_bridge.build_human_outputs(self.st, "quality_issue", "")
        self.assertEqual(out["_control"]["next"], "n21_hc_user")
        self.assertEqual(self.st.counters["user_rework_rounds"], hc_bridge.USER_REWORK_MAX - 1)


if __name__ == "__main__":
    unittest.main()
