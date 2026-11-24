"""Deterministic evidence and required report modules; no model or external calls."""
from __future__ import annotations

import json
import re
from collections import defaultdict

import hot_formula
from interaction import interaction_value  # noqa: F401  (Q2，供 handlers 复用)
import mention_funnel
import overview
import platforms
import validator

# 平台展示名唯一事实源见 platforms.py（FIX-17）：slug → 标准中文名（含 other→其他）。
PLATFORMS = {**{key: canon for canon, key in platforms.CANON.items()},
             platforms.OTHER_SLUG: platforms.OTHER}
# FIX-22：热门表字段与截断长度的唯一事实源在 topic5-validator/scripts/policy.py。
_POLICY = validator.load_policy()
PGC_COLUMNS = list(_POLICY.HOT_PGC_COLUMNS)
UGC_COLUMNS = list(_POLICY.HOT_UGC_COLUMNS)
HOT_EXCERPT_CHARS = int(_POLICY.HOT_EXCERPT_CHARS)
# 观点代表原文 ≤100 字（原版 content_stats.clean 默认 n=100）。
QUOTE_CHARS = 100
# 观点 TOP5 的方向样本门槛（原版 content_stats --min-direction 10）。
OPINION_MIN_DIRECTION = 10
# 观点标签拆分符（原版 content_stats.SPLIT）与无观点占位（NO_OPINION）。
OPINION_SPLIT = re.compile(r"[、/|;；,，]")
NO_OPINION = ("", "无", "N/A", "None", "标注跳过_无内容")
OTHER_LABEL = "其他"
# 「其他」占比超过 20% 提示类别可能不完整（原版 content_stats:94-95）。
OTHER_WARN_RATIO = 0.20
# 热帖配方解构输入的正文截断（FIX_PLAN FIX-23 第 6 点：正文前 200 字）。
FORMULA_CONTENT_CHARS = 200


def excerpt(row, limit=QUOTE_CHARS, *, ellipsis=False):
    text = re.sub(r"\s+", " ", str(row.get("content") or row.get("title") or "")).strip()
    if ellipsis and len(text) > limit:
        return text[:limit] + "…"
    return text[:limit]


def hot_excerpt(row):
    return excerpt(row, HOT_EXCERPT_CHARS, ellipsis=True)


