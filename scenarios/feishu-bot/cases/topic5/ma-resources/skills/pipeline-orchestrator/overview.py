"""传播概况结构结论与高点话术（FIX-47）：纯函数，确定性生成。

原版依据 KS_Writing_Standards「传播概况：结构结论与高点填空（1.1 / 2.N.1）」：
  - 平台声量占比分母=全部帖子；平台互动占比分母=全部互动；两个比例不混用。
  - 内容结构：PGC/UGC/未知，未知单列；主次不明显写「结构接近」。
  - 单平台、仅 UGC/PGC 用各自话术；互动不可得时只写可得结果，不用声量代替。
  - 高点：声量高点 / 单帖互动集中 / 多帖互动高点 / 两指标同日；
    槽位只填可验证数据，无证据的句子删除；最高值称「监测窗口峰值」。
"""
from __future__ import annotations

from interaction import interaction_value as _iv  # Q2 供应商互动量

import platforms

# 两项占比差距小于此值视为「接近」，不强写集中/主导（原版：数字接近时只说分布接近）。
CLOSE_GAP = 0.10
# 单帖占当天互动 ≥ 此比例 → 单帖互动集中；否则列 Top3 合计（多帖互动高点）。
SINGLE_POST_SHARE = 0.50
MULTI_POST_COUNT = 3
CONTENT_LABELS = {"UGC": "UGC", "PGC": "PGC", "UKN": "未知"}


def _name(slug: str) -> str:
    return {key: canon for canon, key in platforms.CANON.items()}.get(slug, slug)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _shares(values: dict) -> list[tuple[str, float]]:
    total = sum(v for v in values.values() if isinstance(v, (int, float)))
    if not total:
        return []
    return sorted(((k, v / total) for k, v in values.items() if isinstance(v, (int, float)) and v),
                  key=lambda kv: -kv[1])


def platform_sentence(volume: dict, interaction: dict | None, total_posts: int,
                      total_interaction: int | None) -> str | None:
    vol = _shares(volume or {})
    if not vol:
        return None
    if len(vol) == 1:
        tail = f"，{total_interaction} 次互动" if total_interaction is not None else ""
        # KS 传播概况单平台：「平台分布：本次仅监测{平台}，共XX条（100%）」，不出平台饼图。
        return f"平台分布：本次仅监测{_name(vol[0][0])}，共 {total_posts} 条（100%）{tail}。"
    top = vol[:2]
    text = f"{_name(top[0][0])}占声量的 {_pct(top[0][1])}，{_name(top[1][0])}占 {_pct(top[1][1])}"
    if top[0][1] - top[1][1] < CLOSE_GAP:
        text += "，平台声量分布接近"
    inter = _shares(interaction or {}) if interaction is not None else []
    if inter:
        lead = inter[0]
        if len(inter) > 1 and lead[1] - inter[1][1] < CLOSE_GAP:
            text += f"；互动在{_name(lead[0])}与{_name(inter[1][0])}之间分布接近"
        else:
            text += f"；互动量有 {_pct(lead[1])} 来自{_name(lead[0])}，互动主要集中在{_name(lead[0])}"
    elif interaction is None:
        text += "；监测数据未提供平台互动分布"
    return text + "。"


def content_sentence(content: dict, scope: str, total_posts: int) -> str | None:
    if scope in ("ugc", "pgc"):
        return f"本次仅分析{scope.upper()}内容，共 {total_posts} 帖。"
    known = {k: v for k, v in (content or {}).items() if k in ("UGC", "PGC") and v}
    total = sum(v for v in (content or {}).values() if isinstance(v, (int, float)))
    if not known or not total:
        return None
    ranked = sorted(known.items(), key=lambda kv: -kv[1])
    first = ranked[0]
    if len(ranked) == 1:
        text = f"讨论以{first[0]}为主，占 {_pct(first[1] / total)}"
    elif (first[1] - ranked[1][1]) / total < CLOSE_GAP:
        text = (f"{first[0]}与{ranked[1][0]}结构接近，分别占 {_pct(first[1] / total)} "
                f"和 {_pct(ranked[1][1] / total)}")
    else:
        text = (f"讨论以{first[0]}为主，占 {_pct(first[1] / total)}；"
                f"{ranked[1][0]}占 {_pct(ranked[1][1] / total)}")
    unknown = (content or {}).get("UKN") or 0
    if unknown:
        text += f"，另有 {_pct(unknown / total)}的内容类型未知"
    return text + "。"


def _peak(daily: dict) -> tuple[str, int, bool] | None:
    values = {d: v for d, v in (daily or {}).items() if isinstance(v, (int, float))}
    if not values:
        return None
    day = max(sorted(values), key=lambda d: values[d])
    return day, int(values[day]), True


def _date(day: str) -> str:
    return f"{int(day[4:6])}月{int(day[6:8])}日" if len(day) == 8 else day


def _post_label(post: dict) -> str:
    text = str(post.get("title") or post.get("content") or "").strip().replace("\n", " ")
    return f"「{text[:20]}」" if text else "一条内容"


