"""第 1 批：n11→n12 与原版对齐（审计 P0 1-4 + 关联 P1），回归 job_bf3c6b52db7b。

原版依据：
  - annot_r2.py:84/94      观点、UGC 均允许「其他」；:107/111 只在 UGC 适用时取类别，其余 N/A
  - annot_r2.py:72-76      某方向 status!=ok（<10 条）→ 观点填「无」，不标注
  - check_coverage.py:194  UGC 行 ugc_category 为空或 N/A 即缺失；非 UGC 行应为 N/A
  - check_coverage.py:217  相关行 discussion_dimension 不在三枚举（含 N/A）即缺失
  - KS_Annotation_Execution_Contract §3「个别错误修对应行」：其余行保留原标注
  - 词云来自外部 SSJH，与标注无关；返工不应让它失效（n13 不会再跑）
  - 执行契约 §2「每活动、每轮…最多500」：轮次一、轮次二额度分开
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import annotate as A  # noqa: E402
import annotation_structure as S  # noqa: E402
import datahub  # noqa: E402
import handlers  # noqa: E402
import semantic  # noqa: E402
from state import RunState, register_run_dir  # noqa: E402

POS, NEG, UGC = ["口感风味"], ["价格偏高"], ["晒单分享"]


def _row(i, senti=1, mode="UGC", rel=True):
    return {"_dedup_key": f"k{i}", "title": "t", "content": f"c{i}", "relevance": rel,
            "ai_sentiment": senti, "content_mode": mode}


def _llm(system, user, **_kw):
    items = json.loads(user)
    if "discussion_dimension" in system:
        return [{"id": i["id"], "ugc_category": "其他", "discussion_dimension": "活动"}
                for i in items]
    return [{"id": i["id"], "opinion": "其他"} for i in items]


def _run_r2(rows, *, llm=_llm, negative_labels=NEG, **kw):
    with patch.object(datahub, "api_available", return_value=False), \
         patch.object(semantic, "llm_available", return_value=True), \
         patch.object(semantic, "chat_json", side_effect=llm) as chat:
        res = A.annotate_round2(rows, opinion_labels=POS, negative_labels=negative_labels,
                                ugc_categories=UGC, threshold=1000, campaign_name="活动", **kw)
    return res, chat


class StructureGateAcceptsOtherTest(unittest.TestCase):
    """P0-1：结构门必须认「其他」（原版 annot_r2 允许，check_coverage 统计其占比）。"""

    def test_round2_output_with_other_passes_structure_gate(self):
        rows = [_row(0, 1), _row(1, -1), _row(2, 0)]
        res, _ = _run_r2(rows)
        cfg = handlers._structure_config(opinion_labels=POS, negative_labels=NEG,
                                         ugc_categories=UGC)
        report = S.check_structure(res["rows"], config=cfg, stage="full")
        self.assertEqual(report["status"], "pass", report["errors"][:5])


class NonUgcRowsTest(unittest.TestCase):
    """P0-2：非 UGC 行 ugc_category 预填 N/A，不要求模型给类别。"""

    def test_pgc_and_unknown_rows_get_na_and_pass(self):
        rows = [_row(0, 1, "UGC"), _row(1, 1, "PGC"), _row(2, 0, "UKN")]

        def llm(system, user, **kw):
            out = _llm(system, user, **kw)
            if "discussion_dimension" not in system:
                out = [{"id": o["id"], "opinion": POS[0]} for o in out]
            return out

        res, _ = _run_r2(rows, llm=llm)
        by = {r["_annot_id"]: r for r in res["rows"]}
        self.assertEqual(by["k1"]["ugc_category"], "N/A")
        self.assertEqual(by["k2"]["ugc_category"], "N/A")
        self.assertEqual(by["k0"]["ugc_category"], "其他")
        self.assertFalse(res["coverage"]["has_error"], res["coverage"])
        self.assertEqual(res["coverage"]["ugc_category"]["scope_total"], 1)
        cfg = handlers._structure_config(opinion_labels=POS, negative_labels=NEG,
                                         ugc_categories=UGC)
        self.assertEqual(S.check_structure(res["rows"], config=cfg, stage="full")["status"],
                         "pass")

    def test_model_category_on_pgc_row_is_ignored(self):
        rows = [_row(1, 0, "PGC")]

        def llm(system, user, **_kw):
            return [{"id": i["id"], "ugc_category": "晒单分享", "discussion_dimension": "品牌"}
                    for i in json.loads(user)]

        res, _ = _run_r2(rows, llm=llm)
        self.assertEqual(res["rows"][0]["ugc_category"], "N/A")


class CoverageCountsNaAsMissingTest(unittest.TestCase):
    """P1：UGC 行类别 N/A、相关行维度 N/A 都算缺失（原版 check_coverage:194/217）。"""

    def test_dimension_na_is_missing(self):
        rows = [_row(0, 0, "UGC")]
        A._tag_ids(rows)
        rows[0].update(opinion="无", ugc_category="晒单分享", discussion_dimension="N/A")
        cov = A.check_coverage(rows)
        self.assertEqual(cov["discussion_dimension"]["missing"], 1)
        self.assertTrue(cov["has_error"])

    def test_ugc_row_category_na_is_missing(self):
        rows = [_row(0, 0, "UGC")]
        A._tag_ids(rows)
        rows[0].update(opinion="无", ugc_category="N/A", discussion_dimension="品牌")
        cov = A.check_coverage(rows)
        self.assertEqual(cov["ugc_category"]["missing"], 1)

    def test_model_dimension_na_not_accepted(self):
        row = {}
        A._apply_attr(row, {"ugc_category": "晒单分享", "discussion_dimension": "N/A"},
                      UGC, True)
        self.assertNotIn("discussion_dimension", row)


class NegativeNotApplicableTest(unittest.TestCase):
    """P1：方向不适用（候选为空）→ 观点填「无」，不借用另一方向标签。"""

    def test_empty_negative_labels_fill_none_without_calls(self):
        rows = [_row(0, -1)]
        res, chat = _run_r2(rows, negative_labels=[])
        self.assertEqual(res["rows"][0]["opinion"], "无")
        sent = [c.args[0] for c in chat.call_args_list]
        self.assertFalse(any("负面" in s and "候选标签" in s for s in sent))
        self.assertFalse(res["coverage"]["has_error"])
        cfg = handlers._structure_config(opinion_labels=POS, negative_labels=[],
                                         ugc_categories=UGC)
        self.assertEqual(S.check_structure(res["rows"], config=cfg, stage="full")["status"],
                         "pass")

    def test_confirmed_labels_do_not_borrow_positive(self):
        src = open(handlers.__file__, encoding="utf-8").read()
        self.assertNotIn('list(opinion.get("negative_candidates") or labels)', src)


class IncrementalReworkTest(unittest.TestCase):
    """P0-3：增量重标以上一版标注为底，只覆盖 rerun_ids。"""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        run_dir = Path(self.ws) / "run"
        for sub in ("artifacts", "attempts", "validation"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        register_run_dir("rx", run_dir)
        self.st = RunState(run_id="rx", task_id="t", dag_slug="s", dag_version="0.2",
                           entry="n11_annotate_r2", params={}, current_node="n11_annotate_r2",
                           frontier=[])

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_round2_rework_keeps_other_rows(self):
        st = self.st
        base = [_row(0, 1), _row(1, 1), _row(2, 0)]
        A._tag_ids(base)
        st.write_artifact("annotated_round1", {"rows": [dict(r) for r in base]})
        labeled = [dict(r) for r in base]
        for r in labeled:
            r.update(ugc_category="晒单分享", discussion_dimension="产品")
        labeled[0]["opinion"] = "口感风味"
        labeled[1]["opinion"] = "口感风味"
        labeled[2]["opinion"] = "无"
        st.write_artifact("annotated_data", {"rows": labeled})
        st.write_artifact("confirmed_opinion_labels", {
            "labels": POS, "positive_labels": POS, "negative_labels": NEG,
            "ugc_categories": UGC})
        st.write_artifact("ugc_category_list", {"categories": UGC})
        st.write_artifact("r2_review", {"wrong_ids": {"opinion": ["k1"]},
                                        "error_types": {"opinion": "local"}})
        out = handlers.annotation_rework({"id": "n11r_rework",
                                          "outputs": ["annotation_rework_plan"]}, st, {})
        for k, v in out["artifacts"].items():
            st.write_artifact(k, v)
        touched = []

        def llm(system, user, **_kw):
            items = json.loads(user)
            touched.extend(i["id"] for i in items)
            return [{"id": i["id"], "opinion": "口感风味", "ugc_category": "晒单分享",
                     "discussion_dimension": "活动"} for i in items]

        with patch.object(datahub, "api_available", return_value=False), \
             patch.object(semantic, "llm_available", return_value=True), \
             patch.object(semantic, "chat_json", side_effect=llm):
            res = handlers.annotate({"id": "n11_annotate_r2", "outputs": ["annotated_data"]},
                                    st, {})
        rows = {r["_annot_id"]: r for r in res["artifacts"]["annotated_data"]["rows"]}
        self.assertEqual(set(touched), {"k1"})
        self.assertEqual(rows["k0"]["opinion"], "口感风味")
        self.assertEqual(rows["k1"]["discussion_dimension"], "活动")
        self.assertEqual(rows["k2"]["discussion_dimension"], "产品")


class ReworkKeepsWordcloudTest(unittest.TestCase):
    """P0-4：返工不让 wordcloud_data 失效（只有 n02 扇出 n13，失效后 n12 永远等不到）。"""

    def test_wordcloud_not_in_rework_invalidate_lists(self):
        src = open(handlers.__file__, encoding="utf-8").read()
        start = src.index('def annotation_rework(')
        end = src.find("\n@handler", start)
        body = src[start:end if end != -1 else len(src)]
        self.assertNotIn('"wordcloud_data"', body)
        rollback = src[src.index("NOISE_ROLLBACK_INVALIDATE = ["):]
        rollback = rollback[:rollback.index("]")]
        self.assertNotIn("wordcloud_data", rollback)


class DataHubErrorMessageTest(unittest.TestCase):
    """P2：DataHub 异常带出原文，不只留类型名。"""

    def test_route_required_carries_error_text(self):
        rows = [_row(i, 1) for i in range(3)]

        def boom(*_a, **_k):
            raise RuntimeError("task 42 failed: quota exceeded")

        with patch.object(datahub, "api_available", return_value=True), \
             patch.object(datahub, "batch_annotate", side_effect=boom):
            with self.assertRaises(A.DataHubRouteRequired) as raised:
                A.annotate_round2(rows, opinion_labels=POS, negative_labels=NEG,
                                  ugc_categories=UGC, threshold=1, campaign_name="活动")
        self.assertIn("quota exceeded", str(raised.exception))


class ReviewQuotaPerStageTest(unittest.TestCase):
    """P1：轮次一、轮次二各有 500 额度，台账按轮次分开。"""

    def test_round2_plan_ignores_round1_reviewed_ids(self):
        src = open(handlers.__file__, encoding="utf-8").read()
        body = src[src.index("def annotation_review(node"):]
        body = body[:body.index("\n@handler", 10)]
        self.assertIn("_stage_reviewed_ids(", body)
        history = {"by_stage": {"round1": [f"a{i}" for i in range(400)]}}
        self.assertEqual(handlers._stage_reviewed_ids(history, "round2"), [])
        self.assertEqual(len(handlers._stage_reviewed_ids(history, "round1")), 400)
        # 旧格式（未分轮次）只算作轮次一
        self.assertEqual(handlers._stage_reviewed_ids({"reviewed_ids": ["x"]}, "round2"), [])


if __name__ == "__main__":
    unittest.main()
