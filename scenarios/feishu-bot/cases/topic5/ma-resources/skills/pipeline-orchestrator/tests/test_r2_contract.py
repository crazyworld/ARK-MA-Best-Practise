"""FIX-28：轮次二三任务拆分、适用范围与覆盖检查对齐原版 annot_r2.py。

原版依据：
  - annot_r2.py   不相关行三字段 N/A；中性行 opinion「无」；PGC 行 ugc N/A；
                  正面/负面各跑一个观点任务；属性任务覆盖全部相关行；
                  每批 15、重试 3；观点最多 2 个标签
  - annotation_prompt.md「UGC 类别清单」「观点候选清单」注入定义与示例
  - KS_Metadata_Structure.md:58/110  讨论对象三枚举

运行：python3 -m unittest tests.test_r2_contract
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import annotate as A  # noqa: E402

LABELS = ["口味好评", "价格讨论", "周边讨论"]
UGC = ["晒单开箱", "打卡参与", "二创内容"]


def _row(i=0, sentiment=1, relevant: bool | None = True, **extra):
    row = {"_dedup_key": f"k{i}", "title": f"标题{i}", "content": f"瑞幸联名内容{i}",
           "_platform": "xiaohongshu", "ai_sentiment": sentiment,
           "relevance": relevant}
    row.update(extra)
    return row


class PresetTest(unittest.TestCase):
    def test_irrelevant_rows_all_na_and_not_sent(self):
        rows = [_row(0, relevant=False), _row(1, relevant=None)]
        A._tag_ids(rows)
        groups = A._preset_r2_defaults(rows, ugc_not_applicable=False)
        for row in rows:
            self.assertEqual(row["opinion"], A._NA)
            self.assertEqual(row["ugc_category"], A._NA)
            self.assertEqual(row["discussion_dimension"], A._NA)
        self.assertEqual(groups["attr"], [])
        self.assertEqual(groups["opinion_pos"], [])

    def test_neutral_relevant_gets_none_opinion(self):
        rows = [_row(0, sentiment=0)]
        A._tag_ids(rows)
        groups = A._preset_r2_defaults(rows, ugc_not_applicable=False)
        self.assertEqual(rows[0]["opinion"], A.OPINION_NONE)
        self.assertEqual(groups["opinion_pos"], [])
        self.assertEqual(groups["opinion_neg"], [])
        # 中性行仍要进属性任务
        self.assertEqual(len(groups["attr"]), 1)

    def test_positive_and_negative_split(self):
        rows = [_row(0, sentiment=1), _row(1, sentiment=-1), _row(2, sentiment=0),
                _row(3, sentiment=1, relevant=False)]
        A._tag_ids(rows)
        groups = A._preset_r2_defaults(rows, ugc_not_applicable=False)
        self.assertEqual([r["_annot_id"] for r in groups["opinion_pos"]], ["k0"])
        self.assertEqual([r["_annot_id"] for r in groups["opinion_neg"]], ["k1"])
        self.assertEqual(len(groups["attr"]), 3)

    def test_pgc_only_project_presets_ugc_na(self):
        rows = [_row(0)]
        A._tag_ids(rows)
        A._preset_r2_defaults(rows, ugc_not_applicable=True)
        self.assertEqual(rows[0]["ugc_category"], A._NA)


class PromptTest(unittest.TestCase):
    def test_ugc_category_required_when_applicable(self):
        with self.assertRaisesRegex(ValueError, "禁止开始 UGC 方式标注"):
            A.annotate_round2([_row()], opinion_labels=LABELS,
                              ugc_categories=[], ugc_not_applicable=False)

    def test_pgc_only_project_needs_no_ugc_categories(self):
        res = A.annotate_round2([_row()], opinion_labels=LABELS,
                                ugc_categories=[], ugc_not_applicable=True)
        self.assertIn("ugc_category", res.get("not_applicable") or [])

    def test_opinion_prompt_is_topic_only(self):
        """观点任务只判断话题，不重复判情感（情感已由轮次一定）。"""
        prompts = A._r2_task_prompts("立白活动", LABELS, UGC, False, None, None)
        prompt = prompts["opinion"]
        self.assertIn("讨论话题", prompt)
        self.assertIn("不含情感", prompt)
        self.assertIn("2 个", prompt)
        self.assertIn("其他", prompt)

    def test_label_table_injects_definition_and_examples(self):
        items = [{"name": "口味好评", "definition": "评价口味口感",
                  "examples": ["这个味道绝了", "偏甜"]}]
        prompts = A._r2_task_prompts("立白活动", LABELS, UGC, False, items, None)
        self.assertIn("评价口味口感", prompts["opinion"])
        self.assertIn("这个味道绝了", prompts["opinion"])

    def test_pgc_only_prompt_says_na(self):
        prompts = A._r2_task_prompts("A", LABELS, [], True, None, None)
        self.assertIn("N/A", prompts["attr"])
        self.assertNotIn("ugc_category（UGC 参与方式）", prompts["attr"])


class ApplyTest(unittest.TestCase):
    def test_opinion_joined_with_comma(self):
        """原版用英文逗号连接，check_annotation_structure 按 "," 拆分。"""
        row = _row()
        A._apply_opinion(row, {"opinion": "口味好评,周边讨论"}, LABELS)
        self.assertEqual(row["opinion"], "口味好评,周边讨论")

    def test_opinion_duplicate_labels_left_for_backfill(self):
        """原版 check_annotation_structure:87 判 duplicate_labels；回填时拒收，交补标重试，
        不在程序里去重修源（job_436842fb74da n11：「X,X」进结构预检整节点失败）。"""
        for value in ("口味好评,口味好评", "口味好评，口味好评", "口味好评、 口味好评"):
            row = _row()
            A._apply_opinion(row, {"opinion": value}, LABELS)
            self.assertNotEqual(row.get("opinion"), "口味好评,口味好评", value)
            self.assertNotIn("口味好评,口味好评", str(row.get("opinion")))

    def test_duplicate_reject_matches_structure_gate(self):
        """回填接受的值必须能过结构预检：两处对 duplicate 的判定一致。"""
        import annotation_structure as S
        row = _row()
        A._apply_opinion(row, {"opinion": "口味好评,周边讨论"}, LABELS)
        labels = S._split_labels(row["opinion"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_opinion_over_two_labels_truncated(self):
        row = _row()
        A._apply_opinion(row, {"opinion": "口味好评,周边讨论,价格讨论"}, LABELS)
        self.assertEqual(row["opinion"], "口味好评,周边讨论")

    def test_opinion_outside_candidates_dropped(self):
        row = _row()
        A._apply_opinion(row, {"opinion": "包装好看"}, LABELS)
        self.assertNotIn("opinion", row)

    def test_opinion_other_accepted(self):
        row = _row()
        A._apply_opinion(row, {"opinion": "其他"}, LABELS)
        self.assertEqual(row["opinion"], "其他")

    def test_invalid_dimension_dropped(self):
        row = _row()
        A._apply_attr(row, {"discussion_dimension": "价格"}, UGC, use_ugc=True)
        self.assertNotIn("discussion_dimension", row)

    def test_ugc_other_accepted(self):
        row = _row(content_mode="UGC")
        A._apply_attr(row, {"ugc_category": "其他", "discussion_dimension": "品牌"},
                      UGC, use_ugc=True)
        self.assertEqual(row["ugc_category"], "其他")
        self.assertEqual(row["discussion_dimension"], "品牌")


class CoverageTest(unittest.TestCase):
    def _labeled(self, rows):
        for row in rows:
            row["opinion"] = "口味好评"
            row["ugc_category"] = "晒单开箱"
            row["discussion_dimension"] = "产品"
        return rows

    def test_scope_excludes_neutral_and_irrelevant(self):
        rows = self._labeled([_row(0, 1), _row(1, 0), _row(2, 1, relevant=False)])
        cov = A.check_coverage(rows, degraded=False)
        # 观点适用范围只有 1 行（正面且相关）
        self.assertEqual(cov["opinion"]["scope_total"], 1)
        # 属性适用范围是 2 行（相关）
        self.assertEqual(cov["discussion_dimension"]["scope_total"], 2)

    def test_opinion_other_blocking_threshold(self):
        rows = [_row(i, 1) for i in range(10)]
        for row in rows:
            row["opinion"] = "口味好评"
            row["ugc_category"] = "晒单开箱"
            row["discussion_dimension"] = "产品"
        rows[0]["opinion"] = "其他"  # 1/10 = 10% ≥ 阈值
        cov = A.check_coverage(rows, degraded=False)
        self.assertEqual(cov["opinion"]["status"], "ERROR")

    def test_ugc_other_warn_not_block(self):
        rows = [_row(i, 1, content_mode="UGC") for i in range(10)]
        for row in rows:
            row["opinion"] = "口味好评"
            row["ugc_category"] = "晒单开箱"
            row["discussion_dimension"] = "产品"
        for row in rows[:3]:  # 30% > 20%
            row["ugc_category"] = "其他"
        cov = A.check_coverage(rows, degraded=False)
        self.assertEqual(cov["ugc_category"]["status"], "WARN")
        self.assertTrue(cov["ugc_rediscovery_suggested"])
        self.assertFalse(cov["has_error"])

    def test_invalid_dimension_reports_values(self):
        rows = self._labeled([_row(0, 1)])
        rows[0]["discussion_dimension"] = "价格"
        cov = A.check_coverage(rows, degraded=False)
        self.assertEqual(cov["discussion_dimension"]["status"], "ERROR")
        self.assertIn("价格", cov["discussion_dimension"]["invalid_values"])

    def test_pgc_only_counts_all_relevant_as_na(self):
        rows = [_row(i, 1) for i in range(3)]
        for row in rows:
            row["opinion"] = "口味好评"
            row["ugc_category"] = A._NA
            row["discussion_dimension"] = "产品"
        cov = A.check_coverage(rows, degraded=False, ugc_not_applicable=True)
        self.assertEqual(cov["ugc_category"]["status"], "OK")
        self.assertEqual(cov["ugc_category"]["na"], 3)


class TaskRoutingTest(unittest.TestCase):
    def test_three_prompts_called_for_full_set(self):
        """正面/负面/属性各调用一次，且属性任务覆盖全部相关行。"""
        rows = [_row(0, 1), _row(1, -1), _row(2, 0)]
        A._tag_ids(rows)
        seen = {}

        def fake_chat(_system, user, **_kwargs):
            payload = json.loads(user)
            ids = [item["id"] for item in payload]
            if "ugc_category" in _system:
                seen["attr"] = ids
                return [{"id": i, "ugc_category": "晒单开箱",
                         "discussion_dimension": "产品"} for i in ids]
            seen.setdefault("opinion", []).extend(ids)
            return [{"id": i, "opinion": "口味好评"} for i in ids]

        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=fake_chat):
            res = A.annotate_round2(rows, opinion_labels=LABELS,
                                    ugc_categories=UGC, campaign_name="立白活动")

        self.assertEqual(sorted(seen["opinion"]), ["k0", "k1"])
        self.assertEqual(sorted(seen["attr"]), ["k0", "k1", "k2"])
        self.assertEqual(res["tasks"]["opinion_positive"], 1)
        self.assertEqual(res["tasks"]["opinion_negative"], 1)
        self.assertEqual(res["tasks"]["attribute"], 3)
        self.assertFalse(res["coverage"]["has_error"])
        self.assertEqual(rows[2]["opinion"], A.OPINION_NONE)


if __name__ == "__main__":
    unittest.main()
