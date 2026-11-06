#!/usr/bin/env python3
"""Run the complete Topic6 C2 cross-platform event archive (blueai C2 3.0.0-rc.14).

Topic6 封装：把 MA 的项目约定（可用子集 xlsx、c2_status.json、run_config、结果 xlsx、
后台 launcher / wait 检查器、单实例锁、签名续跑）适配到 rc.14 统一入口 run_c2.py：

  input.csv -> run_c2.py run --mode cross-platform --frontend fused --execute
     F1 融合抽取（00+01+02 一次调用，各平台并行）-> 合流 -> 补证前判定
     -> E/V 并行（03 实体 ∥ 04 向量）-> 05 召回 -> 06 分块 -> 07 归档（覆盖重试）
     -> x2 置信度（边界带模型裁定）-> x3 合并/拆分复核 -> 原热点保留
     -> 09 registry -> x4 七列明细 -> finalize（c2_quality / provenance / metrics）
  -> 04_标注/C2_事件归档/c2_event_result_r{N}.xlsx（row_id + 一级事件名，契约不变）

MA 无人值守：--review-policy record（审核门禁不停，待审项进 c2_quality.review_pending
交 HC1/HC2）、--degraded-policy accept（07 技术降级不阻断，降级明细进 c2_quality）。
run_c2.py 自带阶段级 checkpoint 与内容寻址缓存：同一签名重跑直接续跑。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from relay import Relay  # noqa: E402

ANNOTATION_SCRIPT_DIR = (
    SCRIPT_DIR.parent.parent / "topic6-annotation" / "scripts"
)
sys.path.insert(0, str(ANNOTATION_SCRIPT_DIR))
from run_config_state import update_run_config  # noqa: E402

PLATFORMS = {
    "weibo": "微博",
    "douyin": "抖音",
    "bilibili": "B站",
    "zhihu": "知乎",
}
RUNNER_SCHEMA_VERSION = 2  # 2 = rc.14 统一入口；签名变化会清理 v1 旧产物
C2_ENGINE = "run_c2.py"
# 整体重试次数(不含首次)：run_c2 有 checkpoint，重跑只补失败节点，不重复计费已成功批次
C2_RETRIES = 1
C2_RETRY_BACKOFF_SECONDS = 15
# 并发与分块参数（重跑时不要改：checkpoint 与配置哈希绑定，改了会整轮重算）
C2_MAX_REQUESTS = 8
C2_TOP_K = 60          # 跨平台合库召回（与 2.2.0 一致）
C2_CAP = 60
C2_OUT_BUDGET = 6000
C2_STAGE_TIMEOUT = 3600
# 同时跑 F1 的平台数。总并发 = C2_FRONT_WORKERS × C2_MAX_REQUESTS = 16，与 V2 生产
# (2 平台 × 8) 及 2.2.0 的 00 阶段(4 × 4)实测量级一致，避免 relay 限流。
C2_FRONT_WORKERS = 2
DEMO_FAST_SYSTEM = """你是社媒热点事件归并器。给定少量热点标题，把描述同一现实事件的记录归入同一事件簇。

