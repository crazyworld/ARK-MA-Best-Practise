"""FIX-04：活动原帖自动采集（ssjh-social-task v0.11.6，只抓帖子）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import excel_source  # noqa: E402
import handlers  # noqa: E402
import hc_bridge  # noqa: E402
import social_task  # noqa: E402
from _util import make_state  # noqa: E402

PLAN = {"confirmed": True, "version": "plan-v1", "platforms": ["小红书", "抖音"],
        "period_start": "2026-10-01", "period_end": "2026-10-07",
        "campaigns": [{"id": "A", "name": "瑞幸联名", "activity_query": "瑞幸+联名"}]}


def _record(i, **kw):
    return {"id": f"p{i}", "site_name": "小红书", "title": f"t{i}", "content": "瑞幸联名好喝",
            "data_type": "UGC", "publish_time": "2026-10-02 10:00:00", "author": "u",
            "url": f"https://x/{i}", "interaction_cnt": 5, "like_cnt": 3, "topics": ["瑞幸"], **kw}


class ConfigTest(unittest.TestCase):
    def test_msg_type_forced_1_and_keyword_fields(self):
        cfg = social_task.build_config(PLAN, PLAN["campaigns"][0], rid="r", output_file="o.xlsx")
        self.assertEqual(cfg["search"]["keyword_fields"], "content,title")
        for section in ("xiaohongshu", "short_video"):
            self.assertEqual(cfg["search"][section]["msg_type"], 1)
        self.assertEqual(cfg["profiler"]["keywords"], "瑞幸+联名")
        social_task.config_scope_check(cfg)                    # Skill 自身规范化通过

    def test_unsupported_platform_rejected(self):
        with self.assertRaises(ValueError):
            social_task.build_config({**PLAN, "platforms": ["电商"]}, PLAN["campaigns"][0],
                                     rid="r", output_file="o.xlsx")

    def test_request_id_stable(self):
        self.assertEqual(social_task.request_id("t", "A", "v1"), social_task.request_id("t", "A", "v1"))


class AdaptTest(unittest.TestCase):
    def test_rows_schema_equals_excel(self):
        adapted = social_task.adapt([_record(1), _record(1), _record(2)], "A", ("2026-10-01", "2026-10-07"))
        self.assertEqual(adapted["verification"]["duplicates"], 1)
        self.assertEqual(list(adapted["rows"][0]), list(social_task.ADAPTED_COLUMNS))
        self.assertIn("收藏数", adapted["missing_fields"])
        normalized = excel_source.normalize_excel_row(adapted["rows"][0])
        self.assertEqual(normalized["analysis_object"], "A")
        self.assertEqual(normalized["content_mode"], "UGC")

    def test_invalid_content_type_blocks_not_treated_as_comment(self):
        """D10：「内容类型」是 PGC/UGC/未知，非法值记异常并阻断 ready，不当作评论剔除。"""
        adapted = social_task.adapt([_record(1), _record(2, data_type="主帖")], "A", ("2026-10-01", "2026-10-07"))
        self.assertFalse(adapted["verification"]["ok"])
        self.assertEqual(adapted["verification"]["invalid_content_types"], {"主帖": 1})
        self.assertEqual(len(adapted["rows"]), 2)
        ok = social_task.adapt([_record(1), _record(2, data_type="未知")], "A", ("2026-10-01", "2026-10-07"))
        self.assertTrue(ok["verification"]["ok"])

    def test_workbook_roundtrip_via_excel_source(self):
        adapted = social_task.adapt([_record(i) for i in range(3)], "A", ("2026-10-01", "2026-10-07"))
        path = Path(tempfile.mkdtemp()) / "activity_adapted.xlsx"
        sha = social_task.write_workbook(adapted["rows"], path)
        plan = {**PLAN, "source_file": {"name": "x", "mount_path": path.name, "transport": "social_task",
                                        "path": str(path), "sha256": sha}}
        os.environ.pop("TOPIC5_ACTIVITY_XLSX_PATH", None)
        data = excel_source.load_activity_rows(plan)
        self.assertEqual(len(data["rows"]), 3)


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())

    def test_user_file_bypasses_collection(self):
        self.st.write_artifact("confirmed_plan", {**PLAN, "source_file": {"mount_path": "a.xlsx"}})
        with patch.object(social_task, "execute") as execute:
            out = handlers.activity_pull({"id": "n03b"}, self.st, {})["artifacts"]
        execute.assert_not_called()
        self.assertEqual(out["activity_detail"]["source"], "user_uploaded_excel")

    def test_precheck_pauses_for_authorization(self):
        self.st.write_artifact("confirmed_plan", PLAN)
        envelope = {"ok": True, "state": "AWAITING_QUANTITY_CONFIRMATION", "data": {"estimated_volume": 1200}}
        with patch.object(social_task, "api_available", return_value=True), \
                patch.object(social_task, "execute", return_value=envelope):
            with self.assertRaises(handlers.HumanPause) as pause:
                handlers.activity_pull({"id": "n03b"}, self.st, {})
        self.assertIn("1200", pause.exception.prompt)
        self.assertEqual(pause.exception.expect, ["collection_authorization"])

    def test_budget_block_stops(self):
        self.st.write_artifact("confirmed_plan", PLAN)
        envelope = {"ok": False, "code": "BUDGET_LIMIT_EXCEEDED", "message": "超出数据费费控阈值1000元限制"}
        with patch.object(social_task, "api_available", return_value=True), \
                patch.object(social_task, "execute", return_value=envelope):
            with self.assertRaisesRegex(RuntimeError, "1000"):
                handlers.activity_pull({"id": "n03b"}, self.st, {})

    def _completed(self, run_dir, msg_type="1"):
        (run_dir / "state.json").write_text(json.dumps({"contract": {"canonical_query": [
            {"platform": "xiaohongshu", "msg_type": msg_type}]}}), encoding="utf-8")
        raw = run_dir / "records.json"
        raw.write_text(json.dumps([_record(i) for i in range(3)], ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "state": "COMPLETED", "job_id": 9,
                "data": {"run_dir": str(run_dir), "raw_data": str(raw), "quality": {"status": "PASS"}}}

    def test_authorized_collect_writes_source_and_handoff(self):
        self.st.write_artifact("confirmed_plan", PLAN)
        self.st.write_artifact("collection_authorization", {"campaigns": {"A": {
            "max_records": 3000, "confirmation_ref": "feishu-hc:1:u"}}})
        run_dir = Path(tempfile.mkdtemp())
        with patch.object(social_task, "api_available", return_value=True), \
                patch.object(social_task, "execute", return_value=self._completed(run_dir)) as execute:
            out = handlers.activity_pull({"id": "n03b"}, self.st, {})["artifacts"]
        self.assertEqual(execute.call_args.kwargs["max_records"], 3000)
        self.assertEqual(out["confirmed_plan"]["source_file"]["transport"], "social_task")
        handoff = out["activity_detail"]["campaigns"][0]
        self.assertTrue(handoff["posts_only"])
        self.assertEqual(handoff["authorization_ref"], "feishu-hc:1:u")
        self.assertEqual(out["activity_detail"]["rows"], 3)

    def test_resume_with_comments_stops(self):
        self.st.write_artifact("confirmed_plan", PLAN)
        self.st.write_artifact("collection_authorization", {"campaigns": {"A": {
            "max_records": 10, "confirmation_ref": "x"}}})
        run_dir = Path(tempfile.mkdtemp())
        with patch.object(social_task, "api_available", return_value=True), \
                patch.object(social_task, "execute", return_value=self._completed(run_dir, "0")):
            with self.assertRaisesRegex(RuntimeError, "范围不符"):
                handlers.activity_pull({"id": "n03b"}, self.st, {})


class SourceFallbackTest(unittest.TestCase):
    """n03b 采集失败后允许改为上传活动明细（replace-source），其他失败节点仍禁止。"""

    def _failed_at(self, node_id, detail):
        import engine
        st = make_state(tempfile.mkdtemp())
        st.write_artifact("confirmed_plan", {**PLAN, "confirmed": True})
        st.status = "failed"
        st.current_node = node_id
        st.node_rec(node_id)["status"] = "failed"
        st.event(node_id, "failed", detail)
        return engine, st

    def test_collect_failure_accepts_upload(self):
        engine, st = self._failed_at("n03b_activity_pull", "RuntimeError: activity_pull: 自动采集不可用")
        engine.replace_failed_source(st, source_name="a.xlsx", source_mount_path="a.xlsx")
        self.assertEqual(st.read_artifact("confirmed_plan")["source_file"]["mount_path"], "a.xlsx")

    def test_other_failure_rejected(self):
        engine, st = self._failed_at("n03b_activity_pull", "RuntimeError: something else")
        with self.assertRaises(ValueError):
            engine.replace_failed_source(st, source_name="a.xlsx", source_mount_path="a.xlsx")


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.human = {"node": "n03b_activity_pull", "expect": ["collection_authorization"]}

    def test_authorization_requires_limit_and_ref(self):
        with self.assertRaises(ValueError):
            hc_bridge.build_human_outputs(self.st, "pass", json.dumps({"campaigns": {"A": {"max_records": 0}}}))
        out = hc_bridge.build_human_outputs(self.st, "pass", json.dumps(
            {"campaigns": {"A": {"max_records": 100, "confirmation_ref": "r"}}}))
        self.assertEqual(out["_control"]["next"], "n03b_activity_pull")

    def test_switch_to_upload(self):
        self.st.write_artifact("confirmed_plan", PLAN)
        out = hc_bridge.build_human_outputs(self.st, "upload", json.dumps(
            {"source_file": {"name": "a.xlsx", "mount_path": "a.xlsx"}}))
        self.assertEqual(out["confirmed_plan"]["source_file"]["mount_path"], "a.xlsx")


if __name__ == "__main__":
    unittest.main()
