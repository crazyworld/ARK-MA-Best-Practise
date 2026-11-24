"""标注审核（n09r / n11r）接通：计划落盘 → 独立 Validator 逐帖判定 → 编排器算正确率。

回归：抽样计划只在内存里算出 sample_size，没有落盘；协调器/Validator 无指令，
Gateway 也不认识这个关卡 → 轮次一完成后流水线停住，无人能继续。
原版口径（KS_Topic5_Validator_QC_Protocol §5）：正确率按字段分别计算，由逐帖判定得出，
不接受审核方自报的汇总数字。
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import handlers
import hc_bridge
from _util import make_state

ROWS = [{"_annot_id": f"k{i}", "title": f"t{i}", "content": f"c{i}", "content_mode": "UGC",
         "ai_sentiment": [1, 0, -1][i % 3], "relevance": i % 4 != 0} for i in range(40)]
META = {"actor_role": "Validator", "mode": "independent-agent", "validator_id": "topic5-validator",
        "execution_source": "sthr-validator-1", "generated_at": "2026-11-25T10:00:00+08:00"}


class AnnotationReviewPlanTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws, current_node="n09r_review_r1")
        self.st.write_artifact("annotated_round1", {"round": 1, "rows": ROWS})
        self.node = {"id": "n09r_review_r1", "name": "轮次一标注审核", "outputs": ["r1_review"]}

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _pause(self):
        with self.assertRaises(handlers.HumanPause) as caught:
            handlers.annotation_review(self.node, self.st, {})
        return caught.exception

    def test_plan_and_samples_are_persisted_for_validator(self):
        pause = self._pause()
        path = self.st.run_dir / "validation" / "annotation_review_r1_plan.json"
        self.assertTrue(path.is_file())
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(plan["stage"], "round1")
        self.assertEqual(plan["fields"], ["sentiment", "relevance"])
        self.assertEqual(plan["subject"]["key"], "annotated_round1")
        ids = {s["id"] for s in plan["samples"]}
        self.assertTrue(ids)
        # 每条样本带原帖与当前标注，Validator 不必自行翻库
        sample = plan["samples"][0]
        for key in ("title", "content", "labels"):
            self.assertIn(key, sample)
        self.assertIn(str(path), pause.prompt)

    def _verdicts(self, wrong=0):
        plan = json.loads((self.st.run_dir / "validation" / "annotation_review_r1_plan.json")
                          .read_text(encoding="utf-8"))
        items = []
        for index, sample in enumerate(plan["samples"]):
            for field in plan["fields"]:
                verdict = "wrong" if index < wrong and field == "sentiment" else "correct"
                items.append({"id": sample["id"], "field": field, "verdict": verdict,
                              **({"expected": "正面"} if verdict == "wrong" else {})})
        return plan, items

    def _resume(self, verdicts, **extra):
        self.st.status = "awaiting_human"
        self.st.human = {"node": "n09r_review_r1", "prompt": "x", "expect": ["r1_review"]}
        note = json.dumps({"verdicts": verdicts, "qc_metadata": META, **extra}, ensure_ascii=False)
        return hc_bridge.build_human_outputs(self.st, "pass", note)

    def test_accuracy_is_computed_from_verdicts(self):
        self._pause()
        plan, verdicts = self._verdicts(wrong=1)
        review = self._resume(verdicts)["r1_review"]
        self.assertAlmostEqual(review["accuracy"]["relevance"], 1.0)
        self.assertLess(review["accuracy"]["sentiment"], 1.0)
        self.assertEqual(review["plan"]["remaining_after_plan"], plan["remaining_after_plan"])
        self.assertEqual(len(review["wrong_ids"]["sentiment"]), 1)

    def test_self_reported_accuracy_is_ignored(self):
        self._pause()
        _, verdicts = self._verdicts(wrong=3)
        review = self._resume(verdicts, accuracy={"sentiment": 1.0})["r1_review"]
        self.assertLess(review["accuracy"]["sentiment"], 1.0)

    def test_verdicts_must_cover_every_planned_sample_and_field(self):
        self._pause()
        _, verdicts = self._verdicts()
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            self._resume(verdicts[:-1])

    def test_verdict_outside_plan_is_rejected(self):
        self._pause()
        _, verdicts = self._verdicts()
        with self.assertRaisesRegex(ValueError, "计划外"):
            self._resume(verdicts + [{"id": "not-in-plan", "field": "sentiment", "verdict": "correct"}])

    def test_review_must_bind_current_annotation_version(self):
        self._pause()
        _, verdicts = self._verdicts()
        self.st.write_artifact("annotated_round1", {"round": 1, "rows": ROWS[:-1]})
        with self.assertRaisesRegex(ValueError, "版本"):
            self._resume(verdicts)

    def test_independent_identity_still_required(self):
        self._pause()
        _, verdicts = self._verdicts()
        self.st.status = "awaiting_human"
        self.st.human = {"node": "n09r_review_r1", "prompt": "x", "expect": ["r1_review"]}
        note = json.dumps({"verdicts": verdicts, "qc_metadata": {**META, "mode": "same-agent"}})
        with self.assertRaisesRegex(ValueError, "independent-agent"):
            hc_bridge.build_human_outputs(self.st, "pass", note)

    def test_round2_plan_uses_opinion_field(self):
        rows = [dict(r, opinion="口味好") for r in ROWS]
        self.st.write_artifact("annotated_data", {"round": 2, "rows": rows})
        self.st.write_artifact("confirmed_opinion_labels", {"labels": ["口味好"]})
        node = {"id": "n11r_review_r2", "name": "轮次二标注审核", "outputs": ["r2_review"]}
        with self.assertRaises(handlers.HumanPause):
            handlers.annotation_review(node, self.st, {})
        plan = json.loads((self.st.run_dir / "validation" / "annotation_review_r2_plan.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(plan["fields"], ["opinion"])
        self.assertEqual(plan["subject"]["key"], "annotated_data")

    def test_gates_have_internal_hc_kinds(self):
        self.assertEqual(hc_bridge.hc_kind_for_node("n09r_review_r1"), "R1_REVIEW")
        self.assertEqual(hc_bridge.hc_kind_for_node("n11r_review_r2"), "R2_REVIEW")


if __name__ == "__main__":
    unittest.main()
