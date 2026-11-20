"""FIX-07：campaign-keyword-generation Skill 的预填与留痕。

原版依据：
  - Topic5/Skills/campaign-keyword-generation/references/contract.md 的输出 schema
    （status / resolved_request / campaign_type{primary,secondary,basis,state} /
     activity_boundary{included,excluded_or_pending,query_period,actual_event_period} /
     brand_keyword / activity_keywords / brand_filter / activity_filter 等）
  - SKILL.md：关键词由本 Skill 拟定后直接采用，不向用户逐项确认；G1 以预填表单发出

本文件只验证「映射与留痕」这一层（Skill 本身是提示词包，由 Agent 侧执行）。

运行：python3 -m unittest tests.test_keyword_prefill
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
TOPIC = os.path.dirname(os.path.dirname(os.path.dirname(ORCH)))
sys.path.insert(0, ORCH)
sys.path.insert(0, TOPIC)

import topic5_intake as intake  # noqa: E402

SKILL_DIR = os.path.join(
    os.path.dirname(ORCH), "campaign-keyword-generation")


# 取自 contract.md 的字段形状（瑞幸×暹罗厘普场景）
SAMPLE_RESOLVED = {
    "brand": "瑞幸",
    "campaign_name": "瑞幸×暹罗厘普联名活动",
    "campaign_period": {"start": "2026-09-28", "end": "2026-10-07"},
    "platforms": ["小红书", "微博"],
    "content_scope": "ALL",
    "brand_keyword": "瑞幸|luckin",
    "brand_filter": "无",
    "activity_keywords": "瑞幸+(暹罗厘普|多栋|厘普|比格)",
    "activity_filter": "贴膜|维修",
    "product_keyword": "瑞幸+流心芝士拿铁",
    "campaign_type": {"primary": "IP联名", "secondary": ["话题挑战"],
                      "basis": "用户原话提到联名", "state": "explicit"},
    "activity_boundary": {"included": ["联名周边", "线下快闪"],
                          "excluded_or_pending": ["其他品牌联名"],
                          "query_period": {"start": "2026-09-28",
                                           "end": "2026-10-07"},
                          "actual_event_period": {"start": "2026-09-30"}},
}


class SkillPackagedTest(unittest.TestCase):
    def test_skill_files_present(self):
        """方案第 1 步：Skill 原样打包到 ma-resources/skills/。"""
        for name in ("SKILL.md", "references/contract.md",
                     "references/generation.md", "references/filtering.md",
                     "references/cases.md", "agents/openai.yaml"):
            self.assertTrue(
                os.path.isfile(os.path.join(SKILL_DIR, name)),
                f"缺少 Skill 文件: {name}")

    def test_skill_frontmatter_name_and_version(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            head = fh.read(600)
        self.assertIn("name: campaign-keyword-generation", head)
        self.assertIn("campaign_keyword_generation", head)

    def test_contract_declares_required_output_fields(self):
        with open(os.path.join(SKILL_DIR, "references/contract.md"),
                  encoding="utf-8") as fh:
            body = fh.read()
        for field in ("status:", "resolved_request:", "missing_fields:",
                      "brand_keyword:", "activity_keywords:",
                      "activity_boundary:", "campaign_type:"):
            self.assertIn(field, body, f"contract.md 缺少字段 {field}")


class ResolvedRequestToG1Test(unittest.TestCase):
    def test_core_fields_mapped(self):
        out = intake.resolved_request_to_g1_defaults(SAMPLE_RESOLVED)
        self.assertEqual(out["brand"], "瑞幸")
        self.assertEqual(out["activity_name"], "瑞幸×暹罗厘普联名活动")
        self.assertEqual(out["period_start"], "2026-09-28")
        self.assertEqual(out["period_end"], "2026-10-07")
        self.assertEqual(out["platforms"], ["小红书", "微博"])
        self.assertEqual(out["brand_query"], "瑞幸|luckin")
        self.assertEqual(out["activity_query"], "瑞幸+(暹罗厘普|多栋|厘普|比格)")
        self.assertEqual(out["activity_filter"], "贴膜|维修")
        self.assertEqual(out["content_scope"], "all")

    def test_content_scope_lowercased(self):
        out = intake.resolved_request_to_g1_defaults({"content_scope": "UGC"})
        self.assertEqual(out["content_scope"], "ugc")

    def test_campaign_type_dict_flattened(self):
        out = intake.resolved_request_to_g1_defaults(SAMPLE_RESOLVED)
        self.assertEqual(out["campaign_type"], "IP联名")
        self.assertEqual(out["campaign_type_state"], "explicit")
        self.assertEqual(out["campaign_type_secondary"], ["话题挑战"])

    def test_campaign_type_string_kept(self):
        out = intake.resolved_request_to_g1_defaults({"campaign_type": "代言"})
        self.assertEqual(out["campaign_type"], "代言")

    def test_activity_boundary_flattened(self):
        out = intake.resolved_request_to_g1_defaults(SAMPLE_RESOLVED)
        self.assertEqual(out["activity_boundary"], "联名周边；线下快闪")
        self.assertIn("start", out["actual_event_period"])

    def test_product_and_goal(self):
        out = intake.resolved_request_to_g1_defaults(
            {**SAMPLE_RESOLVED, "campaign_goal": "提升新品认知"})
        self.assertEqual(out["product_query"], "瑞幸+流心芝士拿铁")
        self.assertEqual(out["campaign_goal"], "提升新品认知")

    def test_empty_values_dropped(self):
        out = intake.resolved_request_to_g1_defaults(
            {"brand": "瑞幸", "platforms": [], "campaign_name": ""})
        self.assertEqual(out, {"brand": "瑞幸"})

    def test_missing_filters_are_not_fabricated(self):
        """contract.md：没有依据的字段留空，不伪造证据；不补「无」。"""
        out = intake.resolved_request_to_g1_defaults({"brand": "瑞幸"})
        self.assertNotIn("brand_filter", out)
        self.assertNotIn("activity_filter", out)
        self.assertNotIn("content_scope", out)

    def test_empty_input_returns_empty(self):
        self.assertEqual(intake.resolved_request_to_g1_defaults({}), {})
        self.assertEqual(intake.resolved_request_to_g1_defaults(None), {})  # type: ignore[arg-type]

    def test_prefill_survives_normalize(self):
        """预填值能原样归一化，且 brand_query 不被改写。"""
        out = intake.resolved_request_to_g1_defaults(SAMPLE_RESOLVED)
        req = intake.normalize_requirement(out)
        self.assertEqual(req["brand"], "瑞幸")
        self.assertEqual(req["brand_query"], "瑞幸|luckin")
        self.assertEqual(req["period_start"], "2026-09-28")
        self.assertEqual(req["period_end"], "2026-10-07")

    def test_prefill_form_uses_default_values(self):
        form = intake.build_requirement_form(
            7, lambda d: {"value": d},
            defaults=intake.resolved_request_to_g1_defaults(SAMPLE_RESOLVED))
        blob = str(form)
        self.assertIn("瑞幸×暹罗厘普联名活动", blob)
        self.assertIn("2026-09-28", blob)

    def test_datetime_period_accepted(self):
        out = intake.resolved_request_to_g1_defaults(
            {"campaign_period": {"start": "2026-09-28T00:00:00+08:00",
                                 "end": "2026-10-07T00:00:00+08:00"}})
        self.assertEqual(out["period_start"], "2026-09-28")
        self.assertEqual(out["period_end"], "2026-10-07")


class KeywordGenerationTraceTest(unittest.TestCase):
    """方案第 4 步：结果存 requirement_confirmed.keyword_generation。"""

    def setUp(self):
        import hc_bridge
        self.hc = hc_bridge

    def test_keyword_generation_preserved(self):
        payload = {
            "brand": "瑞幸", "activity_name": "联名",
            "period_start": "2026-09-28", "period_end": "2026-10-07",
            "platforms": ["小红书"], "brand_query": "瑞幸",
            "activity_query": "瑞幸+暹罗厘普",
            "keyword_generation": {
                "status": "ready",
                "skill_version": "1.1.0",
                "invoked_at": "2026-11-19T10:00:00+08:00",
                "resolved_request": SAMPLE_RESOLVED,
            },
        }
        out = self.hc._with_traceability(dict(payload))
        self.assertEqual(out["keyword_generation"]["status"], "ready")
        self.assertEqual(
            out["keyword_generation"]["resolved_request"]["brand"], "瑞幸")

    def test_missing_keyword_generation_filled_empty(self):
        out = self.hc._with_traceability({"brand": "瑞幸"})
        self.assertEqual(out["keyword_generation"], {})

    def test_non_dict_keyword_generation_ignored(self):
        out = self.hc._with_traceability({"keyword_generation": "bad"})
        self.assertEqual(out["keyword_generation"], {})

    def test_keyword_generation_is_declared_trace_field(self):
        self.assertIn("keyword_generation", self.hc._TRACE_FIELDS)


if __name__ == "__main__":
    unittest.main()
