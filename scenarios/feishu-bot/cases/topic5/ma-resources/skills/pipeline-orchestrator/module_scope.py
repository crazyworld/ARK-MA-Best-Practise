"""逐模块范围记录（FIX-61，原版 KS_Report_Scope_Contract）。

每个活动在 stats_bundle 中附 `_scope_index`（模块名 → 范围记录）：需求指纹、层级、对象、周期、平台、
内容范围、相关性筛选、方法、分母、数据源指纹。值全部来自已确认方案与产物引用，确定性生成。
`_` 前缀键会被 _report_evidence 剔除，不进写作 prompt；Validator（G3）读原始 stats_bundle。
"""
from __future__ import annotations

import hashlib
import json

RELEVANCE_FILTER = "AI 判定活动相关（轮次一）且经 Step5.5 去噪"

# 模块 → (统计键, 方法, 分母)。分母与实际计算一致（例如 NSR 为 pos+neg）。
MODULES: dict[str, tuple[str, str, str]] = {
    "trend": ("trend", "按发表日聚合声量/互动", "活动相关内容"),
    "sentiment": ("sentiment_dist", "AI 情感（轮次一）", "有 AI 情感的活动相关内容"),
    "dimension_nsr": ("dimension_nsr", "AI 讨论对象分组后计算 NSR", "pos+neg"),
    "funnel": ("mention_funnel", "关键词提及分层", "活动相关内容"),
    "kol": ("kol_effectiveness", "粉丝数派生达人层级", "有粉丝数的活动相关内容"),
    "ugc": ("ugc_analysis", "AI UGC 参与方式（轮次二）", "活动相关 UGC"),
    "opinion": ("opinion_analysis", "AI 观点归类（轮次二，已确认观点表）", "同方向情感内容"),
}


def requirement_sha(requirement: dict) -> str:
    body = json.dumps(requirement or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def attach(bundle: dict, *, campaign: str, plan: dict, requirement: dict,
           period: tuple, source_refs: list) -> dict:
    """为活动 bundle 内存在的模块生成范围记录，写入 bundle["_scope_index"]。

    不往分布 dict 内部塞键（情感/平台分布会被直接 sum(values())）。
    """
    base = {
        "requirement_sha": requirement_sha(requirement),
        "level": "activity",
        "object": campaign,
        "period": {"start": period[0], "end": period[1]},
        "platforms": list(plan.get("platforms") or []),
        "content_scope": str(plan.get("content_scope") or "all").lower(),
        "relevance_filter": RELEVANCE_FILTER,
        "source_refs": [ref for ref in source_refs if ref],
    }
    index = {}
    for module, (key, method, denominator) in MODULES.items():
        if key not in bundle:
            continue
        index[module] = {**base, "module": module, "stats_key": key,
                         "method": method, "denominator": denominator}
    bundle["_scope_index"] = index
    return index
