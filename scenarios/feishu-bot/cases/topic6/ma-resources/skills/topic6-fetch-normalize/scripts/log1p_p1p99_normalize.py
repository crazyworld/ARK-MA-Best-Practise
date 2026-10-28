#!/usr/bin/env python3
"""
log1p_p1p99_normalize.py — Phase B:log1p + P1/P99 标准化

  - 基准文件走 /mnt/skills/topic6-fetch-normalize/references/平台热度基准_2026.json
  - --project-dir 支持绝对路径;相对路径以 /workspace 为根

算法:
    heat_raw = hot_index * 10000
    log_heat = log1p(heat_raw)
    score = clip((log_heat - p1) / (p99 - p1), 0, 1) * 60 + 40

用法:
    python log1p_p1p99_normalize.py \\
        --project-dir /workspace/Projects/W35_20260824-20260830

输出:
    {project_dir}/02_标准化/hot_topics_normalized.xlsx(在 hot_index 右侧插入 heat_score 列)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE_ROOT = Path(os.environ.get("MA_WORKSPACE", "/workspace"))
_ANNOTATION_SCRIPTS = Path(__file__).resolve().parents[2] / "topic6-annotation" / "scripts"
MAX_SCORE_FLOOR = 90
# 退出码:1=硬失败(NaN/无基准);2=仅「各平台最高分≤90」,需用户 A/B/C 裁决
EXIT_LOW_MAX = 2


def _update_status(proj: Path, patch: dict, phase: str | None = None) -> None:
    sys.path.insert(0, str(_ANNOTATION_SCRIPTS))
    try:
        from run_config_state import update_run_config
    except ImportError as error:  # pragma: no cover
        print(f"[B] WARN 无法写回 run_config: {error}", file=sys.stderr)
        return
    update_run_config(proj, patch, current_phase=phase)
DEFAULT_BENCHMARK = Path("/mnt/skills/topic6-fetch-normalize/references/平台热度基准_2026.json")


def normalize_score(hot_index: float, p1: float, p99: float) -> float:
    heat_raw = float(hot_index) * 10000.0
    log_heat = float(np.log1p(heat_raw))
    score = float(np.clip((log_heat - p1) / (p99 - p1), 0.0, 1.0)) * 60.0 + 40.0
    return round(score, 1)


def compute_scores(df: pd.DataFrame, benchmark: dict) -> pd.DataFrame:
    df = df.copy()
    missing_mask = df["hot_index"].isna()
    scores = []
    for idx, row in df.iterrows():
        if missing_mask.loc[idx]:
            scores.append(np.nan)
            continue
        b = benchmark.get(row["platform"])
        if b is None:
            scores.append(np.nan)
            continue
        scores.append(normalize_score(float(row["hot_index"]), b["p1"], b["p99"]))

    pos = df.columns.get_loc("hot_index")
    df.insert(pos + 1, "heat_score", scores)
    return df


def platform_stats(df: pd.DataFrame) -> dict:
    out = {}
    for platform, g in df.groupby("platform"):
        out[str(platform)] = {
            "rows": int(len(g)),
            "valid_rows": int((~g["hot_index"].isna()).sum()),
            "mean": None if g["heat_score"].isna().all() else round(float(g["heat_score"].mean()), 1),
            "max": None if g["heat_score"].isna().all() else round(float(g["heat_score"].max()), 1),
            "min": None if g["heat_score"].isna().all() else round(float(g["heat_score"].min()), 1),
        }
    return out


def validate_b(df: pd.DataFrame, benchmark: dict, accept_low_max: bool = False) -> list[str]:
    """硬错误 exit 1;仅最高分≤90 时 exit 2(除非 accept_low_max)。返回被放行的低分平台。"""
    errors = []
    low_max: list[str] = []
    missing_mask = df["hot_index"].isna()

    for platform in benchmark:
        sub = df[(df["platform"] == platform) & (~missing_mask)]
        if sub.empty:
            continue
        max_score = sub["heat_score"].max()
        if pd.isna(max_score):
            errors.append(f"{platform} 无有效 heat_score")
        elif max_score <= MAX_SCORE_FLOOR:
            low_max.append(f"{platform} 最高分 {max_score}")

    valid_nan = df[~missing_mask]["heat_score"].isna().sum()
    if valid_nan > 0:
        errors.append(
            f"{valid_nan} 条 hot_index 非空但 heat_score=NaN,"
            "请检查 platform 名称与基准 JSON 是否一致"
        )

    if errors:
        print("[B-2] Phase B 校验失败", file=sys.stderr)
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if low_max and not accept_low_max:
        # 对齐原版 prompt/02 Step A-4:本周整体降温可能是真实情况,交用户 A/B/C 裁决
        print("[B-2] Phase B 关卡:最高分≤90", file=sys.stderr)
        print(json.dumps({
            "status": "low_max_score",
            "platforms": low_max,
            "stats": platform_stats(df),
            "options": [
                "A) 接受(本周整体降温),带 --accept-low-max --reason 重跑本脚本后继续",
                "B) 暂停,检查平台热度基准是否过时",
                "C) 自定义处理",
            ],
        }, ensure_ascii=False))
        sys.exit(EXIT_LOW_MAX)

    print("[B-2] Phase B 校验通过" + (f"(已放行最高分≤90: {low_max})" if low_max else ""))
    stat = df.groupby("platform").apply(
        lambda g: pd.Series({
            "行数": len(g),
            "有效行": int((~g["hot_index"].isna()).sum()),
            "平均分": round(g["heat_score"].mean(), 1),
            "最高分": round(g["heat_score"].max(), 1),
            "最低分": round(g["heat_score"].min(), 1),
        }),
        include_groups=False,
    )
    print(stat.to_string())
    return low_max


def _project_path(project_dir: str) -> Path:
    p = Path(project_dir)
    return p if p.is_absolute() else WORKSPACE_ROOT / project_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase B:log1p+P1/P99 标准化")
    parser.add_argument("--project-dir", required=True,
                        help="项目目录,绝对路径或相对 /workspace")
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK),
                        help=f"基准 JSON 路径(默认 {DEFAULT_BENCHMARK})")
    parser.add_argument("--accept-low-max", action="store_true",
                        help="用户已裁决接受「最高分≤90」时使用,必须配合 --reason")
    parser.add_argument("--reason", default="", help="--accept-low-max 的放行理由(写入 run_config)")
    args = parser.parse_args()
    if args.accept_low_max and not args.reason.strip():
        parser.error("--accept-low-max 必须同时提供 --reason")

    proj = _project_path(args.project_dir)
    raw_xlsx = proj / "01_原始数据" / "hot_topics_raw.xlsx"
    norm_xlsx = proj / "02_标准化" / "hot_topics_normalized.xlsx"

    if not raw_xlsx.exists():
        print(f"[B-1] 清洗结果不存在:{raw_xlsx},请先跑 fetch_hot_topics.py",
              file=sys.stderr)
        sys.exit(1)

    bench_path = Path(args.benchmark)
    if not bench_path.exists():
        print(f"[B-1] 基准文件不存在:{bench_path}", file=sys.stderr)
        sys.exit(1)

    with open(bench_path, encoding="utf-8") as f:
        benchmark = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    print(f"[B-1] 加载基准:{bench_path}({len(benchmark)} 个平台)")

    df = pd.read_excel(str(raw_xlsx))
    df_norm = compute_scores(df, benchmark)
    accepted_low = validate_b(df_norm, benchmark, accept_low_max=args.accept_low_max)

    norm_xlsx.parent.mkdir(parents=True, exist_ok=True)
    df_norm.to_excel(str(norm_xlsx), index=False)
    print(f"[B-2] 已输出:{norm_xlsx}({len(df_norm)} 行)")
    stats = platform_stats(df_norm)
    ab_patch: dict = {"completed": True}
    if accepted_low:
        # 用户放行「最高分≤90」的审计记录(人工裁决留痕)
        ab_patch["low_max_accepted"] = {"platforms": accepted_low, "reason": args.reason}
    _update_status(proj, {"ab": ab_patch})
    # Phase A+B 用户摘要(对齐原版 Step A-4,Coordinator 原样转述;各平台分布只走 stdout,不落 run_config)
    print(json.dumps({"status": "ok", "summary": stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
