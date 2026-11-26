"""轮次二覆盖检查：「其他」超门槛不再盲目重试（job_436842fb74da n11）。

原版依据：
- KS_Data_Processing:245 观点空值/其他 ≥10% 阻断；
- QC 协议:219 「其他」须检查是否确实无匹配类别，不能为降比例强行归类，额度耗尽
  按契约记录 blocked_quality 并披露。
同一批数据重跑标注结果不变，所以把它当普通节点失败重试只会连烧三次同样的结论。
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import annotate  # noqa: E402


def _rows(n, opinion=None):
    rows = []
    for i in range(n):
        rows.append({"_annot_id": f"r{i}", "title": "t", "content": "c",
                     "ai_sentiment": 1, "relevance": True,
                     "opinion": opinion or "话题A",
                     "ugc_category": "晒单分享", "discussion_dimension": "产品"})
    return rows


class CoverageQualitySplitTest(unittest.TestCase):
    def test_other_over_threshold_is_quality_block_not_retryable(self):
        rows = _rows(100)
        for row in rows[:12]:      # 12% 其他，无缺失
            row["opinion"] = annotate.OPINION_OTHER
        cov = annotate.check_coverage(rows)
        self.assertTrue(cov["has_error"])
        self.assertFalse(cov["has_retryable_error"])
        self.assertEqual([item["field"] for item in cov["quality_blocked"]], ["opinion"])
        self.assertAlmostEqual(cov["quality_blocked"][0]["ratio"], 0.12, places=4)

    def test_missing_rows_stay_retryable(self):
        rows = _rows(100)
        for row in rows[:5]:
            row["opinion"] = ""
        cov = annotate.check_coverage(rows)
        self.assertTrue(cov["has_error"])
        self.assertTrue(cov["has_retryable_error"])
        self.assertEqual(cov["quality_blocked"], [])

    def test_missing_plus_other_stays_retryable(self):
        rows = _rows(100)
        for row in rows[:12]:
            row["opinion"] = annotate.OPINION_OTHER
        rows[0]["opinion"] = ""
        cov = annotate.check_coverage(rows)
        self.assertTrue(cov["has_retryable_error"])
        self.assertEqual(cov["quality_blocked"], [])

    def test_under_threshold_is_clean(self):
        rows = _rows(100)
        for row in rows[:5]:
            row["opinion"] = annotate.OPINION_OTHER
        cov = annotate.check_coverage(rows)
        self.assertFalse(cov["has_error"])


class CoverageDispositionTest(unittest.TestCase):
    def _cov(self, **kw):
        base = {"has_error": True, "has_retryable_error": False,
                "quality_blocked": [{"field": "opinion", "scope": 7773, "other": 1211,
                                     "ratio": 0.156, "threshold": 0.1}]}
        base.update(kw)
        return base

    def _dispo(self, cov, policy="waive"):
        import handlers
        return handlers.coverage_disposition(cov, policy)

    def test_clean_passes(self):
        self.assertEqual(self._dispo({"has_error": False}), "ok")

    def test_missing_retries(self):
        self.assertEqual(self._dispo(self._cov(has_retryable_error=True)), "retry")

    def test_other_only_defaults_to_waive(self):
        self.assertEqual(self._dispo(self._cov()), "waive")
        self.assertEqual(self._dispo(self._cov(), ""), "waive")

    def test_other_only_block_when_configured(self):
        self.assertEqual(self._dispo(self._cov(), "block"), "block")
        self.assertEqual(self._dispo(self._cov(), "BLOCK"), "block")


class DirectionLabelsFromCandidatesTest(unittest.TestCase):
    """轮次二取候选：优先确认产物的分方向字段，其次 opinion_candidates 分方向候选。

    原版 annot_r2.py 读的就是 opinion_candidates_positive/negative.json；
    合并的 13 个标签不能当作某方向的候选表（会把负面专属类别塞给正面任务）。
    """

    def test_uses_candidates_when_confirmed_lacks_direction(self):
        from handlers import _round2_direction_labels
        confirmed = {"labels": ["正面A", "正面B", "负面A"]}
        candidates = {"candidates": ["正面A", "正面B", "负面A"],
                      "positive_candidates": ["正面A", "正面B"],
                      "negative_candidates": ["负面A"]}
        pos, neg = _round2_direction_labels(confirmed, candidates)
        self.assertEqual(pos, ["正面A", "正面B"])
        self.assertEqual(neg, ["负面A"])

    def test_confirmed_direction_wins(self):
        from handlers import _round2_direction_labels
        confirmed = {"positive_labels": ["P"], "negative_labels": ["N"]}
        candidates = {"positive_candidates": ["x"], "negative_candidates": ["y"]}
        self.assertEqual(_round2_direction_labels(confirmed, candidates), (["P"], ["N"]))

    def test_falls_back_to_merged_labels(self):
        from handlers import _round2_direction_labels
        pos, neg = _round2_direction_labels({"labels": ["A"]}, {})
        self.assertEqual((pos, neg), (None, None))


class LimitationNoteTest(unittest.TestCase):
    def _note(self, stats):
        import handlers
        return handlers._limitation_note(stats)

    def test_no_issue_no_note(self):
        self.assertEqual(self._note({"data_issues": []}), "")
        self.assertEqual(self._note({}), "")

    def test_module_degraded_is_disclosed(self):
        note = self._note({"data_issues": [
            {"affected_module": "sentiment_opinion", "severity": "module_degraded",
             "reason": "观点「空值+其他」1211/7773（15.6%）未低于 10% 门槛"},
            {"affected_module": "baseline_lift", "severity": "info", "reason": "x"},
        ]})
        self.assertIn("sentiment_opinion", note)
        self.assertIn("15.6%", note)
        self.assertNotIn("baseline_lift", note)
        self.assertIn("数据说明区", note)


class _FakeState:
    def __init__(self, artifacts):
        self._a = artifacts
        self.human = {"node": "n10_hc_opinion", "expect": ["confirmed_opinion_labels"]}

    def read_artifact(self, key):
        return self._a.get(key)

    def artifact_ref(self, key):
        return {"artifact": key, "version": 1}


class N10KeepsDirectionLabelsTest(unittest.TestCase):
    """N10 人工确认不能丢正/负分方向候选（原版 annot_r2 正负各一套）。

    job_436842fb74da：N10 pass 后 confirmed_opinion_labels 只有合并的 13 个标签，
    轮次二正面任务因此拿负面专属类别去标正面内容。
    """

    def _build(self):
        import hc_bridge
        st = _FakeState({
            "opinion_candidates": {
                "candidates": ["正面A", "正面B", "负面A"],
                "positive_candidates": ["正面A", "正面B"],
                "negative_candidates": ["负面A"],
                "review_items": [],
            },
            "ugc_category_list": {"categories": ["晒单分享"], "not_applicable": False},
        })
        return hc_bridge.build_human_outputs(st, "pass", "新类别C")

    def test_direction_labels_present(self):
        out = self._build()["confirmed_opinion_labels"]
        self.assertEqual(out["positive_labels"], ["正面A", "正面B", "新类别C"])
        self.assertEqual(out["negative_labels"], ["负面A", "新类别C"])
        self.assertEqual(len(out["labels"]), 4)

    def test_reject_keeps_candidates_without_note(self):
        import hc_bridge
        st = _FakeState({
            "opinion_candidates": {
                "candidates": ["正面A"], "positive_candidates": ["正面A"],
                "negative_candidates": [], "review_items": [],
            },
            "ugc_category_list": {"categories": [], "not_applicable": False},
        })
        out = hc_bridge.build_human_outputs(st, "reject", "")["confirmed_opinion_labels"]
        self.assertFalse(out["confirmed"])
        self.assertEqual(out["positive_labels"], ["正面A"])
        self.assertEqual(out["negative_labels"], [])


if __name__ == "__main__":
    unittest.main()
