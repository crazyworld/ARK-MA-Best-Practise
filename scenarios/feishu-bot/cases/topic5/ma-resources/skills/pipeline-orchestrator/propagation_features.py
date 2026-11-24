"""传播特征参考（缺陷清单 Q1，KS_Classification_Rules v0.9）。

旧 6 类「活动类型推断矩阵」改为传播特征参考：只回答「内容通过什么方式传播」，
不判定、不填写 campaign_type。活动主类型沿用需求阶段与关键词 Skill 的确认结果；
仍为「待数据推断」时视为未解决需求项，回到需求阶段澄清（见 hc_bridge G1/G2）。

本模块只从同范围真实数据算可观察线索（平台、内容类型、话题标签、特征词出现率、发布集中度），
不设比例阈值、不按「强信号数量」确认任何类型、不给置信度；每条线索都标注待核实边界，
不能据此认定公关投放、商业合作、直播/线下/联名活动是否发生。
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

import platforms

PENDING = "待数据推断"

# 传播特征线索关键词（沿用旧6类原文词表，仅用于统计出现率）。
KW_SEEDING = ("推荐", "安利", "测评")
KW_TOPIC = ("打卡", "参与", "抽奖")
KW_LIVE = ("直播", "主播", "直播间")
KW_LIVE_BUY = ("链接", "点击", "下单")
KW_OFFLINE = ("打卡", "线下", "到店", "展览", "现场")
KW_COLLAB = ("联名",)

CONFIDENCE_HIGH = "高置信度"
CONFIDENCE_LIKELY = "较可能"
CONFIDENCE_PENDING = "待确认"


def _text(row: dict) -> str:
    parts = [str(row.get("title") or ""), str(row.get("content") or "")]
    kw = row.get("keywords") or []
    if isinstance(kw, list):
        parts.extend(str(x) for x in kw)
    return " ".join(parts)


def _ratio(n: int, total: int) -> float:
    return round(n / total, 4) if total else 0.0


def _has_any(text: str, words: Iterable[str]) -> bool:
    return any(word in text for word in words)


def compute_signals(rows: list[dict], brand_words: list[str] | None = None) -> dict:
    """从同范围活动明细行算出可观察线索的原始数值（全部可复核）。"""
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    total = len(rows)
    platform_counts = Counter(str(row.get("_platform") or "") for row in rows)
    modes = Counter(str(row.get("content_mode") or "").upper() for row in rows)
    texts = [_text(row) for row in rows]

    def share(pred) -> float:
        return _ratio(sum(1 for t in texts if pred(t)), total)

    fans = sorted(int(row.get("fans") or 0) for row in rows if row.get("fans"))
    fans_median = fans[len(fans) // 2] if fans else None

    days = Counter()
    for row in rows:
        ts = row.get("publish_time")
        if isinstance(ts, (int, float)) and ts > 0:
            days[int(ts // 86_400_000)] += 1
    # 活跃天数 ≤ k 时「前 k 天占比」恒为 100%，不能当集中/分散信号（记 None）。
    top3_days_share = (_ratio(sum(n for _, n in days.most_common(3)), total)
                       if len(days) > 3 else None)
    top2_days_share = (_ratio(sum(n for _, n in days.most_common(2)), total)
                       if len(days) > 2 else None)

    sentiments = [row.get("ai_sentiment", row.get("sentiment")) for row in rows]
    positive = sum(1 for v in sentiments if str(v) in ("1", "正", "正面", "positive"))

    brands = [str(w) for w in (brand_words or []) if str(w).strip()]
    multi_brand = (
        share(lambda t: sum(1 for b in brands if b in t) >= 2) if len(brands) >= 2 else 0.0
    )

    return {
        "total": total,
        "news_wechat_ratio": _ratio(
            platform_counts.get(platforms.to_slug("新闻"), 0)
            + platform_counts.get(platforms.to_slug("微信"), 0), total),
        "xhs_douyin_ratio": _ratio(
            platform_counts.get(platforms.to_slug("小红书"), 0)
            + platform_counts.get(platforms.to_slug("抖音"), 0), total),
        "douyin_weibo_ratio": _ratio(
            platform_counts.get(platforms.to_slug("抖音"), 0)
            + platform_counts.get(platforms.to_slug("微博"), 0), total),
        "pgc_ratio": _ratio(modes.get("PGC", 0), total),
        "ugc_ratio": _ratio(modes.get("UGC", 0), total),
        "fans_median": fans_median,
        "positive_ratio": _ratio(positive, total),
        "hashtag_ratio": share(lambda t: "#" in t),
        "kw_seeding_ratio": share(lambda t: _has_any(t, KW_SEEDING)),
        "kw_topic_ratio": share(lambda t: _has_any(t, KW_TOPIC)),
        "kw_live_ratio": share(lambda t: _has_any(t, KW_LIVE)),
        "kw_live_buy_ratio": share(lambda t: _has_any(t, KW_LIVE_BUY)),
        "kw_offline_ratio": share(lambda t: _has_any(t, KW_OFFLINE)),
        "kw_collab_ratio": share(lambda t: _has_any(t, KW_COLLAB)),
        "multi_brand_ratio": multi_brand,
        "top2_days_share": top2_days_share,
        "top3_days_share": top3_days_share,
        "active_days": len(days),
    }


# 旧 6 类 → 可观察线索与核验边界（KS_Classification_Rules v0.9「旧6类对应的传播特征参考」）。
FEATURES = (
    ("新闻公关稿传播", ("news_wechat_ratio",),
     "平台来源不等于媒体身份；未经原帖与发布者核实，不认定统一公关投放，不从PGC占比推断机构主导"),
    ("KOL/KOC种草", ("kw_seeding_ratio",),
     "不以粉丝数、PGC比例或正面情感确认商业合作；合作性质不明时只写「测评内容」「推荐内容」"),
    ("话题活动", ("hashtag_ratio", "kw_topic_ratio"),
     "同一话题出现或UGC占比高不证明存在互动活动；参与方式沿用动态UGC分类，不新增固定类别"),
    ("直播活动", ("kw_live_ratio",),
     "含「直播」「下单」等词不证明本次直播发生；直播是否为复盘对象须回需求阶段核实"),
    ("线下活动", ("kw_offline_ratio",),
     "普通到店或打卡不等于参加本次线下活动；现场内容须有实际读取或核实证据"),
    ("品牌联名", ("kw_collab_ratio", "multi_brand_ratio"),
     "两品牌共现或含「联名」不证明本次合作；不推断受众叠加或覆盖扩大"),
)
_LABELS = {
    "news_wechat_ratio": "新闻/公众号内容占比", "kw_seeding_ratio": "含推荐/安利/测评的内容占比",
    "hashtag_ratio": "含话题标签的内容占比", "kw_topic_ratio": "含打卡/参与/抽奖的内容占比",
    "kw_live_ratio": "含直播/主播/直播间的内容占比", "kw_offline_ratio": "含打卡/线下/到店/展览/现场的内容占比",
    "kw_collab_ratio": "含「联名」的内容占比", "multi_brand_ratio": "同时出现两个及以上品牌词的内容占比",
}


def propagation_features(rows: list[dict], brand_words: list[str] | None = None) -> dict:
    """同范围真实数据上的传播特征线索（不判定活动类型，可同时存在多项）。"""
    signals = compute_signals(rows, brand_words)
    if not signals["total"]:
        return {"status": "no_data", "features": [], "signals": signals}
    features = []
    for name, keys, boundary in FEATURES:
        observed = [f"{_LABELS[k]} {signals[k]:.0%}" for k in keys if signals.get(k)]
        if observed:
            features.append({"feature": name, "observed": observed, "boundary": boundary,
                             "status": "待核实"})
    return {"status": "available", "features": features, "signals": signals,
            "note": "传播特征仅辅助解释实际传播内容，不改变已确认的活动主类型，不扩展取数范围"}
