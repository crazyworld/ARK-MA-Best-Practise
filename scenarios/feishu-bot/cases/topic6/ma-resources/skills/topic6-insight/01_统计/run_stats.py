#!/usr/bin/env python3
"""
01_统计 · 编排入口（MA 版）
读宽表 → 标准化字段 → 过滤营销可用 → 调用4个版块统计函数 → 写出 e1_data.md ~ e4_data.md。

MA 适配：删掉 PROJECT_ROOT=parents[2] 上溯逻辑，--project-dir 支持绝对/相对，
相对路径以 WORKSPACE_ROOT=/workspace 为根。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path("/workspace")

from e1_industry import prep_e1_industry
from e2_nodes import prep_e2_nodes
from e3_platforms import (
    prep_e3_platforms,
    prep_e3_word_freq_audit,
    prep_e3_word_candidates,
    prep_e3_event_fulltext,
)
from e4_marketing import prep_e4_marketing, prep_e4_candidates


def resolve_publish_date(proj: Path, explicit: str | None) -> date:
    """报告发布日 = run_config.period.date_end + 1 天（对齐客户 V2 E_insights.md:291）。

    E2 上周回顾/本周预告窗口都以发布日为锚点，取值必须确定，不能随运行当天漂移：
    - 显式 --publish-date：优先采用；与推导值不一致时打 WARN（保留人工覆盖口子）
    - 未传：读 run_config.yaml 的 period.date_end 推导
    - run_config 缺失/字段不全：退回今天并打 WARN（兼容独立调试，不阻断）
    """
    derived: date | None = None
    cfg_path = proj / "run_config.yaml"
    if cfg_path.exists():
        try:
            import yaml

            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            end_s = (cfg.get("period") or {}).get("date_end")
            if end_s:
                derived = date.fromisoformat(str(end_s)) + timedelta(days=1)
        except Exception as error:  # noqa: BLE001 - 读不到配置时走兜底
            print(f"[run_stats] ⚠️ 读取 run_config.period 失败：{error}", file=sys.stderr)

    if explicit:
        chosen = date.fromisoformat(explicit)
        if derived and chosen != derived:
            print(
                f"[run_stats] ⚠️ --publish-date={chosen} 与 period.date_end+1={derived} 不一致，"
                "按显式参数执行（E2 窗口以此为锚点）",
                file=sys.stderr,
            )
        return chosen
    if derived:
        return derived
    print(
        "[run_stats] ⚠️ 未传 --publish-date 且 run_config.period.date_end 缺失，退回今天",
        file=sys.stderr,
    )
    return date.today()


def _latest_run_id(directory: Path, glob_pattern: str) -> int | None:
    """扫描目录下已有 _r{N} 文件，返回已存在的最大编号（无匹配返回 None）。"""
    nums = []
    for f in directory.glob(glob_pattern):
        m = re.search(r"_r(\d+)\.xlsx$", f.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def normalize_wide_table(df: pd.DataFrame) -> pd.DataFrame:
    if "heat_score" in df.columns and "标准化热度分" not in df.columns:
        df = df.rename(columns={"heat_score": "标准化热度分"})
    rename_map = {
        c: c[3:]
        for c in df.columns
        if c.startswith("c1_") and not c.endswith("_error")
    }
    if rename_map:
        df = df.rename(columns=rename_map)
    if "platform" in df.columns and "平台" not in df.columns:
        df = df.rename(columns={"platform": "平台"})
    if "title" in df.columns and "热点标题" not in df.columns:
        df = df.rename(columns={"title": "热点标题"})
    if "热点驱动词" not in df.columns:
        df["热点驱动词"] = ""
    return df


def filter_usable(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["是否营销可用"] == "是"].copy()


def main():
    parser = argparse.ArgumentParser(description="01_统计：宽表 → 4 版块数据预处理")
    parser.add_argument("--project-dir", required=True,
                        help="项目目录（绝对路径，或相对 /workspace）")
    parser.add_argument(
        "--mode",
        choices=["demo", "full", "skip_sampling"],
        default="skip_sampling",
    )
    parser.add_argument("--publish-date", default=None,
                        help="报告发布日 YYYY-MM-DD（默认 run_config.period.date_end+1）")
    parser.add_argument("--run-id", type=int, default=None,
                        help="宽表版本号（不传时自动取已存在的最新一轮）")
    parser.add_argument("--out-dir", default=None,
                        help="输出目录覆盖（默认 {project_dir}/06_洞察/）")
    args = parser.parse_args()

    proj = Path(args.project_dir)
    if not proj.is_absolute():
        proj = WORKSPACE_ROOT / proj
    if not proj.exists():
        print(f"[run_stats] ❌ 项目目录不存在：{proj}", file=sys.stderr)
        sys.exit(1)

    pub_date = resolve_publish_date(proj, args.publish_date)
    print(f"[run_stats] 发布日：{pub_date}")

    merge_dir = proj / "05_合并"
    run_id = args.run_id
    if run_id is None:
        run_id = _latest_run_id(merge_dir, f"wide_table_{args.mode}_r*.xlsx")

    if run_id is not None:
        wide_file = merge_dir / f"wide_table_{args.mode}_r{run_id}.xlsx"
        print(f"[run_stats] run_id = r{run_id}")
    else:
        wide_file = merge_dir / f"wide_table_{args.mode}.xlsx"
        print(f"[run_stats] 未找到版本化宽表，回退旧命名")

    if not wide_file.exists():
        print(f"[run_stats] ❌ 宽表不存在：{wide_file}", file=sys.stderr)
        sys.exit(1)

    print(f"[run_stats] 读取宽表：{wide_file}")
    df = pd.read_excel(wide_file, dtype=str)
    df = normalize_wide_table(df)
    for col in ["标准化热度分", "hotpost_num"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df_y = filter_usable(df)
    print(f"[run_stats] 总条目：{len(df)}，营销可用：{len(df_y)}")

    out_dir = Path(args.out_dir) if args.out_dir else (proj / "06_洞察")
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = [
        ("E1 行业数据",   "e1_data.md",  lambda: prep_e1_industry(df_y, proj)),
        ("E3 平台数据",   "e3_data.md",  lambda: prep_e3_platforms(df_y)),
        ("E4 营销数据",   "e4_data.md",  lambda: prep_e4_marketing(df_y)),
    ]

    for label, fname, fn in tasks:
        print(f"[run_stats] 生成 {label}...")
        content = fn()
        (out_dir / fname).write_text(content, encoding="utf-8")
        print(f"[run_stats]   ✅ {out_dir / fname}")

    print("[run_stats] 生成 E3 平台热点词审计表...")
    audit_content = prep_e3_word_freq_audit(df_y)
    (out_dir / "e3_word_freq_audit.md").write_text(audit_content, encoding="utf-8")
    print(f"[run_stats]   ✅ {out_dir / 'e3_word_freq_audit.md'}")

    # E3 打标(_tagging)与洞察初稿(_insight_draft)的结构化输入,供 pipeline_e 的 E3 1a/1b 并发步骤读取
    for label, fname, fn in (
        ("E3 平台热点词候选(供打标)", "e3_word_candidates.json", prep_e3_word_candidates),
        ("E3 热门事件全量标题(供洞察初稿)", "e3_event_fulltext.json", prep_e3_event_fulltext),
    ):
        print(f"[run_stats] 生成 {label}...")
        (out_dir / fname).write_text(
            json.dumps(fn(df_y), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[run_stats]   ✅ {out_dir / fname}")

    print("[run_stats] 生成 E2 节点数据...")
    # E2 上周节点回顾用全量 df(对齐 CC V2 E17):节点是否展示不应被"是否营销可用"预先卡掉
    # (W43 霜降只出现在 1 条非商业热点里,用 df_y 会被过滤成"上周无节点");E1/E3/E4 仍用 df_y
    e2_result = prep_e2_nodes(df, pub_date)
    e2_flags_path = out_dir / "e2_flags.json"
    e2_flags_path.write_text(
        json.dumps(e2_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[run_stats]   ✅ {e2_flags_path}")

    print("[run_stats] 生成 E4 候选打标用数据...")
    e4_candidates = prep_e4_candidates(df_y)
    e4_candidates_path = out_dir / "e4_candidates.json"
    e4_candidates_path.write_text(
        json.dumps(e4_candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[run_stats]   ✅ {e4_candidates_path}")

    print(f"\n[run_stats] ✅ 全部完成。输出目录：{out_dir}")


if __name__ == "__main__":
    main()