规则：
1. 每个输入 row_id 必须恰好返回一次，不得增删或修改 row_id。
2. 同一人物/品牌不代表同一事件；只有核心动作、对象和时间语境一致才合并。
3. 一级事件名使用简洁、可读的中文事实短语，不写平台名、热度和评价。
4. 信息不足时宁可保持独立，不要过度合并。
5. 只输出 JSON 对象，不要 markdown：
{"results":[{"row_id":"原样返回","一级事件名":"事件簇名"}]}
"""


def resolve_project_dir(project_dir: str | Path) -> Path:
    path = Path(project_dir)
    return path if path.is_absolute() else Path("/workspace") / path


def build_c2_command(
    run_dir: Path,
    input_csv: Path,
    chat_model: str,
    embedding_model: str,
    *,
    x2_verdict: str = "sonnet",
    window: str = "",
) -> list[str]:
    """组装 rc.14 run_c2.py 的无人值守命令。"""
    return [
        sys.executable, str(SCRIPT_DIR / C2_ENGINE), "run",
        "--input", str(input_csv),
        "--run-dir", str(run_dir / "engine"),
        "--mode", "cross-platform",
        "--frontend", "fused",
        "--heat-col", "heat",
        "--window", window or "unspecified",
        "--chat-model", chat_model,
        "--embedding-model", embedding_model,
        "--max-requests", str(C2_MAX_REQUESTS),
        "--front-workers", str(C2_FRONT_WORKERS),
        "--top-k", str(C2_TOP_K),
        "--cap", str(C2_CAP),
        "--out-budget", str(C2_OUT_BUDGET),
        "--stage-timeout", str(C2_STAGE_TIMEOUT),
        "--verdict", "off" if x2_verdict == "off" else x2_verdict,
        "--review-policy", "record",
        "--degraded-policy", "accept",
        "--execute",
    ]


def resolve_window(project: Path) -> str:
    """Registry 窗口标识(如 2026W43),取 run_config.period.period_label(YYYY-Www)。

    取不到时返回空串,09 仍可提交,只是快照里 window 为空。
    """
    config_path = project / "run_config.yaml"
    if not config_path.exists():
        return ""
    try:
        import yaml

        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 窗口标识缺失不应阻断 C2
        return ""
    period = config.get("period") or {}
    label = str(period.get("period_label") or period.get("period_id") or "")
    match = re.search(r"(\d{4})-?W(\d{1,2})", label)
    return f"{match.group(1)}W{int(match.group(2)):02d}" if match else ""


def _atomic_json(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _load_status(path: Path) -> dict:
    if not path.exists():
        return {"status": "pending", "completed_stages": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "pending", "completed_stages": []}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _engine_digest() -> str:
    """run_c2 config.json 的全部可变来源指纹：scripts/*.py、references/*、封装参数、模型端点。

    run_c2 发现 config.json 与已有运行目录不一致会直接拒绝（"use a new run directory"）。
    任一来源变化都必须改变签名，让封装清空 engine/ 重跑，否则同一项目会永久卡在 FAILED。
    """
    digest = hashlib.sha256()
    for path in [*sorted(SCRIPT_DIR.glob("*.py")),
                 *sorted(p for p in (SCRIPT_DIR.parent / "references").rglob("*") if p.is_file())]:
        digest.update(path.relative_to(SCRIPT_DIR.parent).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    digest.update(json.dumps([C2_MAX_REQUESTS, C2_FRONT_WORKERS, C2_TOP_K, C2_CAP,
                              C2_OUT_BUDGET, C2_STAGE_TIMEOUT]).encode("utf-8"))
    for name in ("LLM_RELAY_BASE_URL", "EMBEDDING_BASE_URL"):
        digest.update(os.environ.get(name, "").encode("utf-8"))
    return digest.hexdigest()


def _run_signature(
    input_sha256: str,
    chat_model: str,
    embedding_model: str,
    x2_verdict: str,
    *,
    window: str = "",
    engine_digest: str = "",
) -> str:
    payload = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "input_sha256": input_sha256,
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "x2_verdict": x2_verdict,
        "window": window,
        "engine_digest": engine_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _reset_stage_outputs(run_dir: Path, result_path: Path) -> None:
    # PLATFORMS/merged 为 2.2.0 遗留目录，升级后首跑一并清理
    for name in (*PLATFORMS, "merged", "engine", "logs"):
        path = run_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)


def _prepare_input(source: Path, destination: Path) -> int:
    if not source.exists():
        raise FileNotFoundError(f"C2 input not found: {source}")
    frame = (
        pd.read_excel(source)
        if source.suffix.lower() in {".xlsx", ".xls"}
        else pd.read_csv(source)
    )
    aliases = {
        "record_id": ("record_id", "row_id"),
        "title": ("title", "热点标题"),
        "platform": ("platform", "平台"),
        "heat": ("heat", "heat_score", "标准化热度分", "hot_index"),
    }
    selected: dict[str, pd.Series] = {}
    for target, candidates in aliases.items():
        source_col = next((name for name in candidates if name in frame.columns), None)
        if source_col is None:
            raise ValueError(
                f"C2 input missing {target}; candidates={candidates}, "
                f"actual={list(frame.columns)}"
            )
        selected[target] = frame[source_col]
    output = pd.DataFrame(selected)
    # run_c2 遇空标题整轮失败；空标题行无法归档，剔出 C2 输入（宽表合并时事件簇名留空）
    blank = output["title"].isna() | (output["title"].astype(str).str.strip() == "")
    if blank.any():
        print(f"[c2] 跳过空标题 {int(blank.sum())} 行: "
              f"{output.loc[blank, 'record_id'].astype(str).tolist()[:20]}", flush=True)
        output = output.loc[~blank].reset_index(drop=True)
    if output.empty:
        raise ValueError("C2 input has no rows with a non-empty title")
    if output["record_id"].astype(str).duplicated().any():
        raise ValueError("C2 input record_id must be unique")
    unknown = sorted(set(output["platform"].dropna()) - set(PLATFORMS.values()))
    if unknown:
        raise ValueError(f"C2 input contains unknown platforms: {unknown}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp.csv")
    output.to_csv(temp, index=False, encoding="utf-8-sig")
    os.replace(temp, destination)
    return len(output)


def _run_command(command: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now().astimezone().isoformat()}] {' '.join(command)}\n")
        log.flush()
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)


def _input_platforms(input_csv: Path) -> tuple[str, ...]:
    """input.csv 里实际有记录的平台 key，按 PLATFORMS 顺序（仅用于 c2_status 展示）。"""
    try:
        frame = pd.read_csv(input_csv, usecols=["platform"])
    except ValueError:
        return tuple(PLATFORMS)
    labels = set(frame["platform"].dropna().astype(str))
    return tuple(key for key, label in PLATFORMS.items() if label in labels)


def _engine_result(run_dir: Path) -> dict:
    path = run_dir / "engine" / "result.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _engine_cost(run_dir: Path) -> dict:
    """汇总 run_c2 成本台账（engine/out/c2_run_metrics.json 的 cost_ledger）。"""
    path = run_dir / "engine" / "out" / "c2_run_metrics.json"
    try:
        ledger = json.loads(path.read_text(encoding="utf-8")).get("cost_ledger") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        ledger = []
    total = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        for key in total:
            value = entry.get(key) or 0
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] += value
    total["cost_usd"] = round(total["cost_usd"], 6)
    return total


def _engine_progress(run_dir: Path) -> dict:
    """从 run_c2 的 stage receipts 汇总当前节点与各节点耗时（供 c2_status 展示）。"""
    stages = run_dir / "engine" / "stages"
    timings: dict[str, float] = {}
    current, latest = "", 0.0
    if stages.is_dir():
        for receipt in stages.glob("*/*/receipt.json"):
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = data.get("node") or receipt.parent.parent.name
            if data.get("status") == "COMPLETE" and data.get("wall_seconds") is not None:
                timings[name] = round(float(data["wall_seconds"]), 1)
            mtime = receipt.stat().st_mtime
            if data.get("status") == "RUNNING" and mtime >= latest:
                current, latest = name, mtime
    return {"current_node": current, "node_seconds": timings}