def fmt_pct(value, *, digits: int = 1, signed: bool = True) -> str:
    """比例值 → 百分比文案（客户要求 NSR/变化率以百分比呈现，带正负号）。

    None / 非数值 → "N/A"。0.2537 → "+25.4%"；-0.042 → "-4.2%"。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    text = f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"
    return text


def public_row(row):
    """Public evidence only. Personal UGC account metadata never enters report input."""
    pgc = str(row.get("content_mode") or "").upper() == "PGC"
    result = {k: row.get(k) for k in ("id", "url", "title", "content_mode", "interaction_cnt")}
    result["content"] = excerpt(row)
    result["site_name"] = row.get("site_name") or PLATFORMS.get(row.get("_platform"), row.get("_platform"))
    if pgc:
        result["author"] = row.get("author") or row.get("nickname") or row.get("account_name")
    return result


# Q2：供应商互动量的统一说明（数据说明与写作提示词共用，单一事实源）。
INTERACTION_NOTE = ("总互动量直接采用数说聚合返回的互动量字段，统计定义沿用供应商口径，"
                    "不根据互动明细字段重新计算；供应商未返回时标为不可得，不补零或估算。")


# 缺陷清单 Q5：央媒/党媒等权威媒体报道的表达边界（写作与质检共用）。
AUTHORITY_MEDIA_RULE = (
    "涉及央媒、党媒及其他权威媒体报道时，声量和互动量仍沿用数说聚合口径；根据可核实的发布主体、"
    "报道内容及来源关系补充说明报道情况，数据支持时才统计报道帖数、可识别转载帖数及转载媒体数量，"
    "并注明监测范围、识别和去重规则。相似内容不直接认定为转载，未监测到的传播不作估算。"
    "权威及背书意义结合具体报道作定性解释，不折算为互动量、曝光量或综合价值分数；"
    "缺少身份或转载证据时明确写该项不可量化。")


def _or_unavailable(value):
    return "不可得" if value is None else value


def interaction_stats(rows) -> dict:
    """总量与条均只按「有互动量」的帖子计算；缺失条数单独披露。"""
    values = [v for v in (interaction_value(r) for r in rows) if v is not None]
    return {"total": sum(values) if values else None, "available": len(values),
            "missing": len(rows) - len(values),
            "avg": round(sum(values) / len(values), 2) if values else None}


def _rank(rows):
    """互动量 Top：只排有供应商互动量的帖子，缺失的不进榜（不按发布时间冒充 Top）。"""
    ranked = [r for r in rows if interaction_value(r) is not None]
    return sorted(ranked, key=lambda r: (-interaction_value(r),
                                         str(r.get("id") or r.get("url") or excerpt(r))))


def top_content(rows):
    return {kind.lower(): [public_row(r) for r in _rank(rows)
                          if str(r.get("content_mode") or "").upper() == kind][:10]
            for kind in ("PGC", "UGC")}


def opinion_labels(raw) -> list[str]:
    """一帖多标签按原版分隔符拆开、去重（content_stats.py:83-85）。"""
    text = str(raw or "").strip()
    if text in NO_OPINION:
        return []
    return list(dict.fromkeys(part.strip() for part in OPINION_SPLIT.split(text)
                              if part.strip() and part.strip() not in NO_OPINION))


def _interaction_sum(rows):
    return sum(interaction_value(r) or 0 for r in rows)


def opinion_analysis(rows, sentiment):
    """正/负面观点 TOP5（原版 content_stats.opinion_stats）。

    - 占比 = 命中该类别的帖子数 ÷ 该方向活动相关帖子总数；一帖多标签分别计入，
      占比之和可能超过 100%。
    - 「其他」单列，不参与排名；并列按该类帖子总互动量降序。
    - 方向样本 < 10 条不做观点提取；代表原文为互动最高的 ≤3 条、按前 30 字去重。
    """
    result = {}
    for name, value in (("positive", 1), ("negative", -1)):
        subset = [r for r in rows if sentiment(r) == value]
        n = len(subset)
        item = {"sample_size": n, "labelled_count": 0, "top5": [], "other": None,
                "extra_categories": 0, "other_warning": False,
                "status": "available" if n >= OPINION_MIN_DIRECTION else "insufficient_sample"}
        result[name] = item
        if n < OPINION_MIN_DIRECTION:
            continue
        groups = defaultdict(list)
        for row in subset:
            for label in opinion_labels(row.get("opinion")):
                groups[label].append(row)
        item["labelled_count"] = sum(1 for r in subset if opinion_labels(r.get("opinion")))
        ranked = sorted(((k, v) for k, v in groups.items() if k != OTHER_LABEL),
                        key=lambda kv: (-len(kv[1]), -_interaction_sum(kv[1]), kv[0]))
        item["top5"] = [{"label": label, "count": len(group), "share": len(group) / n,
                         "representatives": _representatives(group, 3)}
                        for label, group in ranked[:5]]
        item["extra_categories"] = max(len(ranked) - 5, 0)
        other = groups.get(OTHER_LABEL) or []
        if other:
            item["other"] = {"count": len(other), "share": len(other) / n}
            item["other_warning"] = len(other) / n > OTHER_WARN_RATIO
    return result


def _representatives(group, limit):
    """互动最高的 ≤limit 条，按前 30 字去重（原版 content_stats.py:89-93）。"""
    picked, seen = [], set()
    for row in _rank(group):
        key = excerpt(row)[:30]
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append(public_row(row))
        if len(picked) == limit:
            break
    return picked


def ugc_analysis(rows, relevance=None):
    """UGC 参与方式占比：分母为活动相关 UGC（原版 aggregate.py:146-147）。

    relevance(row) 返回 True/False/None；传入时只保留判为相关的行。
    aggregate 传入的已是活动相关行，这里仍过滤一次作为口径断言。
    """
    subset = [r for r in rows if str(r.get("content_mode") or "").upper() == "UGC"
              and (relevance is None or relevance(r) is not False)]
    groups = defaultdict(list)
    for row in subset:
        label = str(row.get("ugc_category") or "").strip()
        if label and label != "N/A":
            groups[label].append(row)
    unlabeled = len(subset) - sum(map(len, groups.values()))
    return {"sample_size": len(subset), "unlabeled": unlabeled,
            "categories": [{"label": label, "count": len(group), "share": len(group) / len(subset),
                            "representatives": _representatives(group, 2)}
                           for label, group in sorted(groups.items(),
                                                      key=lambda item: (-len(item[1]), item[0]))]}


def kol_unknown_note(bundle: dict) -> str | None:
    """粉丝数缺失（kol_level=未知）的占比说明；不并入头部/腰部/尾部。"""
    notes = (bundle or {}).get("kol_notes") or {}
    share = notes.get("unknown_share")
    if not share:
        return None
    count = ((bundle or {}).get("kol_level_dist") or {}).get("unknown", 0)
    return (f"*{{{{muted}}}}另有 {count} 条内容粉丝数缺失（达人层级未知，占活动相关内容 {share:.1%}），"
            "单独计数，不纳入层级对比。*")


def formula_row(row):
    """热帖配方解构输入（FIX-23 第 6 点）：公开字段 + 正文前 200 字。"""
    return {"platform": PLATFORMS.get(row.get("_platform"), row.get("_platform")),
            "title": row.get("title") or "",
            "content": excerpt(row, FORMULA_CONTENT_CHARS),
            "interaction_cnt": interaction_value(row),
            "url": row.get("url") or ""}


def cell(value):
    return str(value if value is not None else "").replace("\\", "\\\\").replace("|", r"\|").replace("\n", " ").replace("\r", " ")


def table(columns, rows):
    return "\n".join(["| " + " | ".join(columns) + " |",
                      "| " + " | ".join(["---"] * len(columns)) + " |"]
                     + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows])


# FIX-03：content_scope 决定只输出哪一张热门表。
SCOPE_KINDS = {"all": ("pgc", "ugc"), "ugc": ("ugc",), "pgc": ("pgc",)}
# 单类型时客户要求展示 Top20（双表时各 Top10）。
SINGLE_SCOPE_TOP_N = 20


def hot_tables(bundle, scope="", content_scope="all"):
    """按 content_scope 输出热门内容表（FIX-03）。

    all → PGC Top10 + UGC Top10；ugc/pgc → 仅对应一张 Top20。
    非 all 时表标题带说明，不再并列两张表。
    """
    mode = str(content_scope or "all").lower()
    kinds = SCOPE_KINDS.get(mode, SCOPE_KINDS["all"])
    limit = SINGLE_SCOPE_TOP_N if len(kinds) == 1 else 10
    parts = []
    for kind in kinds:
        columns = PGC_COLUMNS if kind == "pgc" else UGC_COLUMNS
        rows = (bundle.get("top_content") or {}).get(kind) or []
        parts.append(
            f"**{scope + ' ' if scope else ''}{kind.upper()} 热门内容 Top{limit}**"
        )
        if not rows:
            parts.append(f"本次可用数据中无 {kind.upper()} 内容。")
            continue
        values = []
        for rank, row in enumerate(rows[:limit], 1):
            common = [rank, hot_excerpt(row), row.get("site_name"), row.get("url"), row.get("interaction_cnt")]
            if kind == "pgc":
                common = [rank, row.get("title"), hot_excerpt(row), row.get("content_mode") or "PGC",
                          row.get("site_name"), row.get("author"), row.get("url"), row.get("interaction_cnt")]
            values.append(common)
        parts.append(table(columns, values))
    return "\n\n".join(parts)


# 观点双栏标记（FIX-50）：feishu.layout_blocks 识别后生成 50:50 grid。
GRID_START = "<!-- grid:50:50 -->"
GRID_SPLIT = "<!-- grid:column -->"
GRID_END = "<!-- grid:end -->"
# UGC 代表原文 40-60 字（KS_Report_Template 2.N.4②）；单类 <10 条注明样本偏少。
UGC_QUOTE_MIN, UGC_QUOTE_MAX = 40, 60
UGC_SMALL_CATEGORY = 10
UGC_MIN_POSTS = 10


def _direction_block(item: dict, title: str, mark: str) -> list[str]:
    sample = item.get("sample_size", 0)
    parts = [f"**{mark} {title}观点 TOP5**"]
    if sample < OPINION_MIN_DIRECTION:
        parts.append(f"*{{{{muted}}}}{title}内容仅 {sample} 条，不做观点提取。*")
        return parts
    if not item.get("top5"):
        parts.append(f"*{{{{muted}}}}{title}内容中未提取到有效观点标签。*")
    for topic in item.get("top5") or []:
        parts.append(f"**{topic['label']}**  {topic['share']:.1%}（{topic['count']} 条）")
        for row in topic.get("representatives") or []:
            parts.append(f"「{excerpt(row)}」 [原文]({row.get('url') or ''})")
    other = item.get("other")
    if other:
        line = f"*{{{{muted}}}}其他（未单列）：{other['count']} 条（{other['share']:.1%}）"
        if item.get("other_warning"):
            line += "；占比超过 20%，观点类别可能不完整"
        parts.append(line + "*")
    if item.get("extra_categories"):
        parts.append(f"*{{{{muted}}}}{title}方向另有 {item['extra_categories']} 个类别未入 TOP5。*")
    return parts


def opinion_text(bundle):
    """2.N.3 正/负面观点 TOP5：双栏，左正右负，每观点 2-3 条原文（KS 2.N.3，禁用宽表）。"""
    analysis = bundle.get("opinion_analysis") or {}
    left = _direction_block(analysis.get("positive") or {}, "正面", "✅")
    right = _direction_block(analysis.get("negative") or {}, "负面", "❌")
    note = ("*{{muted}}占比=命中该类别的帖子数÷该方向活动相关帖子总数，"
            "一条内容可同时计入多个类别，占比之和可能超过 100%。*")
    return "\n\n".join([GRID_START, *left, GRID_SPLIT, *right, GRID_END, note])


def ugc_quote(row) -> str:
    """UGC 代表原文 40-60 字：≤60 取全文；超出时在 40-60 字内的句末截断，没有句末则截 60 字。"""
    text = re.sub(r"\s+", " ", str(row.get("content") or row.get("title") or "")).strip()
    if len(text) <= UGC_QUOTE_MAX:
        return text
    window = text[:UGC_QUOTE_MAX]
    cut = max((window.rfind(mark) for mark in "。！？!?；;"), default=-1)
    return window[:cut + 1] if cut + 1 >= UGC_QUOTE_MIN else window + "…"


def ugc_text(bundle, content_scope="all"):
    """2.N.4② UGC 参与方式（FIX-50）：不适用时保留标题、固定措辞说明原因；不出数据表。"""
    title = "**② UGC 参与方式分布**"
    if str(content_scope or "all").lower() == "pgc":
        return f"{title}\n\n*{{{{muted}}}}本次仅分析 PGC 内容，不适用 UGC 参与方式分析。*"
    item = bundle.get("ugc_analysis") or {}
    sample = item.get("sample_size", 0)
    if sample == 0:
        return f"{title}\n\n*{{{{muted}}}}本活动未采集到活动相关 UGC，暂不做参与方式分类。*"
    if sample < UGC_MIN_POSTS:
        return (f"{title}\n\n*{{{{muted}}}}活动相关 UGC 仅 {sample} 条，样本不足，"
                "暂不做参与方式分类。*")
    parts = [title, f"*{{{{muted}}}}占比以活动相关 UGC（{sample} 条）为分母。*"]
    for category in item.get("categories") or []:
        line = f"**{category['label']}**  {category['share']:.1%}（{category['count']} 条）"
        if category["count"] < UGC_SMALL_CATEGORY:
            line += "，样本偏少，仅供参考"
        parts.append(line)
        for row in category.get("representatives") or []:
            parts.append(f"「{ugc_quote(row)}」 [原文]({row.get('url') or ''})")
    return "\n\n".join(parts)


WORDCLOUD_OBJECT_TITLES = {"overall": "活动整体", "brand": "活动中的品牌提及", "product": "活动中的产品提及"}


def _wordcloud_heading(prefix: str, dimension: str | None, platform: str) -> str:
    """词云右栏标题（FIX-54）：品牌 1.3 按平台；活动 2.N.4③ 按分析对象。"""
    if dimension:
        return f"{WORDCLOUD_OBJECT_TITLES.get(dimension, dimension)}核心议题"
    return f"{PLATFORMS.get(platform, platform)}核心议题解读"


def sponsor_text(bundle: dict) -> str | None:
    """赞助型活动 2.N.2 附加表（FIX-24，KS_Classification_Rules 2.N.2 计算定义）。"""
    share = (bundle or {}).get("sponsor_share")
    if not share:
        return None
    if share.get("status") != "available":
        return (f"*{{{{muted}}}}赞助型活动品牌/产品声量占比暂不输出（{share.get('reason')}）。*")
    a = share["activity_volume"]
    rows = [["活动总声量（A）", f"{a} 条", "100%"]]
    if share.get("brand_volume") is not None:
        rows.append(["品牌声量（B）", f"{share['brand_volume']} 条",
                     f"{share['brand_share'] * 100:.1f}%（品牌声量占比 B÷A）"])
    if share.get("product_volume") is not None:
        rows.append(["产品声量（C）", f"{share['product_volume']} 条",
                     f"{share['product_share'] * 100:.1f}%（产品声量占比 C÷A）"])
    return ("**赞助型活动品牌/产品声量占比**\n\n" + table(["指标", "声量", "占比"], rows)
            + "\n\n*{{muted}}A/B/C 为同一时间与平台范围的三次监测查询（活动词、活动词+品牌词、"
              "活动词+产品词）；B 与 C 可重叠。*")


def core_results_table(stats: dict) -> str:
    """四、总结与建议 · 本次活动核心成果（FIX-44，KS §四：数据表汇总各活动关键指标）。

    数值由代码从 stats_bundle 生成；日均拉升为品牌整体口径（2.{N+1}），各活动同值。
    """
    lift = (stats or {}).get("baseline_lift") or {}
    lift_text = (fmt_pct(lift.get("daily_volume_change_pct") / 100)
                 if lift.get("status") == "available"
                 and isinstance(lift.get("daily_volume_change_pct"), (int, float)) else "不可用")
    rows = []
    for cid, bundle in ((stats or {}).get("campaigns") or {}).items():
        metrics = (bundle or {}).get("metrics") or {}
        rows.append([cid, metrics.get("活动相关条数"), _or_unavailable(metrics.get("互动量")),
                     _or_unavailable(metrics.get("条均互动量")), fmt_pct(metrics.get("NSR"))])
    text = "**本次活动核心成果**\n\n" + table(
        ["活动", "活动相关声量", "互动量", "条均互动量", "NSR"], rows)
    return text + f"\n\n*{{{{muted}}}}品牌日均声量较基准期变化（品牌整体口径，见拉升效果节）：{lift_text}*"


def scope_note(bundle: dict) -> str | None:
    """2.N.1 节首口径注（FIX-21，KS_Report_Template:187 原文）。

    数字取自 stats_scope；剔除数=原始−保留。没有范围记录时不输出。
    """
    scope = (bundle or {}).get("stats_scope") or {}
    if not scope or scope.get("raw") is None:
        return None
    note = (f"*{{{{muted}}}}注：本节统计已剔除与活动无关的内容（监测检索原始 {scope['raw']} 条，"
            f"剔除 {scope['excluded']} 条，保留 {scope['kept']} 条）。*")
    ads = (bundle or {}).get("natural_vs_ad") or {}
    if ads.get("ad"):
        note += (f"\n\n*{{{{muted}}}}其中带广告/推广标识 {ads['ad']} 条，"
                 f"自然声量 {ads.get('natural', 0)} 条"
                 + (f"，未标识 {ads['unknown']} 条" if ads.get("unknown") else "") + "。*")
    if scope.get("irrelevant_review_required"):
        note += (f"\n\n*{{{{muted}}}}AI 判为无关的内容占 {scope['ai_irrelevant_ratio']:.1%}，"
                 "超过 10%，已提示复核被判无关的内容。*")
    return note


def key_numbers(stats: dict) -> list[dict]:
    """出口门禁关键数字（FIX-40）：与正文同一格式化函数生成，回读全文逐一查找。

    活动相关声量、正/中/负占比、NSR（各活动）；品牌日均声量变化率（可用时）。
    """
    out = []
    for cid, bundle in ((stats or {}).get("campaigns") or {}).items():
        metrics = (bundle or {}).get("metrics") or {}
        if isinstance(metrics.get("活动相关条数"), int):
            out.append({"label": f"{cid}·活动相关声量", "value_text": str(metrics["活动相关条数"])})
        sd = (bundle or {}).get("sentiment_dist") or {}
        total = sum(v for v in sd.values() if isinstance(v, int))
        if total:
            for key, name in (("positive", "正面"), ("neutral", "中性"), ("negative", "负面")):
                out.append({"label": f"{cid}·{name}占比", "value_text": f"{sd.get(key, 0) / total:.1%}"})
        nsr = fmt_pct(metrics.get("NSR"))
        if nsr != "N/A":
            out.append({"label": f"{cid}·NSR", "value_text": nsr})
    lift = (stats or {}).get("baseline_lift") or {}
    if lift.get("status") == "available" and isinstance(lift.get("daily_volume_change_pct"), (int, float)):
        out.append({"label": "品牌日均声量变化", "value_text": fmt_pct(lift["daily_volume_change_pct"] / 100)})
    return out


_NUMBER = re.compile(r"[+\-]?\d[\d,]*(?:\.\d+)?%?")
_SECTION = re.compile(r"(?m)^#{2,4}\s+(\S+)")


def _section_numbers(md: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = "篇首"
    for line in (md or "").splitlines():
        heading = _SECTION.match(line)
        if heading:
            current = heading.group(1)
            continue
        if "report-module" in line or line.startswith("!["):
            continue
        out.setdefault(current, []).extend(_NUMBER.findall(line))
    return out


def _checkable(value: str) -> bool:
    """只核百分比与 ≥100 的非年份整数；序号、天数、Top N、日期等小数字不核（避免误判编造）。"""
    if value.endswith("%"):
        return True
    bare = value.lstrip("+-").replace(",", "")
    if "." in bare or not bare.isdigit():
        return False
    number = int(bare)
    return number >= 100 and not 1900 <= number <= 2100


def _stats_values(stats) -> set[str]:
    """stats 中所有数值的可能文案（原值、千分位、百分比 1 位、带符号百分比）。"""
    values: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            counts = [v for v in node.values() if isinstance(v, int) and not isinstance(v, bool)]
            total = sum(counts)
            if total and len(counts) >= 2:
                values.update(f"{v / total:.1%}" for v in counts)     # 分布占比（情感/平台/内容）
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            values.update({str(node), f"{node:,}", f"{node:g}"})
            if isinstance(node, float):
                values.update({f"{node:.1f}", f"{node:.2f}", f"{node * 100:.1f}%", fmt_pct(node),
                               f"{node:.1f}%", f"{node:+.1f}%"})
    walk(stats)
    return values


def number_diff(old_md: str, new_md: str, stats: dict, charts: dict) -> dict:
    """返修数字核查（FIX-39）：被改动的数字须能在 stats 找到；同节配图是否含该值逐条记录。"""
    old_sections, new_sections = _section_numbers(old_md), _section_numbers(new_md)
    known = _stats_values(stats)
    chart_items = (charts or {}).get("charts") or []
    records, unknown = [], []
    for section, numbers in new_sections.items():
        before = list(old_sections.get(section) or [])
        changed = [n for n in numbers if n not in before and _checkable(n)]
        if not changed:
            continue
        section_charts = [c for c in chart_items if section in str(c.get("title") or "")
                          or section.startswith(str((c.get("spec") or {}).get("section") or "\0"))]
        for value in changed:
            bare = value.lstrip("+").replace(",", "")
            in_stats = value in known or bare in known or value.replace(",", "") in known
            chart_has = any(bare.rstrip("%") in json.dumps(c.get("spec") or {}, ensure_ascii=False)
                            for c in section_charts)
            records.append({"section": section, "new": value,
                            "chart_ids_in_section": [c.get("chart_id") for c in section_charts],
                            "chart_contains_value": chart_has,
                            "note": "" if chart_has else "已核实配图不含该数字", "in_stats": in_stats})
            if not in_stats:
                unknown.append(f"{section}:{value}")
    return {"records": records, "unknown": unknown}


WAIVER_PREFIX = "本报告以下检查项经用户确认放宽："


def waiver_note(md: str, waivers) -> str:
    """HC7 放宽留痕（FIX-35）：插在「### 数据说明」区块之后；无数据说明时放篇首。幂等。"""
    items = [w for w in waivers or [] if isinstance(w, dict) and w.get("issue_id")]
    if not items or WAIVER_PREFIX in (md or ""):
        return md
    text = ("*{{muted}}" + WAIVER_PREFIX
            + "；".join(f"{w.get('title') or w['issue_id']}（理由：{w.get('reason')}）" for w in items) + "。*")
    match = re.search(r"(?m)^" + re.escape(DESCRIPTION_HEADING) + r"\s*$", md or "")
    if not match:
        return text + "\n\n" + (md or "")
    following = re.search(r"(?m)^#{1,3}\s", md[match.end():])
    cut = match.end() + following.start() if following else len(md)
    return md[:cut].rstrip() + "\n\n" + text + "\n\n" + md[cut:]


