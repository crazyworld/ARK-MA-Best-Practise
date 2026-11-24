"""语义节点严格运行契约与证据护栏测试。"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil
import tempfile
import unittest
from unittest.mock import patch

import handlers
import semantic
from _util import make_state


class TestSemanticDegradedContract(unittest.TestCase):
    def setUp(self):
        # 确保无凭据：语义 handler 走降级分支（不触网）。
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("ARK_BASE_URL", "ARK_API_KEY",
                                 "OPENAI_BASE_URL", "OPENAI_API_KEY")}
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_llm_unavailable_without_creds(self):
        self.assertFalse(semantic.llm_available())

    def test_ugc_dynamic_rejects_missing_model(self):
        self.st.write_artifact("cleaned_data", {"rows": [{"title": "打卡", "content": "试用"}]})
        with self.assertRaisesRegex(RuntimeError, "能力不可用"):
            handlers.ugc_discovery({"id": "n08", "name": "ugc"}, self.st, {})

    def test_opinion_candidates_rejects_missing_model(self):
        self.st.write_artifact("annotated_round1",
                               {"rows": [{"title": "好用", "content": "回购"}]})
        with self.assertRaisesRegex(RuntimeError, "能力不可用"):
            handlers.opinion_candidates({"id": "n09b", "name": "oc"}, self.st, {})

    def test_wordcloud_insight_requires_complete_n14_ranges(self):
        self.st.write_artifact("wordcloud_data", {"campaigns": {"campA": {
            "overall": {"platforms": {"xiaohongshu": {"好用": 10}}}}}})
        self.st.write_artifact("chart_urls", {"charts": []})
        with patch("semantic.llm_available", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "n14 词云范围不完整"):
                handlers.wordcloud_insight({"id": "n15"}, self.st, {})

    def test_wordcloud_insight_preserves_platforms_and_not_applicable(self):
        import charts
        wc = {"campaigns": {"campA": {
            "overall": {"platforms": {"xiaohongshu": {"好用": 10}, "douyin": {"有趣": 5}}},
            "product": {"not_applicable": True, "platforms": {}},
        }}}
        self.st.write_artifact("wordcloud_data", wc)
        self.st.write_artifact("chart_urls", {"charts": [
            {**spec, "wordcloud_artifacts": {"scope": spec["wordcloud_scope"]}}
            for spec in charts.build_chart_specs({}, wc)
        ]})
        def analyze(_state, chart, *_args):
            return {"topics": [{"words": chart["categories"]}], "status": "ok"}
        with patch("semantic.llm_available", return_value=True), \
                patch("wordcloud_adapter.analyze", side_effect=analyze):
            artifact = handlers.wordcloud_insight({"id": "n15"}, self.st, {})["artifacts"]["wordcloud_insight"]
        platforms = artifact["campaigns"]["campA"]["overall"]["platforms"]
        self.assertEqual(platforms["xiaohongshu"]["topics"][0]["words"], ["好用"])
        self.assertEqual(platforms["douyin"]["topics"][0]["words"], ["有趣"])
        self.assertTrue(artifact["campaigns"]["campA"]["product"]["not_applicable"])
        self.assertTrue(artifact["complete"])

    def test_fallback_report_adapter_uses_real_metrics_and_whitelist(self):
        stats = {"campaigns": {
            "campA": {"metrics": {"活动相关条数": 120, "条均互动量": 33.5,
                                  "品牌提及率": 0.42, "产品提及占比": 0.31, "NSR": 0.18}},
        }}
        md = handlers._fallback_report_md(stats, {}, {}, "four", "0.0")
        # 真数字进正文
        self.assertIn("33.5", md)
        self.assertIn("条均互动量", md)
        # 禁用措辞不得出现
        for bad in ("人均", "转化率", "性价比"):
            self.assertNotIn(bad, md)

    def test_fallback_report_formats_distributions_and_evidence_as_prose(self):
        row = {
            "_annot_id": "internal-id",
            "title": "联名活动热门笔记",
            "author": "测试作者",
            "interaction_cnt": 57945,
            "url": "https://www.xiaohongshu.com/discovery/item/post-1",
            "content": "不应整行泄漏到报告",
        }
        stats = {
            "brand": {
                "sentiment_dist": {
                    "_total": {"positive": 10, "neutral": 3, "negative": 2}
                },
                "platform_dist": {"xiaohongshu": 15},
                "content_type_dist": {"PGC": 2, "UGC": 12, "UKN": 1},
                "hot_content": {"xiaohongshu": [row]},
            },
            "campaigns": {
                "campA": {
                    "metrics": {},
                    "content_type_dist": {"PGC": 2, "UGC": 12, "UKN": 1},
                    "top_content": {"pgc": [row], "ugc": []},
                },
            },
        }
        md = handlers._fallback_report_md(stats, {}, {}, "four", "0.0")
        self.assertIn("正面 10 条；中性 3 条；负面 2 条", md)
        self.assertIn("PGC 2 条；UGC 12 条；未知 1 条", md)
        self.assertIn("[联名活动热门笔记]", md)
        self.assertIn("互动量：57,945", md)
        self.assertNotIn('{"PGC"', md)
        self.assertNotIn("_annot_id", md)
        self.assertNotIn("internal-id", md)

    def test_write_report_forbidden_term_guard(self):
        # 正文护栏：命中禁用措辞直接报错（真路径生成后校验）。
        with self.assertRaises(ValueError):
            handlers._assert_no_forbidden_terms("本活动人均互动量很高")

    def test_report_guardrail_flags_forbidden_without_deleting_sentences(self):
        """FIX-20：命中禁用措辞只标记，不删句（删句会连带删掉数据与原文）。"""
        md = (
            "# 总结与建议\n"
            "品牌提及率保持稳定。购买转化仍需后续观察。NSR 表现积极。\n"
            "- 性价比是主要优势\n"
        )
        hits = handlers._scan_forbidden(md)
        # 正文原样保留（不删句）
        self.assertIn("购买转化", md)
        self.assertIn("性价比是主要优势", md)
        # KS_Metrics_Calculation.md:212-219 禁用词表里「购买转化」「转化率」属无条件
        # 禁用；单字「转化」不在表内（不收，避免误伤用户原话），故断言具体词组。
        self.assertTrue(any(h["term"] == "购买转化" for h in hits), hits)
        self.assertFalse(any(h["term"] == "转化" for h in hits), hits)
        with self.assertRaisesRegex(ValueError, "禁用措辞"):
            handlers._assert_no_forbidden_terms(md)

    def test_metric_scope_only_flags_contextual_terms(self):
        """“性价比/渗透率”是条件禁用：达人/层级/声量语境越界，产品讨论放行。"""
        bad = handlers._scan_forbidden("头部达人性价比更高，适合投放。")
        self.assertTrue(any(h["kind"] == "metric_scope" for h in bad))
        bad2 = handlers._scan_forbidden("活动声量渗透率为 12%。")
        self.assertTrue(any(h["kind"] == "metric_scope" for h in bad2))
        good = handlers._scan_forbidden("用户普遍反馈性价比高，愿意回购。")
        self.assertEqual(good, [])

    def test_report_guardrail_exempts_evidence_quotes(self):
        """证据模块里的原文引用不是作者措辞，必须放行（避免误删产品讨论）。"""
        md = (
            "# 口碑分析\n"
            "正面观点以口感讨论为主。\n"
            "<!-- report-module:start -->\n"
            "| 排名 | 内容 |\n"
            "| 1 | 用户说这杯性价比很高、值得回购 |\n"
            "<!-- report-module:end -->\n"
        )
        self.assertEqual(handlers._scan_forbidden(md), [])
        handlers._assert_no_forbidden_terms(md)

    def test_write_report_rejects_invalid_structure_without_fallback(self):
        stats = {"campaigns": {"campA": {"metrics": {}}}, "brand": {}}
        self.st.write_artifact("stats_bundle", stats)
        self.st.write_artifact("chart_urls", {"charts": []})
        self.st.write_artifact("wordcloud_insight", {"campaigns": {}})
        with self.assertRaisesRegex(ValueError, "禁止整篇降级替换"), \
                patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat", return_value="# 简短报告\n没有完整章节"):
            handlers.write_report(
                {"id": "n16", "name": "report"}, self.st, {}
            )

    def test_fix_report_rejects_missing_model_and_increments_attempt(self):
        self.st.write_artifact("report_md", {"markdown": "# 初稿", "chapters": "four",
                                             "version": "0.0"})
        self.st.write_artifact("qc_report", {"has_p0": True,
                                             "issues": [{"level": "P0", "msg": "数值与图表不一致"}]})
        with self.assertRaisesRegex(RuntimeError, "无法执行报告返修"):
            handlers.fix_report({"id": "n19", "name": "fix"}, self.st, {})
        self.assertEqual(self.st.counters["qc_rounds"], 1)
        self.assertEqual(self.st.counters["report_patch"], 1)

    def test_fix_report_rejects_missing_model_with_nonstring_issues(self):
        self.st.write_artifact("report_md", {"markdown": "# 初稿"})
        self.st.write_artifact("qc_report", {"has_p0": True, "issues": [1, 2]})
        with self.assertRaisesRegex(RuntimeError, "无法执行报告返修"):
            handlers.fix_report({"id": "n19", "name": "fix"}, self.st, {})

    def test_fix_report_sends_full_document_and_rejects_truncated_replacement(self):
        self.st.params["run_mode"] = "full"
        stats = {"campaigns": {"campA": {}}}
        self.st.write_artifact("stats_bundle", stats)
        complete = handlers._fallback_report_md(
            stats, {}, {}, "four", "0.0"
        )
        complete += "\n\n" + ("保留正文。" * 1200) + "\nTAIL_SENTINEL"
        self.st.write_artifact("report_md", {
            "markdown": complete, "chapters": "four", "version": "0.0",
        })
        self.st.write_artifact("qc_report", {
            "has_p0": True,
            "issues": [{"severity": "P0", "msg": "修复一处数字"}],
        })
        prompts = []

        def fake_chat(_system, user, **_kwargs):
            prompts.append(user)
            return "# 两行替换\n内容"

        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat", side_effect=fake_chat):
            with self.assertRaises(ValueError):
                handlers.fix_report({"id": "n19", "name": "fix"}, self.st, {})
        self.assertIn("TAIL_SENTINEL", prompts[0])

    def test_fix_report_retries_preflight_inside_same_node(self):
        stats = {"campaigns": {"campA": {}}}
        self.st.write_artifact("stats_bundle", stats)
        self.st.write_artifact("chart_urls", {"charts": []})
        self.st.write_artifact("requirement_confirmed", {})
        complete = handlers._fallback_report_md(
            stats, {}, {}, "four", "0.0"
        )
        self.st.write_artifact("report_md", {
            "markdown": complete, "chapters": "four", "version": "0.0",
        })
        self.st.write_artifact("qc_report", {
            "has_p0": True,
            "issues": [{
                "id": "P0-small-sample-campA-口感",
                "severity": "P0",
                "title": "分维 NSR 小样本未披露",
                "evidence": "campA/口感: sample=19<30",
                "locus": "dimension_nsr",
            }],
        })
        failed = {
            "has_p0": True,
            "p0_count": 1,
            "issues": [{
                "id": "P0-small-sample-campA-口感",
                "severity": "P0",
                "title": "分维 NSR 小样本未披露",
            }],
            "scan_mode": "full_scan",
        }
        passed = {
            "has_p0": False,
            "p0_count": 0,
            "issues": [],
            "scan_mode": "full_scan",
        }
        prompts = []

        def fake_chat(_system, user, **_kwargs):
            prompts.append(user)
            return complete

        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat", side_effect=fake_chat), \
                patch(
                    "handlers.validator_mod.validate_report",
                    side_effect=[failed, passed],
                ):
            out = handlers.fix_report(
                {"id": "n19", "name": "fix"}, self.st, {}
            )
        report = out["artifacts"]["report_md"]
        self.assertEqual(len(prompts), 2)
        self.assertIn("P0-small-sample-campA-口感", prompts[1])
        self.assertEqual(
            report["deterministic_preflight"],
            {"attempts": 2, "p0_count": 0, "scan_mode": "full_scan"},
        )

    def test_fix_report_fails_closed_after_three_preflight_attempts(self):
        stats = {"campaigns": {"campA": {}}}
        self.st.write_artifact("stats_bundle", stats)
        self.st.write_artifact("chart_urls", {"charts": []})
        complete = handlers._fallback_report_md(
            stats, {}, {}, "four", "0.0"
        )
        self.st.write_artifact("report_md", {
            "markdown": complete, "chapters": "four", "version": "0.0",
        })
        issue = {
            "id": "P0-still-open",
            "severity": "P0",
            "title": "仍未修复",
        }
        self.st.write_artifact(
            "qc_report", {"has_p0": True, "issues": [issue]}
        )
        failed = {
            "has_p0": True,
            "p0_count": 1,
            "issues": [issue],
            "scan_mode": "full_scan",
        }
        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat", return_value=complete), \
                patch(
                    "handlers.validator_mod.validate_report",
                    return_value=failed,
                ):
            with self.assertRaisesRegex(
                ValueError, "3 次节点内返修后仍有确定性 P0"
            ):
                handlers.fix_report(
                    {"id": "n19", "name": "fix"}, self.st, {}
                )


if __name__ == "__main__":
    unittest.main()
