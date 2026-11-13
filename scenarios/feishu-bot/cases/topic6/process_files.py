"""Topic6 过程关键文件 → 飞书 job 文件夹/「过程数据」，供任务发起人排查问题。

数据来源：artifact_sync 已同步到本地的节点发布清单（{sync_root}/{job}/{project}/manifest.json），
不额外访问 Session。只传白名单里的关键节点文件，同一文件只取最新轮次：

  - 数据质量：A-2.5 数据质量报告、标准化表
  - 打标后：C0 合并结果（营销可用打标）、可用子集（C2/R1~R5 实际输入）
  - 聚簇后：C2 结果、c2_status、c2_quality、原热点保留审计、事件性判定
  - 合并：健康度摘要
  - 洞察：E1~E4 统计输入、E2 节点判定、E3 词频审计、E4 仲裁审计、Phase E 执行报告

不传：原始抓取、抽样表、单路原始标注、宽表（已是 HC 在线表格）、报告（已是飞书文档）、
checkpoint、洞察草稿。全部失败只记 warning，不影响任务状态。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

try:
    from .feishu_doc_publisher import FEISHU_FILE_URL_PREFIX
    from .pipeline_store import PipelineJob
except ImportError:  # pragma: no cover - standalone compatibility
    from feishu_doc_publisher import FEISHU_FILE_URL_PREFIX
    from pipeline_store import PipelineJob

log = logging.getLogger("arkagent.topic6.process_files")

PROCESS_FOLDER_NAME = "过程数据"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 飞书 upload_all 单文件上限
STATE_FILE = "process_files_state.json"

# 顺序即上传顺序（按流程节点）；{N}/{mode} 用通配。
KEY_FILE_PATTERNS: tuple[str, ...] = (
    "01_原始数据/data_quality_report.json",
    "02_标准化/hot_topics_normalized.xlsx",
    "04_合并/phase1_merged_*_r*.xlsx",
    "04_标注/_可用子集/usable_subset_*_r*.xlsx",
    "04_标注/C2_事件归档/c2_event_result_r*.xlsx",
    "04_标注/C2_事件归档/c2_run/c2_status.json",
    "04_标注/C2_事件归档/c2_run/engine/out/c2_quality.json",
    "04_标注/C2_事件归档/c2_run/engine/out/original_retained.json",
    "04_标注/C2_事件归档/c2_run/engine/out/01_eventness.csv",
    "05_合并/health_summary_*_r*.md",
    "06_洞察/v*/e1_data.md",
    "06_洞察/v*/e2_data.md",
    "06_洞察/v*/e3_data.md",
    "06_洞察/v*/e4_data.md",
    "06_洞察/v*/e2_flags.json",
    "06_洞察/v*/e3_word_freq_audit.md",
    "06_洞察/v*/e4_arbitration_audit.md",
    "06_洞察/v*/e4_quality.json",
    "06_洞察/v*/pipeline_e_report_v*.json",
)

_ROUND = re.compile(r"(?:_r|/v|_v)(\d+)(?=[/._])")


@dataclass(frozen=True)
class ProcessFile:
    source: str          # 项目内相对路径
    path: Path           # 本地物化文件
    sha256: str


def _round_key(source: str) -> tuple[str, int]:
    """把轮次号(_rN / vN / _vN)替换成占位，返回 (同类文件键, 轮次)；用于只保留最新轮次。"""
    rounds = [int(m) for m in _ROUND.findall(source)]
    return _ROUND.sub(lambda m: m.group(0)[: -len(m.group(1))] + "#", source), max(rounds, default=0)


def _pattern_index(source: str) -> Optional[int]:
    for idx, pattern in enumerate(KEY_FILE_PATTERNS):
        if fnmatch.fnmatchcase(source, pattern):
            return idx
    return None


def select_process_files(job_root: Path) -> list[ProcessFile]:
    """从 artifact_sync 物化目录挑出白名单关键文件，每类只取最新轮次。"""
    manifests = sorted(job_root.glob("*/manifest.json")) if job_root.is_dir() else []
    latest: dict[str, tuple[int, int, ProcessFile]] = {}
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        project_dir = manifest_path.parent
        for publication in manifest.get("publications") or []:
            for item in (publication or {}).get("files") or []:
                source = str((item or {}).get("source") or "")
                pure = PurePosixPath(source)
                if not source or pure.is_absolute() or ".." in pure.parts:
                    continue
                idx = _pattern_index(source)
                if idx is None:
                    continue
                local = project_dir.joinpath(*pure.parts)
                if not local.is_file():
                    continue
                key, round_no = _round_key(source)
                candidate = ProcessFile(source, local, str(item.get("sha256") or ""))
                if key not in latest or round_no >= latest[key][1]:
                    latest[key] = (idx, round_no, candidate)
    ordered = sorted(latest.values(), key=lambda entry: (entry[0], entry[2].source))
    return [entry[2] for entry in ordered]


def _flat_name(source: str) -> str:
    """飞书文件夹不分层：用 __ 拼接相对路径，保留节点信息且不重名。"""
    return "__".join(PurePosixPath(source).parts)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProcessFileDelivery:
    """同步调用（由上层 run_in_executor 包装）。同一 job 串行，状态落在 job 同步目录。"""

    def __init__(self, feishu, publisher, store, sync_root: Path) -> None:
        self._feishu = feishu
        self._publisher = publisher
        self._store = store
        self._sync_root = Path(sync_root)
        self._locks: dict[str, threading.Lock] = {}

    @property
    def enabled(self) -> bool:
        return self._feishu is not None and self._publisher is not None and self._publisher.enabled

    def _job_root(self, job_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(job_id).name.strip())
        return self._sync_root / (safe or "unknown-job")

    def deliver(self, job: PipelineJob) -> dict:
        lock = self._locks.setdefault(job.job_id, threading.Lock())
        with lock:
            return self._deliver(job)

    def _deliver(self, job: PipelineJob) -> dict:
        result = {"uploaded": 0, "skipped": 0, "failed": 0, "too_large": [], "folder_url": ""}
        job_root = self._job_root(job.job_id)
        files = select_process_files(job_root)
        if not files:
            return result

        # 任务可能在 HC1 前失败：job 文件夹按需创建并回写，和 HC 宽表共用同一个文件夹
        if not job.docs_folder_token:
            token = self._publisher.create_job_folder(job)
            if not token:
                result["failed"] = len(files)
                return result
            self._store.update_docs_folder_token(job.job_id, token)
            job.docs_folder_token = token
        result["folder_url"] = f"{FEISHU_FILE_URL_PREFIX}{job.docs_folder_token}"

        state_path = job_root / STATE_FILE
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("parent") != job.docs_folder_token:
            state = {"parent": job.docs_folder_token, "folder_token": "", "files": {}}
            # 发起人对 job 文件夹授权一次：未配置共享根时文件夹在机器人云空间，不授权发起人找不到
            self._publisher._grant_editor(job.docs_folder_token, "folder", job.user_open_id, job.job_id)  # noqa: SLF001
        if not state.get("folder_token"):
            try:
                state["folder_token"] = self._feishu.create_folder(job.docs_folder_token, PROCESS_FOLDER_NAME)
            except Exception as err:  # noqa: BLE001
                log.warning("topic6 process folder create failed job=%s: %s", job.job_id, err)
                result["failed"] = len(files)
                return result

        for item in files:
            digest = item.sha256 or _file_sha256(item.path)
            if state["files"].get(item.source, {}).get("sha256") == digest:
                result["skipped"] += 1
                continue
            if item.path.stat().st_size > MAX_UPLOAD_BYTES:
                result["too_large"].append(item.source)
                continue
            try:
                token = self._feishu.upload_drive_file(state["folder_token"], str(item.path), _flat_name(item.source))
            except Exception as err:  # noqa: BLE001
                result["failed"] += 1
                log.warning("topic6 process file upload failed job=%s file=%s: %s", job.job_id, item.source, err)
                continue
            state["files"][item.source] = {"sha256": digest, "file_token": token}
            result["uploaded"] += 1

        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as err:
            log.warning("topic6 process files state write failed job=%s: %s", job.job_id, err)
        log.info("topic6 process files delivered job=%s %s", job.job_id,
                 {k: v for k, v in result.items() if k != "folder_url"})
        return result