def data_exception_note(stats: dict) -> str | None:
    """数据异常披露（FIX-58，KS_Exception_Handling）：样本量有限、仅统计声量、日期异常排除。"""
    profile = (((stats or {}).get("clean_record") or {}).get("profile") or {})
    notes = [n for n in profile.get("notes") or [] if not n.startswith("已读取 Sheet")]
    # Q2：部分帖子供应商未返回互动量 → 披露条数（不补零，不进互动 Top 与条均分母）。
    for cid, bundle in ((stats or {}).get("campaigns") or {}).items():
        missing = (bundle or {}).get("interaction_missing")
        if missing:
            notes.append(f"{cid} 有 {missing} 条内容供应商未返回互动量，标为不可得，"
                         "不计入互动量合计、条均互动量及互动量排行")
    if not notes:
        return None
    return "*{{muted}}数据说明补充：" + "；".join(notes) + "。*"


def _voice_share_line(bundle: dict) -> str:
    """2.N.2 漏斗下方固定一行：声量贡献参考（FIX-19）。

    原版 KS_Metrics_Calculation v0.15：分母<分子时不输出该指标，
    不得输出 > 100% 的数值；不输出时要说明原因，不能留空让读者以为漏了。
    """
    vs = (bundle or {}).get("voice_share") or {}
    value, num, den = vs.get("value"), vs.get("numerator"), vs.get("denominator")
    if value is None:
        reason = vs.get("reason") or "数据不足"
        return f"\n\n*{{{{muted}}}}声量贡献参考：暂不输出（{reason}）*"
    return (f"\n\n声量贡献参考：{fmt_pct(value)}"
            f"（活动相关 {num} / 品牌同期 {den}）")


