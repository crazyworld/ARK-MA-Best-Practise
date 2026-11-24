"""B6 统计口径专项（FIX-18 / FIX-21 / FIX-22 / FIX-23）。

原版依据：
  - blueai-stats-aggregation-skill/scripts/aggregate.py（Tier2=主品牌名、大小写不敏感、
    ≥95% 退化；stats_scope；情感缺失 5%）
  - content_stats.py（观点按 [、/|;；,，] 拆分、「其他」单列、并列按互动、≤3 条代表原文）
  - KS_Feishu_Format.md:271-276（漏斗表 层级/声量/占比，Tier3 写明分母）
  - KS_Report_Template.md:187（2.N.1 口径注原文）、:523（热门表内容 ≤50 字）
  - 检查清单 4.3（漏斗退化 / 分母 / 主对象方法）

运行：python3 -m unittest tests.test_stats_b6
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import excel_source  # noqa: E402
import handlers  # noqa: E402
import mention_funnel as MF  # noqa: E402
import report_contract as RC  # noqa: E402
import review_plan  # noqa: E402
import validator  # noqa: E402


def _row(title="", content="", **extra):
    row = {"title": title, "content": content, "keywords": []}
    row.update(extra)
    return row


class KeywordFunnelTest(unittest.TestCase):
    def test_case_insensitive_brand(self):
        funnel = MF.keyword_funnel([_row(content="luckin coffee"), _row(content="无关")],
                                   "Luckin", [])
        self.assertEqual(funnel["tier2"], 1)

    def test_only_main_brand_name_not_aliases(self):
        """原版 --brand-name「仅匹配主品牌名，不用别名集合」。"""
        funnel = MF.keyword_funnel([_row(content="瑞幸咖啡"), _row(content="luckin")],
                                   "瑞幸", [])
        self.assertEqual(funnel["tier2"], 1)

    def test_tier3_nested_in_tier2(self):
        rows = [_row(content="瑞幸 生椰"), _row(content="生椰"), _row(content="瑞幸"),
                _row(content="x"), _row(content="y")]
        funnel = MF.keyword_funnel(rows, "瑞幸", ["生椰"])
        self.assertEqual((funnel["tier1"], funnel["tier2"], funnel["tier3"]), (5, 2, 1))
        self.assertEqual(funnel["product_in_tier1"], 2)
        self.assertEqual(funnel["tier3_in_tier2_ratio"], 0.5)
        self.assertEqual(funnel["tier3_in_tier1_ratio"], 0.2)

    def test_degraded_at_95_percent(self):
        rows = [_row(content="瑞幸") for _ in range(96)] + [_row(content="x") for _ in range(4)]
        funnel = MF.keyword_funnel(rows, "瑞幸", [])
        self.assertTrue(funnel["degraded"])
        self.assertEqual(MF.headline_metrics(funnel), {"品牌提及率": None, "产品提及占比": None})
        rows = [_row(content="瑞幸") for _ in range(94)] + [_row(content="x") for _ in range(6)]
        self.assertFalse(MF.keyword_funnel(rows, "瑞幸", [])["degraded"])

    def test_no_brand_name_only_tier1(self):
        funnel = MF.keyword_funnel([_row(content="瑞幸")], "", ["生椰"])
        self.assertIsNone(funnel["tier2"])
        self.assertIsNone(MF.chart_levels(funnel))
        self.assertEqual(MF.table_rows(funnel), [["活动总声量（Tier 1）", "1 条", "100%"]])

    def test_passive_only_counted_separately(self):
        rows = [_row(content="好喝", video_cover_text="瑞幸新品"), _row(content="瑞幸")]
        funnel = MF.keyword_funnel(rows, "瑞幸", [])
        self.assertEqual(funnel["tier2"], 1)
        self.assertEqual(funnel["passive_only"], 1)
        self.assertIn("被动带出", MF.method_note(funnel))

    def test_topic_tag_counts_as_active(self):
        funnel = MF.keyword_funnel([_row(content="好喝", keywords=["瑞幸"])], "瑞幸", [])
        self.assertEqual(funnel["tier2"], 1)


class FunnelPresentationTest(unittest.TestCase):
    def _funnel(self):
        rows = [_row(content="瑞幸 生椰")] * 25 + [_row(content="瑞幸")] * 484 \
            + [_row(content="x")] * 5800
        return MF.keyword_funnel(rows, "瑞幸", ["生椰"])

    def test_tier3_row_states_denominator(self):
        rows = MF.table_rows(self._funnel())
        self.assertEqual([r[0] for r in rows],
                         ["活动总声量（Tier 1）", "提及品牌（Tier 2）", "提及产品（Tier 3）"])
        self.assertRegex(rows[2][2], r"品牌内容中的产品提及占比 .*（Tier3 \d+ / Tier2 \d+）")
        self.assertIn("（Tier3 25 / Tier2 509）", rows[2][2])
        self.assertIn("占活动总量", rows[2][2])

    def test_degraded_chart_two_levels_without_tier2(self):
        rows = [_row(content="瑞幸 生椰")] * 10 + [_row(content="瑞幸")] * 90
        funnel = MF.keyword_funnel(rows, "瑞幸", ["生椰"])
        levels = MF.chart_levels(funnel)
        assert levels is not None
        chart_type, cats, vals = levels
        self.assertEqual((chart_type, cats, vals), ("funnel", ["活动相关", "产品提及"], [100, 10]))
        self.assertFalse(any("Tier 2" in r[0] for r in MF.table_rows(funnel)))

    def test_main_discussion_is_bar_not_funnel(self):
        rows = [_row(discussion_dimension="品牌"), _row(discussion_dimension="产品"),
                _row(discussion_dimension="活动"), _row(discussion_dimension="品牌")]
        funnel = MF.build(rows, method=MF.MAIN_DISCUSSION, brand_name="瑞幸", product_words=[])
        levels = MF.chart_levels(funnel)
        assert levels is not None
        self.assertEqual(levels[0], "rank_bar")
        self.assertFalse(funnel["degraded"])
        self.assertEqual(funnel["groups"][0], {"label": "品牌主导讨论", "count": 2, "share": 0.5})
        self.assertIn("不构成递进漏斗", MF.method_note(funnel))


class OpinionAnalysisTest(unittest.TestCase):
    @staticmethod
    def _rows(labels, sentiment=1):
        return [{"opinion": label, "ai_sentiment": sentiment, "content": f"正文{i}",
                 "interaction_cnt": i} for i, label in enumerate(labels)]

    def _analyse(self, rows):
        return RC.opinion_analysis(rows, lambda r: r.get("ai_sentiment"))

    def test_multi_label_counted_separately(self):
        rows = self._rows(["价格,包装"] + ["价格"] * 9)
        top = {t["label"]: t["count"] for t in self._analyse(rows)["positive"]["top5"]}
        self.assertEqual(top, {"价格": 10, "包装": 1})

    def test_other_not_ranked(self):
        rows = self._rows(["其他"] * 5 + ["价格"] * 5)
        item = self._analyse(rows)["positive"]
        self.assertNotIn("其他", [t["label"] for t in item["top5"]])
        self.assertEqual(item["other"]["count"], 5)
        self.assertTrue(item["other_warning"])          # 50% > 20%

    def test_tie_broken_by_interaction(self):
        rows = self._rows(["A"] * 5 + ["B"] * 5)        # B 行互动更高
        self.assertEqual(self._analyse(rows)["positive"]["top5"][0]["label"], "B")

    def test_under_ten_not_extracted(self):
        item = self._analyse(self._rows(["价格"] * 9))["positive"]
        self.assertEqual(item["top5"], [])
        self.assertEqual(item["status"], "insufficient_sample")
        text = RC.opinion_text({"opinion_analysis": {"positive": item, "negative": {}}})
        self.assertIn("正面内容仅 9 条，不做观点提取", text)
        self.assertNotIn("方向性参考", text)

    def test_representatives_deduplicated(self):
        rows = [{"opinion": "价格", "ai_sentiment": 1, "content": "一样的正文", "interaction_cnt": i}
                for i in range(10)]
        top = self._analyse(rows)["positive"]["top5"][0]
        self.assertEqual(len(top["representatives"]), 1)

    def test_share_sum_may_exceed_100(self):
        item = self._analyse(self._rows(["A,B"] * 10))["positive"]
        self.assertGreater(sum(t["share"] for t in item["top5"]), 1)


class UgcAndScopeTest(unittest.TestCase):
    def test_ugc_denominator_relevant_only(self):
        rows = [{"content_mode": "UGC", "ugc_category": "晒单分享", "relevance": True},
                {"content_mode": "UGC", "ugc_category": "晒单分享", "relevance": False}]
        result = RC.ugc_analysis(rows, lambda r: r["relevance"])
        self.assertEqual(result["sample_size"], 1)
        big = {"sample_size": 12, "categories": [{"label": "晒单分享", "count": 12, "share": 1.0}]}
        self.assertIn("占比以活动相关 UGC（12 条）为分母", RC.ugc_text({"ugc_analysis": big}))

    def test_scope_note_text(self):
        note = RC.scope_note({"stats_scope": {"raw": 100, "excluded": 20, "kept": 80,
                                              "irrelevant_review_required": False}})
        assert note is not None
        self.assertIn("监测检索原始 100 条，剔除 20 条，保留 80 条", note)
        self.assertIsNone(RC.scope_note({}))

    def test_hot_excerpt_50_chars_with_ellipsis(self):
        self.assertEqual(len(RC.hot_excerpt({"content": "字" * 80})), 51)
        self.assertTrue(RC.hot_excerpt({"content": "字" * 80}).endswith("…"))
        self.assertEqual(RC.hot_excerpt({"content": "短"}), "短")

    def test_hot_columns_from_policy(self):
        self.assertEqual(RC.PGC_COLUMNS[2], "内容")
        self.assertNotIn("作者", RC.UGC_COLUMNS)
        self.assertNotIn("URL", RC.PGC_COLUMNS)


class IrrelevantReviewPlanTest(unittest.TestCase):
    def _rows(self, irrelevant, total=200):
        return [{"_annot_id": f"r{i}", "relevance": i >= irrelevant, "ai_sentiment": 1}
                for i in range(total)]

    def test_targeted_when_over_10_percent(self):
        plan = review_plan.plan_review(self._rows(60), stage="round1")
        self.assertTrue(plan["irrelevant_check"]["required"])
        picked = [s for s in plan["samples"] if int(s["id"][1:]) < 60]
        self.assertGreaterEqual(len(picked), review_plan.IRRELEVANT_REVIEW_SAMPLE)
        ids = [s["id"] for s in plan["samples"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_not_triggered_under_10_percent(self):
        plan = review_plan.plan_review(self._rows(10), stage="round1")
        self.assertFalse(plan["irrelevant_check"]["required"])
        self.assertFalse(any(s["sample_kind"] == "targeted_irrelevant" for s in plan["samples"]))

    def test_only_round1_targets(self):
        plan = review_plan.plan_review(self._rows(60), stage="attributes",
                                       config={"ugc_labels": ["晒单分享"]})
        self.assertFalse(plan["irrelevant_check"]["required"])


class FunnelValidatorTest(unittest.TestCase):
    """检查清单 4.3：按 stats 的漏斗方法与退化状态判定。"""

    def _scan(self, body, funnel):
        md = f"# 报告\n\n## 2.1 活动\n\n### 2.1.2 提及\n\n{body}\n"
        scan = validator._load_scan()
        assert scan
        stats = {"campaigns": {"A": {"mention_funnel": funnel}}}
        return [i["id"] for i in scan.scan_markdown(md, stats=stats)["issues"]
                if "funnel" in i["id"]]

    def test_degraded_but_tier2_presented(self):
        funnel = {"method": "keyword_mention", "tier1": 10, "tier2": 10, "degraded": True,
                  "tier2_ratio": 1.0}
        self.assertEqual(self._scan("品牌提及率 100%", funnel), ["P1-funnel-degraded-A"])
        self.assertEqual(self._scan("活动相关 10 条", funnel), [])

    def test_missing_denominator(self):
        funnel = {"method": "keyword_mention", "tier1": 100, "tier2": 50, "tier3": 5,
                  "degraded": False, "product_keywords": ["生椰"]}
        self.assertEqual(self._scan("产品提及占比 10%", funnel), ["P1-funnel-denominator-A"])
        ok = "品牌内容中的产品提及占比 10.0%（Tier3 5 / Tier2 50），占活动总量 5.0%"
        self.assertEqual(self._scan(ok, funnel), [])

    def test_missing_product_layer_p0(self):
        funnel = {"method": "keyword_mention", "tier1": 100, "tier2": 50, "tier3": None,
                  "degraded": False, "product_keywords": ["生椰"]}
        self.assertEqual(self._scan("x", funnel), ["P0-funnel-missing-product-A"])

    def test_main_discussion_written_as_funnel(self):
        funnel = {"method": "main_discussion", "tier1": 10, "degraded": False, "groups": []}
        self.assertEqual(self._scan("Tier 2 品牌主导", funnel), ["P0-funnel-main-discussion-A"])

    def test_assembled_table_passes_scan(self):
        """组装出的漏斗表本身必须通过 4.3（防止组装与质检口径漂移）。"""
        rows = [_row(content="瑞幸 生椰")] * 5 + [_row(content="瑞幸")] * 45 + [_row(content="x")] * 50
        funnel = MF.keyword_funnel(rows, "瑞幸", ["生椰"])
        body = RC.table(["层级", "声量", "占比"], MF.table_rows(funnel))
        self.assertEqual(self._scan(body, funnel), [])
        self.assertTrue(re.search(r"\| 层级 \| 声量 \| 占比 \|", body))


class SponsorShareTest(unittest.TestCase):
    """FIX-24：B/A、C/A 复用 n13 三次查询总量（KS_Classification_Rules 2.N.2）。"""

    @staticmethod
    def _wc(a, b, c):
        return {dim: {"raw_source": {"total_volume": v}}
                for dim, v in (("overall", a), ("brand", b), ("product", c))}

    def test_ratios(self):
        share = handlers._sponsor_share(self._wc(1000, 300, 120))
        self.assertEqual((share["brand_share"], share["product_share"]), (0.3, 0.12))

    def test_missing_overall_unavailable(self):
        self.assertEqual(handlers._sponsor_share({})["status"], "unavailable")

    def test_sponsor_type_detection(self):
        self.assertTrue(handlers._is_sponsor_type({"campaign_type": "赛事赞助或合作"}))
        self.assertFalse(handlers._is_sponsor_type({"campaign_type": "IP联名"}))
        # Q1：类型待确认时不从数据推断，赞助型模块不执行。
        self.assertFalse(handlers._is_sponsor_type({"campaign_type": "待数据推断"}))

    def test_aggregate_only_for_sponsor(self):
        import tempfile
        from _util import make_state
        for ctype, expected in (("赛事赞助或合作", True), ("IP联名", False)):
            st = make_state(tempfile.mkdtemp())
            st.write_artifact("annotated_data", {"rows": [
                {"relevance": True, "ai_sentiment": 1, "interaction_cnt": 1}]})
            st.write_artifact("confirmed_plan", {"campaign_type": ctype, "activity_name": "A"})
            st.write_artifact("wordcloud_data", {"campaigns": {"A": self._wc(1000, 300, 120)}})
            bundle = handlers.aggregate({"id": "n12"}, st, {})["artifacts"]["stats_bundle"]
            camp = bundle["campaigns"]["A"]
            self.assertEqual("sponsor_share" in camp, expected, ctype)
            if expected:
                self.assertIn("品牌声量占比 B÷A", RC.sponsor_text(camp))


class UgcDepthRemovedTest(unittest.TestCase):
    """缺陷清单 D5：固定四类参与深度规则已删除，不从参与类别推出口碑/深度评分。"""

    def test_no_depth_rules(self):
        self.assertFalse(hasattr(RC, "ugc_depth"))
        self.assertFalse(hasattr(RC, "DEPTH_RULES"))


class AdAndDuplicateTest(unittest.TestCase):
    """FIX-26：广告标识单列；零粉丝重复内容只标记不剔除。"""

    def test_excel_ad_column(self):
        self.assertTrue(excel_source._ad_flag({"是否广告": "是"}))
        self.assertFalse(excel_source._ad_flag({"是否广告": "否"}))
        self.assertIsNone(excel_source._ad_flag({}))

    def test_natural_vs_ad_counts(self):
        rows = [{"is_ad": True}, {"is_ad": 0}, {"is_ad": False}, {}]
        self.assertEqual(handlers._natural_vs_ad(rows), {"natural": 2, "ad": 1, "unknown": 1})

    def test_zero_fan_duplicates_flagged_not_removed(self):
        rows = [{"id": str(i), "fans": 0, "author": "x", "content": "同一文案"} for i in range(6)]
        rows.append({"id": "k", "fans": 10, "author": "x", "content": "同一文案"})
        found = handlers._zero_fan_duplicates(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["count"], 6)
        self.assertEqual(found[0]["status"], "uncertain")
        self.assertEqual(len(rows), 7)

    def test_unknown_fans_not_treated_as_zero(self):
        rows = [{"id": str(i), "fans": None, "author": "x", "content": "同"} for i in range(6)]
        self.assertEqual(handlers._zero_fan_duplicates(rows), [])


if __name__ == "__main__":
    unittest.main()