def _run_engine(command: list[str], run_dir: Path) -> dict:
    """运行 run_c2.py；失败后退避重跑（checkpoint 续跑）。返回 result.json。"""
    log_path = run_dir / "logs" / "c2_engine.log"
    last_error: Exception | None = None
    for attempt in range(C2_RETRIES + 1):
        try:
            _run_command(command, log_path)
            result = _engine_result(run_dir)
            if result.get("status") != "COMPLETE":
                raise RuntimeError(f"run_c2 status={result.get('status')}: "
                                   f"{result.get('error') or result.get('node') or ''}")
            return result
        except (subprocess.CalledProcessError, RuntimeError) as error:
            result = _engine_result(run_dir)
            last_error = RuntimeError(
                f"run_c2 failed status={result.get('status', 'UNKNOWN')} "
                f"node={result.get('node', '')} error={result.get('error', error)}")
            if attempt >= C2_RETRIES:
                break
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[retry] {last_error}; {C2_RETRY_BACKOFF_SECONDS}s 后第 "
                          f"{attempt + 2}/{C2_RETRIES + 1} 次（checkpoint 续跑）\n")
            print(f"[c2] retry engine attempt {attempt + 2}", flush=True)
            time.sleep(C2_RETRY_BACKOFF_SECONDS)
    raise last_error  # type: ignore[misc]


