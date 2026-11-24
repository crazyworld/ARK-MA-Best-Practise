"""Topic5 过程文件同步：MA Session 输出目录 → Gateway 本地（与 topic6 artifact_sync 同一机制）。

沙箱编排器每次保存 run 状态时把 run 目录增量发布到 /mnt/session/outputs/<task>/<run>/
（pipeline-orchestrator/session_outputs.py），平台登记为 Session 的 Files API 对象。
本模块按 Session 轮询 Files API：
  - 以 file_id + updated_at 增量下载到 <sync_root>/<job_id>/.objects/；
  - 按 <task>/<run>/manifest.json 的 sha256 把对象还原成 run 目录结构
    <sync_root>/<job_id>/<task>/<run>/...（Files API 列表只给文件名，不给路径）。
同步不发飞书消息；失败只记日志并在下个周期重试。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

log = logging.getLogger("arkagent.case.topic5.artifacts")

DEFAULT_POLL_INTERVAL_SEC = 60.0
MANIFEST_NAME = "manifest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_component(value: str, fallback: str) -> str:
    text = Path(value).name.strip()
    if text in {"", ".", ".."}:
        return fallback
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in text)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                     dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _safe_relative(value: str) -> Optional[PurePosixPath]:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    return path


class Topic5ArtifactSync:
    def __init__(
        self,
        ark,
        sync_root: Path,
        is_active: Callable[[str], bool],
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self._ark = ark
        self._sync_root = Path(sync_root)
        self._is_active = is_active
        self._poll_interval_sec = poll_interval_sec
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def sync_root(self) -> Path:
        return self._sync_root

    def job_root(self, job_id: str) -> Path:
        return self._sync_root / _safe_component(job_id, "unknown-job")

    def start(self, job_id: str, session_id: str, loop: asyncio.AbstractEventLoop) -> None:
        """幂等：该 job 已有同步任务在跑时不重复启动。"""
        if not job_id or not session_id:
            return
        current = self._tasks.get(job_id)
        if current is not None and not current.done():
            return
        task = loop.create_task(self._run(job_id, session_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda done, jid=job_id: self._done(jid, done))

    def stop(self, job_id: str) -> None:
        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _done(self, job_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("topic5 artifact sync stopped job=%s: %s", job_id, task.exception())

    async def _run(self, job_id: str, session_id: str) -> None:
        while True:
            active = self._is_active(job_id)
            try:
                await self.sync_once(job_id, session_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 轮询重试瞬时失败
                log.warning("topic5 artifact sync failed job=%s session=%s: %s",
                            job_id, session_id, error)
            if not active:
                return  # 终态后已做最后一次同步
            await asyncio.sleep(self._poll_interval_sec)

    async def sync_once(self, job_id: str, session_id: str) -> int:
        job_root = self.job_root(job_id)
        index_path = job_root / "index.json"
        index = self._read_index(index_path, job_id, session_id)
        downloaded = 0
        for item in await self._ark.list_agent_files(session_id):
            file_id = str(item.get("id") or "").strip()
            download_url = str(item.get("download_url") or "").strip()
            if not file_id or not download_url:
                continue
            fingerprint = {"size": item.get("size"), "updated_at": str(item.get("updated_at") or "")}
            previous = index["files"].get(file_id)
            if (
                isinstance(previous, dict)
                and previous.get("fingerprint") == fingerprint
                and (job_root / str(previous.get("object_path") or "")).is_file()
            ):
                continue
            filename = str(item.get("filename") or item.get("name") or file_id)
            object_name = (f"{hashlib.sha256(file_id.encode()).hexdigest()[:16]}__"
                           f"{_safe_component(filename, 'artifact')}")
            content = await self._ark.download_file(download_url)
            _atomic_write(job_root / ".objects" / object_name, content)
            index["files"][file_id] = {
                "file_id": file_id,
                "filename": filename,
                "fingerprint": fingerprint,
                "object_path": f".objects/{object_name}",
                "sha256": _sha256_bytes(content),
                "synced_at": _utc_now(),
            }
            downloaded += 1
        if downloaded:
            self._materialize(job_root, index)
            index["updated_at"] = _utc_now()
            _atomic_write(index_path, (json.dumps(index, ensure_ascii=False, indent=2) + "\n")
                          .encode("utf-8"))
            log.info("topic5 artifact synced job=%s downloaded=%s root=%s",
                     job_id, downloaded, job_root)
        return downloaded

    @staticmethod
    def _read_index(path: Path, job_id: str, session_id: str) -> dict:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
                    return payload
            except (OSError, ValueError):
                log.warning("ignoring invalid topic5 artifact index: %s", path)
        return {"schema_version": 1, "job_id": job_id, "session_id": session_id,
                "updated_at": "", "files": {}}

    @staticmethod
    def _materialize(job_root: Path, index: dict) -> None:
        """按各 run 的 manifest（相对路径 + sha256）把下载对象还原为目录结构。"""
        objects = [item for item in index["files"].values() if isinstance(item, dict)]
        by_sha: dict[str, list[dict]] = {}
        for item in objects:
            by_sha.setdefault(str(item.get("sha256") or ""), []).append(item)
        manifests = []
        for item in objects:
            if Path(str(item.get("filename") or "")).name != MANIFEST_NAME:
                continue
            try:
                manifest = json.loads((job_root / item["object_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError):
                continue
            if isinstance(manifest, dict) and isinstance(manifest.get("files"), list):
                manifests.append(manifest)
        # 覆盖写会产生新 file_id，同一 run 有多个历史 manifest：只用 published_ns 最新的一份。
        latest: dict[tuple[str, str], dict] = {}
        for manifest in manifests:
            key = (str(manifest.get("task_id") or ""), str(manifest.get("run_id") or ""))
            if key not in latest or int(manifest.get("published_ns") or 0) >= \
                    int(latest[key].get("published_ns") or 0):
                latest[key] = manifest
        for manifest in latest.values():
            task = _safe_component(str(manifest.get("task_id") or ""), "")
            run = _safe_component(str(manifest.get("run_id") or ""), "")
            if not task or not run:
                continue
            run_root = job_root / task / run
            for entry in manifest["files"]:
                if not isinstance(entry, dict):
                    continue
                relative = _safe_relative(str(entry.get("path") or ""))
                candidates = by_sha.get(str(entry.get("sha256") or ""), [])
                if relative is None or not candidates:
                    continue
                named = [c for c in candidates
                         if Path(str(c.get("filename") or "")).name == relative.name]
                source = job_root / (named or candidates)[0]["object_path"]
                destination = run_root.joinpath(*relative.parts)
                if destination.is_file() and destination.stat().st_size == source.stat().st_size \
                        and _sha256_bytes(destination.read_bytes()) == entry.get("sha256"):
                    continue
                _atomic_copy(source, destination)
            _atomic_write(run_root / MANIFEST_NAME,
                          (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def local_run_dirs(self, job_id: str) -> list[Path]:
        """已同步的 run 目录（含 manifest.json），按修改时间倒序。"""
        root = self.job_root(job_id)
        runs = [path.parent for path in root.glob(f"*/*/{MANIFEST_NAME}")
                if not path.parent.name.startswith(".")]
        return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)


# ---- 测试回放包（pipeline-orchestrator/replay.py 的输入）----------------------

REPLAY_SCHEMA_VERSION = 1
# 外部数据产物 + 用于核对取数参数的 confirmed_plan。
REPLAY_ARTIFACT_KEYS = ("confirmed_plan", "activity_detail", "brand_stats",
                        "brand_stats_baseline", "wordcloud_data")


class ReplayBundleError(RuntimeError):
    pass


def _artifact_relative(meta: dict, key: str) -> PurePosixPath:
    """run.json 里记录的是沙箱绝对路径；回放只认 run 目录内的 artifacts/<key>/v<n>/payload.json。"""
    version = meta.get("version")
    if not isinstance(version, int) or version < 1:
        raise ReplayBundleError(f"录制 run.json 中 {key} 版本无效")
    return PurePosixPath("artifacts", key, f"v{version}", "payload.json")


def build_replay_bundle(run_dir: Path, source: dict) -> bytes:
    """把已同步到本地的历史 run 打成回放包（zip 字节）。缺任何必需产物即报错。"""
    import io
    import zipfile

    run_dir = Path(run_dir)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    artifacts = state.get("artifacts") or {}
    invalidated = set(state.get("invalidated_artifacts") or [])
    buffer = io.BytesIO()
    manifest: dict = {"schema_version": REPLAY_SCHEMA_VERSION, "source": dict(source),
                      "artifacts": {}}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for key in REPLAY_ARTIFACT_KEYS:
            meta = artifacts.get(key)
            if not isinstance(meta, dict) or key in invalidated:
                raise ReplayBundleError(f"录制任务缺少可用的 {key}，不能作为回放来源")
            payload_path = run_dir.joinpath(*_artifact_relative(meta, key).parts)
            if not payload_path.is_file():
                raise ReplayBundleError(f"本地尚未同步 {key}（{payload_path}）")
            content = payload_path.read_bytes()
            if meta.get("sha256") and _sha256_bytes(content) != meta["sha256"]:
                raise ReplayBundleError(f"本地 {key} 与 run.json 记录的 sha256 不一致")
            name = f"artifacts/{key}.json"
            archive.writestr(name, content)
            manifest["artifacts"][key] = name
        activity = json.loads(archive.read("artifacts/activity_detail.json"))
        workbook = PurePosixPath(str(activity.get("workbook") or ""))
        if activity.get("source") == "ssjh_social_task":
            if len(workbook.parts) < 2:
                raise ReplayBundleError("录制的 activity_detail 缺少采集工作簿路径")
            relative = PurePosixPath(*workbook.parts[-2:])
            local = run_dir.joinpath(*relative.parts)
            if not local.is_file() or _sha256_bytes(local.read_bytes()) != activity.get("sha256"):
                raise ReplayBundleError(f"本地采集工作簿缺失或 sha256 不一致（{local}）")
            archive.write(local, f"files/{relative.as_posix()}")
        for cache in sorted((run_dir / "annotation_cache").glob("*.json")):
            archive.write(cache, f"files/annotation_cache/{cache.name}")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buffer.getvalue()
