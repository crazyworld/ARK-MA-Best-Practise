"""标注结构预检（只读）— 移植原版 tools/check_annotation_structure.py。

原版口径：
  - 只读预检，不改数据，不调模型（llm_requests 恒为 0）。
  - 检查项：ID 缺失/重复、轮次一取值域、别名冲突、观点适用范围与候选、
    观点去重后不超过 max_opinion_labels（默认 2）、UGC 适用范围、主讨论对象三枚举。
  - 阶段化检查：round1 / positive / negative / attributes / full；单任务阶段还要
    拒绝落在该任务适用范围之外的行（row_outside_task_scope）。
  - 已批准的类别清单缺失时直接抛错（禁止在未确认类别时做预检）。

本模块的入参是本系统标注行的内部字段名（ai_sentiment 为 int、relevance 为 bool），
读出一律经 `_row_view` 转成原版的语义字符串，保证判定条件与原版逐条一致。
"""
from __future__ import annotations

from collections import Counter

# 原版 ALIASES：同一语义的不同列名。用于发现清洗层漏映射导致的别名冲突。
ALIASES = {
    "sentiment": ("情感(AI)", "sentiment"),
    "relevant": ("是否活动相关(AI)", "是否活动相关", "campaign_relevant"),
    "opinion": ("观点类别(AI)", "观点(AI)", "观点"),
    "ugc": ("ugc_category",),
    "dimension": ("discussion_dimension",),
    "type": ("内容类型", "content_type"),
}

SENTIMENT_LABELS = ("正面", "中性", "负面")
RELEVANT_LABELS = ("是", "否")
CONTENT_TYPES = ("UGC", "PGC", "未知")
DIMENSION_LABELS = ("品牌", "产品", "活动")
# 观点非适用范围时的合法占位值（原版 opinion_not_applicable 默认值）。
OPINION_PLACEHOLDERS = ("N/A", "无", "")
STAGES = ("round1", "positive", "negative", "attributes", "full")


def _int_to_label(value) -> str:
    return {1: "正面", 0: "中性", -1: "负面"}.get(value, "")


def _bool_to_label(value) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return ""


def _row_view(row: dict) -> dict:
    """内部行 → 原版语义字段（sentiment/relevant/opinion/ugc/dimension/type）。"""
    mode = str(row.get("content_mode") or "").strip()
    return {
        "sentiment": _int_to_label(row.get("ai_sentiment")),
        "relevant": _bool_to_label(row.get("relevance")),
        "opinion": str(row.get("opinion") or "").strip(),
        "ugc": str(row.get("ugc_category") or "").strip(),
        "dimension": str(row.get("discussion_dimension") or "").strip(),
        "type": mode if mode in CONTENT_TYPES else "未知",
    }


def _alias_value(row: dict, key: str, row_id: str, errors: list[dict]) -> str:
    """按别名取值；多个别名值不一致时记 conflicting_aliases（原版 value()）。"""
    found = [(name, row[name]) for name in ALIASES[key] if name in row]
    values = ["" if value is None else str(value).strip() for _, value in found]
    if len(set(values)) > 1:
        errors.append({"id": row_id, "field": key, "reason": "conflicting_aliases"})
    return values[0] if values else ""


def _split_labels(value: str) -> list[str]:
    """原版用英文逗号分隔观点标签。"""
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _require_config(config: dict, stage: str) -> None:
    """按阶段校验已批准类别清单是否齐备（原版 check() 开头的守卫）。"""
    if stage in ("positive", "negative", "full"):
        required = (("positive_labels", "negative_labels") if stage == "full"
                    else (f"{stage}_labels",))
        for key in required:
            if key not in config:
                raise ValueError(f"Missing approved config: {key}")
    if stage in ("attributes", "full") and "ugc_labels" not in config:
        raise ValueError("Missing approved config: ugc_labels")


def _check_identity(row: dict, index: int, seen: set, errors: list[dict]) -> str:
    """ID 缺失/重复。返回 locator（原版用 row:<index> 兜底）。"""
    row_id = str(row.get("_annot_id") or row.get("id") or "").strip()
    locator = row_id or "row:" + str(index + 1)
    if not row_id:
        errors.append({"id": locator, "field": "id", "reason": "missing_id"})
    elif row_id in seen:
        errors.append({"id": locator, "field": "id", "reason": "duplicate_id"})
    seen.add(row_id)
    return locator


def _check_round1(view: dict, locator: str, errors: list[dict]) -> None:
    """轮次一取值域：情感三枚举、相关性是/否，均不允许空。"""
    if view["sentiment"] not in SENTIMENT_LABELS:
        errors.append({"id": locator, "field": "sentiment",
                       "reason": "illegal_or_empty"})
    if view["relevant"] not in RELEVANT_LABELS:
        errors.append({"id": locator, "field": "relevant",
                       "reason": "illegal_or_empty"})


