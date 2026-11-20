"""FIX-09：需求留痕字段（原话 / 更正 / 来源 / Skill 调用 / 交付要求）。

原版依据：Phase1 需求梳理 v1.10、Supervisor LM、HC-T5-0
  - 需求确认单由 Agent 整理，用户只在缺失或冲突时补问
  - 必须留存原话、最新更正、字段来源、Skill 调用记录、交付要求
  - 下游（Validator 核对最新更正、词云 call-brief）依赖这些字段

运行：python3 -m unittest tests.test_requirement_trace
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
sys.path.insert(0, ORCH)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(ORCH))))

import hc_bridge       # noqa: E402
import topic5_intake as intake  # noqa: E402


def _payload(**extra):
    base = {
        "brand": "瑞幸",
        "activity_name": "瑞幸×暹罗厘普联名活动",
        "period_start": "2026-09-28",
        "period_end": "2026-10-07",
        "platforms": ["小红书"],
        "brand_query": "瑞幸+(暹罗厘普|多栋)",
        "activity_query": "暹罗厘普",
        "content_scope": "all",
        "business_questions": "活动表现",
        "delivery_expectation": "飞书报告",
        "allowed_actions": "调用已登记能力",
    }
    base.update(extra)
    return base


class NormalizeTraceFieldsTest(unittest.TestCase):
    def test_defaults_are_empty_not_missing(self):
        """老表单没有留痕字段时补空结构，下游不必到处写 or {}。"""
        req = intake.normalize_requirement(_payload())
        self.assertEqual(req["raw_request"], "")
        self.assertEqual(req["corrections"], [])
        self.assertEqual(req["field_sources"], {})
        self.assertEqual(req["skill_invocations"], [])
        self.assertEqual(req["delivery_deadline"], "")
        self.assertEqual(req["special_notes"], "")

    def test_raw_request_and_delivery_are_kept(self):
        req = intake.normalize_requirement(_payload(
            raw_request="帮我看下这个联名活动的效果，重点看小红书",
            delivery_deadline="2026-10-12",
            special_notes="需要和去年同名活动对比",
        ))
        self.assertIn("联名活动", req["raw_request"])
        self.assertEqual(req["delivery_deadline"], "2026-10-12")
        self.assertEqual(req["special_notes"], "需要和去年同名活动对比")

    def test_corrections_are_normalized(self):
        req = intake.normalize_requirement(_payload(corrections=[
            {"at": "2026-11-19T10:00", "field": "platforms",
             "old": ["小红书"], "new": ["小红书", "抖音"], "source": "user"},
            {"field": "period_end", "new": "2026-10-10"},   # 缺 at/old，也要保留
            "不是字典，丢弃",
        ]))
        self.assertEqual(len(req["corrections"]), 2)
        self.assertEqual(req["corrections"][0]["field"], "platforms")
        self.assertEqual(req["corrections"][1]["source"], "user")
        self.assertEqual(req["corrections"][1]["at"], "")

    def test_field_sources_only_accepts_known_values(self):
        req = intake.normalize_requirement(_payload(field_sources={
            "brand": "原话", "platforms": "默认", "product_query": "推断",
            "activity_query": "用户更正", "content_scope": "瞎写",
        }))
        self.assertEqual(req["field_sources"]["brand"], "原话")
        self.assertEqual(req["field_sources"]["activity_query"], "用户更正")
        self.assertNotIn("content_scope", req["field_sources"])

    def test_skill_invocations_are_normalized(self):
        req = intake.normalize_requirement(_payload(skill_invocations=[
            {"skill": "campaign-keyword-generation", "version": "1.0.3",
             "at": "2026-11-19T10:00", "ok": True, "note": "生成 2 组"},
        ]))
        item = req["skill_invocations"][0]
        self.assertEqual(item["skill"], "campaign-keyword-generation")
        self.assertEqual(item["version"], "1.0.3")
        self.assertTrue(item["ok"])


class ParseRequirementTraceTest(unittest.TestCase):
    def test_parse_keeps_traceability(self):
        out = hc_bridge._parse_requirement(json.dumps(_payload(
            raw_request="帮我看下效果",
            delivery_deadline="2026-10-12",
            corrections=[{"field": "brand", "old": "X", "new": "瑞幸"}],
            skill_invocations=[{"skill": "campaign-keyword-generation"}],
        ), ensure_ascii=False))
        self.assertEqual(out["raw_request"], "帮我看下效果")
        self.assertEqual(out["delivery_deadline"], "2026-10-12")
        self.assertEqual(out["corrections"][0]["new"], "瑞幸")
        self.assertEqual(out["skill_invocations"][0]["skill"],
                         "campaign-keyword-generation")

    def test_legacy_payload_without_trace_fields_still_passes(self):
        """老载荷缺留痕字段不能被判成缺必填字段。"""
        out = hc_bridge._parse_requirement(json.dumps(_payload(), ensure_ascii=False))
        self.assertEqual(out["raw_request"], "")
        self.assertEqual(out["corrections"], [])
        self.assertEqual(out["brand"], "瑞幸")

    def test_missing_required_field_still_raises(self):
        payload = _payload()
        payload.pop("brand_query")
        with self.assertRaisesRegex(ValueError, "必填字段"):
            hc_bridge._parse_requirement(json.dumps(payload, ensure_ascii=False))


class SnapshotRowsTest(unittest.TestCase):
    def test_snapshot_shows_raw_request_and_deadline(self):
        req = intake.normalize_requirement(_payload(
            raw_request="重点是小红书口碑", delivery_deadline="2026-10-12"))
        rows = dict(intake.requirement_snapshot_rows(req))
        self.assertEqual(rows["原话"], "重点是小红书口碑")
        self.assertEqual(rows["交付截止"], "2026-10-12")

    def test_snapshot_marks_absent_raw_request(self):
        req = intake.normalize_requirement(_payload())
        rows = dict(intake.requirement_snapshot_rows(req))
        self.assertEqual(rows["原话"], "未记录")


if __name__ == "__main__":
    unittest.main()
