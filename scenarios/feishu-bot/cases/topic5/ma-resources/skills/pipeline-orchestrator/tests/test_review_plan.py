"""FIX-30：标注抽检计划与正确率计算，对齐原版 plan_annotation_review.py。

原版依据：tools/plan_annotation_review.py
  - 额度 min(ceil(10%), 500)，已审 ID 跨轮次扣减，reserve 收敛
  - 分层：特殊层优先覆盖，再按 sqrt(容量)/(已分配+1) 加权，特殊层权重 ×3
  - seed=42 层内随机，可复现
  - 阶段适用范围：round1 全量；round2 相关且正负；attributes 相关行
  - 正确率是抽样估计，不是总体真值

运行：python3 -m unittest tests.test_review_plan
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review_plan as R  # noqa: E402

CONFIG = {
    "positive_labels": ["口味好评", "周边讨论"],
    "negative_labels": ["价格抱怨"],
    "ugc_labels": ["晒单开箱", "打卡参与"],
}


def _row(i, sentiment=1, relevant=True, mode="UGC", opinion="口味好评",
         ugc="晒单开箱", dim="产品"):
    return {"_annot_id": f"k{i}", "ai_sentiment": sentiment,
            "relevance": relevant, "content_mode": mode,
            "opinion": opinion, "ugc_category": ugc,
            "discussion_dimension": dim, "title": "t", "content": "c"}


def _rows(n=100, **kw):
    return [_row(i, **kw) for i in range(n)]


class QuotaTest(unittest.TestCase):
    def test_cap_is_ten_percent(self):
        plan = R.plan_review(_rows(100), stage="round1", config=CONFIG)
        self.assertEqual(plan["base_sample_size"], 10)
        self.assertEqual(plan["population"], 100)

    def test_cap_never_exceeds_500(self):
        plan = R.plan_review(_rows(20000), stage="round1", config=CONFIG,
                             reserve=0)
        self.assertEqual(plan["base_sample_size"], 500)
        self.assertLessEqual(plan["sample_size"], 500)

    def test_ceil_rounds_up(self):
        plan = R.plan_review(_rows(15), stage="round1", config=CONFIG)
        self.assertEqual(plan["base_sample_size"], 2)  # ceil(1.5)

    def test_reviewed_ids_deducted(self):
        rows = _rows(100)
        first = R.plan_review(rows, stage="round1", config=CONFIG)
        done = [s["id"] for s in first["samples"]]
        second = R.plan_review(rows, stage="round1", config=CONFIG,
                               reviewed_ids=done)
        self.assertEqual(second["distinct_reviewed_before"], len(done))
        self.assertFalse(set(s["id"] for s in second["samples"]) & set(done))

    def test_reviewed_over_cap_raises(self):
        with self.assertRaisesRegex(ValueError, "500"):
            R.plan_review(_rows(5), stage="round1", config=CONFIG,
                          reviewed_ids=[f"x{i}" for i in range(501)])

    def test_reserve_reduces_sample(self):
        # base 触及 500 上限时 reserve 才削减配额（原版 target=min(base, remaining-reserve, ...)）
        rows = _rows(6000)
        no_reserve = R.plan_review(rows, stage="round1", config=CONFIG, reserve=0)
        with_reserve = R.plan_review(rows, stage="round1", config=CONFIG,
                                     reserve=100)
        self.assertEqual(no_reserve["sample_size"], 500)
        self.assertEqual(with_reserve["sample_size"], 400)
        self.assertEqual(with_reserve["reserve_requested"], 100)

    def test_reserve_bounds(self):
        for bad in (-1, 501):
            with self.subTest(reserve=bad):
                with self.assertRaisesRegex(ValueError, "Reserve"):
                    R.plan_review(_rows(5), stage="round1", config=CONFIG,
                                  reserve=bad)


class StratumTest(unittest.TestCase):
    def test_irrelevant_row_is_special(self):
        key = R.stratum(_row(0, relevant=False), "round1", CONFIG)
        self.assertTrue(key.startswith(R.SPECIAL_PREFIX))
        self.assertIn("不相关", key)

    def test_negative_row_is_special(self):
        key = R.stratum(_row(0, sentiment=-1, opinion="价格抱怨"), "round1", CONFIG)
        self.assertIn("负面", key)

    def test_other_opinion_special_only_in_round2(self):
        row = _row(0, opinion="其他")
        self.assertNotIn("其他", R.stratum(row, "round1", CONFIG))
        self.assertIn("其他", R.stratum(row, "round2", CONFIG))

    def test_illegal_value_is_special(self):
        row = _row(0)
        row["ai_sentiment"] = "很好"
        key = R.stratum(row, "round1", CONFIG)
        self.assertIn("空值或非法", key)

    def test_normal_row_prefix(self):
        key = R.stratum(_row(0), "round1", CONFIG)
        self.assertTrue(key.startswith(R.NORMAL_PREFIX))

    def test_round2_stratum_carries_opinion_labels(self):
        key = R.stratum(_row(0, opinion="口味好评,周边讨论"), "round2", CONFIG)
        self.assertIn("观点:口味好评,周边讨论", key)

    def test_attributes_stratum_carries_ugc_and_dimension(self):
        key = R.stratum(_row(0, ugc="晒单开箱", dim="品牌"), "attributes", CONFIG)
        self.assertIn("参与:晒单开箱", key)
        self.assertIn("主对象:品牌", key)

    def test_unknown_type_is_special_in_attributes(self):
        key = R.stratum(_row(0, mode=""), "attributes", CONFIG)
        self.assertIn("类型未知", key)

    def test_evidence_unresolved_is_special(self):
        row = _row(0)
        row["evidence_status"] = "unresolved"
        self.assertIn("证据歧义", R.stratum(row, "round1", CONFIG))


class ScopeTest(unittest.TestCase):
    def test_round2_scope_only_relevant_positive_negative(self):
        rows = [_row(0, 1), _row(1, -1, opinion="价格抱怨"), _row(2, 0),
                _row(3, 1, relevant=False)]
        plan = R.plan_review(rows, stage="round2", config=CONFIG)
        self.assertEqual(plan["population"], 2)
        self.assertEqual(plan["field_populations"], {"opinion": 2})

    def test_attributes_scope_only_relevant(self):
        rows = [_row(0), _row(1, relevant=False, ugc="N/A", dim="N/A"),
                _row(2, mode="PGC", ugc="N/A")]
        plan = R.plan_review(rows, stage="attributes", config=CONFIG)
        self.assertEqual(plan["population"], 2)
        self.assertEqual(plan["field_populations"]["dimension"], 2)
        # UGC 只统计 UGC 行
        self.assertEqual(plan["field_populations"]["ugc"], 1)

    def test_round1_scope_is_all_rows(self):
        rows = [_row(0), _row(1, relevant=False), _row(2, 0)]
        plan = R.plan_review(rows, stage="round1", config=CONFIG)
        self.assertEqual(plan["population"], 3)
        self.assertEqual(plan["field_populations"],
                         {"sentiment": 3, "relevant": 3})

    def test_pgc_only_project_skips_ugc_field(self):
        cfg = dict(CONFIG, ugc_applicable=False)
        plan = R.plan_review([_row(0)], stage="attributes", config=cfg)
        self.assertNotIn("ugc", plan["field_populations"])


class SelectionTest(unittest.TestCase):
    def test_reproducible_with_same_seed(self):
        rows = _rows(200)
        a = R.plan_review(rows, stage="round1", config=CONFIG)
        b = R.plan_review(rows, stage="round1", config=CONFIG)
        self.assertEqual([s["id"] for s in a["samples"]],
                         [s["id"] for s in b["samples"]])

    def test_different_seed_changes_sample(self):
        rows = _rows(200)
        a = R.plan_review(rows, stage="round1", config=CONFIG, seed=42)
        b = R.plan_review(rows, stage="round1", config=CONFIG, seed=7)
        self.assertNotEqual([s["id"] for s in a["samples"]],
                            [s["id"] for s in b["samples"]])

    def test_no_duplicate_samples(self):
        plan = R.plan_review(_rows(500), stage="round1", config=CONFIG)
        ids = [s["id"] for s in plan["samples"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_uncovered_strata_empty_when_enough_quota(self):
        rows = [_row(i) for i in range(50)] + [_row(100 + i, relevant=False)
                                               for i in range(50)]
        plan = R.plan_review(rows, stage="round1", config=CONFIG)
        self.assertEqual(plan["uncovered_strata"], [])

    def test_special_stratum_oversampled(self):
        """特殊层占比应高于其人口占比（权重 ×3）。"""
        rows = [_row(i) for i in range(180)]
        special = [_row(1000 + i, relevant=False) for i in range(20)]
        plan = R.plan_review(rows + special, stage="round1", config=CONFIG)
        picked_special = sum(1 for s in plan["samples"]
                             if s["stratum"].startswith(R.SPECIAL_PREFIX))
        self.assertGreater(picked_special / max(plan["sample_size"], 1),
                           20 / 200)

    def test_accuracy_not_population_truth(self):
        plan = R.plan_review(_rows(100), stage="round1", config=CONFIG)
        self.assertFalse(plan["accuracy_is_population_truth"])
        self.assertEqual(plan["llm_requests"], 0)


class GuardTest(unittest.TestCase):
    def test_duplicate_or_missing_ids_raise(self):
        rows = [_row(0), _row(0)]
        with self.assertRaisesRegex(ValueError, "unique IDs"):
            R.plan_review(rows, stage="round1", config=CONFIG)

    def test_round2_requires_direction_labels(self):
        with self.assertRaisesRegex(ValueError, "direction labels"):
            R.plan_review(_rows(5), stage="round2", config={})

    def test_attributes_requires_ugc_labels(self):
        with self.assertRaisesRegex(ValueError, "UGC config"):
            R.plan_review(_rows(5), stage="attributes", config={})

    def test_unknown_stage_raises(self):
        with self.assertRaisesRegex(ValueError, "未知 stage"):
            R.plan_review(_rows(5), stage="round9", config=CONFIG)


class AccuracyTest(unittest.TestCase):
    def test_per_field_accuracy(self):
        verdicts = [
            {"id": "k0", "field": "sentiment", "verdict": "correct"},
            {"id": "k1", "field": "sentiment", "verdict": "wrong"},
            {"id": "k0", "field": "relevance", "verdict": "correct"},
        ]
        result = R.accuracy_from_verdicts(verdicts)
        self.assertEqual(result["accuracy"]["sentiment"], 0.5)
        self.assertEqual(result["accuracy"]["relevance"], 1.0)
        self.assertEqual(result["wrong_ids"]["sentiment"], ["k1"])

    def test_field_without_verdict_absent(self):
        result = R.accuracy_from_verdicts(
            [{"id": "k0", "field": "sentiment", "verdict": "correct"}])
        self.assertNotIn("relevance", result["accuracy"])

    def test_restrict_to_reviewed_ids(self):
        verdicts = [
            {"id": "k0", "field": "sentiment", "verdict": "correct"},
            {"id": "k9", "field": "sentiment", "verdict": "wrong"},
        ]
        result = R.accuracy_from_verdicts(verdicts, reviewed_ids=["k0"])
        self.assertEqual(result["accuracy"]["sentiment"], 1.0)
        self.assertEqual(result["wrong_ids"].get("sentiment", []), [])

    def test_illegal_verdict_ignored(self):
        result = R.accuracy_from_verdicts(
            [{"id": "k0", "field": "sentiment", "verdict": "maybe"}])
        self.assertEqual(result["accuracy"], {})

    def test_accuracy_thresholds_documented(self):
        """验收口径：>=0.90 通过，目标 0.95；这里只校验计算，路由在 handlers。"""
        verdicts = ([{"id": f"a{i}", "field": "opinion", "verdict": "correct"}
                     for i in range(9)]
                    + [{"id": "bad", "field": "opinion", "verdict": "wrong"}])
        result = R.accuracy_from_verdicts(verdicts)
        self.assertEqual(result["accuracy"]["opinion"], 0.9)
        self.assertGreaterEqual(result["accuracy"]["opinion"], 0.90)


if __name__ == "__main__":
    unittest.main()