def _headings(markdown):
    """Read headings and numbered bold titles with their semantic hierarchy."""
    pattern = r"(?m)^(?:(#{1,6})\s+([^\n]+)|\*\*(\d+(?:\.\d+)*\s+[^\n]+?)\*\*\s*)$"
    result = []
    for match in re.finditer(pattern, markdown):
        title = (match.group(2) or match.group(3)).strip().strip("*").strip()
        number = re.match(r"^(\d+(?:\.\d+)*)(?=\s|$)", title)
        level = number.group(1).count(".") + 2 if number else len(match.group(1))
        result.append((match.start(), match.end(), level, title))
    return result


def section(markdown, prefix):
    headings = _headings(markdown)
    for index, (start, end, level, title) in enumerate(headings):
        if not re.match(re.escape(prefix) + r"(?=[^\d.]|$)", title):
            continue
        stop = next((item[0] for item in headings[index + 1:] if item[2] <= level),
                    len(markdown))
        return start, end, stop
    return None


def narrative_heading_keys(markdown):
    """Ignore generated evidence modules; numbered sections have stable IDs."""
    text = re.sub(r"<!-- report-module:start -->.*?<!-- report-module:end -->",
                  "", markdown, flags=re.S)
    keys = set()
    for _, _, level, title in _headings(text):
        if level < 2:
            continue
        number = re.match(r"^(\d+(?:\.\d+)*)(?=\s|$)", title)
        keys.add("section:" + number.group(1) if number else
                 "title:" + re.sub(r"[\s*]", "", title))
    return keys


