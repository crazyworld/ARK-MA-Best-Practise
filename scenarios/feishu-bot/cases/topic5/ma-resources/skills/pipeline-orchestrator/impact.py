"""补证/数据修订的影响矩阵（FIX-60，原版 KS_Report_Scope_Contract §5「补证后的最小完整更新链」）。

新证据 → 复审受影响标签 → 保存精确变更 → 重算受影响统计 → 更新榜单、图表、引用 → 独立复检。
没有实际标签或范围变化时复用无关模块；不自动重复付费采集或全量标注。
无法识别的变动类型一律退回全量失效（宁可多算，不漏算）。
"""
from __future__ import annotations

# 报告侧下游：任何统计变化都要重出图、重写/返修、重新质检与交付。
_REPORT_TAIL = ["chart_urls", "report_md", "qc_report", "delivery_draft", "external_receipts", "feishu_doc"]

# 变动类型 → (需失效的产物, 需要重标的轮次, 是否需要重新授权计费采集)
# 原版表：
#   仅由待核实变为已确认、统计标签不变 → 展示资格/排序/高点叙述/旧限制语/榜单；整体数值不变
#   活动相关性/明确噪音 → 统一总体全部重算（按去噪流程）
#   情感 → 整体及分组 NSR、情感分布、正负观点统计和引用
#   主讨论对象 → 分组 NSR、主对象口径漏斗及引用；不改 SSJH 品牌提及词云
#   UGC 参与方式/观点 → 对应分类数量、占比、图表、代表帖与引用
#   产品词、品牌筛选方法、平台或日期范围 → 对应范围全部重算
IMPACT_MATRIX: dict[str, dict] = {
    "display_eligibility": {"invalidate": ["stats_bundle"] + _REPORT_TAIL, "rerun": None},
    "relevance": {"invalidate": ["denoised_data", "noise_review", "annotated_round1", "r1_review",
                                 "opinion_candidates", "ugc_category_list", "annotated_data", "r2_review",
                                 "stats_bundle", "wordcloud_insight"] + _REPORT_TAIL,
                  "rerun": "round1"},
    "sentiment": {"invalidate": ["annotated_round1", "r1_review", "annotated_data", "r2_review",
                                 "stats_bundle"] + _REPORT_TAIL, "rerun": "round1"},
    "main_object": {"invalidate": ["annotated_data", "r2_review", "stats_bundle"] + _REPORT_TAIL,
                    "rerun": "round2"},
    "ugc": {"invalidate": ["annotated_data", "r2_review", "stats_bundle"] + _REPORT_TAIL, "rerun": "round2"},
    "opinion": {"invalidate": ["annotated_data", "r2_review", "stats_bundle"] + _REPORT_TAIL, "rerun": "round2"},
}
# 范围类变动：对应范围全部重算，且涉及取数/计费采集，需重新授权（FIX-04）。
SCOPE_CHANGES = ("product_words", "brand_filter", "platform", "date_range", "keyword")
FIELD_TO_TYPE = {
    "display": "display_eligibility", "relevance": "relevance", "is_relevant": "relevance",
    "noise": "relevance", "ai_sentiment": "sentiment", "sentiment": "sentiment",
    "discussion_dimension": "main_object", "main_object": "main_object",
    "ugc_category": "ugc", "opinion": "opinion", "opinion_label": "opinion",
    "product_words": "product_words", "brand_filter": "brand_filter",
    "platform": "platform", "_platform": "platform", "publish_time": "date_range",
    "date_range": "date_range", "keyword": "keyword",
}
FULL_INVALIDATE = [
    "brand_stats", "brand_stats_baseline", "activity_detail", "cleaned_data", "denoised_data",
    "noise_review", "annotated_round1", "r1_review", "opinion_candidates", "confirmed_opinion_labels",
    "ugc_category_list", "annotated_data", "r2_review", "wordcloud_data", "stats_bundle",
    "wordcloud_insight",
] + _REPORT_TAIL


def plan_revision(changes: list[dict]) -> dict:
    """changes=[{row_id, field, old, new}] → {invalidate, rerun_rows, requires_authorization, types}。"""
    invalidate: list[str] = []
    rerun_rows: dict[str, list[str]] = {"round1": [], "round2": []}
    types: list[str] = []
    authorization = False
    for change in changes or []:
        change_type = FIELD_TO_TYPE.get(str(change.get("field") or ""))
        if change_type in SCOPE_CHANGES:
            authorization = authorization or change_type in ("date_range", "keyword", "platform")
            return {"invalidate": list(FULL_INVALIDATE), "rerun_rows": {"round1": [], "round2": []},
                    "requires_authorization": authorization, "types": [change_type], "full": True}
        if change_type not in IMPACT_MATRIX:
            # 未识别的变动：退回全量失效，避免漏失效。
            return {"invalidate": list(FULL_INVALIDATE), "rerun_rows": {"round1": [], "round2": []},
                    "requires_authorization": False, "types": [str(change.get("field"))], "full": True}
        rule = IMPACT_MATRIX[change_type]
        types.append(change_type)
        invalidate += [k for k in rule["invalidate"] if k not in invalidate]
        if rule["rerun"] and change.get("row_id"):
            rerun_rows[rule["rerun"]].append(str(change["row_id"]))
            if rule["rerun"] == "round1":
                rerun_rows["round2"].append(str(change["row_id"]))   # 轮次二依赖轮次一结果
    return {"invalidate": invalidate,
            "rerun_rows": {k: sorted(set(v)) for k, v in rerun_rows.items()},
            "requires_authorization": authorization, "types": sorted(set(types)), "full": False}


# 失效产物 → 重跑入口节点（按 DAG 顺序，取最早者）。
ENTRY_NODES = (
    ("brand_stats", "n02_plan"), ("activity_detail", "n02_plan"), ("cleaned_data", "n07_clean"),
    ("denoised_data", "n07b_noise_review"), ("annotated_round1", "n09_annotate_r1"),
    ("annotated_data", "n11_annotate_r2"), ("stats_bundle", "n12_aggregate"),
)


def entry_node(invalidate: list[str]) -> str:
    for artifact, node in ENTRY_NODES:
        if artifact in invalidate:
            return node
    return "n19_fix"
