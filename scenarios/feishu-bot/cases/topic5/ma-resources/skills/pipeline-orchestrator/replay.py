"""外部数据回放（测试复用）：用上一次真实 Run 的外部取数结果替代计费/外部调用。

回放包由 Gateway 从已同步到本地的历史 Run 打包上传，挂载路径经环境变量
``TOPIC5_REPLAY_BUNDLE`` 传入。包内结构（zip）：
  manifest.json                         {schema_version, source:{job_id,task_id,run_id}, artifacts:{key: path}}
  artifacts/<key>.json                  confirmed_plan / activity_detail / brand_stats /
                                        brand_stats_baseline / wordcloud_data 的当时版本
  files/activity_collection/...         自动采集适配工作簿（activity_detail.workbook）
  files/annotation_cache/*.json         标注缓存（键含行内容与 prompt 指纹，变化即失效）

复用范围只限外部数据（n03b 采集、n05/n06/n13 数说取数）与标注缓存；清洗、去噪、统计、
写作、质检、发布全部照常真跑。回放前核对取数参数指纹：与录制时不一致直接报错，
不允许拿错数据继续生成报告。未配置回放包时本模块完全不生效。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ENV_BUNDLE = "TOPIC5_REPLAY_BUNDLE"
SCHEMA_VERSION = 1
REPLAYABLE_KEYS = ("activity_detail", "brand_stats", "brand_stats_baseline", "wordcloud_data")

# 决定外部取数结果的全部参数；任一不同即视为另一份数据。
_SCOPE_FIELDS = (
    "brand", "activity_name", "period_start", "period_end",
    "baseline_start", "baseline_end", "platforms", "content_scope",
    "brand_query", "brand_filter", "brand_words", "keywords",
    "activity_query", "activity_filter", "product_query", "product_words",
)
_CAMPAIGN_FIELDS = ("id", "name", "activity_query", "activity_filter", "query",
                    "filter_words", "period_start", "period_end")


class ReplayMismatch(RuntimeError):
    """回放包与当前任务取数参数不一致。"""


def bundle_path() -> Path | None:
    raw = (os.environ.get(ENV_BUNDLE) or "").strip()
    return Path(raw) if raw else None


def enabled() -> bool:
    return bundle_path() is not None


def scope_fingerprint(plan: dict) -> str:
    plan = plan if isinstance(plan, dict) else {}
    scope: dict[str, Any] = {key: plan.get(key) for key in _SCOPE_FIELDS}
    scope["campaigns"] = [
        {key: campaign.get(key) for key in _CAMPAIGN_FIELDS}
        for campaign in (plan.get("campaigns") or []) if isinstance(campaign, dict)
    ]
    text = json.dumps(scope, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"replay: 非法包内路径 {name}")
    return member


def _extracted_root(run_dir: Path) -> Path:
    """回放包解压到 run 目录内（随 run 一起留痕），只解压一次。"""
    source = bundle_path()
    if source is None:
        raise RuntimeError("replay: 未配置回放包")
    if not source.is_file():
        raise RuntimeError(f"replay: 回放包不存在 {source}")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = Path(run_dir) / "replay_source"
    marker = root / "bundle.sha256"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        return root
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = _safe_member(info.filename)
            target = root.joinpath(*member.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("replay: 回放包版本不支持")
    marker.write_text(digest, encoding="utf-8")
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _recorded(root: Path, key: str) -> Any:
    path = (_manifest(root).get("artifacts") or {}).get(key)
    if not path:
        return None
    return json.loads(root.joinpath(*_safe_member(path).parts).read_text(encoding="utf-8"))


def _verify_scope(root: Path, plan: dict) -> dict:
    recorded_plan = _recorded(root, "confirmed_plan")
    if not isinstance(recorded_plan, dict):
        raise RuntimeError("replay: 回放包缺少录制时的 confirmed_plan，无法核对取数参数")
    if scope_fingerprint(recorded_plan) != scope_fingerprint(plan):
        diff = [key for key in _SCOPE_FIELDS if recorded_plan.get(key) != plan.get(key)]
        if scope_fingerprint({"campaigns": recorded_plan.get("campaigns")}) != \
                scope_fingerprint({"campaigns": plan.get("campaigns")}):
            diff.append("campaigns")
        raise ReplayMismatch(
            "replay: 当前 G1/G2 取数参数与回放包不一致（" + "、".join(diff or ["未知字段"])
            + "），已停止；请按录制任务的参数重填，或去掉 TOPIC5_REPLAY_JOB_ID 走真实取数")
    return _manifest(root).get("source") or {}


def load(run_dir: Path, key: str, plan: dict) -> Any:
    """返回录制的 artifact payload（已核对参数）；包中没有该 key 时报错。"""
    if key not in REPLAYABLE_KEYS:
        raise ValueError(f"replay: {key} 不在可回放范围")
    root = _extracted_root(run_dir)
    source = _verify_scope(root, plan)
    payload = _recorded(root, key)
    if payload is None:
        raise RuntimeError(f"replay: 回放包缺少 {key}（来源 job={source.get('job_id')}）")
    if isinstance(payload, dict) and (payload.get("degraded") or payload.get("unavailable")):
        raise RuntimeError(f"replay: 录制的 {key} 为降级/不可用产物，不能复用")
    return payload


def restore_file(run_dir: Path, relative: str) -> Path:
    """把包内 files/<relative> 恢复到当前 run 目录同一相对位置，返回绝对路径。"""
    root = _extracted_root(run_dir)
    member = _safe_member(relative)
    source = root / "files" / Path(*member.parts)
    if not source.is_file():
        raise RuntimeError(f"replay: 回放包缺少文件 {relative}")
    target = Path(run_dir).joinpath(*member.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def seed_annotation_cache(run_dir: Path) -> int:
    """把录制的标注缓存放入当前 run（已有同名缓存不覆盖），返回放入的文件数。"""
    root = _extracted_root(run_dir)
    cache_src = root / "files" / "annotation_cache"
    if not cache_src.is_dir():
        return 0
    seeded = 0
    target_dir = Path(run_dir) / "annotation_cache"
    for source in sorted(cache_src.glob("*.json")):
        target = target_dir / source.name
        if target.exists():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        seeded += 1
    return seeded


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
