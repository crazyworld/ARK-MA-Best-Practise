"""活动提及漏斗（FIX-18 / FIX-23）：纯函数，无副作用。

原版依据：
  - blueai-stats-aggregation-skill/scripts/aggregate.py:179-204
    Tier1=活动相关全量；Tier2=文本含**主品牌名**（大小写不敏感，不用别名集合）；
    Tier3=Tier2 中再命中任一产品词；Tier2/Tier1 ≥ 95% 视为退化。
  - KS_Report_Scope_Contract.md:23-28：两种方法 keyword_mention（默认）/
    main_discussion（需求明确时）；三个主讨论对象组互斥，不构成递进漏斗。
  - KS_Report_Template.md:467：退化时有产品词→分析活动全量中的产品提及，
    无产品词→只保留活动全量与范围说明；不得自动切换到主对象方法。
  - KS_Feishu_Format.md:271-276：表头 层级/声量/占比，Tier3 行写明分母。
"""
from __future__ import annotations

KEYWORD_MENTION = "keyword_mention"
MAIN_DISCUSSION = "main_discussion"
METHODS = (KEYWORD_MENTION, MAIN_DISCUSSION)
DEGRADE_RATIO = 0.95

# 主动提及的匹配范围：标题、正文、话题标签（KS 提及范围「文本/标签」）。
ACTIVE_FIELDS = ("title", "content")
# 仅出现在识别文本里的品牌名 = 被动带出（图片/封面/视频），不计入品牌提及层。
PASSIVE_FIELDS = ("video_cover_text", "video_flower_text", "video_text", "audio_text")

# 主讨论对象三组（与 handlers 分维 NSR 的键名一致，客户 Report 命名）。
MAIN_DISCUSSION_GROUPS = (
    ("品牌", "品牌主导讨论"),
    ("产品", "产品主导讨论"),
    ("活动", "活动流程与获取主导讨论"),
)


def _active_text(row: dict) -> str:
    text = " ".join(str(row.get(key) or "") for key in ACTIVE_FIELDS)
    tags = row.get("keywords") or []
    if isinstance(tags, list):
        text += " " + " ".join(str(tag) for tag in tags)
    return text.lower()


def _passive_text(row: dict) -> str:
    return " ".join(str(row.get(key) or "") for key in PASSIVE_FIELDS).lower()


def _hit(text: str, words: list[str]) -> bool:
    return any(word and word.lower() in text for word in words)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def keyword_funnel(rows: list[dict], brand_name: str,
                   product_words: list[str]) -> dict:
    """关键词提及漏斗。无主品牌名时只输出 Tier1（原版 aggregate.py:197-199）。"""
    tier1 = len(rows)
    brand = str(brand_name or "").strip()
    products = [str(word).strip() for word in product_words or [] if str(word).strip()]
    result: dict = {
        "method": KEYWORD_MENTION, "brand_name": brand or None,
        "product_keywords": products, "tier1": tier1,
        "tier2": None, "tier3": None, "product_in_tier1": None,
        "tier2_ratio": None, "tier3_in_tier2_ratio": None,
        "tier3_in_tier1_ratio": None, "degraded": False, "passive_only": 0,
    }
    if not brand:
        return result
    texts = [_active_text(row) for row in rows]
    tier2_idx = [i for i, text in enumerate(texts) if brand.lower() in text]
    tier2 = len(tier2_idx)
    result["tier2"] = tier2
    result["tier2_ratio"] = _ratio(tier2, tier1)
    result["degraded"] = bool(tier1) and tier2 / tier1 >= DEGRADE_RATIO
    result["passive_only"] = sum(
        1 for i, row in enumerate(rows)
        if brand.lower() not in texts[i] and brand.lower() in _passive_text(row))
    if products:
        tier3 = sum(1 for i in tier2_idx if _hit(texts[i], products))
        result["tier3"] = tier3
        result["tier3_in_tier2_ratio"] = _ratio(tier3, tier2)
        result["tier3_in_tier1_ratio"] = _ratio(tier3, tier1)
        result["product_in_tier1"] = sum(1 for text in texts if _hit(text, products))
    return result


def main_discussion_funnel(rows: list[dict]) -> dict:
    """AI 主讨论对象方法：三组互斥计数，不是递进漏斗，不做退化判断。"""
    counts = {label: 0 for _, label in MAIN_DISCUSSION_GROUPS}
    keys = dict(MAIN_DISCUSSION_GROUPS)
    for row in rows:
        label = keys.get(str(row.get("discussion_dimension") or "").strip())
        if label:
            counts[label] += 1
    return {"method": MAIN_DISCUSSION, "tier1": len(rows), "degraded": False,
            "groups": [{"label": label, "count": count,
                        "share": _ratio(count, len(rows))}
                       for label, count in counts.items()]}


