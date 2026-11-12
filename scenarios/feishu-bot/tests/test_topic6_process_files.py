"""过程关键文件归档到飞书（job 文件夹/过程数据），供发起人排查问题。

范围：只传白名单里的关键节点文件（打标后、聚簇后、C2 诊断、洞察统计输入与审计），
同一文件只取最新轮次；宽表已是 HC 在线表格、报告已是飞书文档，不重复上传。
失败只记 warning，不影响任务状态。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

CASE_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"


def _load(module_name: str, file_name: str):
    if str(CASE_DIR) not in sys.path:
        sys.path.insert(0, str(CASE_DIR))
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, CASE_DIR / file_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


store_mod = _load("pipeline_store", "pipeline_store.py")
pf = _load("process_files", "process_files.py")
PipelineJob = store_mod.PipelineJob

PROJECT = "W45热点周报_20261102-20261108"


def _job(**kw):
    base = dict(job_id="job_fe11fa6b2761", chat_id="oc_chat", thread_id="", user_open_id="ou_creator",
                ma_session_id="sess", mode="skip_sampling", project_dir=f"/workspace/Projects/{PROJECT}",
                current_phase="E", status="failed", docs_folder_token="")
    base.update(kw)
    return PipelineJob(**base)


def _write_sync(tmp_path: Path, publications: list[tuple[str, int, list[str]]]) -> Path:
    """按 artifact_sync 的 materialize 结构写 {job}/{project}/manifest.json 与文件。"""
    job_root = tmp_path / "job_fe11fa6b2761"
    pubs = []
    for node, run_id, sources in publications:
        files = []
        for src in sources:
            path = job_root / PROJECT / src
            path.parent.mkdir(parents=True, exist_ok=True)
            content = f"{node}:{run_id}:{src}".encode()
            path.write_bytes(content)
            files.append({"source": src, "output": f"{PROJECT}/{src}", "size": len(content),
                          "sha256": hashlib.sha256(content).hexdigest()})
        pubs.append({"node": node, "run_id": str(run_id), "status": "complete",
                     "published_at": f"2026-11-0{run_id}T00:00:00Z", "files": files})
    (job_root / PROJECT).mkdir(parents=True, exist_ok=True)
    (job_root / PROJECT / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "project": PROJECT, "publications": pubs}, ensure_ascii=False),
        encoding="utf-8")
    return job_root


W45_PUBLICATIONS = [
    ("fetch_normalize", 1, ["01_原始数据/hot_topics_raw.xlsx", "01_原始数据/hot_topics_skill_raw.json",
                            "02_标准化/hot_topics_normalized.xlsx", "01_原始数据/data_quality_report.json"]),
    ("sample", 1, ["03_抽样/sample_500.xlsx"]),
    ("c0", 1, ["04_标注/C0_基础事实/c0_postprocess_r1.xlsx", "04_标注/C0_基础事实/c0_result_raw_r1.xlsx"]),
    ("c0_filter", 1, ["04_合并/phase1_merged_skip_sampling_r1.xlsx",
                      "04_标注/_可用子集/usable_subset_skip_sampling_r1.xlsx"]),
    ("r2", 1, ["04_标注/R2_商业合作/r2_postprocess_r1.xlsx"]),
    ("c2", 1, ["04_标注/C2_事件归档/c2_event_result_r1.xlsx", "04_标注/C2_事件归档/c2_run/c2_status.json",
               "04_标注/C2_事件归档/c2_run/engine/out/c2_quality.json",
               "04_标注/C2_事件归档/c2_run/engine/out/original_retained.json",
               "04_标注/C2_事件归档/c2_run/engine/out/01_eventness.csv"]),
    ("merge", 1, ["05_合并/wide_table_skip_sampling_r1.xlsx", "05_合并/health_summary_skip_sampling_r1.md"]),
    ("insight", 1, ["06_洞察/v1/e1_data.md", "06_洞察/v1/e4_data.md", "06_洞察/v1/e2_flags.json",
                    "06_洞察/v1/e4_arbitration_audit.md", "06_洞察/v1/e3_word_freq_audit.md",
                    "06_洞察/v1/pipeline_e_report_v1.json", "06_洞察/v1/pipeline_e_checkpoint_v1.json",
                    "06_洞察/v1/e4_v1.md", "06_洞察/v1/e4_candidates.json"]),
    ("report", 1, ["07_报告/热点报告_2026-W45_v1.md"]),
]


def test_select_only_key_files_and_skip_bulky_or_duplicated(tmp_path):
    job_root = _write_sync(tmp_path, W45_PUBLICATIONS)
    picked = [p.source for p in pf.select_process_files(job_root)]
    assert picked == [
        "01_原始数据/data_quality_report.json",
        "02_标准化/hot_topics_normalized.xlsx",
        "04_合并/phase1_merged_skip_sampling_r1.xlsx",
        "04_标注/_可用子集/usable_subset_skip_sampling_r1.xlsx",
        "04_标注/C2_事件归档/c2_event_result_r1.xlsx",
        "04_标注/C2_事件归档/c2_run/c2_status.json",
        "04_标注/C2_事件归档/c2_run/engine/out/c2_quality.json",
        "04_标注/C2_事件归档/c2_run/engine/out/original_retained.json",
        "04_标注/C2_事件归档/c2_run/engine/out/01_eventness.csv",
        "05_合并/health_summary_skip_sampling_r1.md",
        "06_洞察/v1/e1_data.md",
        "06_洞察/v1/e4_data.md",
        "06_洞察/v1/e2_flags.json",
        "06_洞察/v1/e3_word_freq_audit.md",
        "06_洞察/v1/e4_arbitration_audit.md",
        "06_洞察/v1/pipeline_e_report_v1.json",
    ]
    # 原始数据、抽样、单路原始标注、宽表(HC 在线表)、报告(飞书文档)、checkpoint、洞察草稿不传
    for skipped in ("hot_topics_raw.xlsx", "sample_500.xlsx", "c0_result_raw", "r2_postprocess",
                    "wide_table_", "热点报告_", "checkpoint", "e4_v1.md", "e4_candidates.json"):
        assert not any(skipped in s for s in picked), skipped


def test_select_takes_latest_run_only(tmp_path):
    job_root = _write_sync(tmp_path, [
        ("c2", 1, ["04_标注/C2_事件归档/c2_event_result_r1.xlsx"]),
        ("c2", 2, ["04_标注/C2_事件归档/c2_event_result_r2.xlsx"]),
        ("insight", 1, ["06_洞察/v1/e1_data.md"]),
        ("insight", 2, ["06_洞察/v2/e1_data.md"]),
    ])
    picked = [p.source for p in pf.select_process_files(job_root)]
    assert picked == ["04_标注/C2_事件归档/c2_event_result_r2.xlsx", "06_洞察/v2/e1_data.md"]


def test_select_without_manifest_returns_empty(tmp_path):
    assert pf.select_process_files(tmp_path / "missing") == []


class _Feishu:
    def __init__(self, fail_upload: bool = False):
        self.folders, self.uploads, self.perms = [], [], []
        self.fail_upload = fail_upload

    def create_folder(self, parent_token, name):
        self.folders.append((parent_token, name))
        return f"fld_{len(self.folders)}"

    def upload_drive_file(self, folder_token, file_path, file_name):
        if self.fail_upload:
            raise RuntimeError("drive upload forbidden")
        self.uploads.append((folder_token, Path(file_path).read_bytes(), file_name))
        return f"box_{len(self.uploads)}"

    def add_permission(self, token, token_type, member_open_id, perm="full_access"):
        self.perms.append((token, token_type, member_open_id, perm))

    def list_permission_members(self, token, token_type):
        return [{"member_id": m, "perm": p} for t, tt, m, p in self.perms if t == token and tt == token_type]


def _deliverer(tmp_path, feishu, job, root_token="fld_root"):
    store = SimpleNamespace(updated={}, update_docs_folder_token=lambda jid, tok: store.updated.__setitem__(jid, tok))
    publisher = _load("feishu_doc_publisher", "feishu_doc_publisher.py").FeishuDocPublisher(feishu, root_token)
    return pf.ProcessFileDelivery(feishu, publisher, store, tmp_path), store


def test_deliver_creates_job_folder_subfolder_grants_creator_and_is_idempotent(tmp_path):
    _write_sync(tmp_path, W45_PUBLICATIONS)
    feishu = _Feishu()
    job = _job()
    delivery, store = _deliverer(tmp_path, feishu, job)

    result = delivery.deliver(job)
    assert result["uploaded"] == 16 and result["failed"] == 0
    # 任务在 HC 前失败也能落地：先建 job 文件夹并回写，再建「过程数据」子文件夹
    assert feishu.folders[0] == ("fld_root", f"{PROJECT}_job_fe11fa6b")
    assert feishu.folders[1] == ("fld_1", "过程数据")
    assert store.updated["job_fe11fa6b2761"] == "fld_1" and job.docs_folder_token == "fld_1"
    # 发起人对 job 文件夹有编辑权限（能找到宽表与过程数据）
    assert ("fld_1", "folder", "ou_creator", "edit") in feishu.perms
    assert result["folder_url"].endswith("/fld_1")
    names = [n for _, _, n in feishu.uploads]
    assert "04_标注__C2_事件归档__c2_event_result_r1.xlsx" in names
    assert all(folder == "fld_2" for folder, _, _ in feishu.uploads)

    # 重复触发（HC 后再到终态）不重复上传未变化的文件
    again = delivery.deliver(job)
    assert again["uploaded"] == 0 and again["skipped"] == 16
    assert len(feishu.uploads) == 16


def test_deliver_reuses_existing_job_folder(tmp_path):
    _write_sync(tmp_path, [("c2", 1, ["04_标注/C2_事件归档/c2_event_result_r1.xlsx"])])
    feishu = _Feishu()
    job = _job(docs_folder_token="fld_existing")
    delivery, _ = _deliverer(tmp_path, feishu, job)
    result = delivery.deliver(job)
    assert result["uploaded"] == 1
    assert feishu.folders == [("fld_existing", "过程数据")]


def test_deliver_upload_failure_is_reported_not_raised(tmp_path):
    _write_sync(tmp_path, [("c2", 1, ["04_标注/C2_事件归档/c2_event_result_r1.xlsx"])])
    feishu = _Feishu(fail_upload=True)
    job = _job(docs_folder_token="fld_existing")
    delivery, _ = _deliverer(tmp_path, feishu, job)
    result = delivery.deliver(job)
    assert result["uploaded"] == 0 and result["failed"] == 1
    # 失败的文件下次仍会重试
    feishu.fail_upload = False
    assert delivery.deliver(job)["uploaded"] == 1


def test_deliver_skips_oversized_file(tmp_path, monkeypatch):
    _write_sync(tmp_path, [("c2", 1, ["04_标注/C2_事件归档/c2_event_result_r1.xlsx"])])
    monkeypatch.setattr(pf, "MAX_UPLOAD_BYTES", 3)
    feishu = _Feishu()
    job = _job(docs_folder_token="fld_existing")
    delivery, _ = _deliverer(tmp_path, feishu, job)
    result = delivery.deliver(job)
    assert result["uploaded"] == 0 and result["too_large"] == ["04_标注/C2_事件归档/c2_event_result_r1.xlsx"]


def test_terminal_notification_includes_process_folder_link():
    notifier_mod = _load("job_notifier", "job_notifier.py")
    sent = []
    feishu = SimpleNamespace(send_to_chat=lambda chat, text, roster=None: sent.append(text),
                             resolve_operator_name=lambda *a, **k: "")
    notifier = notifier_mod.JobNotifier(feishu)
    notifier._get_roster = lambda chat_id: None  # noqa: SLF001
    notifier.notify_process_files(_job(), "https://bluefocus.feishu.cn/drive/folder/fld_1", 16)
    assert "过程数据" in sent[0] and "fld_1" in sent[0] and "16" in sent[0]


def test_coordinator_publishes_c2_diagnostics_and_quality_report():
    coordinator = (CASE_DIR / "ma-resources" / "agents" / "coordinator.system.md").read_text(encoding="utf-8")
    for path in ("c2_run/engine/out/c2_quality.json", "c2_run/engine/out/original_retained.json",
                 "c2_run/engine/out/01_eventness.csv", "01_原始数据/data_quality_report.json"):
        assert path in coordinator, path



def test_hitl_delivers_process_files_before_hc_card_without_blocking():
    hitl_src = (CASE_DIR / "topic6_hitl.py").read_text(encoding="utf-8")
    assert "process_files: Optional[\"ProcessFileDelivery\"] = None" in hitl_src
    assert "self._process_files.deliver" in hitl_src and "create_task(self._deliver_process_files(_job))" in hitl_src


def test_runner_delivers_process_files_on_terminal_state_and_notifies_once():
    runner_src = (CASE_DIR / "topic6_runner.py").read_text(encoding="utf-8")
    assert "def bind_process_files" in runner_src
    assert "_deliver_process_files(job_id)" in runner_src
    assert "notify_process_files" in runner_src
    gateway_src = (CASE_DIR / "gateway.py").read_text(encoding="utf-8")
    assert "ProcessFileDelivery(" in gateway_src and "runner.bind_process_files(" in gateway_src


def test_runner_terminal_delivery_syncs_then_uploads_then_notifies(tmp_path):
    runner_mod = _load("topic6_runner", "topic6_runner.py")
    calls = []

    class _Sync:
        async def sync_once(self, job_id, session_id):
            calls.append(("sync", job_id))

    class _Delivery:
        enabled = True

        def deliver(self, job):
            calls.append(("deliver", job.job_id))
            return {"uploaded": 3, "skipped": 13, "failed": 0, "too_large": [],
                    "folder_url": "https://x/drive/folder/fld_1"}

    class _Notifier:
        def notify_process_files(self, job, url, count):
            calls.append(("notify", url, count))

    job = _job()
    runner = runner_mod.Topic6Runner.__new__(runner_mod.Topic6Runner)
    runner._store = SimpleNamespace(get_job=lambda jid: job)  # noqa: SLF001
    runner._notifier = _Notifier()  # noqa: SLF001
    runner.bind_process_files(_Delivery(), _Sync())
    asyncio.run(runner._deliver_process_files(job.job_id))  # noqa: SLF001
    assert calls == [("sync", job.job_id), ("deliver", job.job_id), ("notify", "https://x/drive/folder/fld_1", 16)]

    # 没有新上传（重复终态）时不重复通知
    class _Nothing(_Delivery):
        def deliver(self, job):
            return {"uploaded": 0, "skipped": 16, "failed": 0, "too_large": [], "folder_url": "u"}
    calls.clear()
    runner.bind_process_files(_Nothing(), _Sync())
    asyncio.run(runner._deliver_process_files(job.job_id))  # noqa: SLF001
    assert [c[0] for c in calls] == ["sync"]
