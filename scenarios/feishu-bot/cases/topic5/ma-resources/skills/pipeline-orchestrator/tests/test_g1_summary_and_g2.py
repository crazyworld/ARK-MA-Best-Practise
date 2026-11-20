"""FIX-09：G1 卡片业务解析摘要 + G2 代用户确认开关。

原版依据：
  - Phase1_需求梳理_Topic5_v1.10.md:157-161  HC-T5-0 展示解析结果（无需确认）；
    关键词与过滤词不向用户展示，只写入确认单。
  - HB:430-470  G2 冻结 plan_bundle_sha256 的机制必须保留，只改放行主体。

运行：python3 -m unittest tests.test_g1_summary_and_g2
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
TOPIC = os.path.dirname(os.path.dirname(os.path.dirname(ORCH)))
sys.path.insert(0, ORCH)
sys.path.insert(0, TOPIC)

import handlers          # noqa: E402
import topic5_intake as intake  # noqa: E402
from handlers import HumanPause  # noqa: E402


class _State:
    def __init__(self, artifacts=None):
        self.artifacts = dict(artifacts or {})

    def read_artifact(self, name):
        return self.artifacts.get(name)


def _req(**extra):
    return intake.normalize_requirement({**intake.DEMO_REQUIREMENT_DEFAULTS, **extra})


class ParsedSummaryTest(unittest.TestCase):
    def test_contains_business_fields(self):
        text = intake.requirement_parsed_summary(_req())
        self.assertIn("业务解析", text)
        self.assertIn("瑞幸", text)
        self.assertIn("统计窗口", text)
        self.assertIn("小红书", text)
        self.assertIn("内容范围", text)

    def test_never_shows_keywords_or_filterwords(self):
        """原版明文：关键词、过滤词不向用户展示，也不提表达式。"""
        req = _req()
        text = intake.requirement_parsed_summary(req)
        # 品牌口径恰好等于品牌名，不具区分度；用带表达式的活动/产品检索式断言。
        self.assertNotIn(req["activity_query"], text)
        self.assertNotIn(req["product_query"], text)
        self.assertNotIn("+", text)
        for word in ("品牌口径", "活动口径", "过滤词"):
            self.assertNotIn(word, text)

    def test_source_tags_reflect_field_sources(self):
        req = _req()
        req["field_sources"] = {"brand": "原话", "campaign_type": "推断"}
        text = intake.requirement_parsed_summary(req)
        self.assertIn("瑞幸（原话）", text)
        if "活动类型" in text:
            self.assertIn("（推断）", text)

    def test_missing_field_marked_red(self):
        req = _req()
        req["missing_fields"] = ["period_start"]
        text = intake.requirement_parsed_summary(req)
        self.assertIn('<font color="red">统计窗口', text)
        self.assertIn("请补充", text)

    def test_flag_only_on_missing_or_conflict(self):
        req = _req()
        req["conflicts"] = [{"field": "activity_name", "detail": "两个活动名"}]
        text = intake.requirement_parsed_summary(req)
        self.assertIn('<font color="red">活动', text)
        self.assertNotIn('<font color="red">统计窗口', text)

    def test_multi_campaign_shows_merge_window(self):
        req = intake.normalize_requirement({
            **intake.DEMO_REQUIREMENT_DEFAULTS,
            "campaign_lines": [
                "秋季活动；2026-09-01；2026-09-20；瑞幸+秋日",
                "国庆活动；2026-10-01；2026-10-07；瑞幸+国庆",
            ],
        })
        self.assertEqual(len(req["campaigns"]), 2)
        text = intake.requirement_parsed_summary(req)
        self.assertIn("合并窗口：2026-09-01 ～ 2026-10-07", text)

    def test_empty_requirement_returns_empty(self):
        self.assertEqual(intake.requirement_parsed_summary({}), "")


class G2AutoConfirmTest(unittest.TestCase):
    NODE = {"id": "n03_hc_plan", "gate": "G2", "outputs": ["confirmed_plan"]}

    def _ctx(self, **params):
        return {"params": params}

    def test_default_off_still_pauses(self):
        st = _State({"analysis_plan": {"plan_id": "p1"},
                     "preflight": {"status": "READY"}})
        with self.assertRaises(HumanPause):
            handlers.human_gate(self.NODE, st, {})

    def test_explicit_false_still_pauses(self):
        st = _State({"analysis_plan": {"plan_id": "p1"},
                     "preflight": {"status": "READY"}})
        with self.assertRaises(HumanPause):
            handlers.human_gate(self.NODE, st,
                                self._ctx(g2_auto_confirm_when_no_cost=False))

    def test_auto_confirms_when_no_cost_and_ready(self):
        st = _State({"analysis_plan": {"plan_id": "p1"},
                     "preflight": {"status": "READY", "estimated_cost": 0}})
        out = handlers.human_gate(self.NODE, st,
                                  self._ctx(g2_auto_confirm_when_no_cost=True))
        self.assertEqual(out["authority"], "user-delegated")
        self.assertEqual(out["artifacts"]["confirmed_plan"], {"plan_id": "p1"})
        self.assertIn("g2_auto_confirm_when_no_cost", out["reason"])

    def test_blocked_when_preflight_not_ready(self):
        st = _State({"analysis_plan": {"plan_id": "p1"},
                     "preflight": {"status": "BLOCKED", "estimated_cost": 0}})
        with self.assertRaises(HumanPause):
            handlers.human_gate(self.NODE, st,
                                self._ctx(g2_auto_confirm_when_no_cost=True))

    def test_blocked_when_billable(self):
        for preflight in ({"status": "READY", "estimated_cost": 12.5},
                          {"status": "READY", "billable": True},
                          {"status": "READY", "requires_payment": True}):
            st = _State({"analysis_plan": {"plan_id": "p1"}, "preflight": preflight})
            with self.subTest(preflight=preflight):
                with self.assertRaises(HumanPause):
                    handlers.human_gate(self.NODE, st,
                                        self._ctx(g2_auto_confirm_when_no_cost=True))

    def test_other_gates_never_auto_confirm(self):
        st = _State({"analysis_plan": {"plan_id": "p1"},
                     "preflight": {"status": "READY", "estimated_cost": 0}})
        for node in ({"id": "n01_hc_requirement", "gate": "G1"},
                     {"id": "n21_hc_user", "gate": "G4"}):
            with self.subTest(node=node["gate"]):
                with self.assertRaises(HumanPause):
                    handlers.human_gate(node, st,
                                        self._ctx(g2_auto_confirm_when_no_cost=True))


if __name__ == "__main__":
    unittest.main()