def _write_result(run_dir: Path, destination: Path, expected_rows: int) -> None:
    source = run_dir / "engine" / "out" / "热点明细_含事件归属.csv"
    if not source.exists():
        raise FileNotFoundError(f"C2 x4 output not found: {source}")
    frame = pd.read_csv(source)
    required = {"record_id", "一级事件名"}
    if not required.issubset(frame.columns):
        raise ValueError(f"C2 x4 output missing columns: {sorted(required - set(frame.columns))}")
    if len(frame) != expected_rows:
        raise ValueError(f"C2 output rows {len(frame)} != input rows {expected_rows}")
    result = frame[["record_id", "一级事件名"]].rename(
        columns={"record_id": "row_id"}
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp.xlsx")
    result.to_excel(temp, index=False)
    os.replace(temp, destination)


def _normalize_demo_fast_results(
    records: list[dict], payload: dict
) -> list[dict]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("C2 demo fast output missing results list")
    expected = [str(record["record_id"]) for record in records]
    by_id: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        row_id = str(item.get("row_id") or "")
        event_name = str(item.get("一级事件名") or "").strip()
        if row_id in by_id:
            raise ValueError(f"C2 demo fast duplicate row_id: {row_id}")
        if row_id in expected and event_name:
            by_id[row_id] = event_name
    missing = [row_id for row_id in expected if row_id not in by_id]
    if missing:
        raise ValueError(f"C2 demo fast missing row_ids: {missing}")
    return [
        {"row_id": row_id, "一级事件名": by_id[row_id]}
        for row_id in expected
    ]


def _run_fast_demo(args: argparse.Namespace) -> Path:
    project = resolve_project_dir(args.project_dir)
    source = Path(args.input) if args.input else (
        project / "04_标注" / "_可用子集"
        / f"usable_subset_{args.mode}_r{args.run_id}.xlsx"
    )
    run_dir = project / "04_标注" / "C2_事件归档" / "c2_run"
    status_path = run_dir / "c2_status.json"
    result_path = (
        project / "04_标注" / "C2_事件归档"
        / f"c2_event_result_r{args.run_id}.xlsx"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    input_csv = run_dir / "input.csv"
    rows = _prepare_input(source, input_csv)
    frame = pd.read_csv(input_csv)
    records = [
        {
            "record_id": str(row["record_id"]),
            "platform": "" if pd.isna(row["platform"]) else str(row["platform"]),
            "title": "" if pd.isna(row["title"]) else str(row["title"]),
        }
        for _, row in frame.iterrows()
    ]
    signature = _run_signature(
        _file_sha256(input_csv),
        args.chat_model,
        "demo-fast-no-embedding",
        "demo-fast-v1",
    )
    status = _load_status(status_path)
    if (
        status.get("status") == "done"
        and status.get("run_signature") == signature
        and result_path.exists()
    ):
        print(f"[c2] demo fast cache hit: {result_path}", flush=True)
        return result_path

    started_at = datetime.now().astimezone().isoformat()
    status = {
        "status": "running",
        "current_stage": "demo_fast_cluster",
        "mode": "demo",
        "strategy": "single_llm_cluster",
        "run_id": args.run_id,
        "input_file": str(source),
        "input_rows": rows,
        "run_signature": signature,
        "chat_model": args.chat_model,
        "started_at": started_at,
        "updated_at": started_at,
    }
    _atomic_json(status_path, status)
    update_run_config(
        project,
        {
            "c2_cluster": {
                "status": "running",
                "stage": "demo_fast_cluster",
                "strategy": "single_llm_cluster",
                "input_file": str(source),
                "row_count": rows,
                "run_id": args.run_id,
            }
        },
    )
    try:
        relay = Relay(args.chat_model, 6000, DEMO_FAST_SYSTEM)
        prompt = json.dumps({"records": records}, ensure_ascii=False)
        last_error: Exception | None = None
        normalized: list[dict] = []
        for attempt in range(3):
            try:
                payload = relay.call_json(prompt, attempts=1)
                normalized = _normalize_demo_fast_results(records, payload)
                break
            except (RuntimeError, ValueError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if not normalized:
            raise RuntimeError(
                f"C2 demo fast failed after 3 attempts: {last_error}"
            )
        result = pd.DataFrame(normalized)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temp = result_path.with_name(
            f".{result_path.stem}.{os.getpid()}.tmp.xlsx"
        )
        result.to_excel(temp, index=False)
        os.replace(temp, result_path)

        completed_at = datetime.now().astimezone().isoformat()
        status.update(
            {
                "status": "done",
                "current_stage": "done",
                "output_file": str(result_path),
                "input_tokens": relay.usage["input_tokens"],
                "output_tokens": relay.usage["output_tokens"],
                "cost_usd": relay.cost(),
                "completed_at": completed_at,
                "updated_at": completed_at,
            }
        )
        _atomic_json(status_path, status)
        update_run_config(
            project,
            {
                "c2_cluster": {
                    "status": "done",
                    "stage": "done",
                    "strategy": "single_llm_cluster",
                    "run_id": args.run_id,
                    "row_count": rows,
                    "output_file": str(result_path),
                    "chat_model": args.chat_model,
                    "completed_at": completed_at,
                }
            },
        )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return result_path
    except Exception as error:
        failed_at = datetime.now().astimezone().isoformat()
        status.update(
            {
                "status": "failed",
                "error": str(error),
                "failed_at": failed_at,
                "updated_at": failed_at,
            }
        )
        _atomic_json(status_path, status)
        update_run_config(
            project,
            {
                "c2_cluster": {
                    "status": "failed",
                    "stage": "demo_fast_cluster",
                    "strategy": "single_llm_cluster",
                    "error": str(error),
                    "failed_at": failed_at,
                }
            },
        )
        raise


def _run_unlocked(args: argparse.Namespace) -> Path:
    project = resolve_project_dir(args.project_dir)
    source = Path(args.input) if args.input else (
        project / "04_标注" / "_可用子集"
        / f"usable_subset_{args.mode}_r{args.run_id}.xlsx"
    )
    run_dir = project / "04_标注" / "C2_事件归档" / "c2_run"
    input_csv = run_dir / "input.csv"
    status_path = run_dir / "c2_status.json"
    result_path = (
        project / "04_标注" / "C2_事件归档"
        / f"c2_event_result_r{args.run_id}.xlsx"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = _prepare_input(source, input_csv)
    input_sha256 = _file_sha256(input_csv)
    window = resolve_window(project)
    run_signature = _run_signature(
        input_sha256,
        args.chat_model,
        args.embedding_model,
        args.x2_verdict,
        window=window,
        engine_digest=_engine_digest(),
    )
    status = _load_status(status_path)
    if status.get("run_signature") != run_signature:
        _reset_stage_outputs(run_dir, result_path)
        status = {"status": "pending", "completed_stages": []}
    already_done = status.get("status") == "done" and result_path.exists()
    platforms = _input_platforms(input_csv)
    skipped_platforms = [PLATFORMS[k] for k in PLATFORMS if k not in platforms]
    if skipped_platforms:
        print(f"[c2] 输入中无记录的平台(run_c2 自动跳过): {skipped_platforms}", flush=True)
    status.update({
        "platforms": list(platforms),
        "status": "running",
        "mode": args.mode,
        "run_id": args.run_id,
        "input_file": str(source),
        "input_rows": rows,
        "input_sha256": input_sha256,
        "run_signature": run_signature,
        "chat_model": args.chat_model,
        "embedding_model": args.embedding_model,
        "updated_at": datetime.now().astimezone().isoformat(),
    })
    _atomic_json(status_path, status)
    update_run_config(
        project,
        {"c2_cluster": {
            "status": "running",
            "stage": status.get("current_stage", "prepare"),
            "input_file": str(source),
            "row_count": rows,
            "run_id": args.run_id,
        }},
    )

    started = time.monotonic()
    try:
        if not already_done:
            command = build_c2_command(
                run_dir, input_csv, args.chat_model, args.embedding_model,
                x2_verdict=args.x2_verdict, window=window,
            )
            status.update({"current_stage": "c2_engine", "engine": C2_ENGINE,
                           "updated_at": datetime.now().astimezone().isoformat()})
            _atomic_json(status_path, status)
            update_run_config(project, {"c2_cluster": {"status": "running", "stage": "c2_engine"}})
            print(f"[c2] start: {C2_ENGINE} (rc.14 cross-platform, fused)", flush=True)
            engine = _run_engine(command, run_dir)
            _write_result(run_dir, result_path, rows)
        else:
            engine = _engine_result(run_dir)
        out_dir = run_dir / "engine" / "out"
        quality_path = out_dir / "c2_quality.json"
        quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else {}
        registry_snapshot = out_dir / "registry_snapshot.jsonl"
        progress = _engine_progress(run_dir)
        status["registry"] = {
            "status": "done" if registry_snapshot.exists() else "skipped",
            "window": window,
            "snapshot": str(registry_snapshot) if registry_snapshot.exists() else "",
            "errors": {},
        }
        status["quality"] = {
            "coverage_status": quality.get("coverage_status"),
            "degraded_count": len(quality.get("degraded_ids") or []),
            "technical_missing_count": len(quality.get("technical_missing_ids") or []),
            "original_retained_count": quality.get("original_retained_count", 0),
            "review_pending": [
                {k: v for k, v in item.items() if k != "files"}
                for item in quality.get("review_pending") or engine.get("review_pending") or []
            ],
            "file": str(quality_path) if quality_path.exists() else "",
        }
        cost = _engine_cost(run_dir)
        status.update({
            "status": "done",
            "current_stage": "done",
            "completed_stages": ["c2_engine"],
            "input_tokens": cost["input_tokens"],
            "output_tokens": cost["output_tokens"],
            "llm_calls": cost["calls"],
            "cost_usd": cost["cost_usd"],
            "node_seconds": progress["node_seconds"],
            "engine_wall_seconds": round(float(engine.get("wall_seconds") or 0), 1),
            "runner_wall_seconds": round(time.monotonic() - started, 1),
            "output_file": str(result_path),
            "completed_at": datetime.now().astimezone().isoformat(),
        })
        _atomic_json(status_path, status)
        update_run_config(
            project,
            {"c2_cluster": {
                "status": "done",
                "stage": "done",
                "run_id": args.run_id,
                "row_count": rows,
                "output_file": str(result_path),
                "chat_model": args.chat_model,
                "embedding_model": args.embedding_model,
                "engine": "blueai-canonical-event-registry 3.0.0-rc.14",
                "registry_status": status["registry"]["status"],
                "degraded_count": status["quality"]["degraded_count"],
                "review_pending": len(status["quality"]["review_pending"]),
                "completed_at": status["completed_at"],
            }},
        )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return result_path
    except Exception as error:
        status.update({
            "status": "failed",
            "error": str(error),
            "node_seconds": _engine_progress(run_dir)["node_seconds"],
            "failed_at": datetime.now().astimezone().isoformat(),
        })
        _atomic_json(status_path, status)
        update_run_config(
            project,
            {"c2_cluster": {
                "status": "failed",
                "stage": status.get("current_stage"),
                "error": str(error),
                "failed_at": status["failed_at"],
            }},
        )
        raise


def run(args: argparse.Namespace) -> Path:
    if getattr(args, "demo_fast", False) and args.mode != "demo":
        raise ValueError("--demo-fast is only valid with --mode demo")
    project = resolve_project_dir(args.project_dir)
    lock_path = project / "04_标注" / "C2_事件归档" / ".c2_runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another C2 runner is already active") from error
        try:
            if args.mode == "demo" and getattr(args, "demo_fast", False):
                return _run_fast_demo(args)
            return _run_unlocked(args)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument(
        "--mode",
        choices=["demo", "full", "skip_sampling"],
        required=True,
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--input")
    parser.add_argument(
        "--chat-model",
        default=os.environ.get("C2_CHAT_MODEL_ID") or "DeepSeek-V4.1-Flash",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("EMBEDDING_MODEL_ID")
        or "Doubao-embedding",
    )
    parser.add_argument(
        "--x2-verdict",
        choices=["off", "opus", "sonnet"],
        default="sonnet",
    )
    parser.add_argument(
        "--demo-fast",
        action="store_true",
        help="demo only: replace the production multi-stage pipeline with one batch clustering call",
    )
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except Exception as error:
        print(f"[c2] failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
