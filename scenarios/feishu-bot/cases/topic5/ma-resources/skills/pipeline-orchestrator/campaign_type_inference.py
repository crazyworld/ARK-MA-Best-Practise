"""活动类型推断（FIX-10）。

唯一依据：客户 KS_Classification_Rules.md:194-213「信号矩阵」与「推断步骤」。
触发条件：需求确认单 campaign_type == "待数据推断"。

只做确定性推断（表驱动），不调用模型：
  - 强信号/佐证信号逐条可复核，basis 记录每条命中信号与实际数值。
  - 综合判断：≥2 个强信号 → 确认（高置信度）；1 个强信号 + ≥1 佐证 → 较可能；
    否则 → 待确认。
  - 无法从现有字段计算的信号（如「标题相似度」「现场图片描述」「粉丝覆盖面广」）
    不纳入，不伪造；basis 只写实际算出来的信号。
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

import platforms

PENDING = "待数据推断"

# 信号矩阵里的关键词（原文逐字）。
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
    """从活动明细行算出信号矩阵用到的原始数值（全部可复核）。"""
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


# 信号矩阵原文只写「内容含 X」，无最低比例要求：有任意命中即满足（ratio > 0）。
# 唯一的比例判断是「话题标签稀少」（原文如此），按 <20% 判。
SPARSE_RATIO = 0.20


def _share_label(value) -> str:
    return "活跃天数不足，不计" if value is None else f"{value:.0%}"


def _rules(s: dict) -> dict[str, dict[str, list[tuple[str, bool]]]]:
    """信号矩阵（KS_Classification_Rules.md:198-205），逐条对应原文。"""
    fans = s["fans_median"]
    t2, t3 = s["top2_days_share"], s["top3_days_share"]
    concentrated2 = t2 is not None and t2 >= 0.5
    concentrated3 = t3 is not None and t3 >= 0.5
    dispersed3 = t3 is not None and t3 < 0.5
    return {
        "新闻公关稿传播": {
            "strong": [
                (f"新闻/公众号占比 {s['news_wechat_ratio']:.0%}（≥50%）", s["news_wechat_ratio"] >= 0.5),
                (f"PGC 占比 {s['pgc_ratio']:.0%}（≥80%）", s["pgc_ratio"] >= 0.8),
            ],
            "support": [
                (f"话题标签占比 {s['hashtag_ratio']:.0%}（稀少）", s["hashtag_ratio"] < SPARSE_RATIO),
                (f"前 2 天声量占比 {_share_label(t2)}（发布高度集中）", concentrated2),
            ],
        },
        "KOL/KOC 种草": {
            "strong": [
                (f"PGC 占比 {s['pgc_ratio']:.0%}（≥40%）", s["pgc_ratio"] >= 0.4),
                (f"作者粉丝中位数 {fans}（≥1万）", fans is not None and fans >= 10_000),
                (f"含推荐/安利/测评 {s['kw_seeding_ratio']:.0%}", s["kw_seeding_ratio"] > 0),
            ],
            "support": [
                (f"小红书+抖音占比 {s['xhs_douyin_ratio']:.0%}（高）", s["xhs_douyin_ratio"] >= 0.5),
                (f"正面占比 {s['positive_ratio']:.0%}（≥60%）", s["positive_ratio"] >= 0.6),
            ],
        },
        "话题活动": {
            "strong": [
                (f"含话题标签 {s['hashtag_ratio']:.0%}", s["hashtag_ratio"] > 0),
                (f"UGC 占比 {s['ugc_ratio']:.0%}（≥60%）", s["ugc_ratio"] >= 0.6),
                (f"含打卡/参与/抽奖 {s['kw_topic_ratio']:.0%}", s["kw_topic_ratio"] > 0),
            ],
            "support": [
                (f"前 3 天声量占比 {_share_label(t3)}（有明显峰值）", concentrated3),
            ],
        },
        "直播活动": {
            "strong": [
                (f"含直播/主播/直播间 {s['kw_live_ratio']:.0%}", s["kw_live_ratio"] > 0),
                (f"抖音+微博占比 {s['douyin_weibo_ratio']:.0%}（集中）", s["douyin_weibo_ratio"] >= 0.5),
                (f"前 3 天声量占比 {_share_label(t3)}（集中在某几天）", concentrated3),
            ],
            "support": [
                (f"含链接/点击/下单 {s['kw_live_buy_ratio']:.0%}", s["kw_live_buy_ratio"] > 0),
            ],
        },
        "线下活动": {
            "strong": [
                (f"含打卡/线下/到店/展览/现场 {s['kw_offline_ratio']:.0%}", s["kw_offline_ratio"] > 0),
            ],
            "support": [
                (f"前 3 天声量占比 {_share_label(t3)}（发布分散）", dispersed3),
                (f"UGC 占比 {s['ugc_ratio']:.0%}（高）", s["ugc_ratio"] >= 0.6),
                (f"正面占比 {s['positive_ratio']:.0%}（偏正面）", s["positive_ratio"] >= 0.6),
            ],
        },
        "品牌联名": {
            "strong": [
                (f"同时出现两个及以上品牌 {s['multi_brand_ratio']:.0%}", s["multi_brand_ratio"] > 0),
                (f"含联名 {s['kw_collab_ratio']:.0%}", s["kw_collab_ratio"] > 0),
            ],
            "support": [],
        },
    }


def infer_campaign_type(rows: list[dict], brand_words: list[str] | None = None) -> dict:
    """按信号矩阵推断活动类型。返回 {value, confidence, basis, signals, candidates}。"""
    signals = compute_signals(rows, brand_words)
    if not signals["total"]:
        return {"value": PENDING, "confidence": CONFIDENCE_PENDING,
                "basis": ["无活动相关内容，无法推断"], "signals": signals,
                "candidates": []}
    candidates = []
    for name, groups in _rules(signals).items():
        strong = [label for label, ok in groups["strong"] if ok]
        support = [label for label, ok in groups["support"] if ok]
        if len(strong) >= 2:
            confidence = CONFIDENCE_HIGH
        elif len(strong) == 1 and support:
            confidence = CONFIDENCE_LIKELY
        else:
            confidence = CONFIDENCE_PENDING
        candidates.append({
            "type": name, "confidence": confidence,
            "strong": strong, "support": support,
        })
    rank = {CONFIDENCE_HIGH: 2, CONFIDENCE_LIKELY: 1, CONFIDENCE_PENDING: 0}
    # 同置信度按强信号数、佐证数排序；矩阵原文顺序作为最后的稳定序。
    order = {name: i for i, name in enumerate(_rules(signals))}
    candidates.sort(key=lambda c: (-rank[c["confidence"]], -len(c["strong"]),
                                   -len(c["support"]), order[c["type"]]))
    best = candidates[0]
    if best["confidence"] == CONFIDENCE_PENDING:
        return {"value": PENDING, "confidence": CONFIDENCE_PENDING,
                "basis": ["强信号不足，按规则标注待确认"],
                "signals": signals, "candidates": candidates}
    basis = [f"{label}（强信号）" for label in best["strong"]]
    basis += [f"{label}（佐证）" for label in best["support"]]
    return {"value": best["type"], "confidence": best["confidence"],
            "basis": basis, "signals": signals, "candidates": candidates}