def build(rows: list[dict], *, method: str, brand_name: str,
          product_words: list[str]) -> dict:
    if method == MAIN_DISCUSSION:
        return main_discussion_funnel(rows)
    return keyword_funnel(rows, brand_name, product_words)


def headline_metrics(funnel: dict) -> dict:
    """白名单指标：品牌提及率（Tier2/Tier1）、产品提及占比（Tier3/Tier2）。

    退化时不输出独立的品牌提及率（原版 aggregate.py:191-193 告警原文）；
    产品层改看活动全量中的产品提及，不再输出嵌套于 Tier2 的占比。
    """
    if funnel.get("method") != KEYWORD_MENTION or funnel.get("degraded"):
        return {"品牌提及率": None, "产品提及占比": None}
    return {"品牌提及率": funnel.get("tier2_ratio"),
            "产品提及占比": funnel.get("tier3_in_tier2_ratio")}


def chart_levels(funnel: dict) -> tuple[str, list[str], list[int]] | None:
    """图表层级 (chart_type, categories, values)；不应出图时返回 None。"""
    if funnel.get("method") == MAIN_DISCUSSION:
        groups = funnel.get("groups") or []
        return ("rank_bar", [g["label"] for g in groups], [g["count"] for g in groups])
    if funnel.get("tier2") is None:
        return None
    tier1 = int(funnel.get("tier1") or 0)
    if funnel.get("degraded"):
        if funnel.get("tier3") is None:
            return None
        return ("funnel", ["活动相关", "产品提及"],
                [tier1, int(funnel.get("product_in_tier1") or 0)])
    cats, vals = ["活动相关", "品牌提及"], [tier1, int(funnel["tier2"])]
    if funnel.get("tier3") is not None:
        cats.append("产品提及")
        vals.append(int(funnel["tier3"]))
    return ("funnel", cats, vals)


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "N/A"


def table_rows(funnel: dict) -> list[list[str]]:
    """漏斗数据表（层级 / 声量 / 占比），Tier3 行按客户固定写法写明分母。"""
    tier1 = int(funnel.get("tier1") or 0)
    if funnel.get("method") == MAIN_DISCUSSION:
        rows = [["活动相关全量", f"{tier1} 条", "100%"]]
        rows += [[g["label"], f"{g['count']} 条", _pct(g["share"])]
                 for g in funnel.get("groups") or []]
        return rows
    rows = [["活动总声量（Tier 1）", f"{tier1} 条", "100%"]]
    if funnel.get("tier2") is None:
        return rows
    if funnel.get("degraded"):
        if funnel.get("tier3") is not None:
            product = int(funnel.get("product_in_tier1") or 0)
            rows.append(["提及产品", f"{product} 条",
                         f"占活动总量 {_pct(_ratio(product, tier1))}"])
        return rows
    rows.append(["提及品牌（Tier 2）", f"{funnel['tier2']} 条",
                 f"{_pct(funnel.get('tier2_ratio'))}（品牌提及率）"])
    if funnel.get("tier3") is not None:
        rows.append(["提及产品（Tier 3）", f"{funnel['tier3']} 条",
                     f"品牌内容中的产品提及占比 {_pct(funnel.get('tier3_in_tier2_ratio'))}"
                     f"（Tier3 {funnel['tier3']} / Tier2 {funnel['tier2']}），"
                     f"占活动总量 {_pct(funnel.get('tier3_in_tier1_ratio'))}"])
    return rows


def method_note(funnel: dict) -> str:
    """表下一行方法说明（灰色斜体）。"""
    if funnel.get("method") == MAIN_DISCUSSION:
        return ("*{{muted}}方法：AI 主讨论对象。三组互斥、不构成递进漏斗；"
                "品牌主导讨论占比反映讨论焦点，不是品牌名称出现率。*")
    brand = funnel.get("brand_name")
    if not brand:
        return "*{{muted}}方法：关键词提及。未记录主品牌名，仅输出活动相关全量。*"
    note = (f"*{{{{muted}}}}方法：关键词提及（标题、正文、话题标签命中主品牌名「{brand}」，"
            "不区分大小写）；分母为活动相关全量。")
    if funnel.get("degraded"):
        note += ("品牌提及层占活动相关 ≥95%（检索词已含品牌名），该层无独立分析意义，"
                 "不输出品牌提及率。")
    passive = int(funnel.get("passive_only") or 0)
    if passive:
        note += f"另有 {passive} 条仅在图片/视频识别文本中出现品牌名（被动带出），未计入品牌提及。"
    return note + "*"
