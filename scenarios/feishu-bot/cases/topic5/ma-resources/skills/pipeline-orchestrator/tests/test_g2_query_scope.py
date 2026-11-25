"""G2 方案卡展示取数口径（用户选定方案 A）。

原版 Phase1 v1.10:159：HC-T5-0 解析展示不出现关键词/过滤词，完整写入确认单；
用户主动询问时如实告知。G2 是方案确认关卡，取数口径在此展示供核对，有误可打回。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hc_bridge  # noqa: E402
from _util import make_state  # noqa: E402

BRAND_FILTER = "塔斯汀地貌|(塔斯汀·比伯+(justinbieber|贾斯汀比伯))"


def _skill_requirement(**over):
    req = {
        "brand": "塔斯汀", "brand_query": "塔斯汀", "brand_filter": BRAND_FILTER,
        "activity_query": "塔斯汀+pingu", "activity_filter": "", "product_query": "",
        "requirement_source": "campaign_keyword_generation",
        "campaigns": [{"name": "塔斯汀×Pingu联名活动", "activity_query": "塔斯汀+pingu"}],
        "keyword_generation": {"resolved_request": {"field_sources": {
            "brand_keyword": "user", "brand_filter": "user", "activity_keywords": "user",
            "activity_filter": "user_blank", "product_keywords": "user_blank"}}},
    }
    req.update(over)
    return req


class QueryScopeTest(unittest.TestCase):
    def _pending(self, requirement):
        st = make_state(tempfile.mkdtemp())
        st.human = {"node": "n03_hc_plan", "prompt": "方案确认", "expect": ["confirmed_plan"]}
        st.write_artifact("requirement_confirmed", requirement)
        for key in ("analysis_plan", "execution_plan", "selected_modules"):
            st.write_artifact(key, {"k": key})
        return hc_bridge.write_hc_pending(st, st.run_dir)["review_context"]["query_scope"]

    def test_user_given_expressions_shown_verbatim(self):
        rows = {r["label"]: r for r in self._pending(_skill_requirement())["rows"]}
        self.assertEqual(rows["品牌取数关键词"]["value"], "塔斯汀")
        self.assertEqual(rows["品牌取数过滤词"]["value"], BRAND_FILTER)
        self.assertEqual(rows["品牌取数过滤词"]["source"], "用户给定")
        self.assertEqual(rows["活动取数关键词"]["value"], "塔斯汀+pingu")
        self.assertEqual(rows["活动取数过滤词"]["value"], "")
        self.assertEqual(rows["活动取数过滤词"]["source"], "用户留空")
        self.assertEqual(rows["产品关键词"]["source"], "用户留空")

    def test_generated_expression_marked(self):
        req = _skill_requirement()
        req["keyword_generation"]["resolved_request"]["field_sources"]["activity_keywords"] = "generated"
        rows = {r["label"]: r for r in self._pending(req)["rows"]}
        self.assertEqual(rows["活动取数关键词"]["source"], "能力生成")

    def test_form_requirement_is_user_given(self):
        req = _skill_requirement(requirement_source="g1_form", keyword_generation={})
        rows = {r["label"]: r for r in self._pending(req)["rows"]}
        self.assertEqual(rows["品牌取数关键词"]["source"], "用户给定")

    def test_multi_campaign_listed_per_activity(self):
        req = _skill_requirement(campaigns=[
            {"name": "活动A", "activity_query": "塔斯汀+pingu", "activity_filter": "x"},
            {"name": "活动B", "activity_query": "塔斯汀+(汉堡|新品)"}])
        scope = self._pending(req)
        labels = [r["label"] for r in scope["rows"]]
        self.assertIn("活动取数关键词 · 活动A", labels)
        self.assertIn("活动取数关键词 · 活动B", labels)
        self.assertIn("活动取数过滤词 · 活动A", labels)

    def test_missing_requirement_no_scope(self):
        st = make_state(tempfile.mkdtemp())
        st.human = {"node": "n03_hc_plan", "prompt": "", "expect": ["confirmed_plan"]}
        ctx = hc_bridge.write_hc_pending(st, st.run_dir)["review_context"]
        self.assertNotIn("query_scope", ctx)


if __name__ == "__main__":
    unittest.main()
