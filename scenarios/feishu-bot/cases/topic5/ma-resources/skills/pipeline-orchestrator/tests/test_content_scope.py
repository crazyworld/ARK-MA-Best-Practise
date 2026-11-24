"""FIX-03：content_scope 贯通取数与报告呈现。

原版依据：
  - Producer Step4 按 content_scope 映射 SSJH content_mode 取数（品牌期与基准期同口径）
  - KS_Report_Template：非「全部」时跳过 PGC/UGC 结构对比，热门内容只展示对应一张表
  - 检查清单 1.1 项：content_scope 不是「全部」却仍展示对比或分表时判 P0

运行：python3 -m unittest tests.test_content_scope
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers        # noqa: E402
import report_contract  # noqa: E402


def _stats():
    row = {"title": "标题", "content": "正文", "site_name": "小红书",
           "url": "https://example.com/1", "interaction_cnt": 10,
           "content_mode": "PGC", "author": "作者"}
    return {
        "brand": {"top_content": {"pgc": [row], "ugc": [row]}},
        "campaigns": {"c1": {"top_content": {"pgc": [row], "ugc": [row]}}},
    }


class HotTablesScopeTest(unittest.TestCase):
    def test_all_scope_emits_two_tables_top10(self):
        text = report_contract.hot_tables(_stats()["campaigns"]["c1"], "活动 c1", "all")
        self.assertIn("PGC 热门内容 Top10", text)
        self.assertIn("UGC 热门内容 Top10", text)

    def test_ugc_scope_emits_only_ugc_table_with_top20(self):
        text = report_contract.hot_tables(_stats()["campaigns"]["c1"], "活动 c1", "ugc")
        self.assertIn("UGC 热门内容 Top20", text)
        self.assertNotIn("PGC 热门内容", text)

    def test_pgc_scope_emits_only_pgc_table(self):
        text = report_contract.hot_tables(_stats()["campaigns"]["c1"], "活动 c1", "pgc")
        self.assertIn("PGC 热门内容 Top20", text)
        self.assertNotIn("UGC 热门内容", text)

    def test_single_scope_renders_up_to_20_rows(self):
        rows = [{"title": f"t{i}", "content": "c", "site_name": "小红书",
                 "url": f"https://e/{i}", "interaction_cnt": i}
                for i in range(30)]
        bundle = {"top_content": {"ugc": rows}}
        text = report_contract.hot_tables(bundle, "", "ugc")
        # 表头 1 行 + 分隔行 1 行 + 20 条数据
        data_rows = [ln for ln in text.splitlines()
                     if ln.startswith("|") and not set(ln) <= set("|- ")]
        self.assertEqual(len(data_rows), 1 + 20)


class AssertHotTablesScopeTest(unittest.TestCase):
    def _doc(self, body_14, body_21):
        # 章节层级要符合真实报告：章用 ##，小节用编号（1.4 为 3 级、2.1.5 为 4 级），
        # 所以 1.4 与 2.1.5 之间必须有一个 ≤3 级的章标题收口。
        return (
            "# 报告\n\n"
            "## 1. 品牌传播表现\n\n### 1.4 热门内容\n\n" + body_14 + "\n\n"
            "## 2. 活动传播表现\n\n### 2.1 活动一\n\n#### 2.1.5 热门内容\n\n"
            + body_21 + "\n\n"
        )

    def test_scope_ugc_passes_when_only_ugc_table(self):
        bundle = _stats()["campaigns"]["c1"]
        body = report_contract.hot_tables(bundle, "", "ugc")
        md = self._doc(body, body)
        report_contract.assert_hot_tables(md, _stats(), "ugc")  # 不抛异常

    def test_scope_ugc_rejects_pgc_table(self):
        bundle = _stats()["campaigns"]["c1"]
        body = report_contract.hot_tables(bundle, "", "all")
        md = self._doc(body, body)
        with self.assertRaisesRegex(ValueError, "PGC 热门"):
            report_contract.assert_hot_tables(md, _stats(), "ugc")


class DataDescriptionScopeTest(unittest.TestCase):
    def test_scope_note_appears_when_not_all(self):
        plan = {"content_scope": "ugc", "period_start": "2026-09-28",
                "period_end": "2026-10-07", "platforms": ["小红书"]}
        # FIX-43：口径说明属方法说明，移出数据说明区，放在 1.1 节首灰色斜体。
        self.assertNotIn("不做 PGC/UGC 结构对比", report_contract.data_description(plan, {}))
        md = report_contract.assemble(
            "# 报告\n\n### 数据说明\n\n## 整体洞察\nx\n\n## 一、品牌传播表现\n\n"
            "### 1.1 传播概况\n正文\n\n### 1.4 热门内容\n",
            plan=plan, stats={"brand": {}}, charts={}, insight={})
        body = md[report_contract.section(md, "1.1")[1]:]
        self.assertTrue(body.lstrip().startswith("<!-- report-module:start -->\n*{{muted}}本次仅分析 UGC 内容"))

    def test_all_scope_has_no_scope_note(self):
        plan = {"content_scope": "all", "period_start": "2026-09-28",
                "period_end": "2026-10-07", "platforms": ["小红书"]}
        text = report_contract.data_description(plan, {})
        self.assertNotIn("不做 PGC/UGC 结构对比", text)


class UgcTextScopeTest(unittest.TestCase):
    def test_pgc_scope_marks_not_applicable(self):
        text = report_contract.ugc_text({}, "pgc")
        self.assertIn("不适用 UGC 参与方式分析", text)

    def test_all_scope_renders_normally(self):
        # KS 2.N.4②：活动相关 UGC <10 条不做参与方式分类，保留标题并说明。
        text = report_contract.ugc_text({"ugc_analysis": {"sample_size": 3}}, "all")
        self.assertIn("活动相关 UGC 仅 3 条，样本不足，暂不做参与方式分类", text)
        self.assertIn("② UGC 参与方式分布", text)


class ContentModeMappingTest(unittest.TestCase):
    def test_scope_maps_to_ssjh_content_mode(self):
        self.assertEqual(handlers.CONTENT_MODE_BY_SCOPE["all"], "ALL")
        self.assertEqual(handlers.CONTENT_MODE_BY_SCOPE["ugc"], "UGC")
        self.assertEqual(handlers.CONTENT_MODE_BY_SCOPE["pgc"], "PGC")


class SsjhPullReceivesContentModeTest(unittest.TestCase):
    """品牌期、基准期、词云三路取数都必须带 content_mode。"""

    def _run(self, outputs, scope):
        calls = []

        def fake_pull(plan, **kwargs):
            calls.append(kwargs.get("content_mode"))
            return {"articles": {}, "cloud_word": {}, "meta": {}}

        class _State:
            params = {}

            def __init__(self):
                self.artifacts = {}

            def read_artifact(self, key):
                if key == "confirmed_plan":
                    return {"content_scope": scope, "brand": "瑞幸",
                            "brand_words": ["瑞幸"], "campaigns": [{"id": "c1"}],
                            "platforms": ["小红书"]}
                return {}

        with patch.object(handlers.ssjh, "api_available", return_value=True), \
                patch.object(handlers.ssjh, "pull", side_effect=fake_pull):
            handlers.ssjh_pull({"outputs": outputs}, _State(), {})
        return calls

    def test_brand_and_baseline_use_scope(self):
        for outputs in (["brand_stats"], ["brand_stats_baseline"]):
            calls = self._run(outputs, "ugc")
            self.assertEqual(calls, ["UGC"], outputs)

    def test_wordcloud_uses_scope(self):
        calls = self._run(["wordcloud_data"], "pgc")
        self.assertTrue(calls, "词云未取数")
        self.assertTrue(all(c == "PGC" for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
