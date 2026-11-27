"""原版脚本适配层：原版脚本零修改复用，这里只做输入输出形状转换。

原则（见 docs/ORIGINAL_RULES.md）：
  - 原版有脚本的确定性逻辑，直接 import 原版文件执行，不在编排器里重写。
  - 本模块只允许做「内部行 → 原版列名」「内部配置 → 原版配置键」的映射，
    不得加入任何判定逻辑；判定一律由原版脚本给出。
  - 原版文件由 original/MANIFEST.json 记录 sha256，`original_verify.py` 门禁校验。

已迁入 original/ 的原版资产：
  tools/check_annotation_structure.py   标注结构预检（纯 stdlib）
  tools/plan_annotation_review.py       标注抽检计划（纯 stdlib）
  stats/*.py                            统计聚合五脚本（openpyxl）
  discovery/*.py                        类别发现两脚本（anthropic SDK，待接口确认后启用）
  annot/*.py                            两轮标注脚本（anthropic SDK，待接口确认后启用）
  ks/*.md, prompts/*.md                 原版知识源原文（供 Coordinator/Validator 直接引用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ORIGINAL_ROOT = Path(__file__).resolve().parent / "original"
_TOOLS = ORIGINAL_ROOT / "tools"
_STATS = ORIGINAL_ROOT / "stats"
_DISCOVERY = ORIGINAL_ROOT / "discovery"
_ANNOT = ORIGINAL_ROOT / "annot"

for _p in (_TOOLS, _STATS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_annotation_structure as _orig_structure  # noqa: E402
import plan_annotation_review as _orig_plan            # noqa: E402

# 内部行字段 → 原版列名。原版脚本按 ALIASES 取值，这里给出唯一规范列名。
_INTERNAL_TO_ORIGINAL = {
    "ai_sentiment": "情感(AI)",
    "relevance": "是否活动相关(AI)",
    "opinion": "观点类别(AI)",
    "ugc_category": "ugc_category",
    "discussion_dimension": "discussion_dimension",
    "content_mode": "内容类型",
}

SENTIMENT_BY_INT = {1: "正面", 0: "中性", -1: "负面"}


def _sentiment_label(value) -> str:
    if isinstance(value, int) and value in SENTIMENT_BY_INT:
        return SENTIMENT_BY_INT[value]
    text = str(value or "").strip()
    return text if text in ("正面", "中性", "负面") else ""


def _relevant_label(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value or "").strip()
    return text if text in ("是", "否") else ""


def to_original_rows(rows: list[dict]) -> list[dict]:
    """内部行 → 原版列名的行。

    若内部行本身已带某字段的原版列名（清洗层漏映射的现场），原样透传所有别名取值，
    让原版 check() 自己判 conflicting_aliases；否则由内部字段映射成唯一规范列名。
    """
    out = []
    for row in rows:
        row = row if isinstance(row, dict) else {}
        item: dict = {"id": str(row.get("_annot_id") or row.get("id") or "")}
        canonical = {
            "sentiment": _sentiment_label(row.get("ai_sentiment")),
            "relevant": _relevant_label(row.get("relevance")),
            "opinion": str(row.get("opinion") or "").strip(),
            "ugc": str(row.get("ugc_category") or "").strip(),
            "dimension": str(row.get("discussion_dimension") or "").strip(),
            "type": (str(row.get("content_mode") or "").strip()
                     if str(row.get("content_mode") or "").strip() in ("PGC", "UGC", "未知")
                     else "未知"),
        }
        for key, names in _orig_structure.ALIASES.items():
            present = [name for name in names if name in row]
            if present:
                for name in present:
                    value = row[name]
                    item[name] = "" if value is None else str(value).strip()
            else:
                item[names[0]] = canonical[key]
        if row.get("parse_error"):
            item["parse_error"] = row["parse_error"]
        out.append(item)
    return out


def normalize_config(config: dict | None) -> dict:
    """补原版默认键（原版 check() 用 config.get 取默认值，这里显式化便于审计）。"""
    cfg = dict(config or {})
    cfg.setdefault("opinion_not_applicable", ["N/A", "无", ""])
    cfg.setdefault("max_opinion_labels", 2)
    cfg.setdefault("dimension_labels", ["品牌", "产品", "活动"])
    return cfg


# 编排器的两轮 → 原版脚本的检查阶段。轮次二在 MA 是单节点承载「观点 + UGC 属性 +
# 讨论维度 + 覆盖」三任务（口径修订 2026-10-21），因此对应原版 full 阶段，
# 正/负向分方向检查由 config 的 positive_labels/negative_labels 在 full 内一并执行。
STAGE_TO_ORIGINAL = {
    "round1": "round1", "round2": "full",
    # 原版四阶段名直接透传（编排器只用两轮，分方向/属性阶段供单测与原版脚本对齐核对）
    "positive": "positive", "negative": "negative", "attributes": "attributes", "full": "full",
}


def check_structure(rows: list[dict], *, config: dict, stage: str) -> dict:
    """调用原版 tools/check_annotation_structure.py 的 check()。"""
    if stage not in STAGE_TO_ORIGINAL:
        raise ValueError(f"check_structure: 未知 stage={stage!r}")
    stage = STAGE_TO_ORIGINAL[stage]
    report = _orig_structure.check(to_original_rows(rows), normalize_config(config), stage)
    report["source_script"] = "original/tools/check_annotation_structure.py"
    return report


def plan_review(rows: list[dict], *, stage: str, reviewed_ids: list[str] | None = None,
                reserve: int = 100, seed: int = 42, config: dict | None = None) -> dict:
    """调用原版 tools/plan_annotation_review.py 的 plan()。

    reviewed_ids 必须是**本活动本轮所有已审帖 ID 的并集**（校准、类别发现来源核验、
    随机抽查、重点核查、返工复审共用 500 额度，执行契约 §2）。
    """
    result = _orig_plan.plan(
        to_original_rows(rows), stage=stage, reviewed_ids=reviewed_ids,
        reserve=reserve, seed=seed, config=normalize_config(config))
    result["source_script"] = "original/tools/plan_annotation_review.py"
    return result


def manifest() -> dict:
    path = ORIGINAL_ROOT / "MANIFEST.json"
    return json.loads(path.read_text(encoding="utf-8"))
