"""FIX-29：标注结构预检（只读）对齐原版 check_annotation_structure.py。

原版依据：tools/check_annotation_structure.py
  - 只读，不改数据、不调模型（llm_requests=0）
  - ID 缺失/重复、轮次一取值域、别名冲突
  - 观点只在「相关且正/负」行合法，值须在已批准类别内、去重 ≤ max_opinion_labels
  - UGC 只在「相关且 UGC」行取值，其余必须 N/A
  - 主讨论对象 ∈ {品牌, 产品, 活动}，不相关行必须 N/A
  - 单任务阶段拒绝落在适用范围外的行（row_outside_task_scope）
  - 已批准类别清单缺失时抛错

运行：python3 -m unittest tests.test_annotation_structure
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import annotation_structure as S  # noqa: E402

CONFIG = {
    "positive_labels": ["口味好评", "周边讨论"],
    "negative_labels": ["价格抱怨"],
    "ugc_labels": ["晒单开箱", "打卡参与"],
}


def _row(i: "str | None" = "k0", sentiment: "int | None" = 1,
         relevant: "bool | None" = True, mode: str = "UGC",
         opinion: str = "", ugc: "str | None" = None,
         dim: "str | None" = None) -> dict:
    row = {"_annot_id": i, "ai_sentiment": sentiment, "relevance": relevant,
           "content_mode": mode, "title": "t", "content": "c"}
    if opinion:
        row["opinion"] = opinion
    if ugc is not None:
        row["ugc_category"] = ugc
    if dim is not None:
        row["discussion_dimension"] = dim
    return row


def _full_ok(i: "str | None" = "k0", **kw) -> dict:
    """一条完全合法的满阶段数据。"""
    base = dict(i=i, sentiment=1, relevant=True, mode="UGC",
                opinion="口味好评", ugc="晒单开箱", dim="产品")
    base.update(kw)
    return _row(**base)


def _reasons(report, field=None):
    return [e["reason"] for e in report["errors"]
            if field is None or e["field"] == field]


class Round1Test(unittest.TestCase):
    def test_valid_round1_passes(self):
        rows = [_row("k0", 1, True), _row("k1", 0, False), _row("k2", -1, True)]
        report = S.check_structure(rows, config={}, stage="round1")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["llm_requests"], 0)
        self.assertFalse(report["source_modified"])

    def test_missing_sentiment_is_error(self):
        report = S.check_structure([_row("k0", None, True)], config={},
                                   stage="round1")
        self.assertIn("illegal_or_empty", _reasons(report, "sentiment"))

    def test_missing_relevance_is_error(self):
        report = S.check_structure([_row("k0", 1, None)], config={},
                                   stage="round1")
        self.assertIn("illegal_or_empty", _reasons(report, "relevant"))

    def test_duplicate_and_missing_id(self):
        rows = [_row("k0"), _row("k0"), _row(None)]
        report = S.check_structure(rows, config={}, stage="round1")
        reasons = _reasons(report, "id")
        self.assertIn("duplicate_id", reasons)
        self.assertIn("missing_id", reasons)

    def test_parse_error_flagged(self):
        row = _row("k0")
        row["parse_error"] = "bad json"
        report = S.check_structure([row], config={}, stage="round1")
        self.assertIn("parse_error", _reasons(report, "output"))

    def test_conflicting_aliases(self):
        row = _row("k0")
        row["sentiment"] = "正面"
        row["情感(AI)"] = "负面"
        report = S.check_structure([row], config={}, stage="round1")
        self.assertIn("conflicting_aliases", _reasons(report, "sentiment"))


class OpinionTest(unittest.TestCase):
    def test_opinion_on_neutral_row_is_error(self):
        row = _full_ok(sentiment=0, opinion="口味好评")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertIn("label_outside_applicable_scope", _reasons(report, "opinion"))

    def test_opinion_on_irrelevant_row_is_error(self):
        row = _full_ok(relevant=False, opinion="口味好评", ugc="N/A", dim="N/A")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertIn("label_outside_applicable_scope", _reasons(report, "opinion"))

    def test_neutral_row_with_none_placeholder_passes(self):
        row = _full_ok(sentiment=0, opinion="无")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertEqual(report["status"], "pass")

    def test_opinion_outside_candidates(self):
        row = _full_ok(opinion="包装好看")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertIn("illegal_or_empty", _reasons(report, "opinion"))

    def test_too_many_labels(self):
        row = _full_ok(opinion="口味好评,周边讨论,口味好评")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertIn("too_many_labels", _reasons(report, "opinion"))

    def test_duplicate_labels(self):
        row = _full_ok(opinion="口味好评,口味好评")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertIn("duplicate_labels", _reasons(report, "opinion"))

    def test_two_labels_pass(self):
        row = _full_ok(opinion="口味好评,周边讨论")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertEqual(report["status"], "pass")

    def test_negative_labels_use_negative_set(self):
        row = _full_ok(sentiment=-1, opinion="口味好评")
        report = S.check_structure([row], config=CONFIG, stage="full")
        self.assertIn("illegal_or_empty", _reasons(report, "opinion"))

    def test_stage_rejects_row_outside_scope(self):
        """positive 阶段只接受「相关且正面」的行，其余记 row_outside_task_scope。"""
        rows = [_full_ok("k0", sentiment=-1, opinion="价格抱怨"),
                _full_ok("k1", sentiment=1)]
        report = S.check_structure(rows, config=CONFIG, stage="positive")
        self.assertIn("row_outside_task_scope", _reasons(report, "opinion"))


class AttributeTest(unittest.TestCase):
    def test_pgc_row_must_be_na(self):
        row = _full_ok(mode="PGC", ugc="晒单开箱")
        report = S.check_structure([row], config=CONFIG, stage="attributes")
        self.assertIn("label_outside_applicable_scope", _reasons(report, "ugc"))

    def test_pgc_row_with_na_passes(self):
        row = _full_ok(mode="PGC", ugc="N/A")
        report = S.check_structure([row], config=CONFIG, stage="attributes")
        self.assertEqual(report["status"], "pass")

    def test_ugc_outside_candidates(self):
        row = _full_ok(ugc="直播参与")
        report = S.check_structure([row], config=CONFIG, stage="attributes")
        self.assertIn("illegal_or_empty", _reasons(report, "ugc"))

    def test_dimension_outside_enum(self):
        row = _full_ok(dim="服务")
        report = S.check_structure([row], config=CONFIG, stage="attributes")
        self.assertIn("illegal_or_empty", _reasons(report, "dimension"))

    def test_irrelevant_row_dimension_must_be_na(self):
        row = _full_ok(relevant=False, ugc="N/A", dim="产品")
        report = S.check_structure([row], config=CONFIG, stage="attributes")
        self.assertIn("label_outside_applicable_scope", _reasons(report, "dimension"))

    def test_ugc_applicable_false_requires_na(self):
        """仅 PGC 项目：ugc_applicable=False 时相关 UGC 行也必须 N/A。"""
        cfg = dict(CONFIG, ugc_applicable=False)
        row = _full_ok(mode="UGC", ugc="晒单开箱")
        report = S.check_structure([row], config=cfg, stage="attributes")
        self.assertIn("label_outside_applicable_scope", _reasons(report, "ugc"))


class ConfigGuardTest(unittest.TestCase):
    def test_missing_positive_labels_raises(self):
        with self.assertRaisesRegex(ValueError, "positive_labels"):
            S.check_structure([], config={}, stage="positive")

    def test_missing_ugc_labels_raises(self):
        with self.assertRaisesRegex(ValueError, "ugc_labels"):
            S.check_structure([], config={}, stage="attributes")

    def test_max_opinion_labels_must_be_positive_int(self):
        with self.assertRaisesRegex(ValueError, "max_opinion_labels"):
            S.check_structure([], config=dict(CONFIG, max_opinion_labels=0),
                              stage="positive")

    def test_unknown_stage_raises(self):
        with self.assertRaisesRegex(ValueError, "未知 stage"):
            S.check_structure([], config=CONFIG, stage="nope")


class ReportShapeTest(unittest.TestCase):
    def test_errors_by_reason_counted(self):
        rows = [_full_ok("k0", dim="服务"), _full_ok("k1", dim="价格")]
        report = S.check_structure(rows, config=CONFIG, stage="attributes")
        self.assertEqual(report["errors_by_reason"].get("illegal_or_empty"), 2)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["rows"], 2)
        self.assertFalse(report["semantic_accuracy_checked"])


if __name__ == "__main__":
    unittest.main()
