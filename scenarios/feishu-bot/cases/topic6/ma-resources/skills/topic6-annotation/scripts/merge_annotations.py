#!/usr/bin/env python3
"""
merge_annotations.py — 七路标注宽表合并 (C0 + R1~R5 + C2 + C3)

  1. 输出 28 列宽表 (基础字段 9 列 + C0 7 列 + [提取节点, 事件簇名] + R1~R5 各 2 列)
  2. C3 只保留过滤后的 "提取节点"; C2 只保留 "事件簇名"
  3. 单路缺失只告警不中断, 用 health_summary md 汇总

输入 (相对 project_dir):
  02_标准化/hot_topics_normalized.xlsx
  03_抽样/sample_500.xlsx                     (full/demo 模式过滤 row_id)
  04_标注/{C0,R1..R5,C3}_*/{task}_postprocess_r{run_id}.xlsx
  04_标注/C2_事件归档/c2_event_result_r{run_id}.xlsx

输出:
  05_合并/wide_table_{mode}_r{run_id}.xlsx
  05_合并/health_summary_{mode}_r{run_id}.md

CLI:
  python merge_annotations.py --project-dir /workspace/Projects/W35 \\
    --mode full --run-id 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_config_state import run_config_mode, update_run_config, uses_sample_base

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

TASK_MERGE_SPECS: dict[str, dict] = {
    "c0": {
        "subdir": "C0_基础事实",
        "raw_cols": [
            "c0_商业实体", "c0_热点驱动词", "c0_行业归属", "c0_营销触发方式",
            "c0_营销维度", "c0_是否营销可用", "c0_判断说明",
        ],
        "rename": {
            "c0_商业实体": "商业实体", "c0_热点驱动词": "热点驱动词", "c0_行业归属": "行业归属",
            "c0_营销触发方式": "营销触发方式", "c0_营销维度": "营销维度",
            "c0_是否营销可用": "是否营销可用", "c0_判断说明": "判断说明",
        },
    },
    "r1": {"subdir": "R1_平台借势", "raw_cols": ["r1_是否平台玩法", "r1_判断说明"],
           "rename": {"r1_是否平台玩法": "是否平台玩法", "r1_判断说明": "平台玩法说明"}},
    "r2": {"subdir": "R2_商业合作", "raw_cols": ["r2_是否商业合作", "r2_判断说明"],
           "rename": {"r2_是否商业合作": "是否商业合作", "r2_判断说明": "合作动态说明"}},
    "r3": {"subdir": "R3_风险预警", "raw_cols": ["r3_是否风险预警", "r3_判断说明"],
           "rename": {"r3_是否风险预警": "是否风险预警", "r3_判断说明": "风险判断说明"}},
    "r4": {"subdir": "R4_创意借鉴", "raw_cols": ["r4_是否营销发现", "r4_判断说明"],
           "rename": {"r4_是否营销发现": "是否营销发现", "r4_判断说明": "营销发现说明"}},
    "r5": {"subdir": "R5_消费者行为", "raw_cols": ["r5_是否消费者行为", "r5_判断说明"],
           "rename": {"r5_是否消费者行为": "是否消费者行为", "r5_判断说明": "消费者行为说明"}},
}
TASK_ORDER = ["c0", "r1", "r2", "r3", "r4", "r5"]

C3_SOURCE_COLS = ["提取节点", "是否节日营销", "是否窗口期内", "c3_parse_error"]

# Step 9 综合质量验证阈值(对齐原版 skill/annotation/SKILL.md Step 9)
VALID_PASS = 0.92
VALID_FAIL = 0.80
C2_COVERAGE_MIN = 0.50          # 营销可用行中「事件簇名」非空率
USABLE_BASELINE = (0.55, 0.58)  # C0 是否营销可用=是 的历史区间
USABLE_TOLERANCE = 0.05         # 偏离基线超过该值才提示人工复核
PLATFORM_MIN_SHARE = 0.05       # 单平台行数占比低于该值视为「平台行数异常偏少」
CLUSTER_MAX_SHARE = 0.10        # 最大事件簇占营销可用行比例超过该值视为「聚类规模极端」
CLUSTER_MIN_ROWS = 10
PLATFORM_ORDER = ["微博", "抖音", "B站", "知乎"]

BASE_RENAME = {
    "row_id": "序号", "platform": "平台", "title": "热点标题",
    "hottopic_desc": "热点描述", "hot_index": "平台热度值",
    "heat_score": "标准化热度分", "hotpost_time": "发布时间",
    "hottopic_url": "链接", "week_num": "周数",
}


def _resolve_project_dir(project_dir: str) -> Path:
    p = Path(project_dir)
    if not p.is_absolute():
        p = Path("/workspace") / project_dir
    return p


def _read_or_warn(file_path: Path, label: str) -> pd.DataFrame | None:
    if not file_path.exists():
        print(f"[merge] ⚠️ {label} 不存在: {file_path}", file=sys.stderr)
        return None
    df = pd.read_excel(file_path)
    print(f"[merge] {label}: {len(df)} 行 × {len(df.columns)} 列")
    return df


def _filter_c3_nodes(row: pd.Series) -> str | float:
    """按位置过滤: 只保留 是否节日营销=是 且 是否窗口期内=是 的节点。"""
    nodes_raw = row.get("提取节点")
    if pd.isna(nodes_raw):
        return float("nan")
    nodes = str(nodes_raw).split("|")
    festival = str(row.get("是否节日营销") or "").split("|")
    window = str(row.get("是否窗口期内") or "").split("|")
    kept = [
        n for i, n in enumerate(nodes)
        if n and i < len(festival) and i < len(window)
        and festival[i].strip() == "是" and window[i].strip() == "是"
    ]
    return "|".join(kept) if kept else float("nan")


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _check(item: str, value, expect: str, status: str) -> dict:
    return {"item": item, "value": value, "expect": expect, "status": status}


def _valid_status(rate: float | None) -> str:
    if rate is None:
        return "missing"
    return "pass" if rate >= VALID_PASS else ("warn" if rate >= VALID_FAIL else "fail")


def quality_report(
    df: pd.DataFrame,
    *,
    missing: dict[str, bool],
    valid_rates: dict[str, float | None],
    unusable_reasons: dict[str, int],
) -> dict:
    """HC1/HC2 展示指标 + Step 9 综合质量验证。df 为已改名的宽表。

    返回 metrics(可原样放进 HC distribution_summary)、checks(逐项 pass/warn/fail)、
    warnings(需写进 issues_detected 的中文提示)。不改变 gate_ok。
    """
    rows = len(df)
    usable_mask = df.get("是否营销可用", pd.Series("", index=df.index)) == "是"
    usable_rows = int(usable_mask.sum())
    c0_col = df.get("是否营销可用", pd.Series(dtype=object))
    c0_null = int(c0_col.isna().sum() + (c0_col.astype(str).str.strip() == "").sum()) if rows else 0

    cluster = df.get("事件簇名", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    usable_cluster = cluster[usable_mask]
    clustered = usable_cluster[usable_cluster != ""]
    max_cluster = int(clustered.value_counts().iloc[0]) if len(clustered) else 0
    max_cluster_name = str(clustered.value_counts().index[0]) if len(clustered) else ""
    c2_coverage = None if missing.get("c2") else _rate(len(clustered), usable_rows)

    nodes = df.get("提取节点", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    c3_coverage = None if missing.get("c3") else _rate(int((nodes != "").sum()), rows)

    platform_counts = df.get("平台", pd.Series(dtype=object)).value_counts().to_dict()
    platform_dist = {
        name: {"rows": int(platform_counts.get(name, 0)),
               "share": _rate(int(platform_counts.get(name, 0)), rows)}
        for name in PLATFORM_ORDER
    }
    usable_ratio = _rate(usable_rows, rows)

    metrics = {
        "rows": rows,
        "cols": len(df.columns),
        "c0_valid_rate": valid_rates.get("c0"),
        "r1_r5_valid_rates": [valid_rates.get(t) for t in ("r1", "r2", "r3", "r4", "r5")],
        "c3_valid_rate": valid_rates.get("c3"),
        "c0_null_rate": _rate(c0_null, rows),
        "marketing_hit_rate": usable_ratio,
        "r_subset_rows": usable_rows,
        "unusable_reasons": dict(sorted(unusable_reasons.items(), key=lambda kv: -kv[1])[:5]),
        "c2_coverage": c2_coverage,
        "c3_coverage": c3_coverage,
        "max_cluster_rows": max_cluster,
        "max_cluster_name": max_cluster_name,
        "platform_distribution": platform_dist,
    }

    checks: list[dict] = []
    for task in ("c0", "r1", "r2", "r3", "r4", "r5", "c3"):
        rate = valid_rates.get(task)
        # 整批缺失单列为 missing(对齐原版:0~1 路缺失标记后继续),不与有效率 <80% 的 fail 混淆
        status = "missing" if missing.get(task) else _valid_status(rate)
        checks.append(_check(f"{task.upper()} 有效率", rate, f">= {VALID_PASS:.0%}", status))
    if c2_coverage is None:
        checks.append(_check("C2 事件簇非空率", None, f"> {C2_COVERAGE_MIN:.0%}", "missing"))
    else:
        checks.append(_check("C2 事件簇非空率", c2_coverage, f"> {C2_COVERAGE_MIN:.0%}",
                             "pass" if c2_coverage > C2_COVERAGE_MIN else "warn"))
    low, high = USABLE_BASELINE
    if usable_ratio is not None:
        off = usable_ratio < low - USABLE_TOLERANCE or usable_ratio > high + USABLE_TOLERANCE
        checks.append(_check("C0 营销可用率", usable_ratio,
                             f"历史 {low:.0%}~{high:.0%}", "warn" if off else "pass"))
    thin = [n for n, v in platform_dist.items() if (v["share"] or 0) < PLATFORM_MIN_SHARE]
    checks.append(_check("平台行数分布", {n: v["rows"] for n, v in platform_dist.items()},
                         f"每平台 >= {PLATFORM_MIN_SHARE:.0%}", "warn" if thin else "pass"))
    extreme = (
        max_cluster >= CLUSTER_MIN_ROWS and usable_rows
        and max_cluster / usable_rows > CLUSTER_MAX_SHARE
    )
    checks.append(_check("最大事件簇规模", max_cluster,
                         f"<= {CLUSTER_MAX_SHARE:.0%} 营销可用行", "warn" if extreme else "pass"))

    warnings: list[str] = []
    for c in checks:
        if c["status"] == "pass":
            continue
        if c["status"] == "missing":
            task = c["item"].split()[0]
            warnings.append(f"{task} 整批缺失:按缺失继续,对应版块无候选数据")
        elif c["item"] == "平台行数分布":
            warnings.append(f"平台行数异常偏少: {thin}")
        elif c["item"] == "最大事件簇规模":
            warnings.append(f"聚类规模极端: 「{max_cluster_name}」{max_cluster} 行")
        else:
            warnings.append(f"{c['item']} {c['status']}: {c['value']}(期望 {c['expect']})")
    # 口径与 wait_datahub_batch 一致:C0 缺失或 >=2 路缺失=fail(系统性);恰 1 路缺失=degraded
    missing_checks = [c for c in checks if c["status"] == "missing"]
    if any(c["status"] == "fail" for c in checks) or len(missing_checks) >= 2 \
            or missing.get("c0"):
        status = "fail"
    elif missing_checks:
        status = "degraded"
    elif any(c["status"] == "warn" for c in checks):
        status = "warn"
    else:
        status = "pass"
    return {
        "metrics": metrics,
        "checks": checks,
        "warnings": warnings,
        "status": status,
    }


def _unusable_reasons(task_file: Path) -> dict[str, int]:
    if not task_file.exists():
        return {}
    frame = pd.read_excel(task_file, usecols=lambda c: c in {"c0_是否营销可用", "c0_不可用原因"})
    if "c0_不可用原因" not in frame.columns:
        return {}
    if "c0_是否营销可用" in frame.columns:
        frame = frame[frame["c0_是否营销可用"] == "否"]
    reasons = frame["c0_不可用原因"].fillna("").astype(str).str.strip()
    return {k: int(v) for k, v in reasons[reasons != ""].value_counts().items()}


def run(project_dir: str, mode: str, run_id: int) -> dict:
    proj = _resolve_project_dir(project_dir)
    ann_dir = proj / "04_标注"
    merge_dir = proj / "05_合并"
    merge_dir.mkdir(parents=True, exist_ok=True)
    print(f"[merge] run_id=r{run_id} mode={mode}")

    base_file = proj / "02_标准化" / "hot_topics_normalized.xlsx"
    if not base_file.exists():
        raise FileNotFoundError(f"基础表不存在: {base_file}")
    df_base = pd.read_excel(base_file)
    base_rows = len(df_base)
    print(f"[merge] 基础表: {base_rows} 行")

    is_smoke = run_config_mode(proj) == "smoke"
    if uses_sample_base(proj, mode):
        sample_file = proj / "03_抽样" / "sample_500.xlsx"
        if sample_file.exists():
            sample_ids = set(pd.read_excel(sample_file, usecols=["row_id"])["row_id"])
            df_base = df_base[df_base["row_id"].isin(sample_ids)].copy()
            base_rows = len(df_base)
            print(f"[merge] {mode} 模式过滤至 {base_rows} 行")
        elif is_smoke:
            raise FileNotFoundError(f"smoke 模式样本表不存在: {sample_file}")

    df = df_base.copy()
    missing: dict[str, bool] = {}
    valid_rates: dict[str, float | None] = {}

    for task in TASK_ORDER:
        spec = TASK_MERGE_SPECS[task]
        task_file = ann_dir / spec["subdir"] / f"{task}_postprocess_r{run_id}.xlsx"
        df_task = _read_or_warn(task_file, f"{task.upper()}")
        missing[task] = df_task is None
        valid_rates[task] = None
        if df_task is None:
            continue

        keep = ["row_id"] + [c for c in spec["raw_cols"] if c in df_task.columns]
        df = df.merge(df_task[keep], on="row_id", how="left")
        if f"{task}_parse_error" in df_task.columns:
            n = len(df_task)
            valid_rates[task] = round(1.0 - df_task[f"{task}_parse_error"].sum() / n, 4) if n else None

    c3_file = ann_dir / "C3_节点标注" / f"c3_postprocess_r{run_id}.xlsx"
    df_c3 = _read_or_warn(c3_file, "C3")
    missing["c3"] = df_c3 is None
    if df_c3 is not None:
        keep = ["row_id"] + [c for c in C3_SOURCE_COLS if c in df_c3.columns]
        df_c3 = df_c3[keep].copy()
        df_c3["提取节点"] = df_c3.apply(_filter_c3_nodes, axis=1)
        # smoke/demo 样本量小，严格窗口期过滤可能导致「提取节点」整列为空，
        # 此时 fallback 到原始未过滤值以确保后续流程（HC/洞察）有数据可用。
        # 脚本侧 --mode 不会是 smoke(第一轮 full/第二轮 skip_sampling)，原始 mode 读 run_config.yaml。
        filtered_hit = (df_c3["提取节点"].fillna("") != "").sum()
        if filtered_hit == 0:
            if is_smoke:
                # 从原始列恢复：重读 C3 postprocess 的原始「提取节点」字段
                c3_raw = pd.read_excel(c3_file, usecols=lambda c: c in {"row_id", "提取节点"})
                if "提取节点" in c3_raw.columns:
                    raw_hit = (c3_raw["提取节点"].fillna("") != "").sum()
                    if raw_hit > 0:
                        print(
                            f"[merge] ⚠️ C3 窗口期过滤后提取节点全空 (smoke 模式, {len(df_c3)} 行)，"
                            f"fallback 到原始值 ({raw_hit}/{len(c3_raw)} 行有值)",
                            file=sys.stderr,
                        )
                        raw_map = c3_raw.set_index("row_id")["提取节点"]
                        df_c3["提取节点"] = df_c3["row_id"].map(raw_map)
        df = df.merge(df_c3[["row_id", "提取节点"]], on="row_id", how="left")
        if "c3_parse_error" in df_c3.columns and len(df_c3):
            valid_rates["c3"] = round(
                1.0 - df_c3["c3_parse_error"].fillna(0).astype(int).sum() / len(df_c3), 4)

    c2_file = ann_dir / "C2_事件归档" / f"c2_event_result_r{run_id}.xlsx"
    df_c2 = _read_or_warn(c2_file, "C2")
    missing["c2"] = df_c2 is None
    if df_c2 is not None:
        df_c2 = df_c2.rename(columns={"event_name": "事件簇名", "一级事件名": "事件簇名"})
        if "事件簇名" not in df_c2.columns:
            print("[merge] ⚠️ C2 缺少 event_name/一级事件名, 跳过", file=sys.stderr)
            missing["c2"] = True
        else:
            if "row_id" in df_c2.columns:
                df_c2 = df_c2[["row_id", "事件簇名"]]
                df = df.merge(df_c2, on="row_id", how="left")
            elif "record_id" in df_c2.columns:
                df_c2 = df_c2.rename(columns={"record_id": "row_id"})[["row_id", "事件簇名"]]
                df = df.merge(df_c2, on="row_id", how="left")
            elif "word" in df_c2.columns and "platform" in df_c2.columns:
                df_c2 = df_c2.rename(columns={"word": "title"})[["platform", "title", "事件簇名"]]
                df = df.merge(df_c2, on=["platform", "title"], how="left")
            else:
                print("[merge] ⚠️ C2 缺少 row_id/record_id/word, 跳过", file=sys.stderr)
                missing["c2"] = True

    if missing.get("c2") and "事件簇名" not in df.columns:
        df["事件簇名"] = ""
    # 单路整批缺失(wait_datahub_batch=degraded)时补空占位列,保证宽表列结构固定:
    # 下游 01_统计/ 按列名直接取值(df["是否商业合作"] 等),缺列会 KeyError。
    # 空值 != "是",对应版块自然无候选。C0 缺失时批次已熔断,不会走到这里。
    if missing.get("c3") and "提取节点" not in df.columns:
        df["提取节点"] = ""
    for task in ("r1", "r2", "r3", "r4", "r5"):
        if missing.get(task):
            for col in TASK_MERGE_SPECS[task]["raw_cols"]:
                if col not in df.columns:
                    df[col] = ""

    output_rows = len(df)
    row_match = output_rows == base_rows
    if not row_match:
        print(f"[merge] ⚠️ 行数不一致: 基础表 {base_rows}, 输出 {output_rows}", file=sys.stderr)

    dup_rows = int(df["row_id"].duplicated().sum())
    if dup_rows:
        print(f"[merge] ⚠️ row_id 重复: {dup_rows} 行", file=sys.stderr)

    c3_hit_rate = None
    if "提取节点" in df.columns:
        hit = (df["提取节点"].fillna("") != "").sum()
        c3_hit_rate = round(hit / output_rows, 4) if output_rows else None

    task_rename: dict[str, str] = {}
    for task in TASK_ORDER:
        task_rename.update(TASK_MERGE_SPECS[task]["rename"])
    df = df.rename(columns={**BASE_RENAME, **task_rename})

    drop_cols = [f"{t}_parse_error" for t in TASK_ORDER] + ["hot_thumbnail_url"]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    c0_names = list(TASK_MERGE_SPECS["c0"]["rename"].values())
    routing_names: list[str] = []
    for task in ("r1", "r2", "r3", "r4", "r5"):
        routing_names.extend(TASK_MERGE_SPECS[task]["rename"].values())
    target = (
        list(BASE_RENAME.values())
        + c0_names
        + ["提取节点", "事件簇名"]
        + routing_names
    )
    ordered = [c for c in target if c in df.columns]
    leftover = [c for c in df.columns if c not in set(ordered)]
    df = df[ordered + leftover]

    out_file = merge_dir / f"wide_table_{mode}_r{run_id}.xlsx"
    tmp = out_file.with_suffix(".tmp.xlsx")
    try:
        df.to_excel(tmp, index=False)
        os.replace(tmp, out_file)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    print(f"\n[merge] ✅ 宽表: {out_file}")
    print(f"[merge] 规格: {output_rows} 行 × {len(df.columns)} 列")

    usable_yes = int((df["是否营销可用"] == "是").sum()) if "是否营销可用" in df.columns else None
    usable_ratio = round(usable_yes / output_rows, 4) if usable_yes is not None and output_rows else None

    lines = [
        f"# HC 健康度摘要 (run_id=r{run_id}, mode={mode})", "",
        f"宽表规格: {output_rows} 行 × {len(df.columns)} 列, row_match={row_match}, "
        f"序号重复={dup_rows}", "",
        "## 各任务缺失情况",
    ]
    for task in TASK_ORDER + ["c3", "c2"]:
        is_missing = missing.get(task, False)
        vr = valid_rates.get(task)
        vr_str = f", 有效率 {vr:.2%}" if vr is not None else ""
        flag = "❌ 整批缺失" if is_missing else ("✅ 正常" if vr is None or vr >= 0.92 else "⚠️ 有效率偏低")
        lines.append(f"- {task.upper()}: {flag}{vr_str}")
    if usable_ratio is not None:
        lines += ["", f"## C0 是否营销可用占比: {usable_ratio:.2%}"]
    if c3_hit_rate is not None:
        lines.append(f"## C3 窗口期节点命中率: {c3_hit_rate:.2%}")
    missing_count = sum(1 for v in missing.values() if v)
    if missing_count >= 2:
        lines += ["", "## ⚠️ ≥2 路整批缺失, 建议人工确认后再送 HC"]

    quality = quality_report(
        df, missing=missing, valid_rates=valid_rates,
        unusable_reasons=_unusable_reasons(
            ann_dir / TASK_MERGE_SPECS["c0"]["subdir"] / f"c0_postprocess_r{run_id}.xlsx"),
    )
    status_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌", "missing": "❌"}
    lines += ["", f"## 综合质量验证: {quality['status']}", "",
              "| 检查项 | 值 | 期望 | 结果 |", "|---|---|---|---|"]
    for c in quality["checks"]:
        value = c["value"]
        shown = f"{value:.2%}" if isinstance(value, float) else value
        lines.append(f"| {c['item']} | {shown} | {c['expect']} | {status_icon[c['status']]} |")
    m = quality["metrics"]
    lines += ["", "## 平台分布", "", "| 平台 | 行数 | 占比 |", "|---|---|---|"]
    for name, v in m["platform_distribution"].items():
        share = f"{v['share']:.2%}" if v["share"] is not None else "-"
        lines.append(f"| {name} | {v['rows']} | {share} |")
    if m["unusable_reasons"]:
        lines += ["", "## C0 不可用原因 TOP5"]
        lines += [f"- {k}: {v}" for k, v in m["unusable_reasons"].items()]

    summary_text = "\n".join(lines)
    summary_file = merge_dir / f"health_summary_{mode}_r{run_id}.md"
    summary_file.write_text(summary_text, encoding="utf-8")
    print(f"\n[merge] 健康度摘要: {summary_file}")
    print(summary_text)

    # 关卡由脚本判定(宽表里 row_id 已改名为「序号」,Agent 不必再读 xlsx 自校验)
    gate_ok = bool(row_match and dup_rows == 0)
    result = {
        "base_rows": base_rows, "output_rows": output_rows,
        "row_match": row_match, "dup_rows": dup_rows, "gate_ok": gate_ok,
        "id_column": "序号", "columns": len(df.columns),
        "missing": missing, "valid_rates": valid_rates,
        "c3_hit_rate": c3_hit_rate, "usable_ratio": usable_ratio,
        "quality_status": quality["status"],
        "quality_checks": quality["checks"],
        "quality_warnings": quality["warnings"],
        "hc_metrics": quality["metrics"],
        "run_id": run_id, "mode": mode,
        "out_file": str(out_file),
        "health_summary_file": str(summary_file),
    }
    status_key = "d_full" if mode == "skip_sampling" else "d_sample"
    update_run_config(
        proj,
        {status_key: {
            "completed": True,
            "mode": mode,
            "run_id": run_id,
            "row_count": output_rows,
            "row_match": row_match,
            "dup_rows": dup_rows,
            "gate_ok": gate_ok,
            "quality_status": quality["status"],
            "output_file": str(out_file),
            "health_summary_file": str(summary_file),
        }},
    )
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--mode",
        default="full",
        choices=["demo", "full", "skip_sampling"],
    )
    p.add_argument("--run-id", type=int, required=True)
    args = p.parse_args()
    try:
        result = run(args.project_dir, args.mode, args.run_id)
        print("\n=== 合并完成 ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        status_key = "d_full" if args.mode == "skip_sampling" else "d_sample"
        try:
            update_run_config(
                args.project_dir,
                {status_key: {
                    "completed": False,
                    "status": "failed",
                    "mode": args.mode,
                    "run_id": args.run_id,
                    "error": str(exc),
                    "failed_at": datetime.now().astimezone().isoformat(),
                }},
            )
        except Exception as status_error:
            print(f"[WARN] 写入失败状态失败: {status_error}", file=sys.stderr)
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
