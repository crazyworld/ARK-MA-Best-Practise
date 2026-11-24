"""过程文件发布（session_outputs）与测试回放（replay）。

回放只替换外部数据（n03b 采集、n05/n06/n13 数说）和标注缓存，且必须核对取数参数。
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import handlers
import replay
import session_outputs
from _util import make_state

PLAN = {
    "brand": "瑞幸", "activity_name": "联名", "platforms": ["xiaohongshu"],
    "period_start": "2026-09-28", "period_end": "2026-10-07",
    "brand_query": "瑞幸", "activity_query": "瑞幸+厘普", "content_scope": "all",
    "version": "v1",
}


def _bundle(path: Path, plan: dict, *, workbook_bytes: bytes = b"xlsx-bytes",
            stats: dict | None = None) -> Path:
    sha = hashlib.sha256(workbook_bytes).hexdigest()
    old_wb = "/workspace/old/tasks/t0/runs/r0/activity_collection/activity_adapted.xlsx"
    artifacts = {
        "confirmed_plan": plan,
        "activity_detail": {"source": "ssjh_social_task", "status": "ready", "rows": 3,
                            "workbook": old_wb, "sha256": sha, "campaigns": []},
        "brand_stats": stats or {"articles": {"xiaohongshu": [{"id": "a"}]}, "volume": 1},
        "brand_stats_baseline": {"articles": {}, "volume": 0},
        "wordcloud_data": {"campaigns": {"联名": {}}},
    }
    with zipfile.ZipFile(path, "w") as archive:
        manifest = {"schema_version": 1, "source": {"job_id": "job_old"}, "artifacts": {}}
        for key, payload in artifacts.items():
            archive.writestr(f"artifacts/{key}.json", json.dumps(payload, ensure_ascii=False))
            manifest["artifacts"][key] = f"artifacts/{key}.json"
        archive.writestr("files/activity_collection/activity_adapted.xlsx", workbook_bytes)
        archive.writestr("files/annotation_cache/round1.json", json.dumps({"k": {"x": 1}}))
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


class SessionOutputsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.outputs = self.tmp / "outputs"
        self.outputs.mkdir()
        session_outputs._last_publish.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_publishes_run_dir_incrementally_with_manifest(self):
        with patch.dict(os.environ, {"TOPIC5_SESSION_OUTPUTS_ROOT": str(self.outputs)}):
            st = make_state(self.tmp / "ws")
            st.write_artifact("confirmed_plan", PLAN)
            st.status = "awaiting_human"
            st.save()
            target = self.outputs / "t1" / "r1"
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            paths = {item["path"] for item in manifest["files"]}
            self.assertIn("run.json", paths)
            self.assertIn("artifacts/confirmed_plan/v1/payload.json", paths)
            self.assertFalse(any(p.startswith(".") for p in paths))
            # 未变化文件不重复复制
            self.assertEqual(session_outputs.publish(st.run_dir, "t1", "r1", force=True), 0)
            st.write_artifact("brand_stats", {"volume": 1})
            self.assertEqual(session_outputs.publish(st.run_dir, "t1", "r1", force=True), 1)

    def test_running_saves_are_throttled_and_missing_root_is_noop(self):
        with patch.dict(os.environ, {"TOPIC5_SESSION_OUTPUTS_ROOT": str(self.outputs)}):
            st = make_state(self.tmp / "ws")
            st.save()
            st.write_artifact("confirmed_plan", PLAN)
            st.save()  # running 且在节流窗口内：不发布新产物
            manifest = json.loads((self.outputs / "t1" / "r1" / "manifest.json").read_text())
            self.assertNotIn("artifacts/confirmed_plan/v1/payload.json",
                             {item["path"] for item in manifest["files"]})
        with patch.dict(os.environ, {"TOPIC5_SESSION_OUTPUTS_ROOT": str(self.tmp / "absent")}):
            self.assertEqual(session_outputs.publish(st.run_dir, "t1", "r1", force=True), 0)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st = make_state(self.tmp / "ws", task_id="t2", run_id="r2")
        self.st.write_artifact("confirmed_plan", dict(PLAN))
        self.bundle = _bundle(self.tmp / "bundle.zip", dict(PLAN))
        self.env = patch.dict(os.environ, {replay.ENV_BUNDLE: str(self.bundle)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_activity_pull_replays_workbook_without_paid_task(self):
        with patch("handlers.social_task.execute",
                   side_effect=AssertionError("回放不得创建计费任务")):
            result = handlers.activity_pull({"id": "n03b_activity_pull"}, self.st, {})
        detail = result["artifacts"]["activity_detail"]
        workbook = Path(detail["workbook"])
        self.assertEqual(workbook, self.st.run_dir / "activity_collection" / "activity_adapted.xlsx")
        self.assertEqual(workbook.read_bytes(), b"xlsx-bytes")
        source = result["artifacts"]["confirmed_plan"]["source_file"]
        self.assertEqual((source["transport"], source["path"]), ("social_task", str(workbook)))
        self.assertTrue((self.st.run_dir / "annotation_cache" / "round1.json").is_file())

    def test_ssjh_pull_replays_recorded_payload(self):
        with patch("handlers.ssjh.pull", side_effect=AssertionError("回放不得调用数说")):
            result = handlers.ssjh_pull(
                {"id": "n05_brand_pull", "outputs": ["brand_stats"]}, self.st, {})
        self.assertEqual(result["artifacts"]["brand_stats"]["volume"], 1)

    def test_scope_mismatch_stops(self):
        self.st.write_artifact("confirmed_plan", {**PLAN, "period_end": "2026-10-08"})
        with self.assertRaises(replay.ReplayMismatch) as caught:
            handlers.ssjh_pull({"id": "n05_brand_pull", "outputs": ["brand_stats"]}, self.st, {})
        self.assertIn("period_end", str(caught.exception))

    def test_degraded_recording_is_rejected(self):
        _bundle(self.bundle, dict(PLAN), stats={"degraded": True})
        with self.assertRaises(RuntimeError):
            handlers.ssjh_pull({"id": "n05_brand_pull", "outputs": ["brand_stats"]}, self.st, {})

    def test_tampered_workbook_is_rejected(self):
        with zipfile.ZipFile(self.bundle) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["files/activity_collection/activity_adapted.xlsx"] = b"tampered"
        with zipfile.ZipFile(self.bundle, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        with self.assertRaisesRegex(RuntimeError, "sha256"):
            handlers.activity_pull({"id": "n03b_activity_pull"}, self.st, {})

    def test_replay_disabled_without_env(self):
        self.env.stop()
        try:
            self.assertFalse(replay.enabled())
        finally:
            self.env.start()


if __name__ == "__main__":
    unittest.main()