def assert_preserved_structure(previous, revised):
    missing = narrative_heading_keys(previous) - narrative_heading_keys(revised)
    # Unnumbered titles may legitimately switch from headings to bold labels.
    compact = re.sub(r"[\s*#]", "", revised)
    missing = {key for key in missing
               if not (key.startswith("title:") and key[6:] in compact)}
    if missing:
        raise ValueError("修订丢失原有章节: " + ", ".join(sorted(missing)))


def _table_rows(markdown):
    tables, rows = [], []
    for line in markdown.splitlines() + [""]:
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            rows.append([part.strip() for part in re.split(r"(?<!\\)\|", line)[1:-1]])
        elif rows:
            tables.append(rows)
            rows = []
    return tables


def assert_hot_tables(markdown, stats, content_scope="all"):
    """Check actual columns, rows and source values, regardless of title style."""
    mode = str(content_scope or "all").lower()
    kinds = SCOPE_KINDS.get(mode, SCOPE_KINDS["all"])
    bundles = [("1.4", stats.get("brand") or {})]
    bundles += [(f"2.{index}.5", bundle)
                for index, bundle in enumerate((stats.get("campaigns") or {}).values(), 1)]
    for prefix, bundle in bundles:
        bounds = section(markdown, prefix)
        if not bounds:
            raise ValueError(f"热门表缺所属章节: {prefix}")
        body = markdown[bounds[1]:bounds[2]]
        # 先判类型越界（客户 1.1 项 P0）：错误口径的根因优先于表内容不一致。
        for kind in ("pgc", "ugc"):
            if kind in kinds:
                continue
            if f"{kind.upper()} 热门内容" in body:
                raise ValueError(
                    f"content_scope={mode} 却输出了 {kind.upper()} 热门表: {prefix}"
                )
        expected = _table_rows(hot_tables(bundle, "", mode))
        if _table_rows(body) != expected:
            raise ValueError(f"热门表字段、行数或源数据不一致: {prefix}")
        for kind in kinds:
            if not (bundle.get("top_content") or {}).get(kind):
                if f"本次可用数据中无 {kind.upper()} 内容。" not in body:
                    raise ValueError(f"热门表缺无数据披露: {prefix}/{kind}")


