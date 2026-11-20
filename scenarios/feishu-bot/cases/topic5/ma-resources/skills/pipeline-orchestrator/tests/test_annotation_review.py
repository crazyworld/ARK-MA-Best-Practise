"""FIX-30：标注审核路由、重标范围与 HB 校验（独立 Validator 抽检闭环）。

原版/方案依据：
  - FIX_PLAN FIX-30 路由：正确率 ≥ annotation_accuracy_min 放行；低于且额度有余
    回 n09r_rework / n11r_rework；额度或轮次用尽置 blocked_quality 走 HC7。
  - 重标范围：系统性错误重标全部行，局部错误只重标 wrong_ids。
  - HB 校验与 G3 同构：actor_role=Validator、mode=independent-agent、必须有判定。
  - 开关 annotation_review_enabled=false 时 gate 直接放行并留痕 P1。

运行：python3 -m unittest tests.test_annotation_review
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import engine  # noqa: E402
import handlers  # noqa: E402
import hc_bridge  # noqa: E402
from _util import load_dag  # noqa: E402
from state import RunState, register_run_dir  # noqa: E402


def _state(ws, params=None, run_id="r1"):
    run_dir = Path(ws) / "tasks" / "t" / "runs" / run_id
    for sub in ("artifacts", "attempts", "validation"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    register_run_dir(run_id, run_dir)
    return RunState(
        run_id=run_id, task_id="t", dag_slug="s", dag_version="0.2",
        entry="n09r_gate", params=dict(params or {}), current_node="n09r_gate",
        frontier=["n09r_gate"])


def _review(accuracy, *, wrong_ids=None, error_types=None, remaining=None,
            enabled=True):
    return {
        "enabled": enabled,
        "accuracy": accuracy,
        "wrong_ids": wrong_ids or {},
        "error_types": error_types or {},
        "plan": {"remaining_after_plan": remaining},
    }


class ReviewDecisionTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _route(self, review, *, gate="n09r_gate", params=None, counters=None):
        st = _state(self.ws, params=params)
        key = "r1_review" if "n09r" in gate else "r2_review"
        st.write_artifact(key, review)
        for k, v in (counters or {}).items():
            st.counters[k] = v
        node = {"id": gate, "outputs": [key]}
        return handlers.annotation_review_decision(node, st, {})

    def test_accuracy_pass_routes_forward(self):
        result = self._route(_review({"sentiment": 0.95, "relevance": 0.92}, remaining=400))
        self.assertEqual(result["route"], "pass")

    def test_accuracy_exactly_at_threshold_passes(self):
        result = self._route(_review({"sentiment": 0.90}, remaining=400))
        self.assertEqual(result["route"], "pass")

    def test_accuracy_below_threshold_retries(self):
        result = self._route(_review({"sentiment": 0.85}, remaining=400))
        self.assertEqual(result["route"], "retry")

    def test_single_field_under_blocks_whole_round(self):
        """两个字段分别判定，任一不达标即未达标。"""
        result = self._route(_review({"sentiment": 0.99, "relevance": 0.60},
                                     remaining=400))
        self.assertEqual(result["route"], "retry")

    def test_custom_threshold_from_params(self):
        review = _review({"sentiment": 0.85}, remaining=400)
        result = self._route(review, params={"annotation_accuracy_min": 0.80})
        self.assertEqual(result["route"], "pass")

    def test_no_verdict_does_not_pass(self):
        """没有任何判定结果时不能当作通过（没有证据不能放行）。"""
        result = self._route(_review({}, remaining=400))
        self.assertEqual(result["route"], "retry")

    def test_rounds_exhausted_escalates(self):
        result = self._route(
            _review({"sentiment": 0.5}, remaining=400),
            counters={"annotation_review_rounds_round1": 2})
        self.assertEqual(result["route"], "maxed")

    def test_quota_exhausted_escalates(self):
        result = self._route(_review({"sentiment": 0.5}, remaining=0))
        self.assertEqual(result["route"], "maxed")

    def test_max_rounds_configurable(self):
        result = self._route(
            _review({"sentiment": 0.5}, remaining=400),
            params={"annotation_review_max_rounds": 1},
            counters={"annotation_review_rounds_round1": 1})
        self.assertEqual(result["route"], "maxed")

    def test_disabled_review_passes_through(self):
        result = self._route(_review({}, enabled=False))
        self.assertEqual(result["route"], "pass")

    def test_round2_uses_r2_review(self):
        result = self._route(_review({"opinion": 0.95}, remaining=400),
                             gate="n11r_gate")
        self.assertEqual(result["route"], "pass")


class ReworkTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, review, *, gate="n09r_rework", counters=None):
        st = _state(self.ws, run_id=f"r{abs(hash(str(review))) % 10000}")
        key = "r1_review" if "n09r" in gate else "r2_review"
        st.write_artifact(key, review)
        st.write_artifact("annotated_round1", {"rows": []})
        st.write_artifact("annotated_data", {"rows": []})
        st.write_artifact("stats_bundle", {})
        st.write_artifact("report_md", {})
        for k, v in (counters or {}).items():
            st.counters[k] = v
        result = handlers.annotation_rework({"id": gate, "outputs": ["annotation_rework_plan"]},
                                            st, {})
        return st, result["artifacts"]["annotation_rework_plan"]

    def test_local_errors_rerun_only_wrong_ids(self):
        st, plan = self._run(_review(
            {"opinion": 0.8}, wrong_ids={"opinion": ["k3", "k7"]},
            error_types={"opinion": "local"}))
        self.assertEqual(plan["scope"], "wrong_ids")
        self.assertEqual(plan["rerun_ids"], ["k3", "k7"])

    def test_systematic_relevance_error_reruns_all(self):
        st, plan = self._run(_review(
            {"relevance": 0.5}, wrong_ids={"relevance": ["k1"]},
            error_types={"relevance": "relevance"}))
        self.assertEqual(plan["scope"], "all")
        self.assertEqual(plan["rerun_ids"], [])

    def test_no_wrong_ids_counts_as_systematic(self):
        st, plan = self._run(_review({"sentiment": 0.6}))
        self.assertEqual(plan["scope"], "all")

    def test_rerun_epoch_increments(self):
        st, plan = self._run(
            _review({"sentiment": 0.6}), counters={"annotation_rerun_epoch_round1": 2})
        self.assertEqual(plan["rerun_epoch"], 3)

    def test_review_round_counter_increments(self):
        st, plan = self._run(
            _review({"sentiment": 0.6}),
            counters={"annotation_review_rounds_round1": 1})
        self.assertEqual(plan["review_rounds"], 2)

    def test_round1_rework_invalidates_downstream(self):
        st, plan = self._run(_review({"sentiment": 0.6}, wrong_ids={"sentiment": ["k1"]},
                                     error_types={"sentiment": "local"}))
        self.assertIn("annotated_round1", plan["invalidated"])
        self.assertIn("annotated_data", plan["invalidated"])
        self.assertIn("report_md", plan["invalidated"])
        self.assertIn("annotated_round1", st.invalidated_artifacts)

    def test_round2_rework_keeps_round1(self):
        st, plan = self._run(_review({"opinion": 0.6}, wrong_ids={"opinion": ["k1"]},
                                     error_types={"opinion": "local"}),
                             gate="n11r_rework")
        self.assertNotIn("annotated_round1", plan["invalidated"])
        self.assertIn("annotated_data", plan["invalidated"])
        self.assertNotIn("annotated_round1", st.invalidated_artifacts)

    def test_reviewed_ids_accumulate(self):
        review = _review({"opinion": 0.6}, wrong_ids={"opinion": ["k1"]},
                         error_types={"opinion": "local"})
        review["plan"] = {"samples": [{"id": "k1"}, {"id": "k2"}],
                          "remaining_after_plan": 300}
        st, plan = self._run(review, gate="n11r_rework")
        self.assertEqual(plan["history"]["reviewed_ids"], ["k1", "k2"])


class BridgeTest(unittest.TestCase):
    """HB 对两个审核节点的校验（与 G3 同构）。"""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = _state(self.ws)
        self.st.human = {"node": "n09r_review_r1"}

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _note(self, **overrides):
        payload = {
            "accuracy": {"sentiment": 0.95},
            "verdict_count": 5,
            "qc_metadata": {"actor_role": "Validator", "mode": "independent-agent",
                            "validator_id": "topic5-validator",
                            "generated_at": "2026-01-01T00:00:00+08:00"},
        }
        payload.update(overrides)
        return json.dumps(payload, ensure_ascii=False)

    def test_valid_review_accepted(self):
        outs = hc_bridge.build_human_outputs(self.st, "pass", self._note())
        self.assertIn("r1_review", outs)
        self.assertEqual(outs["r1_review"]["accuracy"]["sentiment"], 0.95)

    def test_r2_maps_to_r2_key(self):
        self.st.human = {"node": "n11r_review_r2"}
        outs = hc_bridge.build_human_outputs(self.st, "pass", self._note())
        self.assertIn("r2_review", outs)
        self.assertNotIn("r1_review", outs)

    def test_missing_actor_role_rejected(self):
        with self.assertRaisesRegex(ValueError, "actor_role"):
            hc_bridge.build_human_outputs(
                self.st, "pass",
                self._note(qc_metadata={"mode": "independent-agent"}))

    def test_non_independent_mode_rejected(self):
        with self.assertRaisesRegex(ValueError, "independent-agent"):
            hc_bridge.build_human_outputs(
                self.st, "pass",
                self._note(qc_metadata={"actor_role": "Validator",
                                        "mode": "same-agent"}))

    def test_missing_accuracy_rejected(self):
        with self.assertRaisesRegex(ValueError, "accuracy"):
            hc_bridge.build_human_outputs(
                self.st, "pass",
                self._note(accuracy={}))

    def test_zero_verdicts_rejected(self):
        with self.assertRaisesRegex(ValueError, "逐帖判定"):
            hc_bridge.build_human_outputs(
                self.st, "pass", self._note(verdict_count=0))

    def test_non_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "JSON"):
            hc_bridge.build_human_outputs(self.st, "pass", "not-json")


class DagWiringTest(unittest.TestCase):
    def test_review_nodes_sit_between_annotation_and_next(self):
        dag = load_dag()
        self.assertEqual(dag.nodes["n09_annotate_r1"]["next"], "n09r_review_r1")
        self.assertEqual(dag.nodes["n11_annotate_r2"]["next"], "n11r_review_r2")
        # FIX-31：审核通过后观点发现 ∥ UGC 发现
        self.assertEqual(dag.nodes["n09r_gate"]["routes"]["pass"],
                         ["n09b_opinion_candidates", "n08_ugc_discovery"])
        self.assertEqual(dag.nodes["n11r_gate"]["routes"]["pass"], "n12_aggregate")
        for gate in ("n09r_gate", "n11r_gate"):
            self.assertEqual(dag.nodes[gate]["routes"]["maxed"], "n_hc7_escalate")
        self.assertEqual(dag.nodes["n09r_rework"]["next"], "n09_annotate_r1")
        self.assertEqual(dag.nodes["n11r_rework"]["next"], "n11_annotate_r2")

    def test_aggregate_waits_for_round2_review(self):
        """n12 必须依赖 r2_review，否则会被提前入队抢在审核之前执行。"""
        dag = load_dag()
        self.assertIn("r2_review", dag.nodes["n12_aggregate"]["inputs"])
        # n06 不再触发 n12（只提供 brand_stats_baseline）
        self.assertIsNone(dag.nodes["n06_brand_baseline"].get("next"))

    def test_review_is_read_only(self):
        dag = load_dag()
        for node_id in ("n09r_review_r1", "n11r_review_r2"):
            self.assertEqual(dag.nodes[node_id]["side_effect"], "none")
            self.assertTrue(dag.nodes[node_id].get("pause_capable"))


class RerunScopeTest(unittest.TestCase):
    """FIX-30：增量重标（rerun_ids）与校准样例（few-shot）。"""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = _state(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _rows(self, n=3):
        return [{"_annot_id": f"k{i}", "title": f"标题{i}", "content": f"内容{i}",
                 "ai_sentiment": 1, "relevance": True} for i in range(n)]

    def test_no_plan_means_full_rerun(self):
        rerun_ids, calib = handlers._rerun_scope(self.st, "round1", "r1_review", [])
        self.assertIsNone(rerun_ids)
        self.assertEqual(calib, [])

    def test_stage_mismatch_ignored(self):
        self.st.write_artifact("annotation_rework_plan",
                               {"stage": "round2", "scope": "wrong_ids",
                                "rerun_ids": ["k1"]})
        rerun_ids, _ = handlers._rerun_scope(self.st, "round1", "r1_review", [])
        self.assertIsNone(rerun_ids)

    def test_wrong_ids_scope_limits_rerun(self):
        self.st.write_artifact("annotation_rework_plan",
                               {"stage": "round1", "scope": "wrong_ids",
                                "rerun_ids": ["k1", "k2"]})
        rerun_ids, _ = handlers._rerun_scope(self.st, "round1", "r1_review", [])
        self.assertEqual(rerun_ids, ["k1", "k2"])

    def test_all_scope_ignores_rerun_ids(self):
        self.st.write_artifact("annotation_rework_plan",
                               {"stage": "round1", "scope": "all",
                                "rerun_ids": ["k1"]})
        rerun_ids, _ = handlers._rerun_scope(self.st, "round1", "r1_review", [])
        self.assertIsNone(rerun_ids)

    def test_calibration_samples_from_verdicts(self):
        rows = self._rows()
        review = {"verdicts": [
            {"id": "k1", "field": "campaign_relevant", "verdict": "wrong",
             "expected": "是"},
            {"id": "k2", "field": "sentiment", "verdict": "correct"},
        ]}
        samples = handlers._calibration_samples(review, rows)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["field"], "campaign_relevant")
        self.assertEqual(samples[0]["correct"], "是")
        self.assertIn("内容1", samples[0]["content"])

    def test_verdict_without_expected_skipped(self):
        samples = handlers._calibration_samples(
            {"verdicts": [{"id": "k1", "field": "sentiment", "verdict": "wrong"}]},
            self._rows())
        self.assertEqual(samples, [])

    def test_no_verdicts_means_no_calibration(self):
        self.assertEqual(handlers._calibration_samples({}, self._rows()), [])

    def test_round1_rerun_only_touches_given_ids(self):
        import annotate as A
        from unittest.mock import patch
        rows = self._rows(3)
        touched = []

        def fake_chat(_system, user, **_kw):
            payload = json.loads(user)
            touched.extend(item["id"] for item in payload)
            return [{"id": item["id"], "sentiment": "正面",
                     "campaign_relevant": "是"} for item in payload]

        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=fake_chat):
            A.annotate_round1(rows, context={"campaign_name": "A",
                                             "campaign_context": "B",
                                             "activity_keywords": "C"},
                              rerun_ids=["k1"])
        self.assertEqual(touched, ["k1"])
        # 未重标的行保留原标注
        self.assertEqual(rows[0]["ai_sentiment"], 1)

    def test_calibration_reaches_prompt(self):
        import annotate as A
        base = A._calibration_block(None)
        with_calib = A._calibration_block([
            {"content": "活动没提品牌", "field": "campaign_relevant",
             "wrong": "否", "correct": "是"}])
        self.assertEqual(base, "")
        self.assertIn("校准样例", with_calib)
        self.assertIn("活动没提品牌", with_calib)
        ctx = {"campaign_name": "A", "campaign_context": "B",
               "activity_keywords": "C"}
        self.assertIn("校准样例", A._r1_sys(ctx, [
            {"content": "x", "field": "sentiment", "wrong": "负面", "correct": "中性"}]))
        self.assertNotIn("校准样例", A._r1_sys(ctx))


if __name__ == "__main__":
    unittest.main()
