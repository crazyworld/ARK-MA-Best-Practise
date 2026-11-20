"""标注抽检计划 — 移植原版 tools/plan_annotation_review.py。

原版口径（纯确定性，不调模型）：
  - 额度：每个活动每轮 min(ceil(10%), 500) 个不同的帖子；已审 ID 跨轮次扣减。
  - reserve：为定向抽查与复审预留，最多 500，按剩余额度收敛。
  - 分层：特殊层（不相关/负面/其他/空值或非法/类型未知/证据歧义）优先覆盖，
    再按 sqrt(层容量) / (已分配+1) 加权过采样，特殊层权重 ×3。
  - 层内用 seed=42 的 Random 随机抽取，结果可复现。
  - 阶段化适用范围：round1 全量；round2 只看「相关且正/负」；attributes 只看相关行。
  - 抽样得到的正确率是抽样估计，不是总体真值（accuracy_is_population_truth=False）。

本模块沿用内部字段名（ai_sentiment 为 int、relevance 为 bool），判定条件经
`annotation_structure._row_view` 转成原版语义值，与原版逐条一致。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

from annotation_structure import (
    ALIASES, CONTENT_TYPES, DIMENSION_LABELS, RELEVANT_LABELS,
    SENTIMENT_LABELS, _row_view, _split_labels,
)

# 抽检总额度上限（原版 cap=500）
REVIEW_CAP = 500
# 抽检比例（原版 base = min(ceil(population * .1), 500)）
REVIEW_RATIO = 0.10
# 特殊层过采样权重（原版 3 if 特殊 else 1）
SPECIAL_WEIGHT = 3
DEFAULT_SEED = 42
DEFAULT_RESERVE = 100
PLAN_STAGES = ("round1", "round2", "attributes")
# 观点与 UGC 的「其他」标记（特殊层）
OTHER_LABEL = "其他"
SPECIAL_PREFIX = "特殊:"
NORMAL_PREFIX = "正常"


def _row_id(row: dict) -> str:
    return str(row.get("_annot_id") or row.get("id") or "").strip()


def _special_labels(row: dict, view: dict, stage: str, config: dict) -> list[str]:
    """收集该行命中的特殊层标签（原版 stratum() 的逐条判定）。"""
    special: list[str] = []
    if view["relevant"] == "否":
        special.append("不相关")
    if view["sentiment"] == "负面":
        special.append("负面")
    opinion_labels = _split_labels(view["opinion"])
    if stage == "round2" and OTHER_LABEL in opinion_labels:
        special.append(OTHER_LABEL)
    # 空值或非法：轮次一取值域越界、解析失败
    if (view["relevant"] not in RELEVANT_LABELS
            or view["sentiment"] not in SENTIMENT_LABELS
            or row.get("parse_error")):
        special.append("空值或非法")
    if stage == "round2":
        if not view["opinion"]:
            special.append("空值或非法")
        direction = {"正面": "positive", "负面": "negative"}.get(view["sentiment"])
        allowed = config.get(f"{direction or ''}_labels")
        if allowed is not None and any(
                label not in allowed for label in opinion_labels):
            special.append("空值或非法")
    if stage == "attributes":
        if view["type"] == "未知":
            special.append("类型未知")
        if view["type"] == "UGC" and view["ugc"] == OTHER_LABEL:
            special.append(OTHER_LABEL)
        if (view["type"] == "UGC" and config.get("ugc_applicable", True)
                and view["ugc"] not in config.get("ugc_labels", [view["ugc"]])):
            special.append("空值或非法")
        if view["dimension"] not in config.get("dimension_labels",
                                               list(DIMENSION_LABELS)):
            special.append("空值或非法")
    if row.get("evidence_status") == "unresolved":
        special.append("证据歧义")
    return special


def stratum(row: dict, stage: str, config: dict | None = None) -> str:
    """分层键：特殊层标签 + 基础三元组（类型|情感|相关性[|观点/参与方式+主对象]）。"""
    config = config or {}
    view = _row_view(row)
    special = _special_labels(row, view, stage, config)
    base_parts = [view["type"], view["sentiment"], view["relevant"]]
    if stage == "round2":
        base_parts.append("观点:" + ",".join(sorted(set(_split_labels(view["opinion"])))))
    elif stage == "attributes":
        base_parts.extend([f"参与:{view['ugc']}", f"主对象:{view['dimension']}"])
    prefix = (SPECIAL_PREFIX + "+".join(sorted(set(special)))
              if special else NORMAL_PREFIX)
    return prefix + "|" + "|".join(base_parts)


def _in_scope(row: dict, stage: str, config: dict) -> bool:
    """阶段适用范围（原版 plan() 循环开头的三条 continue）。"""
    view = _row_view(row)
    if stage == "round2":
        if view["relevant"] != "是" or view["sentiment"] not in ("正面", "负面"):
            return False
        direction = {"正面": "positive", "负面": "negative"}[view["sentiment"]]
        return bool(config.get(f"{direction}_applicable", True))
    if stage == "attributes":
        return view["relevant"] == "是"
    return True


def _field_populations(rows: list[dict], stage: str, config: dict) -> dict[str, int]:
    """按字段统计适用范围容量（原版 field_populations）。

    原版在同一循环的 scope 过滤（continue）之后计数，因此这里也必须先过
    `_in_scope`；否则 round2/attributes 的分母会把不在范围内的行算进去。
    """
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if not _in_scope(row, stage, config):
            continue
        view = _row_view(row)
        if stage == "round1":
            counts["sentiment"] += 1
            counts["relevant"] += 1
        elif stage == "round2":
            counts["opinion"] += 1
        else:
            counts["dimension"] += 1
            if view["type"] == "UGC" and config.get("ugc_applicable", True):
                counts["ugc"] += 1
    return dict(counts)


def _allocate(groups: dict[str, list[str]], target: int) -> dict[str, int]:
    """分层配额：先覆盖各层，再按加权容量过采样特殊层（原版配额分配）。"""
    quotas = {key: 0 for key in groups}
    if target <= 0:
        return quotas
    # 正常层排到第 2 位，保证「先覆盖特殊层，再补一个正常层」
    order = sorted(groups, key=lambda key: (not key.startswith(SPECIAL_PREFIX), key))
    normals = [key for key in order if key.startswith(NORMAL_PREFIX)]
    if target >= 2 and normals:
        order.remove(normals[0])
        order.insert(1, normals[0])
    for key in order[:target]:
        quotas[key] += 1
    while sum(quotas.values()) < target:
        candidates = [key for key in groups if quotas[key] < len(groups[key])]
        if not candidates:
            break
        key = max(candidates, key=lambda item: (
            (SPECIAL_WEIGHT if item.startswith(SPECIAL_PREFIX) else 1)
            * math.sqrt(len(groups[item])) / (quotas[item] + 1), item))
        quotas[key] += 1
    return quotas


def _validate_inputs(rows: list[dict], stage: str, reviewed_ids, reserve: int,
                     config: dict) -> set[str]:
    """入参与前置条件校验（原版 plan() 的守卫，逐条对应）。"""
    if stage == "round2" and not all(
            key in config for key in ("positive_labels", "negative_labels")):
        raise ValueError("Round two requires approved direction labels/applicability config")
    if stage == "attributes" and "ugc_labels" not in config:
        raise ValueError("Attributes require approved UGC config")
    if reviewed_ids is not None and not isinstance(reviewed_ids, list):
        raise ValueError("Reviewed IDs must be a JSON array")
    if any(value is None or not str(value).strip() for value in (reviewed_ids or [])):
        raise ValueError("Reviewed IDs must be nonempty stable IDs")
    reviewed = {str(value) for value in (reviewed_ids or [])}
    if len(reviewed) > REVIEW_CAP:
        raise ValueError("This round already exceeds the 500-distinct-post cap")
    if reserve < 0 or reserve > REVIEW_CAP:
        raise ValueError("Reserve must be between 0 and 500")
    ids = [_row_id(row) for row in rows]
    if any(not row_id for row_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Stable unique IDs required; run the structural precheck")
    return reviewed


def plan_review(
    rows: list[dict],
    *,
    stage: str,
    reviewed_ids: list[str] | None = None,
    reserve: int = DEFAULT_RESERVE,
    seed: int = DEFAULT_SEED,
    config: dict | None = None,
) -> dict:
    """生成分层抽检计划（纯确定性）。返回原版同构的计划报告。"""
    if stage not in PLAN_STAGES:
        raise ValueError(f"plan_review: 未知 stage={stage!r}，应属 {PLAN_STAGES}")
    config = config or {}
    reviewed = _validate_inputs(rows, stage, reviewed_ids, reserve, config)

    groups: dict[str, list[str]] = defaultdict(list)
    populations: dict[str, int] = defaultdict(int)
    for row in rows:
        if not _in_scope(row, stage, config):
            continue
        key = stratum(row, stage, config)
        populations[key] += 1
        row_id = _row_id(row)
        if row_id not in reviewed:
            groups[key].append(row_id)

    population = sum(populations.values())
    base = min(math.ceil(population * REVIEW_RATIO), REVIEW_CAP)
    remaining = REVIEW_CAP - len(reviewed)
    reserve_actual = min(reserve, max(0, remaining - 1)) if population else 0
    target = min(base, remaining - reserve_actual, sum(len(v) for v in groups.values()))
    quotas = _allocate(groups, target)

    rng = random.Random(seed)
    samples: list[dict] = []
    for key in sorted(groups):
        picked = rng.sample(sorted(groups[key]), quotas[key])
        samples.extend({"id": row_id, "stratum": key,
                        "sample_kind": "stratified_random"} for row_id in picked)

    return {
        "stage": stage,
        "population": population,
        "field_populations": _field_populations(rows, stage, config),
        "base_sample_size": base,
        "sample_size": len(samples),
        "distinct_reviewed_before": len(reviewed),
        "distinct_reviewed_after_plan": len(reviewed) + len(samples),
        "cap": REVIEW_CAP,
        "reserve_requested": reserve,
        "remaining_after_plan": remaining - len(samples),
        "seed": seed,
        "method": "special-prioritized stratified random; unequal inclusion probabilities",
        # 抽样正确率是估计值，不能当成总体真值对外呈现。
        "accuracy_is_population_truth": False,
        "strata": {key: {"population": populations[key],
                         "planned": quotas.get(key, 0)}
                   for key in sorted(populations)},
        "uncovered_strata": [key for key in sorted(populations)
                             if not quotas.get(key)],
        "samples": samples,
        "llm_requests": 0,
    }


def accuracy_from_verdicts(
    verdicts: list[dict], *, reviewed_ids: list[str] | None = None
) -> dict:
    """按字段计算抽检正确率（原版验收口径：两个字段分别算，阈值 90%/目标 95%）。

    verdicts 每项 {id, field, verdict: correct|wrong, expected?}；
    正确率分母是该字段被判定的条数，未被判定的字段不出现在结果里（不补 100%）。
    """
    reviewed = {str(x) for x in (reviewed_ids or [])}
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "correct": 0, "wrong": 0})
    wrong_ids: dict[str, set[str]] = defaultdict(set)
    for item in verdicts:
        field = str(item.get("field") or "").strip()
        if not field:
            continue
        row_id = str(item.get("id") or "").strip()
        if reviewed and row_id not in reviewed:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in ("correct", "wrong"):
            continue
        buckets[field]["total"] += 1
        if verdict == "correct":
            buckets[field]["correct"] += 1
        else:
            buckets[field]["wrong"] += 1
            wrong_ids[field].add(row_id)
    accuracy: dict[str, float | None] = {}
    for field, stat in buckets.items():
        accuracy[field] = (round(stat["correct"] / stat["total"], 4)
                           if stat["total"] else None)
    return {
        "accuracy": accuracy,
        "field_stats": {field: dict(stat) for field, stat in buckets.items()},
        "wrong_ids": {field: sorted(ids) for field, ids in wrong_ids.items()},
        "reviewed_total": sum(item["total"] for item in buckets.values()),
    }
