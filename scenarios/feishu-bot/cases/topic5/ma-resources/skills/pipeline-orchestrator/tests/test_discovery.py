"""FIX-31/32：类别发现的抽样、分向、不适用判定、命名约束（对齐原版 discovery run.py）。

原版依据：
  - blueai-opinion-discovery-skill/scripts/run.py：draw_sample（互动 Top50% + 随机50%，
    seed=42）、sample_size=200、min_posts=10、正面 5-8 / 负面 3-5、
    SENTIMENT_WORDS / EVAL_SUFFIX / CATCHALL_NAMES、lint 只告警不中断
  - blueai-phase-ugc-discovery-skill/scripts/run.py：样本=活动相关且 UGC、150 条、3-6 类
  - pipeline_config.yaml phase_discovery：per_campaign=true（逐活动，禁止混池）

运行：python3 -m unittest tests.test_discovery
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import label_review as L  # noqa: E402
from handlers import HumanPause  # noqa: E402
from _util import make_state  # noqa: E402


def _post(i, interaction=0, **extra):
    row = {"id": f"p{i}", "_dedup_key": f"d{i}", "title": f"标题{i}",
           "content": f"正文{i}", "interaction_cnt": interaction}
    row.update(extra)
    return row


class MixedSampleTest(unittest.TestCase):
    def test_top_half_is_highest_interaction(self):
        pool = [_post(i, interaction=i) for i in range(1000)]
        picked, sampling = L.draw_mixed_sample(pool, 200, key=lambda r: r["interaction_cnt"])
        top = picked[:100]
        self.assertEqual(sorted(r["interaction_cnt"] for r in top),
                         list(range(900, 1000)))
        self.assertEqual(sampling["mode"], "mixed")
        self.assertEqual(sampling["top_n"], 100)
        self.assertEqual(sampling["random_n"], 100)
        self.assertEqual(sampling["seed"], 42)

    def test_random_half_reproducible_and_disjoint(self):
        pool = [_post(i, interaction=i) for i in range(1000)]
        key = lambda r: r["interaction_cnt"]
        a, _ = L.draw_mixed_sample(pool, 200, key=key)
        b, _ = L.draw_mixed_sample(pool, 200, key=key)
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])
        top_ids = {r["id"] for r in a[:100]}
        rand_ids = {r["id"] for r in a[100:]}
        self.assertFalse(top_ids & rand_ids)

    def test_small_pool_takes_all(self):
        pool = [_post(i) for i in range(30)]
        picked, sampling = L.draw_mixed_sample(pool, 200)
        self.assertEqual(len(picked), 30)
        self.assertEqual(sampling["mode"], "all")


class SourceCatalogTest(unittest.TestCase):
    def test_excerpt_covers_title_200_and_content_800(self):
        row = _post(0, title="题" * 500, content="文" * 2000)
        samples, catalog, sampling = L.source_catalog([row])
        title_quotes = [q for q in catalog.values() if q["source_field"] == "title"]
        content_quotes = [q for q in catalog.values() if q["source_field"] == "content"]
        self.assertEqual(len(title_quotes[0]["excerpt"]), 200)
        self.assertEqual(sum(len(q["excerpt"]) for q in content_quotes), 800)
        self.assertEqual(max(q["end"] for q in content_quotes), 800)

    def test_quotes_are_exact_source_slices(self):
        row = _post(0, content="甲乙丙丁" * 100)
        _, catalog, _ = L.source_catalog([row])
        for quote in catalog.values():
            source = row[quote["source_field"]]
            self.assertEqual(quote["excerpt"], source[quote["start"]:quote["end"]])

    def test_sampling_reports_pool_and_sampled(self):
        rows = [_post(i, interaction=i) for i in range(300)]
        samples, _, sampling = L.source_catalog(rows, 200)
        self.assertEqual(sampling["pool_size"], 300)
        self.assertEqual(sampling["sampled"], 200)
        self.assertEqual(len(samples), 200)


class NamingLintTest(unittest.TestCase):
    def test_tendency_word_detected(self):
        self.assertEqual(L.tendency_hits("包装好评"), ["好评"])
        self.assertEqual(L.tendency_hits("包装设计"), [])

    def test_eval_suffix_detected(self):
        self.assertIn("评价后缀", L.tendency_hits("价格贵"))
        self.assertIn("评价后缀", L.tendency_hits("口感好"))

    def test_ugc_tendency_names(self):
        for name in ("吐槽避雷", "种草安利", "好评分享"):
            with self.subTest(name=name):
                self.assertTrue(L.tendency_hits(name))

    def test_lint_count_and_catchall(self):
        warns = L.lint_categories(["口感风味", "其他"], "positive")
        self.assertTrue(any("兜底类" in w for w in warns))
        self.assertTrue(any("要求 5-8" in w for w in warns))

    def test_negative_range_is_3_to_5(self):
        self.assertEqual(L.lint_categories(["门店排队", "价格优惠", "联名周边"], "negative"), [])

    def test_prompt_by_direction(self):
        pos = L.discovery_prompt("candidates", kind="positive", campaign_name="A")
        neg = L.discovery_prompt("candidates", kind="negative", campaign_name="A")
        ugc = L.discovery_prompt("categories", kind="ugc", campaign_name="A")
        self.assertIn("正面内容样本", pos)
        self.assertIn("5-8", pos)
        self.assertIn("负面内容样本", neg)
        self.assertIn("3-5", neg)
        self.assertIn("3-6", ugc)
        self.assertIn("2-4字", ugc)
        # 原版删掉了「覆盖正面、负面和属性讨论」这一与「类别不带倾向」冲突的要求
        self.assertNotIn("覆盖实际出现的正面、负面和属性讨论", pos)
        self.assertIn("MECE", pos)


def _r1_rows(pos=0, neg=0, ugc=0, campaign="", irrelevant=0):
    rows = []
    for i in range(pos):
        rows.append(_post(f"pos{i}", ai_sentiment=1, relevance=True, content_mode="PGC",
                          analysis_object=campaign))
    for i in range(neg):
        rows.append(_post(f"neg{i}", ai_sentiment=-1, relevance=True, content_mode="PGC",
                          analysis_object=campaign))
    for i in range(ugc):
        rows.append(_post(f"ugc{i}", ai_sentiment=0, relevance=True, content_mode="UGC",
                          analysis_object=campaign))
    for i in range(irrelevant):
        rows.append(_post(f"irr{i}", ai_sentiment=1, relevance=False, content_mode="UGC",
                          analysis_object=campaign))
    return rows


def _fake_discovery(calls):
    """按 prompt 方向返回 grounded 类别；记录每次调用的方向与样本 post 数。"""
    def fake(system, user, **_kw):
        samples = json.loads(user)
        quote = samples[0]["quotes"][0]["quote_id"]
        if "UGC样本" in system:
            kind, key, n = "ugc", "categories", 3
        elif "负面内容样本" in system:
            kind, key, n = "negative", "candidates", 3
        else:
            kind, key, n = "positive", "candidates", 5
        calls.append((kind, len(samples)))
        return {key: [{
            "name": f"{kind}类别{i}", "definition": "定义", "boundary": "边界",
            "evidence_refs": [{"quote_id": quote, "usage": "representative",
                               "reason": "支持"}]} for i in range(n)]}
    return fake


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws)
        self.st.write_artifact("confirmed_plan", {"activity_name": "A活动"})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _opinion(self, rows):
        self.st.write_artifact("annotated_round1", {"rows": rows})
        calls = []
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=_fake_discovery(calls)):
            out = handlers.opinion_candidates(
                {"id": "n09b", "outputs": ["opinion_candidates"]}, self.st, {})
        return out["artifacts"]["opinion_candidates"], calls

    def test_sparse_negative_is_not_applicable_and_not_called(self):
        art, calls = self._opinion(_r1_rows(pos=20, neg=8))
        self.assertEqual([c[0] for c in calls], ["positive"])
        neg = art["campaigns"]["default"]["negative"]
        self.assertTrue(neg["not_applicable"])
        self.assertIn("8 条", neg["reason"])
        self.assertEqual(art["negative_candidates"], [])
        self.assertIn("default/negative", art["not_applicable_directions"])

    def test_two_campaigns_four_calls_grouped(self):
        rows = _r1_rows(pos=12, neg=12, campaign="活动甲") + \
               _r1_rows(pos=12, neg=12, campaign="活动乙")
        art, calls = self._opinion(rows)
        self.assertEqual(len(calls), 4)
        self.assertEqual(set(art["campaigns"]), {"活动甲", "活动乙"})
        for cid in ("活动甲", "活动乙"):
            self.assertEqual(set(art["campaigns"][cid]), {"positive", "negative"})

    def test_neutral_and_irrelevant_excluded_from_opinion_pool(self):
        rows = _r1_rows(pos=12, neg=12, ugc=50, irrelevant=50)
        art, calls = self._opinion(rows)
        self.assertEqual(sorted(calls), [("negative", 12), ("positive", 12)])

    def test_directions_kept_separate(self):
        art, _ = self._opinion(_r1_rows(pos=12, neg=12))
        self.assertTrue(all(n.startswith("positive") for n in art["positive_candidates"]))
        self.assertTrue(all(n.startswith("negative") for n in art["negative_candidates"]))
        directions = {item["direction"] for item in art["review_items"]}
        self.assertEqual(directions, {"positive", "negative"})

    def test_sampling_recorded(self):
        art, _ = self._opinion(_r1_rows(pos=12, neg=12))
        sampling = art["campaigns"]["default"]["positive"]["sampling"]
        self.assertEqual(sampling["pool_size"], 12)
        self.assertEqual(sampling["mode"], "all")

    def _ugc(self, rows):
        self.st.write_artifact("annotated_round1", {"rows": rows})
        calls = []
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=_fake_discovery(calls)):
            out = handlers.ugc_discovery(
                {"id": "n08", "outputs": ["ugc_category_list"]}, self.st, {})
        return out["artifacts"]["ugc_category_list"], calls

    def test_ugc_pool_is_relevant_ugc_only(self):
        art, calls = self._ugc(_r1_rows(pos=30, ugc=15, irrelevant=40))
        self.assertEqual(calls, [("ugc", 15)])
        self.assertEqual(len(art["categories"]), 3)

    def test_ugc_under_ten_not_applicable(self):
        art, calls = self._ugc(_r1_rows(pos=30, ugc=9))
        self.assertEqual(calls, [])
        self.assertTrue(art["not_applicable"])
        self.assertIn("9 条", art["reason"])

    def test_fixed_four_has_no_tendency_words(self):
        self.st.params["ugc_category_mode"] = "fixed_four"
        out = handlers.ugc_discovery(
            {"id": "n08", "outputs": ["ugc_category_list"]}, self.st, {})
        art = out["artifacts"]["ugc_category_list"]
        self.assertNotIn("自发安利", art["categories"])
        self.assertFalse([n for n in art["categories"] if L.tendency_hits(n)])
        self.assertTrue(art["warnings"])


class PolicyGateTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws)
        self.st.write_artifact("ugc_category_list", {
            "mode": "fixed_four", "categories": ["模仿打卡", "创意改编", "测评分享", "晒单分享"]})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _gate(self, opinion):
        self.st.write_artifact("opinion_candidates", {
            "source": "llm", "evidence_count": 20, **opinion})
        return handlers.opinion_policy_gate(
            {"id": "n10", "outputs": ["confirmed_opinion_labels"]}, self.st, {})

    def test_tendency_name_pauses(self):
        names = ["包装好评", "口感风味", "价格优惠", "联名周边", "门店排队"]
        with self.assertRaises(HumanPause) as raised:
            self._gate({"candidates": names})
        self.assertIn("类别名带倾向", raised.exception.prompt)
        self.assertIn("包装好评", raised.exception.prompt)

    def test_neutral_name_passes(self):
        names = ["包装设计", "口感风味", "价格优惠", "联名周边", "门店排队"]
        out = self._gate({"candidates": names})
        self.assertTrue(out["artifacts"]["confirmed_opinion_labels"]["confirmed"])

    def test_direction_counts_checked_separately(self):
        pos = ["口感风味", "价格优惠", "联名周边", "门店排队", "包装设计"]
        neg = ["排队时长", "出杯速度"]   # 负面要求 3-5
        with self.assertRaises(HumanPause) as raised:
            self._gate({"candidates": pos + neg, "positive_candidates": pos,
                        "negative_candidates": neg})
        self.assertIn("负面观点标签数量 2", raised.exception.prompt)

    def test_not_applicable_direction_exempt(self):
        pos = ["口感风味", "价格优惠", "联名周边", "门店排队", "包装设计"]
        out = self._gate({"candidates": pos, "positive_candidates": pos,
                          "negative_candidates": [],
                          "not_applicable_directions": ["default/negative"]})
        self.assertTrue(out["artifacts"]["confirmed_opinion_labels"]["confirmed"])


class Round2DirectionTest(unittest.TestCase):
    """正负两套候选分别注入轮次二两个观点任务，互不串用。"""

    def test_positive_and_negative_labels_isolated(self):
        import annotate as A
        rows = [{"_annot_id": "p", "title": "t", "content": "c", "ai_sentiment": 1,
                 "relevance": True, "content_mode": "UGC"},
                {"_annot_id": "n", "title": "t", "content": "c2", "ai_sentiment": -1,
                 "relevance": True, "content_mode": "UGC"}]
        prompts = {}

        def fake(system, user, **_kw):
            ids = [item["id"] for item in json.loads(user)]
            if "正面内容" in system:
                prompts["pos"] = system
                return [{"id": i, "opinion": "口感风味"} for i in ids]
            if "负面内容" in system:
                prompts["neg"] = system
                return [{"id": i, "opinion": "排队时长"} for i in ids]
            return [{"id": i, "ugc_category": "晒单分享", "discussion_dimension": "产品"}
                    for i in ids]

        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=fake):
            result = A.annotate_round2(rows, opinion_labels=["口感风味"],
                                       negative_labels=["排队时长"],
                                       ugc_categories=["晒单分享"])
        self.assertIn("口感风味", prompts["pos"])
        self.assertNotIn("排队时长", prompts["pos"])
        self.assertIn("排队时长", prompts["neg"])
        self.assertNotIn("口感风味", prompts["neg"])
        self.assertEqual({r["_annot_id"]: r["opinion"] for r in result["rows"]},
                         {"p": "口感风味", "n": "排队时长"})

    def test_negative_label_rejected_for_positive_row(self):
        """正面行返回负面候选 → 视为越界值丢弃（结构上不允许串用）。"""
        import annotate as A
        row = {"_annot_id": "p"}
        A._apply_opinion(row, {"opinion": "排队时长"}, ["口感风味"])
        self.assertNotIn("opinion", row)


class ValidatorCategoryNameTest(unittest.TestCase):
    """检查清单 3.9（P0）：类别名带倾向；只判类别名，不判代表原文。"""

    def _scan(self, bundle):
        import validator
        scan = validator._load_scan()
        result = scan.scan_markdown("# 报告", stats={"campaigns": {"A": bundle}})
        return [i for i in result["issues"] if "category-tendency" in i["id"]]

    def test_tendency_opinion_label_is_p0(self):
        issues = self._scan({"opinion_analysis": {"positive": {"top5": [
            {"label": "包装好评", "representatives": [{"content": "真心好评"}]}]}}})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "P0")

    def test_ugc_tendency_label_is_p0(self):
        issues = self._scan({"ugc_category_dist": {"吐槽避雷": 3, "晒单分享": 2}})
        self.assertEqual([i["id"] for i in issues],
                         ["P0-category-tendency-UGC 参与方式-吐槽避雷"])

    def test_multi_label_split_and_other_skipped(self):
        issues = self._scan({"opinion_analysis": {"negative": {"top5": [
            {"label": "排队时长,价格贵"}, {"label": "其他"}]}}})
        self.assertEqual(len(issues), 1)
        self.assertIn("价格贵", issues[0]["evidence"])

    def test_neutral_labels_pass(self):
        self.assertEqual(self._scan({
            "opinion_analysis": {"positive": {"top5": [{"label": "口感风味"}]}},
            "ugc_category_dist": {"晒单分享": 1}}), [])

    def test_representative_text_not_scanned(self):
        """代表原文里的用户措辞（如「好评」）不在 3.9 判定范围内。"""
        self.assertEqual(self._scan({"opinion_analysis": {"positive": {"top5": [
            {"label": "口感风味",
             "representatives": [{"content": "五星好评，强烈推荐"}]}]}}}), [])


class EvidenceBoundaryTest(unittest.TestCase):
    def test_opinion_prompts_carry_evidence_boundary(self):
        import annotate as A
        prompts = A._r2_task_prompts("A", ["口感风味"], ["晒单分享"], False, None, None)
        for key in ("opinion", "opinion_negative"):
            self.assertIn("价格列表", prompts[key])
            self.assertIn("抱怨第三方的情绪不转成品牌负评", prompts[key])


if __name__ == "__main__":
    unittest.main()
