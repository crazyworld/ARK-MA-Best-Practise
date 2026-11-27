"""Field-bound N10 evidence validation; usable from the standalone Skill package."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import label_review


class LabelGroundingTest(unittest.TestCase):
    def setUp(self):
        self.rows = [{
            "id": "post-1", "title": "活动抽奖",
            "content": "中奖了之后在门店购买了酸奶。", "content_mode": "UGC",
            "author": "不得复制的作者字段", "_review_source_index": 8,
        }]
        self.samples, self.catalog, self.sampling = label_review.source_catalog(self.rows)
        quote_id = next(key for key in self.catalog if ":content:" in key)
        self.response = {"candidates": [{
            "name": "购买行动", "definition": "明确描述购买行为。", "boundary": "仅出现话题不纳入。",
            "evidence_refs": [{"quote_id": quote_id, "usage": "representative", "reason": "明确描述购买。"}],
        }]}

    def test_count_over_range_warns_not_fails(self):
        """原版 run.py 数量超区间只告警（lint_count）；job_436842fb74da n08 给 7 个 UGC 类即失败。"""
        base = self.response["candidates"][0]
        many = {"categories": [{**copy.deepcopy(base), "name": f"参与方式{i}"} for i in range(7)]}
        result = label_review.ground_candidates(many, "categories", self.catalog, 6)
        self.assertEqual(len(result["categories"]), 7)
        warns = label_review.lint_categories(result["categories"], "ugc")
        self.assertTrue(any("类别数为 7" in w for w in warns))

    def test_policy_gate_routes_ugc_count_to_human(self):
        import handlers
        src = open(handlers.__file__, encoding="utf-8").read()
        gate = src[src.index("def opinion_policy_gate("):]
        gate = gate[:gate.index("raise HumanPause(")]
        self.assertIn('CATEGORY_COUNT["ugc"]', gate)

    def test_empty_still_fails(self):
        with self.assertRaisesRegex(ValueError, "非空"):
            label_review.ground_candidates({"categories": []}, "categories", self.catalog, 6)

    def test_exact_source_quotes_and_no_personal_fields(self):
        result = label_review.ground_candidates(self.response, "candidates", self.catalog, 8)
        evidence = result["review_items"][0]["evidence"][0]
        self.assertEqual(
            evidence["excerpt"],
            self.rows[0][evidence["source_field"]][evidence["start"]:evidence["end"]],
        )
        self.assertEqual(evidence["post_id"], "post-1")
        self.assertEqual(evidence["source_row_index"], 8)
        self.assertNotIn("author", evidence)
        self.assertEqual(result["evidence_count"], 1)

    def test_unknown_cross_field_and_fabricated_quotes_fail_closed(self):
        for overwrite in (
            {"quote_id": "invented"}, {"source_field": "title"},
            {"excerpt": "模型拼出的中奖原句"}, {"post_id": "another-post"},
        ):
            with self.subTest(overwrite=overwrite):
                response = copy.deepcopy(self.response)
                response["candidates"][0]["evidence_refs"][0].update(overwrite)
                with self.assertRaises(ValueError):
                    label_review.ground_candidates(response, "candidates", self.catalog, 8)

    def test_missing_evidence_is_explicit_and_not_invented(self):
        self.response["candidates"][0]["evidence_refs"] = []
        result = label_review.ground_candidates(self.response, "candidates", {}, 8)
        self.assertEqual(result["review_items"][0]["evidence_status"], "missing")
        self.assertEqual(result["review_items"][0]["evidence"], [])


if __name__ == "__main__":
    unittest.main()
