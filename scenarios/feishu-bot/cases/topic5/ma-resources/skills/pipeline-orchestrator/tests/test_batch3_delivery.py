"""第 3 批：n20–n24 交付链（审计 P0-8/9/10/11 + P1-6/7/9 + P2-10）。

原版依据：
  - 业务流程文档 §一「版本一变，确认作废」；§5.4 交接后修正需更新交接记录
  - Producer v0.60 HC-T5-2:333-335 发布后原地修复，复检对象是飞书回读内容
  - Producer v0.60:338-340 G4 2 个工作日催办，再 1 个工作日上报 Supervisor（escalate）
  - KS_Feishu_Format §九 所有权转移失败须人工处理后重试
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _util import make_state  # noqa: E402
import engine  # noqa: E402
import feishu  # noqa: E402
import handlers  # noqa: E402
import hc_bridge  # noqa: E402


class EffectJournalPurgeTest(unittest.TestCase):
    """P0-8：产物失效时同步清除引用它的副作用回执，返工后不再重放旧结果。"""

    def test_invalidate_removes_effect_journal_of_invalidated_outputs(self):
        st = make_state(tempfile.mkdtemp())
        engine._save_effect_journal(st, "n23_delivery_commit", "commit:k",
                                    {"artifacts": {"delivery_record": {"v": 1}}})
        engine._save_effect_journal(st, "n13_wordcloud_pull", "wc:k",
                                    {"artifacts": {"wordcloud_data": {"v": 1}}})
        st.write_artifact("delivery_record", {"v": 1})
        st.write_artifact("wordcloud_data", {"v": 1})
        st.invalidate_artifacts(["delivery_record"])
        self.assertIsNone(engine._load_effect_journal(st, "n23_delivery_commit", "commit:k"))
        self.assertIsNotNone(engine._load_effect_journal(st, "n13_wordcloud_pull", "wc:k"))

    def test_commit_key_binds_report_and_rework_round(self):
        from _util import DAG_PATH
        dag = Path(DAG_PATH).read_text(encoding="utf-8")
        block = dag[dag.index("id: n23_delivery_commit"):]
        line = next(l for l in block.splitlines() if "idempotency:" in l)
        self.assertIn("{report_artifact_sha256}", line)
        self.assertIn("{user_rework_rounds}", line)


class RepublishAfterFixTest(unittest.TestCase):
    """P0-9：报告版本变化后不复用旧回执跳过写入，而是在已发布文档上原位修正。"""

    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("confirmed_plan", {"brand": "B"})
        self.st.write_artifact("stats_bundle", {})
        self.calls = []

        def fake_create(report_md, **kw):
            self.calls.append(kw)
            return {"doc_token": "doc1", "url": "u", "revision_id": len(self.calls),
                    "ownership_transferred": True, "folder_configured": True,
                    "folder_access": {"transferred": True},
                    "readback": {"ok": True, "chapters_complete": True,
                                 "images_referenced": True, "layout_complete": True,
                                 "table_layout": {"ok": True}}}
        self.fake_create = fake_create

    def _publish(self, md):
        self.st.write_artifact("report_md", {"markdown": md, "version": "1.0"})
        node = {"id": "n20_delivery_draft",
                "outputs": ["delivery_draft", "external_receipts", "feishu_doc", "qc_report"]}
        with patch.object(feishu, "api_available", return_value=True), \
             patch.object(feishu, "create_delivery", side_effect=self.fake_create):
            out = handlers.delivery_draft(node, self.st, {})
        for k, v in out["artifacts"].items():
            self.st.write_artifact(k, v)

    def test_changed_report_is_patched_not_reused(self):
        self._publish("# v1")
        self._publish("# v2")
        second = self.calls[1]
        self.assertEqual(second["reused_doc_token"], "")
        self.assertEqual(second["published"]["doc_token"], "doc1")

    def test_same_report_reuses_receipt(self):
        self._publish("# v1")
        self._publish("# v1")
        self.assertEqual(self.calls[1]["reused_doc_token"], "doc1")


class Hc7LoopTest(unittest.TestCase):
    """P0-10：HC7 回 n20 时交付产物失效（重新发布）；放宽后发布路由尊重豁免。"""

    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("report_md", {"markdown": "x", "version": "1.0"})
        self.st.human = {"node": "n_hc7_escalate", "expect": ["escalation_decision"]}

    def test_external_permission_retry_invalidates_delivery(self):
        issue = {"id": "P0-postpublish-owner", "severity": "P0", "locus": "feishu-permission"}
        self.st.write_artifact("delivery_draft", {"post_publish_validation": {"issues": [issue]}})
        self.st.write_artifact("qc_report", {"issues": [issue], "has_p0": True, "p0_count": 1})
        out = hc_bridge.build_human_outputs(self.st, "pass", "已在飞书后台修好权限")
        self.assertEqual(out["_control"]["next"], "n20_delivery_draft")
        self.assertIn("delivery_draft", out["_control"]["invalidate_artifacts"])
        self.assertIn("external_receipts", out["_control"]["invalidate_artifacts"])

    def test_delivery_decision_honors_waivers(self):
        issue = {"id": "P0-postpublish-layout", "severity": "P0", "locus": "feishu-docx"}
        self.st.write_artifact("delivery_draft", {
            "state": "needs_review",
            "post_publish_validation": {"issues": [issue], "has_p0": True, "p0_count": 1}})
        self.st.write_artifact("qc_report", {"waivers": [{"issue_id": "P0-postpublish-layout"}]})
        self.assertEqual(handlers.delivery_draft_decision({"id": "g"}, self.st, {})["route"], "ready")

    def test_unwaived_p0_still_retries(self):
        issue = {"id": "P0-postpublish-layout", "severity": "P0", "locus": "feishu-docx"}
        self.st.write_artifact("delivery_draft", {
            "state": "needs_review",
            "post_publish_validation": {"issues": [issue], "has_p0": True, "p0_count": 1}})
        self.st.write_artifact("qc_report", {})
        self.assertEqual(handlers.delivery_draft_decision({"id": "g"}, self.st, {})["route"],
                         "has_p0_retry")


class TableLayoutIssueTest(unittest.TestCase):
    """P1-6：表格布局回读失败要生成具体问题项，不能 issues 为空却判需返修。"""

    def test_table_layout_failure_creates_issue(self):
        src = open(handlers.__file__, encoding="utf-8").read()
        self.assertIn("P0-postpublish-table-layout", src)


class G4EscalateCliTest(unittest.TestCase):
    """P0-11：run_pipeline --decision 接受 escalate（G4 超时上报 Supervisor）。"""

    def test_cli_accepts_escalate(self):
        proc = subprocess.run(
            [sys.executable, str(HERE.parent / "run_pipeline.py"), "resume",
             "--decision", "escalate", "--help"],
            capture_output=True, text=True)
        self.assertNotIn("invalid choice", proc.stderr)


class G4LayoutReworkTest(unittest.TestCase):
    """P1-7：版式返工必须把用户说明交给返修，原样重发等于什么都没改。"""

    def test_layout_rework_carries_note_to_fix(self):
        st = make_state(tempfile.mkdtemp())
        st.write_artifact("report_md", {"markdown": "x", "version": "1.0"})
        st.write_artifact("delivery_draft", {"revision_id": 3, "readback": {}})
        st.write_artifact("qc_report", {"has_p0": False, "issues": []})
        st.human = {"node": "n21_hc_user", "expect": ["user_confirmation"]}
        out = hc_bridge.build_human_outputs(
            st, "reject", json.dumps({"change_type": "layout", "note": "表格太挤"}))
        self.assertEqual(out["_control"]["next"], "n19_fix")
        issue = out["qc_report"]["issues"][-1]
        self.assertIn("表格太挤", issue["evidence"])
        self.assertEqual(issue["change_type"], "layout")


class HandoffRevisionErrorTest(unittest.TestCase):
    """P1-9：复读 revision 失败要带原因抛出，让 n24 失败可重试，而不是吞掉。"""

    def test_current_revision_raises_with_reason(self):
        with patch.object(feishu, "api_available", return_value=True), \
             patch.object(feishu, "_tenant_token", return_value="t"), \
             patch.object(feishu, "_req", side_effect=RuntimeError("HTTP 403 forbidden")):
            with self.assertRaisesRegex(RuntimeError, "403"):
                feishu.current_revision("doc1")

    def test_no_creds_still_none(self):
        with patch.object(feishu, "api_available", return_value=False):
            self.assertIsNone(feishu.current_revision("doc1"))


if __name__ == "__main__":
    unittest.main()
