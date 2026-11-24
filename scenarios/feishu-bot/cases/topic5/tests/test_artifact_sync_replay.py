"""过程文件同步 → 本地还原 → 回放包 → 沙箱回放 的端到端往返。

用真实的 session_outputs 发布一份 run 目录，模拟 Files API（只给文件名 + 下载地址，
不给路径、不给 size，与 212 实测一致），验证 Gateway 还原的目录与沙箱一致，
并能打成 replay.py 可直接消费的回放包。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

CASE = Path(__file__).resolve().parent.parent
ORCH = CASE / "ma-resources" / "skills" / "pipeline-orchestrator"
sys.path[:0] = [str(CASE), str(CASE.parents[1]), str(ORCH)]

from artifact_sync import ReplayBundleError, Topic5ArtifactSync, build_replay_bundle  # noqa: E402

PLAN = {"brand": "瑞幸", "activity_name": "联名", "platforms": ["xiaohongshu"],
        "period_start": "2026-09-28", "period_end": "2026-10-07",
        "brand_query": "瑞幸", "activity_query": "瑞幸+厘普", "version": "v1"}


class FakeFilesApi:
    def __init__(self, root: Path):
        self.root = root

    async def list_agent_files(self, session_id):
        items = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(self.root).as_posix()
                items.append({"id": "file-" + hashlib.sha256(rel.encode()).hexdigest()[:10],
                              "filename": path.name, "download_url": "https://x/" + rel,
                              "updated_at": str(path.stat().st_mtime_ns)})
        return items

    async def download_file(self, url):
        return (self.root / url.removeprefix("https://x/")).read_bytes()


def _record_run(tmp_path: Path, monkeypatch) -> Path:
    """在“沙箱”里跑出一份带外部数据的 run，并发布到 outputs。"""
    import session_outputs
    from state import RunState, register_run_dir

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("TOPIC5_SESSION_OUTPUTS_ROOT", str(outputs))
    session_outputs._last_publish.clear()
    run_dir = tmp_path / "sandbox" / "tasks" / "task-1" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    register_run_dir("run-1", run_dir)
    st = RunState(run_id="run-1", task_id="task-1", dag_slug="s", dag_version="0.2",
                  entry="n00", params={})
    workbook = run_dir / "activity_collection" / "activity_adapted.xlsx"
    workbook.parent.mkdir(parents=True)
    workbook.write_bytes(b"xlsx")
    # 两个同名 payload.json + 同内容不同文件，覆盖“只给文件名”的还原难点
    st.write_artifact("confirmed_plan", PLAN)
    st.write_artifact("activity_detail", {
        "source": "ssjh_social_task", "status": "ready", "rows": 1,
        "workbook": str(workbook), "sha256": hashlib.sha256(b"xlsx").hexdigest()})
    st.write_artifact("brand_stats", {"volume": 1})
    st.write_artifact("brand_stats_baseline", {"volume": 0})
    st.write_artifact("wordcloud_data", {"volume": 0})
    (run_dir / "annotation_cache").mkdir()
    (run_dir / "annotation_cache" / "round1.json").write_text("{}", encoding="utf-8")
    st.status = "completed"
    st.save()
    return outputs


def test_sync_restores_run_tree_and_builds_replayable_bundle(tmp_path, monkeypatch):
    outputs = _record_run(tmp_path, monkeypatch)
    sync = Topic5ArtifactSync(FakeFilesApi(outputs), tmp_path / "local", lambda _: False)
    assert asyncio.run(sync.sync_once("job_old", "sesn")) > 0
    runs = sync.local_run_dirs("job_old")
    assert [p.relative_to(tmp_path / "local").as_posix() for p in runs] == \
        ["job_old/task-1/run-1"]
    local_run = runs[0]
    sandbox_run = tmp_path / "sandbox" / "tasks" / "task-1" / "runs" / "run-1"
    for path in sandbox_run.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            rel = path.relative_to(sandbox_run)
            assert (local_run / rel).read_bytes() == path.read_bytes(), rel
    # 增量：再同步无新下载
    assert asyncio.run(sync.sync_once("job_old", "sesn")) == 0

    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(build_replay_bundle(local_run, {"job_id": "job_old"}))

    import replay
    from state import RunState, register_run_dir
    monkeypatch.setenv(replay.ENV_BUNDLE, str(bundle))
    new_run = tmp_path / "new" / "run-2"
    new_run.mkdir(parents=True)
    register_run_dir("run-2", new_run)
    assert replay.load(new_run, "brand_stats", dict(PLAN)) == {"volume": 1}
    restored = replay.restore_file(new_run, "activity_collection/activity_adapted.xlsx")
    assert restored.read_bytes() == b"xlsx"
    assert replay.seed_annotation_cache(new_run) == 1


def test_bundle_requires_all_external_artifacts(tmp_path, monkeypatch):
    outputs = _record_run(tmp_path, monkeypatch)
    sync = Topic5ArtifactSync(FakeFilesApi(outputs), tmp_path / "local", lambda _: False)
    asyncio.run(sync.sync_once("job_old", "sesn"))
    local_run = sync.local_run_dirs("job_old")[0]
    state = json.loads((local_run / "run.json").read_text(encoding="utf-8"))
    state["artifacts"].pop("wordcloud_data")
    (local_run / "run.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ReplayBundleError, match="wordcloud_data"):
        build_replay_bundle(local_run, {"job_id": "job_old"})
