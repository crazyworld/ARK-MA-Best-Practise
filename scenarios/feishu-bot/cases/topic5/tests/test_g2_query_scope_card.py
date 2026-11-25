"""G2 方案卡「取数口径」渲染（方案 A）：表达式原样、标来源；解析卡仍不展示（原版 v1.10:159）。"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_store as ps  # noqa: E402
import test_host_stdlib as _base  # noqa: E402

BRAND_FILTER = "塔斯汀地貌|(塔斯汀·比伯+(justinbieber|贾斯汀比伯|baby|仿妆|pubg|合拍))"


def _load_hitl():
    holder = types.SimpleNamespace()
    _base.HitlActionParsingTest.setUpClass.__func__(holder)
    return holder.hitl


def _payload(query_scope=None):
    ctx = {"analysis_plan": {"scope": {"brand": "塔斯汀", "activities": ["塔斯汀×Pingu联名活动"]}},
           "execution_plan": {"stages": []}, "selected_modules": {}}
    if query_scope is not None:
        ctx["query_scope"] = query_scope
    return {"hc": "G2", "review_context": ctx}


class G2QueryScopeCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hitl = _load_hitl()

    def _text(self, payload):
        job = ps.PipelineJob(job_id="job_x", chat_id="c", thread_id="", user_open_id="u",
                             ma_session_id="s", mode="full", project_dir="d")
        card = self.hitl.build_hc_card(job, "G2", 1, payload)
        return "\n".join(e.get("content", "") for e in card["body"]["elements"]
                         if isinstance(e, dict))

    def test_expressions_and_sources_rendered(self):
        text = self._text(_payload({"rows": [
            {"label": "品牌取数关键词", "value": "塔斯汀", "source": "用户给定"},
            {"label": "品牌取数过滤词", "value": BRAND_FILTER, "source": "用户给定"},
            {"label": "活动取数关键词", "value": "塔斯汀+pingu", "source": "能力生成"},
            {"label": "活动取数过滤词", "value": "", "source": "用户留空"},
        ]}))
        self.assertIn("**取数口径**", text)
        self.assertIn("`" + BRAND_FILTER + "`（用户给定）", text)
        self.assertIn("`塔斯汀+pingu`（能力生成）", text)
        self.assertIn("**活动取数过滤词**：（空）（用户留空）", text)

    def test_no_scope_no_section(self):
        self.assertNotIn("取数口径", self._text(_payload()))

    def test_intake_summary_still_hides_expressions(self):
        import importlib
        intake = importlib.import_module(self.hitl.__name__.rsplit(".", 1)[0] + ".topic5_intake") \
            if "." in self.hitl.__name__ else importlib.import_module("topic5_intake")
        text = intake.intake_summary_text({"brand": "塔斯汀", "activity_name": "A",
                                           "brand_query": "塔斯汀", "brand_filter": BRAND_FILTER})
        self.assertNotIn("塔斯汀地貌", text)


if __name__ == "__main__":
    unittest.main()
