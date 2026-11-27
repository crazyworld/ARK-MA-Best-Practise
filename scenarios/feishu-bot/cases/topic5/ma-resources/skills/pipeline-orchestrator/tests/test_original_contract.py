"""原版对齐的契约测试：原版资产完整性 + 关键控制流（对齐原版 4 项要求）。

覆盖本次路由/质量阻断/类别审核三处修复，防止再退回「迁移版自创流程」。
"""
import tempfile
import unittest
from pathlib import Path

import handlers
import original_verify
from _util import load_dag, make_state


class OriginalAssetsTest(unittest.TestCase):
    def test_manifest_hashes_match(self):
        """原版脚本零改写：sha256 与 MANIFEST 一致。"""
        self.assertEqual(original_verify.verify(), [])

    def test_adapter_preserves_original_column_semantics(self):
        """内部行 → 原版列名后，原版 check() 能判出本轮真实失败原因 duplicate_labels。"""
        import original_adapter
        rows = [{"id": "p1", "ai_sentiment": 1, "relevance": True,
                 "opinion": "价格,价格", "content_mode": "UGC"}]
        report = original_adapter.check_structure(
            rows, config={"positive_labels": ["价格"], "negative_labels": ["口味"],
                          "ugc_labels": ["到店打卡", "开箱测评"]},
            stage="round2")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["errors_by_reason"].get("duplicate_labels"), 1)
        self.assertEqual(report["source_script"],
                         "original/tools/check_annotation_structure.py")

    def test_plan_delegates_to_original_script(self):
        import original_adapter
        rows = [{"id": f"p{i}", "ai_sentiment": 1, "relevance": True,
                 "opinion": "价格" if i % 2 else "口味", "content_mode": "UGC"}
                for i in range(20)]
        plan = original_adapter.plan_review(
            rows, stage="round2", reviewed_ids=[], reserve=100, seed=42,
            config={"positive_labels": ["价格"], "negative_labels": ["口味"]})
        self.assertEqual(plan["source_script"],
                         "original/tools/plan_annotation_review.py")
        self.assertEqual(plan["cap"], 500)
        self.assertLessEqual(plan["sample_size"], 100)

    def test_review_plan_reviewed_ids_are_union(self):
        """台账是并集：已审过的 ID 不再进入新样本（执行契约 §2）。"""
        import review_plan
        rows = [{"id": f"p{i}", "ai_sentiment": 1, "relevance": True,
                 "opinion": "价格", "content_mode": "UGC"} for i in range(40)]
        plan = review_plan.plan_review(
            rows, stage="round2", reviewed_ids=["p1", "p2"],
            config={"positive_labels": ["价格"], "negative_labels": ["口味"]})
        picked = {s["id"] for s in plan["samples"]}
        self.assertFalse(picked & {"p1", "p2"})


class LabelGateRoutingTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.ws, ignore_errors=True))

    def _state(self):
        return make_state(self.ws)

    def test_pass_when_no_feedback(self):
        st = self._state()
        st.write_artifact("confirmed_opinion_labels", {"label_review": {"passed": True}})
        self.assertEqual(handlers.label_policy_decision({}, st, {})["route"], "pass")

    def test_rework_then_human_after_two_attempts(self):
        st = self._state()
        st.write_artifact("confirmed_opinion_labels", {
            "label_review": {"passed": False, "reasons": ["类别带倾向"], "attempts": 0}})
        self.assertEqual(handlers.label_policy_decision({}, st, {})["route"], "rework")
        st.write_artifact("confirmed_opinion_labels", {
            "label_review": {"passed": False, "reasons": ["类别带倾向"], "attempts": 2}})
        self.assertEqual(handlers.label_policy_decision({}, st, {})["route"], "maxed")

    def test_rework_invalidates_label_downstream(self):
        st = self._state()
        st.write_artifact("confirmed_opinion_labels", {
            "label_review": {"passed": False, "reasons": ["来源审核未通过"]}})
        out = handlers.label_rework({}, st, {})
        self.assertIn("confirmed_opinion_labels", out["artifacts"]["label_review_feedback"]["invalidated"])
        self.assertEqual(st.counters["label_review_attempts"], 1)


class AnnotationBlockTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.ws, ignore_errors=True))

    def test_block_record_and_route(self):
        st = make_state(self.ws)
        st.write_artifact("r2_review", {
            "accuracy": {"opinion": 0.72}, "blocked_reason": "quota_exhausted",
            "verdicts": [{"id": "p1", "verdict": "wrong", "field": "opinion"}],
            "wrong_ids": {"opinion": ["p1"]}})
        out = handlers.annotation_block({"id": "n11r_blocked"}, st, {})
        record = out["artifacts"]["annotation_block_record"]
        self.assertTrue(record["blocked_quality"])
        self.assertEqual(record["affected_modules"], ["sentiment_opinion"])
        self.assertTrue(record["diagnosis"]["requires_human_decision"])
        dag = load_dag()
        self.assertEqual(dag.nodes["n11r_gate"]["routes"]["maxed"], "n11r_blocked")

    def test_aggregate_marks_module_degraded(self):
        """阻断记录必须落到 stats_bundle.data_issues，n16 才有依据在观点小节披露。"""
        st = make_state(self.ws)
        rows = [{"id": f"p{i}", "campaign": "A", "platform": "xiaohongshu",
                 "ai_sentiment": 1, "relevance": True, "opinion": "价格",
                 "content_mode": "UGC", "like_count": 1, "comment_count": 0,
                 "share_count": 0, "collect_count": 0, "publish_time": "2026-01-02"}
                for i in range(12)]
        st.write_artifact("annotated_data", {"round": 2, "rows": rows,
                                            "coverage": {"has_error": False},
                                            "degraded": False})
        st.write_artifact("annotation_block_record", {
            "blocked_quality": True, "accuracy": {"opinion": 0.7}, "threshold": 0.9,
            "blocked_reason": "quota_exhausted", "diagnosis": {}})
        out = handlers.aggregate({"id": "n12_aggregate"}, st, {})
        bundle = out["artifacts"]["stats_bundle"]
        issues = bundle.get("data_issues") or []
        self.assertTrue(any(i.get("affected_module") == "sentiment_opinion" for i in issues),
                        f"issues={issues}")


if __name__ == "__main__":
    unittest.main()
