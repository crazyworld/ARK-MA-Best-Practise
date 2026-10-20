#!/usr/bin/env python3
"""Validator 确定性数据检查脚本 — 在 MA 沙箱内由 Validator agent 调用。

对 HC1/HC2 阶段的宽表 xlsx 做结构化统计，输出 JSON 供 Validator agent
读取并与检查清单阈值比对。不做任何语义判断，只提供确定性指标。

依赖: pandas >= 2.0, openpyxl >= 3.1 (environment.json 已声明)

用法:
    python /mnt/skills/validator/scripts/xlsx_checker.py \
        --file <xlsx_path> \
        --task <hc1|hc2> \
        [--enum-file <枚举定义 JSON 路径>]

输出到 stdout 的 JSON 结构见 _build_report() 文档。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import pandas as pd
except ImportError:
    print(
        json.dumps({"error": "pandas not installed", "detail": "pip install pandas openpyxl"}),
        file=sys.stdout,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 乱码检测正则: 控制字符(除 \t\n\r)、连续非 CJK/ASCII 的 Unicode 替换字符等
_GARBLED_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f]"  # ASCII 控制字符(排除 \t \n \r)
    r"|[\ufffd]{2,}"                    # 连续 U+FFFD 替换字符
    r"|parse[_\s]?error"               # 显式解析失败标记
    r"|FAILED"                         # 显式失败标记
    r"|\\x[0-9a-fA-F]{2}",            # 转义的十六进制序列
    re.IGNORECASE,
)

# HC1/HC2 需要检查的标注结果列(与 coordinator.system.md + _字段速查.md 对齐)。
# key = 列名, value = 是否为分类枚举列(True → 做枚举越界检查)
_ANNOTATION_COLUMNS: dict[str, bool] = {
    "商业实体": False,
    "热点驱动词": False,
    "行业归属": True,
    "营销触发方式": True,
    "营销维度": True,
    "平台原生形式": True,
    "不可用原因": True,
    "是否营销可用": True,
    "判断说明": False,
    "关联节点": False,
    "是否节日营销": True,
    "涉及品牌": False,
    "是否节点定制营销": True,
    "是否平台玩法": True,
    "是否商业合作": True,
    "合作类型": True,
    "是否风险预警": True,
    "风险类型": True,
    "是否营销发现": True,
    "创意类型": True,
    "是否消费者行为": True,
    "行为类型": True,
}


# ---------------------------------------------------------------------------
# 核心检查逻辑
# ---------------------------------------------------------------------------

def _check_column(
    series: pd.Series,
    is_enum: bool,
    known_enums: Optional[set[str]] = None,
) -> dict[str, Any]:
    """对单列做确定性统计。"""
    total = len(series)
    if total == 0:
        return {
            "null_rate": 0.0,
            "garbled_rate": 0.0,
            "unique_values": [],
            "unknown_values": [],
            "anomaly_rate": 0.0,
        }

    # 空值率
    null_count = int(series.isna().sum())
    null_rate = round(null_count / total, 4)

    # 非空值
    non_null = series.dropna().astype(str)

    # 乱码率
    garbled_mask = non_null.str.contains(_GARBLED_PATTERN, na=False)
    garbled_count = int(garbled_mask.sum())
    garbled_rate = round(garbled_count / total, 4)

    # 唯一值(最多返回 50 个,避免 JSON 过大)
    unique_vals = sorted(non_null.unique().tolist())
    unique_display = unique_vals[:50]

    # 枚举越界
    unknown_vals: list[str] = []
    if is_enum and known_enums:
        for v in unique_vals:
            v_stripped = v.strip()
            if v_stripped and v_stripped not in known_enums:
                unknown_vals.append(v_stripped)

    # 异常率 = 空值 + 乱码 的联合率(去重)
    anomaly_count = null_count + garbled_count
    anomaly_rate = round(min(anomaly_count / total, 1.0), 4)

    return {
        "null_rate": null_rate,
        "garbled_rate": garbled_rate,
        "unique_values": unique_display,
        "unknown_values": unknown_vals[:20],
        "anomaly_rate": anomaly_rate,
    }


def _load_enums(enum_file: Optional[str]) -> dict[str, set[str]]:
    """加载枚举定义 JSON: {"列名": ["值1", "值2", ...]}。"""
    if not enum_file or not Path(enum_file).is_file():
        return {}
    try:
        with open(enum_file, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: set(v) for k, v in raw.items() if isinstance(v, list)}
    except (json.JSONDecodeError, OSError):
        return {}


def _build_report(
    df: pd.DataFrame,
    task: str,
    enum_map: dict[str, set[str]],
) -> dict[str, Any]:
    """构建完整检查报告。

    返回 JSON 结构:
    {
        "task": "hc1",
        "shape": {"rows": 500, "cols": 28},
        "column_stats": {
            "<列名>": {
                "null_rate": 0.02,
                "garbled_rate": 0.0,
                "unique_values": [...],
                "unknown_values": [...],
                "anomaly_rate": 0.02
            }, ...
        },
        "overall": {
            "any_column_null_above_50pct": false,
            "any_column_null_above_20pct": false,
            "any_column_garbled_above_5pct": false,
            "total_unknown_categories": 1,
            "columns_checked": 12,
            "columns_missing": ["列名", ...]
        }
    }
    """
    rows, cols = df.shape
    column_stats: dict[str, dict] = {}
    columns_missing: list[str] = []

    for col_name, is_enum in _ANNOTATION_COLUMNS.items():
        if col_name not in df.columns:
            columns_missing.append(col_name)
            continue
        known = enum_map.get(col_name)
        col_series: pd.Series = df[col_name]  # type: ignore[assignment]
        column_stats[col_name] = _check_column(
            col_series, is_enum=is_enum, known_enums=known
        )

    # 汇总指标
    any_null_50 = any(
        s["null_rate"] > 0.50 for s in column_stats.values()
    )
    any_null_20 = any(
        s["null_rate"] > 0.20 for s in column_stats.values()
    )
    any_garbled_5 = any(
        s["garbled_rate"] > 0.05 for s in column_stats.values()
    )
    total_unknown = sum(
        len(s["unknown_values"]) for s in column_stats.values()
    )

    return {
        "task": task,
        "shape": {"rows": rows, "cols": cols},
        "column_stats": column_stats,
        "overall": {
            "any_column_null_above_50pct": any_null_50,
            "any_column_null_above_20pct": any_null_20,
            "any_column_garbled_above_5pct": any_garbled_5,
            "total_unknown_categories": total_unknown,
            "columns_checked": len(column_stats),
            "columns_missing": columns_missing,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validator 确定性数据检查脚本",
    )
    parser.add_argument(
        "--file", required=True, help="待检查的 xlsx 文件路径",
    )
    parser.add_argument(
        "--task",
        required=True,
        choices=["hc1", "hc2"],
        help="检查任务类型",
    )
    parser.add_argument(
        "--enum-file",
        default="",
        help="枚举定义 JSON 路径(可选)",
    )
    args = parser.parse_args()

    xlsx_path = Path(args.file)
    if not xlsx_path.is_file():
        print(
            json.dumps(
                {"error": "file_not_found", "path": str(xlsx_path)},
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    try:
        df = pd.read_excel(xlsx_path, engine="openpyxl")
    except Exception as exc:
        print(
            json.dumps(
                {"error": "read_failed", "detail": str(exc)[:500]},
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    enum_map = _load_enums(args.enum_file)
    report = _build_report(df, args.task, enum_map)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