def _replace(markdown, prefix, body, append=False):
    bounds = section(markdown, prefix)
    if not bounds:
        raise ValueError(f"报告缺必需章节: {prefix}")
    start, heading_end, stop = bounds
    previous = markdown[heading_end:stop].strip() if append else ""
    return markdown[:heading_end] + "\n\n" + (previous + "\n\n" if previous else "") + body + "\n\n" + markdown[stop:]


def _prepend(markdown, prefix, body):
    """把 body 插到章节标题之后、正文之前（口径注须在节首）。"""
    bounds = section(markdown, prefix)
    if not bounds:
        raise ValueError(f"报告缺必需章节: {prefix}")
    _, heading_end, stop = bounds
    rest = markdown[heading_end:stop].strip()
    return (markdown[:heading_end] + "\n\n" + body + "\n\n"
            + (rest + "\n\n" if rest else "") + markdown[stop:])


def supplement_terms(plan):
    """需求确认单「补入品牌关键词的活动关键词」；「无」或空返回 []（FIX-10）。"""
    raw = str((plan or {}).get("brand_supplement_activity_terms") or "").strip()
    if not raw or raw == "无":
        return []
    return [t.strip() for t in re.split(r"[|,，、;；\s]+", raw) if t.strip()]


def brand_supplement_notes(plan):
    """返回 (1.1 口径注, 拉升节口径注)；未补入返回 (None, None)。

    原文：KS_Report_Template.md:189（1.1 专用）与 :642（2.{N+1} 专用），
    均为一句灰色斜体；该行为「无」时不加。
    """
    terms = supplement_terms(plan)
    if not terms:
        return None, None
    first = ("*{{muted}}注：品牌口径含活动关键词「" + "、".join(terms)
             + "」，统计范围包含参与该活动但未提及品牌名的内容。*")
    lift = ("*{{muted}}注：品牌口径含活动关键词，活动期声量包含参与该活动的内容；"
            "基准期使用同一套检索词。*")
    return first, lift


DESCRIPTION_HEADING = "### 数据说明"


def _cell_filter(value) -> str:
    """过滤词为空或「无」时留空（KS §零：不写「无」或占位符）。"""
    text = str(value or "").strip()
    return "" if text in ("", "无") else text


def _period_text(start, end) -> str:
    def fmt(value):
        parts = str(value or "").replace("-", "")[:8]
        return f"{int(parts[:4])}.{int(parts[4:6])}.{int(parts[6:8])}" if len(parts) == 8 else str(value or "")
    return f"{fmt(start)}-{fmt(end)}"


def data_description(plan, stats):
    """篇头数据说明（FIX-43，KS_Report_Template §零 用户定稿版）。

    只放数据平台、时间周期和取数关键词表；分析对象写实际名称；
    禁止方法论、清洗、抽样、版本说明（抽样披露移到整体洞察之后的提示框）。
    """
    platforms = "、".join(PLATFORMS.get(p, p) for p in plan.get("platforms") or [])
    text = (f"{DESCRIPTION_HEADING}\n\n数据平台：{platforms}\n\n"
            f"时间周期：{_period_text(plan.get('period_start'), plan.get('period_end'))}\n\n"
            "取数关键词：\n\n")
    brand = str(plan.get("brand") or "品牌")
    rows = [[brand, plan.get("brand_query") or brand, _cell_filter(plan.get("brand_filter"))]]
    campaigns = [c for c in plan.get("campaigns") or [] if c.get("id")]
    if len(campaigns) > 1:
        for campaign in campaigns:
            query = (campaign.get("activity_query") or campaign.get("query")
                     or "|".join(campaign.get("filter_words") or []))
            name = str(campaign.get("id"))
            if campaign.get("period_start"):
                name += f"（{_period_text(campaign.get('period_start'), campaign.get('period_end'))}）"
            rows.append([name, query, _cell_filter(campaign.get("activity_filter")
                                                   or plan.get("activity_filter"))])
    else:
        rows.append([plan.get("activity_name") or "活动", plan.get("activity_query"),
                     _cell_filter(plan.get("activity_filter"))])
    if plan.get("product_query"):
        rows.append([f"{brand}产品", plan.get("product_query"), ""])
    return text + table(["分析对象", "关键词", "过滤词"], rows)