def peak_sentences(daily_volume: dict, daily_interaction: dict, posts: list[dict],
                   *, content_by_day: dict | None = None) -> list[str]:
    """按情形选择高点话术；posts 需与日级数据同范围、同快照（含 _publish_ymd）。"""
    volume_peak, interaction_peak = _peak(daily_volume), _peak(daily_interaction)
    sentences: list[str] = []
    if volume_peak and interaction_peak and volume_peak[0] == interaction_peak[0]:
        sentences.append(f"{_date(volume_peak[0])}讨论量达到 {volume_peak[1]} 帖、互动量达到 "
                         f"{interaction_peak[1]} 次，两项指标均为监测窗口峰值。")
    elif volume_peak:
        line = f"{_date(volume_peak[0])}讨论量达到 {volume_peak[1]} 帖，为监测窗口峰值。"
        mix = (content_by_day or {}).get(volume_peak[0]) or {}
        lead = _shares(mix)
        if lead and volume_peak[1]:
            line = line[:-1] + f"，其中{CONTENT_LABELS.get(lead[0][0], lead[0][0])}占 {_pct(lead[0][1])}。"
        sentences.append(line)
    if interaction_peak and interaction_peak[1]:
        day, total = interaction_peak[0], interaction_peak[1]
        same_day = sorted((p for p in posts or [] if p.get("_publish_ymd") == day
                           and _iv(p) is not None),
                          key=lambda p: -(_iv(p) or 0))
        if same_day:
            top = same_day[0]
            share = (_iv(top) or 0) / total
            prefix = "" if volume_peak and volume_peak[0] == day else f"{_date(day)}互动量达到 {total} 次，"
            if share >= SINGLE_POST_SHARE:
                sentences.append(f"{prefix}其中{_post_label(top)}获得 {(_iv(top) or 0)} 次互动，"
                                 f"占当天 {_pct(share)}，当天互动主要集中在这条内容上。")
            else:
                group = same_day[:MULTI_POST_COUNT]
                values = "、".join(str((_iv(p) or 0)) for p in group)
                combined = sum((_iv(p) or 0) for p in group) / total
                sentences.append(f"{prefix}{'、'.join(_post_label(p) for p in group)}分别获得 {values} 次互动，"
                                 f"合计占当天 {_pct(combined)}。")
        elif not (volume_peak and volume_peak[0] == day):
            sentences.append(f"{_date(day)}互动量达到 {total} 次，为监测窗口峰值。")
    return sentences


def campaign_overview(rows: list[dict], scope: str) -> dict:
    """活动层（2.N.1）：活动相关行同范围计算。"""
    volume: dict[str, int] = {}
    interaction: dict[str, int] = {}
    content: dict[str, int] = {}
    daily_volume: dict[str, int] = {}
    daily_interaction: dict[str, int] = {}
    content_by_day: dict[str, dict[str, int]] = {}
    for row in rows:
        slug = row.get("_platform") or "unknown"
        value = (_iv(row) or 0)
        mode = str(row.get("content_mode") or "").upper()
        mode = mode if mode in ("UGC", "PGC") else "UKN"
        volume[slug] = volume.get(slug, 0) + 1
        interaction[slug] = interaction.get(slug, 0) + value
        content[mode] = content.get(mode, 0) + 1
        day = row.get("_publish_ymd")
        if day:
            daily_volume[day] = daily_volume.get(day, 0) + 1
            daily_interaction[day] = daily_interaction.get(day, 0) + value
            bucket = content_by_day.setdefault(day, {})
            bucket[mode] = bucket.get(mode, 0) + 1
    total_interaction = sum(interaction.values())
    sentences = [s for s in (
        platform_sentence(volume, interaction if total_interaction else {}, len(rows), total_interaction),
        content_sentence(content, scope, len(rows)),
    ) if s]
    return {
        "platform_volume_share": dict(_shares(volume)),
        "platform_interaction_share": dict(_shares(interaction)) if total_interaction else None,
        "content_type_share": dict(_shares(content)),
        "structure_sentences": sentences,
        "peak_sentences": peak_sentences(daily_volume, daily_interaction, rows,
                                         content_by_day=content_by_day),
    }


def brand_overview(brand_period: dict, daily_totals, scope: str, publish_ymd) -> dict:
    """品牌层（1.1）：SSJH 聚合。平台互动取 interaction_trend 的平台分项（无则不可得）。"""
    data_type = {k: v for k, v in (brand_period.get("data_type") or {}).items()
                 if isinstance(v, (int, float))}
    by_platform = {k: v for k, v in (brand_period.get("interaction_trend") or {}).items()
                   if k != "_total" and isinstance(v, dict)}
    interaction = ({k: sum(daily_totals(v).values()) for k, v in by_platform.items()}
                   if by_platform else None)
    content = ((brand_period.get("volume_type") or {}).get("_total") or {})
    total_posts = int(brand_period.get("total_volume") or sum(data_type.values()))
    sentences = [s for s in (
        platform_sentence(data_type, interaction, total_posts, brand_period.get("total_interaction")),
        content_sentence(content, scope, total_posts),
    ) if s]
    # 品牌热帖是 SSJH Top 帖（非全量）：分子为该帖真实互动、分母为当天全量互动，同一快照。
    posts = []
    for platform_rows in (brand_period.get("articles") or {}).values():
        for row in platform_rows or []:
            posts.append({**row, "_publish_ymd": publish_ymd(row)})
    return {
        "structure_sentences": sentences,
        "peak_sentences": peak_sentences(
            daily_totals(brand_period.get("volume_trend") or {}),
            daily_totals(brand_period.get("interaction_trend") or {}), posts),
    }


def overview_text(overview: dict) -> str | None:
    lines = list((overview or {}).get("structure_sentences") or []) + \
        list((overview or {}).get("peak_sentences") or [])
    return "".join(lines) if lines else None
