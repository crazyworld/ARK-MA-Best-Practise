#!/usr/bin/env python3
"""
fetch_hot_topics.py — Phase A:MCP 取数 + 清洗

  - MCP 端点走平台注入的 hot-topics MCP,API Key 从环境变量读取
  - --project-dir 支持绝对路径(如 /workspace/Projects/W35_...);相对路径以 /workspace 为根
  - prompts 由 skill 挂载在 /mnt/skills/topic6-annotation/prompts/ 提供,无需 snapshot

用法:
    python fetch_hot_topics.py \\
        --start "2026-08-18" --end "2026-08-31" \\
        --project-dir /workspace/Projects/W35_20260824-20260830

输出:
    {project_dir}/01_原始数据/hot_topics_skill_raw.json  原始 JSON
    {project_dir}/01_原始数据/hot_topics_raw.xlsx        清洗结果

API Key 优先级(平台自动注入):
    BLUEAI_API_KEY / HOT_TOPICS_API_KEY(热点数据 MCP 专用 key;不回退 ARK_API_KEY,
    方舟 Key 对取数无效,误用只会得到误导性的鉴权报错)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

MCP_URL = os.environ.get(
    "HOT_TOPICS_MCP_URL",
    "https://smartai.blueviewai.com/mcp/crawler-hot-topics-server",
)
TOOL_NAME = "query_hot_topics"

PLATFORM_MAP: dict = {
    "1": "抖音", 1: "抖音",
    "3": "B站",  3: "B站",
    "5": "微博",  5: "微博",
    "8": "知乎",  8: "知乎",
}
TARGET_PLATFORMS: set = {"微博", "抖音", "B站", "知乎"}
MIN_ROWS: int = 200

# A-2.5 数据质量校验(对齐 CC V2):硬性问题在 A 阶段就阻断,不让坏数据跑完 C 阶段才返工
REQUIRED_NONNULL_COLS: list = ["row_id", "platform", "title", "hot_index", "hotpost_time"]
OPTIONAL_COLS: list = ["hottopic_desc", "hottopic_url", "hot_thumbnail_url"]
SOFT_WARN_RATE: float = 0.05   # 必填字段空值率 / hot_index≤0 占比超过 → 预警(不阻断,写入 HC1 issues)
HARD_BLOCK_RATE: float = 0.99  # 必填字段空值率达到 → 判定数据源故障,exit 1

OUTPUT_COLS: list = [
    "row_id", "platform", "title", "hottopic_desc",
    "hot_index", "hotpost_time", "hottopic_url",
    "hot_thumbnail_url", "week_num",
]

WORKSPACE_ROOT = Path(os.environ.get("MA_WORKSPACE", "/workspace"))
_ANNOTATION_SCRIPTS = Path(__file__).resolve().parents[2] / "topic6-annotation" / "scripts"


def _update_status(proj: Path, patch: dict, phase: str | None = None) -> None:
    """写回 run_config.status(共享 run_config_state 原子更新;run_config 不存在时静默跳过)。"""
    sys.path.insert(0, str(_ANNOTATION_SCRIPTS))
    try:
        from run_config_state import update_run_config
    except ImportError as error:  # pragma: no cover - 沙箱内必然存在
        print(f"[A] WARN 无法写回 run_config: {error}", file=sys.stderr)
        return
    update_run_config(proj, patch, current_phase=phase)


def fetch_window_from_config(proj: Path) -> tuple[str | None, str | None]:
    """--start/--end 缺省时取 init_project.py 写入的 run_config.fetch 窗口。"""
    cfg_path = proj / "run_config.yaml"
    if not cfg_path.exists():
        return None, None
    import yaml

    fetch = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("fetch") or {}
    return fetch.get("fetch_start"), fetch.get("fetch_end")


def get_api_key() -> str:
    key = (os.environ.get("BLUEAI_API_KEY")
           or os.environ.get("HOT_TOPICS_API_KEY"))
    if key:
        return key
    print(
        "[fetch] 未找到 API Key。请在环境变量中注入 BLUEAI_API_KEY 或 HOT_TOPICS_API_KEY(热点数据 MCP 专用)。",
        file=sys.stderr,
    )
    sys.exit(1)


def fetch_from_mcp(start: str, end: str, api_key: str) -> list:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    init_r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fetch_hot_topics", "version": "1.0"},
        },
        "id": 0,
    }, headers=headers, timeout=30)
    init_r.raise_for_status()

    sid = init_r.headers.get("Mcp-Session-Id") or init_r.headers.get("mcp-session-id")
    if sid:
        headers["Mcp-Session-Id"] = sid

    requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, headers=headers, timeout=10)

    resp = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": TOOL_NAME, "arguments": {"start_time": start, "end_time": end}},
        "id": 1,
    }, headers=headers, timeout=120, stream=True)
    resp.raise_for_status()

    raw_data = None
    for line in resp.iter_lines():
        if line:
            text = line.decode("utf-8", errors="replace")
            if text.startswith("data:"):
                raw_data = text[5:].strip()
                break

    if not raw_data:
        raise RuntimeError("MCP 响应中未找到 data 行,服务端可能超时或返回空数据")

    mcp_obj = json.loads(raw_data)
    if "error" in mcp_obj:
        raise RuntimeError(f"MCP 返回错误:{mcp_obj['error']}")

    api_resp = json.loads(mcp_obj["result"]["content"][0]["text"])
    if api_resp.get("code") != 200:
        raise RuntimeError(f"API 返回非 200:code={api_resp.get('code')},detail={api_resp.get('detail')}")

    records = api_resp.get("data", [])
    print(f"[A-1] 取数完成:{len(records)} 条原始记录({start} ~ {end})")
    return records


def find_complete_week(df: pd.DataFrame) -> tuple:
    df = df.copy()
    dt = pd.to_datetime(df["hotpost_time"])
    iso = dt.dt.isocalendar()
    df["_iso_yw"] = (
        iso["year"].astype(str) + "-W" +
        iso["week"].astype(int).astype(str).str.zfill(2)
    )
    df["_date"] = dt.dt.date

    day_count = df.groupby("_iso_yw")["_date"].nunique()
    complete = day_count[day_count >= 7].index.tolist()

    if not complete:
        best = day_count.idxmax()
        raise ValueError(
            f"未找到完整 7 天的 ISO 自然周;最近的 {best} 仅有 {day_count[best]} 天,"
            f"请将 --start 再往前推 7 天后重试。"
        )

    latest_iso = sorted(complete)[-1]
    result = df[df["_iso_yw"] == latest_iso].drop(columns=["_iso_yw", "_date"]).copy()
    iso_num = int(latest_iso.split("-W")[1])
    result["week_num"] = f"week{iso_num}"
    return result, latest_iso, f"week{iso_num}"


def clean(records: list) -> tuple:
    df = pd.DataFrame(records)

    if "platform" not in df.columns and "platform_id" in df.columns:
        df = df.rename(columns={"platform_id": "platform"})
    df["platform"] = df["platform"].map(PLATFORM_MAP)
    df = df[df["platform"].isin(TARGET_PLATFORMS)].copy()

    if "hottopic_title" in df.columns:
        df = df.rename(columns={"hottopic_title": "title"})
    if "hottopic_time" in df.columns:
        df = df.rename(columns={"hottopic_time": "hotpost_time"})

    df["hot_index"] = pd.to_numeric(df.get("hot_index"), errors="coerce")

    df, iso_label, week_num = find_complete_week(df)
    print(f"[A-2] 目标周:{iso_label}({week_num}),"
          f"过滤后 {len(df)} 条,有效热度 {(~df['hot_index'].isna()).sum()} 条")

    trend = (
        df.groupby(["title", "platform"])["hotpost_time"]
        .nunique()
        .reset_index(name="_trend_days")
    )
    df["_sort_key"] = df["hot_index"].fillna(-np.inf)
    df = (
        df.sort_values("_sort_key", ascending=False)
        .drop_duplicates(subset=["title", "platform"], keep="first")
        .drop(columns=["_sort_key"])
        .reset_index(drop=True)
    )
    df = df.merge(trend, on=["title", "platform"], how="left")

    df.insert(0, "row_id", [f"T{i + 1:04d}" for i in range(len(df))])

    for col in ("hottopic_desc", "hot_thumbnail_url"):
        if col not in df.columns:
            df[col] = None

    return df, iso_label, week_num


def validate_a(df: pd.DataFrame, week_num: str) -> None:
    errors = []
    missing = TARGET_PLATFORMS - set(df["platform"].unique())
    if missing:
        errors.append(f"以下平台无数据:{sorted(missing)}")
    if len(df) < MIN_ROWS:
        errors.append(f"去重后总行数 {len(df)} 低于最低要求 {MIN_ROWS}")
    if errors:
        print("[A-3] Phase A 校验失败", file=sys.stderr)
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("[A-3] Phase A 校验通过")
    stat = df.groupby("platform").agg(
        行数=("row_id", "count"),
        热度缺失=("hot_index", lambda s: s.isna().sum()),
        平均上榜天数=("_trend_days", "mean"),
    ).round(1)
    print(stat.to_string())
    print(f"      周次:{week_num},总行数:{len(df)},hot_index 缺失:{df['hot_index'].isna().sum()}")


def check_data_quality(df: pd.DataFrame, raw_record_count: int) -> dict:
    """清洗后、标准化前执行,返回结构化质量报告(hard_errors 非空 → 调用方 exit 1)。"""
    report: dict = {
        "raw_record_count": raw_record_count,
        "clean_row_count": len(df),
        "field_null_rate": {},
        "invalid_hot_index": {"count": 0, "ratio": 0.0},
        "duplicate_row_id_count": 0,
        "hard_errors": [],
        "soft_warnings": [],
    }
    n = len(df)

    for col in REQUIRED_NONNULL_COLS:
        if col not in df.columns:
            report["hard_errors"].append(f"必填字段「{col}」整列缺失")
            continue
        # 文本列把空串也算空(pandas≥3 字符串列 dtype 为 str 而非 object,不能按 object 判断)
        if pd.api.types.is_numeric_dtype(df[col]):
            null_rate = df[col].isna().mean()
        else:
            null_rate = (df[col].isna() | (df[col].astype(str).str.strip() == "")).mean()
        null_rate = float(round(null_rate, 4)) if n else 0.0
        report["field_null_rate"][col] = null_rate
        if null_rate >= HARD_BLOCK_RATE:
            report["hard_errors"].append(
                f"必填字段「{col}」空值率 {null_rate:.1%},整列几乎全空,判定为数据源故障"
            )
        elif null_rate > SOFT_WARN_RATE:
            report["soft_warnings"].append(
                f"必填字段「{col}」空值率 {null_rate:.1%},超过预警阈值 {SOFT_WARN_RATE:.0%}"
            )

    # 可选字段只记录:部分平台原生无该字段
    for col in OPTIONAL_COLS:
        if col in df.columns and n:
            report["field_null_rate"][col] = float(round(df[col].isna().mean(), 4))

    if "hot_index" in df.columns and n:
        invalid = int((df["hot_index"].notna() & (df["hot_index"] <= 0)).sum())
        ratio = round(invalid / n, 4)
        report["invalid_hot_index"] = {"count": invalid, "ratio": ratio}
        if invalid and ratio > SOFT_WARN_RATE:
            report["soft_warnings"].append(
                f"hot_index≤0 共 {invalid} 条(占比 {ratio:.1%}),超过预警阈值 {SOFT_WARN_RATE:.0%}"
            )

    # row_id 重复会让下游 LEFT JOIN 行数错位,不是可容忍的噪音
    if "row_id" in df.columns:
        dup = int(df["row_id"].duplicated().sum())
        report["duplicate_row_id_count"] = dup
        if dup:
            report["hard_errors"].append(f"检测到 {dup} 个重复 row_id,会导致后续 LEFT JOIN 行数错位")

    return report


def _project_path(project_dir: str) -> Path:
    p = Path(project_dir)
    return p if p.is_absolute() else WORKSPACE_ROOT / project_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase A:MCP 取数 + 清洗")
    parser.add_argument("--start", default=None,
                        help="取数起始日期 YYYY-MM-DD;缺省取 run_config.fetch.fetch_start")
    parser.add_argument("--end", default=None,
                        help="取数截止日期 YYYY-MM-DD;缺省取 run_config.fetch.fetch_end")
    parser.add_argument("--project-dir", required=True,
                        help="项目目录,绝对路径或相对 /workspace 的路径")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="跳过取数,复用已有 hot_topics_skill_raw.json")
    args = parser.parse_args()

    proj = _project_path(args.project_dir)
    if not args.start or not args.end:
        cfg_start, cfg_end = fetch_window_from_config(proj)
        args.start = args.start or cfg_start
        args.end = args.end or cfg_end
    if not args.skip_fetch and (not args.start or not args.end):
        print("[A-1] 缺少取数窗口:请传 --start/--end 或先运行 init_project.py", file=sys.stderr)
        sys.exit(1)
    raw_json = proj / "01_原始数据" / "hot_topics_skill_raw.json"
    raw_xlsx = proj / "01_原始数据" / "hot_topics_raw.xlsx"

    if args.skip_fetch:
        if not raw_json.exists():
            print(f"[A-1] --skip-fetch 但文件不存在:{raw_json}", file=sys.stderr)
            sys.exit(1)
        print(f"[A-1] 跳过取数,复用:{raw_json}")
        with open(raw_json, encoding="utf-8") as f:
            records = json.load(f)
        if isinstance(records, dict):
            records = records.get("data", records)
        print(f"[A-1] 已加载 {len(records)} 条原始记录")
    else:
        api_key = get_api_key()
        records = fetch_from_mcp(args.start, args.end, api_key)
        raw_json.parent.mkdir(parents=True, exist_ok=True)
        with open(raw_json, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"[A-1] 原始 JSON 已保存:{raw_json}")

    df, iso_label, week_num = clean(records)

    quality = check_data_quality(df, len(records))
    quality_path = proj / "01_原始数据" / "data_quality_report.json"
    quality_path.parent.mkdir(parents=True, exist_ok=True)
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    if quality["hard_errors"]:
        print("[A-2.5] 数据质量校验失败(硬性阻断,不可人工放行)", file=sys.stderr)
        for e in quality["hard_errors"]:
            print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    for w in quality["soft_warnings"]:
        print(f"[A-2.5] WARN: {w}")
    print(f"[A-2.5] 数据质量校验通过,报告:{quality_path.name}")

    validate_a(df, week_num)

    out_cols = [c for c in OUTPUT_COLS if c in df.columns]
    raw_xlsx.parent.mkdir(parents=True, exist_ok=True)
    df[out_cols].to_excel(str(raw_xlsx), index=False)
    print(f"[A-3] 已输出:{raw_xlsx}({len(df)} 行)")
    # 只写契约字段(run_config契约.md · status.ab);ab.completed 由 B 标准化成功后写入,
    # A 单独完成不置 completed,避免抽样前置检查在 B 未跑时误放行
    _update_status(proj, {"ab": {
        "week_num": week_num,
        "iso_label": iso_label,
        "raw_rows": len(records),
        "clean_rows": len(df),
        "quality_warnings": quality["soft_warnings"],
    }}, phase="ab")
    print(f"\n完成:{week_num} / {iso_label}")


if __name__ == "__main__":
    main()