def sampling_notice(stats) -> str | None:
    """demo 抽样提示（FIX-43）：用户必须知道，放在整体洞察之后单独一段。"""
    sampling = ((stats or {}).get("clean_record") or {}).get("sampling") or {}
    if not sampling.get("applied"):
        return None
    return (f"> [!warn] 本报告为演示抽样：清洗后活动明细 {sampling.get('population_size')} 条，"
            f"实际分析 {sampling.get('sample_size')} 条，结论不代表全量。")


def scope_disclosure(plan) -> str | None:
    """content_scope ≠ 全部时的口径说明（FIX-03），属方法口径，不放数据说明区。"""
    scope = str((plan or {}).get("content_scope") or "all").lower()
    if scope not in ("ugc", "pgc"):
        return None
    label = "UGC" if scope == "ugc" else "PGC"
    return f"*{{{{muted}}}}本次仅分析 {label} 内容，不做 PGC/UGC 结构对比。*"


def assemble(markdown, *, plan, stats, charts, insight):
    """Bind required tables, citations and charts to their actual analysis object."""
    md = re.sub(r"<!-- report-module:start -->.*?<!-- report-module:end -->",
                "", markdown, flags=re.S)
    # Emit standard numbered headings so the independent scanner and Feishu
    # converter receive the same canonical structure as the repair validator.
    for start, end, level, title in reversed(_headings(md)):
        if md[start:end].startswith("**"):
            md = md[:start] + "#" * level + " " + title + md[end:]
    def module(text):
        return "<!-- report-module:start -->\n" + text + "\n<!-- report-module:end -->"
    # Replace only the description area; retain the model's overall insight.
    description = re.search(r"(?m)^#{1,6}\s+[^\n]*数据说明[^\n]*", md)
    if description:
        # A combined intro has no reliable split between notes and narrative.
        # Retain its body under the insight title rather than guessing a range.
        if "整体洞察" in description.group():
            md = (md[:description.start()] + data_description(plan, stats)
                  + "\n\n## 整体洞察" + md[description.end():])
            description = re.search(r"(?m)^" + re.escape(DESCRIPTION_HEADING) + r"[^\n]*", md)
        tail = re.search(
            r"(?m)^(?:#{1,6}\s+|\*\*(?:\d+(?:\.\d+)*\s+)?整体洞察[^\n]*\*\*)",
            md[description.end():],
        )
        stop = description.end() + tail.start() if tail else len(md)
        md = md[:description.start()] + data_description(plan, stats) + "\n\n" + md[stop:]
    else:
        title_end = md.find("\n")
        title_end = title_end if title_end >= 0 else len(md)
        md = md[:title_end] + "\n\n" + data_description(plan, stats) + "\n\n" + md[title_end:]
    scope = str(plan.get("content_scope") or "all").lower()
    md = _replace(md, "1.4", module(hot_tables(stats.get("brand") or {}, "品牌", scope)))
    campaigns = stats.get("campaigns") or {}
    for index, (cid, bundle) in enumerate(campaigns.items(), 1):
        md = _replace(md, f"2.{index}.5", module(hot_tables(bundle, f"活动 {cid}", scope)))
    # Reject invented local paths before rebuilding the known chart placement.
    items = charts.get("charts") or []
    paths = {item.get("local_path") for item in items if item.get("local_path")}
    cover = charts.get("cover") or {}
    if isinstance(cover, dict) and cover.get("local_path"):
        paths.add(cover["local_path"])
    for path in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md):
        if path not in paths:
            raise ValueError("报告图片引用不属于已生成图表清单")
    def chart_order(item):
        cid = str(item.get("chart_id") or "")
        return next((i for i, suffix in enumerate(("_trend", "_platform", "_content"))
                     if cid.endswith(suffix)), 3)
    for item in sorted(items, key=chart_order):
        path = item.get("local_path")
        if not path:
            raise ValueError("必需图表缺少本地文件")
        md = re.sub(r"!\[[^\]]*\]\(" + re.escape(path) + r"\)\s*", "", md)
        cid = str(item.get("chart_id") or "")
        prefix = "1.1"
        topic_bundle = None
        dimension, platform = None, ""
        if cid.startswith("brand_"):
            prefix = "1.2" if "sentiment" in cid else "1.1"
        elif cid.startswith("wordcloud_brand_"):
            prefix = "1.3"
            platform = cid[len("wordcloud_brand_overall_"):]
            topic_bundle = ((insight.get("brand") or {}).get("overall") or {}).get("platforms", {}).get(platform)
        elif cid == "baseline_lift":
            prefix = _POLICY.lift_prefix(len(campaigns))   # FIX-42：二章末 2.{N+1}
        else:
            for index, campaign in enumerate(campaigns, 1):
                safe = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", campaign)
                if cid.startswith(f"wordcloud_{safe}_"):
                    prefix = f"2.{index}.4"
                    dimension_platform = cid[len(f"wordcloud_{safe}_"):]
                    dimension, _, platform = dimension_platform.partition("_")
                    topic_bundle = ((insight.get("campaigns") or {}).get(campaign, {}).get(dimension) or {}).get("platforms", {}).get(platform)
                    break
                if cid.startswith(campaign + "_"):
                    suffix = "2" if "mention_funnel" in cid else "3" if "sentiment" in cid or "dimension_nsr" in cid else "4" if "ugc_category" in cid or "kol_effectiveness" in cid else "1"
                    prefix = f"2.{index}.{suffix}"
                    break
        body = f"![{item.get('title') or cid}]({path})"
        if "wordcloud" in cid:
            if not topic_bundle or not topic_bundle.get("topics"):
                raise ValueError(f"词云缺少对应解读: {cid}")
            body += f"\n\n**{_wordcloud_heading(prefix, dimension, platform)}**"
            for topic in topic_bundle["topics"]:
                frequencies = topic.get("word_frequencies") or {}
                words = "、".join(f"{word}（{frequencies[word]}）" if word in frequencies else word
                                 for word in topic.get("words") or [])
                body += f"\n\n**{topic.get('topic')}**（关键词：{words}）\n\n{topic.get('insight') or ''}\n\n{topic.get('action') or ''}"
        elif "mention_funnel" in cid:
            # FIX-18：层级/声量/占比三列，Tier3 写明分母；表下一行方法说明。
            funnel_bundle = (list(campaigns.values())[int(prefix.split(".")[1]) - 1]
                             if prefix.startswith("2.") and prefix.split(".")[1].isdigit() else {})
            funnel = funnel_bundle.get("mention_funnel") or {}
            body += "\n\n" + table(["层级", "声量", "占比"], mention_funnel.table_rows(funnel))
            body += "\n\n" + mention_funnel.method_note(funnel)
            body += _voice_share_line(funnel_bundle)
        elif "kol_effectiveness" in cid and prefix.startswith("2."):
            # 缺陷清单 D2：达人层级「未知」单独计数，图注说明占比；头腰尾对比只含适用层级。
            bundle = list(campaigns.values())[int(prefix.split(".")[1]) - 1]
            note = kol_unknown_note(bundle)
            if note:
                body += "\n\n" + note
        elif "sentiment" in cid and prefix == "1.2":
            # FIX-51：1.2 只放品牌情感图，不放数据表；图后一句跨口径注（KS 1.2/2.N.3）。
            body += ("\n\n*{{muted}}注：本报告中品牌层与活动层的情感判定方法不同，两者情感比例"
                     "仅供各自参考，不建议直接横向对比绝对数值。*")
        elif "sentiment" in cid and prefix.startswith("2."):
            index = int(prefix.split(".")[1]) - 1
            bundle = list(campaigns.values())[index]
            sd = bundle.get("sentiment_dist") or {}
            total = sum(sd.values())
            body += "\n\n" + table(["情感", "条数", "占比"], [[name, sd.get(key, 0), f"{sd.get(key, 0)/total:.1%}" if total else "无样本"]
                                                             for key, name in (("positive", "正面"), ("neutral", "中性"), ("negative", "负面"))])
            body += "\n\n活动整体净情感度（NSR）：" + fmt_pct(
                (bundle.get("metrics") or {}).get("NSR")
            )
            body += "\n\n*{{muted}}NSR =（正面−负面）/（正面+负面）*"
        md = _replace(md, prefix, module(body), append=True)
    for index, (cid, bundle) in enumerate(campaigns.items(), 1):
        md = _replace(md, f"2.{index}.3", module(opinion_text(bundle)), append=True)
        md = _replace(md, f"2.{index}.4", module(ugc_text(bundle, scope)), append=True)
        formula = hot_formula.render(((insight or {}).get("hot_post_formula") or {}).get(cid) or {})
        if formula:
            md = _replace(md, f"2.{index}.4", module(formula), append=True)
        summary = overview.overview_text(bundle.get("overview") or {})
        if summary and section(md, f"2.{index}.1"):
            md = _prepend(md, f"2.{index}.1", module(summary))
        note = scope_note(bundle)
        if note and section(md, f"2.{index}.1"):
            md = _prepend(md, f"2.{index}.1", module(note))
        sponsor = sponsor_text(bundle)
        if sponsor and section(md, f"2.{index}.2"):
            md = _replace(md, f"2.{index}.2", module(sponsor), append=True)
    exception = data_exception_note(stats)
    if exception and section(md, "1.1") and exception not in md:
        md = _prepend(md, "1.1", module(exception))
    # FIX-44：四章核心成果表由代码生成，放在章首；模型只写待改善方向。
    final = re.search(r"(?m)^#{1,2}\s+" + re.escape(_POLICY.REPORT_CHAPTERS[3]) + r"[^\n]*", md)
    if final:
        rest = md[final.end():].lstrip("\n")
        md = md[:final.end()] + "\n\n" + module(core_results_table(stats)) + "\n\n" + rest
    # FIX-43：抽样提示放在整体洞察之后、一章之前；content_scope 口径注放 1.1 节首。
    notice = sampling_notice(stats)
    if notice:
        chapter = re.search(r"(?m)^#{1,2}\s+" + re.escape(_POLICY.REPORT_CHAPTERS[0]), md)
        if chapter:
            md = (md[:chapter.start()].rstrip() + "\n\n" + module(notice) + "\n\n"
                  + md[chapter.start():])
    brand_summary = overview.overview_text((stats.get("brand") or {}).get("overview") or {})
    if brand_summary and section(md, "1.1"):
        md = _prepend(md, "1.1", module(brand_summary))
    disclosure = scope_disclosure(plan)
    if disclosure and section(md, "1.1"):
        md = _prepend(md, "1.1", module(disclosure))
    # FIX-10：品牌关键词补入了活动关键词时，1.1 与拉升节节首各加一句口径注。
    # 包在 module 里：assemble 开头会剥掉旧 module，重组时不会重复叠加。
    first_note, lift_note = brand_supplement_notes(plan)
    if first_note:
        md = _prepend(md, "1.1", module(first_note))
        lift_prefix = _POLICY.lift_prefix(len(campaigns))
        if section(md, lift_prefix):
            md = _prepend(md, lift_prefix, module(lift_note))
    assert_hot_tables(md, stats, str(plan.get("content_scope") or "all").lower())
    return md
