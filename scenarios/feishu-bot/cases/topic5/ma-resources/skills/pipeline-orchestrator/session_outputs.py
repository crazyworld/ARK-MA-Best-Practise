"""把 durable run 目录增量发布到 MA Session 输出目录（/mnt/session/outputs）。

MA 平台把 /mnt/session/outputs 下的文件登记为 Session 的 Files API 对象，Gateway 轮询
下载到本地（与 topic6 artifact_sync 同一机制）。本模块只做「搬运」：
  - 发布范围 = 整个 run 目录（run.json、artifacts/、annotation_cache/、activity_collection/ 等）；
  - 增量：按 (size, mtime_ns) 判断变更，只复制变化的文件；
  - 每次发布重写 <task>/<run>/manifest.json（相对路径 + sha256 + size），供 Gateway 还原目录；
  - 尽力而为：任何异常只记日志，绝不影响节点执行与状态机。
输出根不存在（本地单测、非 MA 环境）时静默跳过。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_OUTPUTS_ROOT = "/mnt/session/outputs"
MANIFEST_NAME = "manifest.json"
STATE_NAME = ".outputs_published.json"
MANIFEST_VERSION = 1
MIN_INTERVAL_SEC = 60.0

_last_publish: dict[str, float] = {}


def outputs_root() -> Path | None:
    root = Path(os.environ.get("TOPIC5_SESSION_OUTPUTS_ROOT") or DEFAULT_OUTPUTS_ROOT)
    return root if root.is_dir() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _iter_run_files(run_dir: Path):
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_dir)
        # 隐藏文件/临时文件（含本模块状态文件、原子写临时文件）与字节码不发布。
        if any(part.startswith(".") for part in relative.parts):
            continue
        # 回放包解压副本是输入而非本 Run 产出，不重复发布。
        if relative.parts[0] == "replay_source":
            continue
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".tmp"):
            continue
        yield path, relative


def publish(run_dir: Path, task_id: str, run_id: str, *, force: bool = False) -> int:
    """发布 run 目录；返回本次复制的文件数。非 MA 环境或节流期内返回 0。"""
    root = outputs_root()
    if root is None:
        return 0
    key = f"{task_id}/{run_id}"
    now = time.monotonic()
    if not force and now - _last_publish.get(key, -MIN_INTERVAL_SEC) < MIN_INTERVAL_SEC:
        return 0
    _last_publish[key] = now

    run_dir = Path(run_dir)
    target = root / task_id / run_id
    state_path = run_dir / STATE_NAME
    try:
        published = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(published, dict):
            published = {}
    except (OSError, ValueError):
        published = {}

    copied = 0
    files = []
    for path, relative in _iter_run_files(run_dir):
        rel = relative.as_posix()
        stat = path.stat()
        stamp = [stat.st_size, stat.st_mtime_ns]
        entry = published.get(rel)
        if not isinstance(entry, dict) or entry.get("stamp") != stamp:
            _atomic_copy(path, target / relative)
            entry = {"stamp": stamp, "sha256": _sha256(target / relative), "size": stat.st_size}
            published[rel] = entry
            copied += 1
        files.append({"path": rel, "sha256": entry["sha256"], "size": entry["size"]})

    if copied:
        manifest = {
            "schema_version": MANIFEST_VERSION,
            "task_id": task_id,
            "run_id": run_id,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "published_ns": time.time_ns(),  # Gateway 选最新 manifest 的排序键
            "files": files,
        }
        _atomic_write(target / MANIFEST_NAME,
                      json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(state_path, json.dumps(published, ensure_ascii=False))
    return copied


def publish_safely(run_dir: Path, task_id: str, run_id: str, *, force: bool = False) -> None:
    try:
        publish(run_dir, task_id, run_id, force=force)
    except Exception as error:  # noqa: BLE001 — 发布失败不影响业务状态机
        print(f"[session_outputs] publish skipped: {type(error).__name__}: {error}",
              file=sys.stderr)