def _check_opinion(view: dict, locator: str, config: dict, stage: str,
                   errors: list[dict]) -> None:
    """观点：只在「相关且正面/负面」的行上合法，值须在已批准类别内。"""
    direction = {"正面": "positive", "负面": "negative"}.get(view["sentiment"])
    applicable = view["relevant"] == "是" and direction is not None
    if stage != "full" and (not applicable or direction != stage):
        errors.append({"id": locator, "field": "opinion",
                       "reason": "row_outside_task_scope"})
        return
    if applicable and config.get(direction + "_applicable", True):
        labels = _split_labels(view["opinion"])
        allowed = set(config[direction + "_labels"])
        if not labels or any(label not in allowed for label in labels):
            errors.append({"id": locator, "field": "opinion",
                           "reason": "illegal_or_empty"})
        if len(labels) != len(set(labels)):
            errors.append({"id": locator, "field": "opinion",
                           "reason": "duplicate_labels"})
        if len(labels) > config["max_opinion_labels"]:
            errors.append({"id": locator, "field": "opinion",
                           "reason": "too_many_labels"})
    elif view["opinion"] not in config["opinion_not_applicable"]:
        errors.append({"id": locator, "field": "opinion",
                       "reason": "label_outside_applicable_scope"})


def _check_attributes(view: dict, locator: str, config: dict,
                      errors: list[dict]) -> None:
    """属性任务：UGC 只在「相关且 UGC」行取值；主讨论对象只在相关行取值。"""
    if view["type"] not in CONTENT_TYPES:
        errors.append({"id": locator, "field": "type", "reason": "illegal_or_empty"})
    ugc_applicable = (view["relevant"] == "是" and view["type"] == "UGC"
                      and config.get("ugc_applicable", True))
    if ugc_applicable:
        if view["ugc"] not in config["ugc_labels"]:
            errors.append({"id": locator, "field": "ugc",
                           "reason": "illegal_or_empty"})
    elif view["ugc"] != "N/A":
        errors.append({"id": locator, "field": "ugc",
                       "reason": "label_outside_applicable_scope"})
    if view["relevant"] == "是":
        if view["dimension"] not in config["dimension_labels"]:
            errors.append({"id": locator, "field": "dimension",
                           "reason": "illegal_or_empty"})
    elif view["dimension"] != "N/A":
        errors.append({"id": locator, "field": "dimension",
                       "reason": "label_outside_applicable_scope"})


def _normalize_config(config: dict) -> dict:
    """补齐可选配置的默认值（原版各处 get(..., 默认值) 的集中写法）。"""
    merged = dict(config or {})
    merged.setdefault("opinion_not_applicable", list(OPINION_PLACEHOLDERS))
    merged.setdefault("max_opinion_labels", 2)
    merged.setdefault("dimension_labels", list(DIMENSION_LABELS))
    limit = merged["max_opinion_labels"]
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("max_opinion_labels must be a positive integer")
    return merged


def check_structure(
    rows: list[dict], *, config: dict, stage: str
) -> dict:
    """只读结构预检，返回原版同构的报告（不含 sha256 字段，由调用方按需补）。

    stage ∈ {round1, positive, negative, attributes, full}；两个轮次二阶段
    （positive/negative/attributes）只检查该任务适用范围，full 检查全部规则。
    """
    if stage not in STAGES:
        raise ValueError(f"check_structure: 未知 stage={stage!r}，应属 {STAGES}")
    _require_config(config or {}, stage)
    cfg = _normalize_config(config)

    errors: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        locator = _check_identity(row, index, seen, errors)
        view = _row_view(row)
        # 别名冲突：行里若混入原始列名且取值不一致，说明清洗层漏映射。
        for key in ALIASES:
            _alias_value(row, key, locator, errors)
        if row.get("parse_error"):
            errors.append({"id": locator, "field": "output",
                           "reason": "parse_error"})
        _check_round1(view, locator, errors)
        if stage in ("positive", "negative", "full"):
            _check_opinion(view, locator, cfg, stage, errors)
        if stage in ("attributes", "full"):
            _check_attributes(view, locator, cfg, errors)

    return {
        "status": "pass" if not errors else "fail",
        "stage": stage,
        "rows": len(rows),
        "error_count": len(errors),
        "errors_by_reason": dict(Counter(e["reason"] for e in errors)),
        "errors": errors,
        # 结构预检不做语义正确性判断（由 FIX-30 抽检负责），也不改数据、不调模型。
        "semantic_accuracy_checked": False,
        "source_modified": False,
        "llm_requests": 0,
    }
