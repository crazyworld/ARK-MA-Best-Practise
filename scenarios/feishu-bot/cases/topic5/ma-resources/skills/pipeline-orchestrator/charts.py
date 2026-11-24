"""图表 + 封面渲染适配层 — 供 charts_render handler（n14_charts）使用。

图表只渲染成**本地 PNG**。正式交付物是飞书文档，图片通过 medias/upload_all
上传本地二进制拿 file_token 再引用（见 feishu.py）。

设计（类比 ssjh.py / semantic.py）：
  - 规格构建 build_chart_specs(stats_bundle, wordcloud_data)：**纯确定性**，把统计产物
    翻译成图表 spec 列表（符合仓内 blueai-yuqing-echarts-skill-new 的 render_from_spec.js
    bundle 契约：chart_id / title / chart_type / categories / series[{name, values, unit}]）。
    此步无副作用、可单测，是真路径与降级路径共用的“图表选题 + 数据绑定”层。
  - 真路径 render_charts()：逐张调 node render_from_spec.js 渲染 PNG 到 outdir，逐张自检
    （文件存在且非空）；封面调 report-cover-skill（best-effort）。返回每张图的本地
    local_path，由飞书交付层上传 media。无 node_modules / node / 渲染失败即抛异常，由
    handler 降级。
  - 降级 fallback()：返回与真路径同构的 chart_urls（degraded=True），每张图带 spec，
    local_path=None（无渲染器时无图），供下游 write_report/飞书交付无差别消费（缺图跳过）。

凭据只从环境变量读（生产由 MA Vault 注入）：
  REPORT_COVER_API_KEY —— 缺失即跳过封面真生成（降级，不影响图表主体）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import mention_funnel
import wordcloud_adapter
from typing import Any, Optional

# 图表 / 封面 skill 根目录。节点恢复包会把 pipeline-orchestrator 解压到 /tmp，
# 此时兄弟 Skill 仍位于 MA 标准挂载根 /mnt/skills。
_ORCH_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILLS_DIR = os.path.dirname(_ORCH_DIR)


def _resolve_skill_dir(name: str) -> str:
    roots = [
        os.environ.get("TOPIC5_SKILLS_ROOT", ""),
        _SKILLS_DIR,
        "/mnt/skills",
    ]
    for root in roots:
        candidate = os.path.join(root, name) if root else ""
        if candidate and os.path.isdir(candidate):
            return candidate
    return os.path.join(_SKILLS_DIR, name)


ECHARTS_DIR = _resolve_skill_dir("blueai-yuqing-echarts-skill-new")
COVER_DIR = _resolve_skill_dir("report-cover-skill")
WORDCLOUD_DIR = _resolve_skill_dir("wordcloud-insight")
_RENDER_SCRIPT = os.path.join(ECHARTS_DIR, "scripts", "render_from_spec.js")
_ECHARTS_NODE_MODULES = os.path.join(ECHARTS_DIR, "scripts", "node_modules")

# 情感维度中文标签（与 _campaign_stats.sentiment_dist 键对齐）。
_SENTI_LABEL = {"positive": "正面", "neutral": "中性", "negative": "负面"}
# 平台 slug → 中文展示名（与 clean_campaign 的 _PLATFORM_CN 逆映射对齐）。
_PLATFORM_LABEL = {
    "xiaohongshu": "小红书", "douyin": "抖音", "bilibili": "哔哩哔哩",
    "weibo": "微博", "weixin": "微信", "unknown": "未知",
}
_CONTENT_LABEL = {"UGC": "UGC", "PGC": "PGC", "UKN": "未知"}


# ── 可用性探测 ──────────────────────────────────────────────
def renderer_available() -> bool:
    """图表真渲染条件：render 脚本在仓 + node_modules 已安装（含 echarts/resvg）+ node 可执行。

    node_modules 不入库（.gitignore），仅在 MA Linux 沙箱安装；本地测试环境缺它即降级。
    """
    if not (os.path.isfile(_RENDER_SCRIPT) and os.path.isdir(_ECHARTS_NODE_MODULES)):
        return False
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


def renderer_diagnostic() -> str:
    return (
        f"echarts_dir={ECHARTS_DIR}, "
        f"render_script={os.path.isfile(_RENDER_SCRIPT)}, "
        f"node_modules={os.path.isdir(_ECHARTS_NODE_MODULES)}"
    )


def cover_available() -> bool:
    """封面真生成条件：有 relay key 且封面脚本在仓。"""
    return bool(os.environ.get("REPORT_COVER_API_KEY")) and \
        os.path.isfile(os.path.join(COVER_DIR, "scripts", "generate_cover_gpt.py"))


# ── 图表选题 + 数据绑定（纯确定性，真/降级共用）──────────────
# FIX-53：客户图表规范（KS_Chart_Spec）——配色与尺寸常量，builder 写入 spec。
PALETTE = {
    "volume": "#4A7AE3", "interaction": "#F06060",
    "sentiment": {"正面": "#5DBB7D", "中性": "#AAAAAA", "负面": "#F06060"},
    "content": {"PGC": "#4A7AE3", "UGC": "#5DBB7D", "未知": "#AAAAAA"},
    "platform": {"小红书": "#FF2442", "抖音": "#333333", "微博": "#FF8200", "B站": "#00A1D6",
                 "快手": "#FF4906", "微信": "#07C160", "知乎": "#0066FF"},
}
SIZES = {
    "trend": (1400, 600), "platform": (600, 450), "content": (800, 520), "sentiment": (800, 520),
    "ugc_category": (800, 520), "mention_funnel": (700, 500), "kol_effectiveness": (1000, 500),
    "dimension_nsr": (900, 400),
}
X_AXIS_SHORT_SPAN_DAYS = 92
X_AXIS_SHORT_STEP = 5


def x_axis_policy(day_count: int) -> dict:
    """日级 x 轴：≤3 个月每 5 天一个标签，更长按周/双周/月/季/年（目标 6-12 个），首末强制。"""
    if day_count <= X_AXIS_SHORT_SPAN_DAYS:
        step = X_AXIS_SHORT_STEP
    else:
        step = next((s for s in (7, 14, 30, 91, 365) if day_count / s <= 12), 365)
    return {"min_labels": 6, "max_labels": 12, "force_ends": True, "step": step}


def _style(spec: dict | None, kind: str) -> dict | None:
    """按 chart kind 写入客户配色/尺寸；None 透传（不出图）。"""
    if not spec:
        return spec
    width, height = SIZES.get(kind, (1200, 600))
    if kind == "dimension_nsr":
        longest = max((len(str(c)) for c in spec.get("categories") or []), default=0)
        width = max(900, 400 + 14 * longest)
    spec.update(width=width, height=height)
    if kind == "sentiment":
        spec["colors"] = PALETTE["sentiment"]
    elif kind == "content":
        spec["colors"] = PALETTE["content"]
    elif kind == "platform":
        spec["colors"] = {name: PALETTE["platform"].get(name, "#86909C") for name in spec.get("categories") or []}
    elif kind == "trend":
        spec["colors"] = [PALETTE["volume"], PALETTE["interaction"]]
        spec["x_axis_policy"] = x_axis_policy(len(spec.get("categories") or []))
    return spec


def _series(name: str, values: list, unit: Optional[str] = None) -> dict:
    s = {"name": name, "values": values}
    if unit:
        s["unit"] = unit
    return s


def _sentiment_spec(cid: str, stats: dict) -> Optional[dict]:
    """情感分布环形图（正/中/负）。全零不出图。"""
    sd = (stats or {}).get("sentiment_dist") or {}
    cats, vals = [], []
    for k in ("positive", "neutral", "negative"):
        cats.append(_SENTI_LABEL[k])
        vals.append(int(sd.get(k) or 0))
    if sum(vals) == 0:
        return None
    return {"chart_id": f"{cid}_sentiment", "title": f"活动 {cid}·情感分布",
            "chart_type": "donut", "metric": "声量占比", "dimension": "情感",
            "categories": cats, "series": [_series("声量", vals)],
            "source_mode": "native"}


def _platform_spec(cid: str, stats: dict) -> Optional[dict]:
    """平台声量分布环形图。少于 2 个平台不出图。"""
    pd = (stats or {}).get("platform_dist") or {}
    items = sorted(pd.items(), key=lambda kv: kv[1], reverse=True)
    items = [(p, v) for p, v in items if v > 0]
    if len(items) < 2:
        return None
    cats = [_PLATFORM_LABEL.get(p, p) for p, _ in items]
    vals = [int(v) for _, v in items]
    return {"chart_id": f"{cid}_platform", "title": f"活动 {cid}·平台声量分布",
            "chart_type": "donut", "metric": "声量", "dimension": "平台",
            "categories": cats, "series": [_series("声量", vals)],
            "source_mode": "native"}


def _trend_spec(cid: str, stats: dict) -> Optional[dict]:
    """日声量+互动量双序列趋势。"""
    tr = daily_totals((stats or {}).get("trend") or {})
    interaction = daily_totals((stats or {}).get("interaction_trend") or {})
    pts = sorted(tr.items())
    if len(pts) < 2:
        return None
    cats = [k for k, _ in pts]
    vals = [int(v) for _, v in pts]
    return {"chart_id": f"{cid}_trend", "title": f"活动 {cid}·日声量趋势",
            "chart_type": "line", "metric": "声量", "dimension": "日期",
            "categories": cats, "series": [
                _series("日声量", vals, "条"),
                _series("日互动量", [int(interaction.get(key) or 0) for key in cats]),
            ],
            "source_mode": "native"}


def daily_totals(values: dict) -> dict:
    """Accept date→count and SSJH platform→date→count; sum matching dates."""
    result = {}
    for key, value in (values or {}).items():
        if key == "_total":
            continue
        if isinstance(value, dict):
            for day, count in daily_totals(value).items():
                result[day] = result.get(day, 0) + count
        else:
            day = str(key).replace("-", "")
            if not re.fullmatch(r"\d{8}", day):
                raise ValueError(f"趋势日期格式无效: {key}")
            result[day] = result.get(day, 0) + int(value or 0)
    # A supplied total series is authoritative; do not add it to platforms.
    if isinstance((values or {}).get("_total"), dict):
        return daily_totals(values["_total"])
    return dict(sorted(result.items()))


def _day_range(start: str, end: str) -> list[str]:
    from datetime import date, timedelta
    a = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    b = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    return [(a + timedelta(days=i)).strftime("%Y%m%d") for i in range((b - a).days + 1)]


def _bundle_window(bundle: dict) -> tuple[str, str] | None:
    meta = (bundle or {}).get("meta") or {}
    start = str(meta.get("start_date") or "").replace("-", "")[:8]
    end = str(meta.get("end_date") or "").replace("-", "")[:8]
    return (start, end) if len(start) == 8 and len(end) == 8 else None


def _sha(value) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value or {}, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:16]


def two_period_trend(period: dict, base: dict | None) -> dict:
    """1.1 品牌趋势：基准期 + 活动期连续日级（FIX-48，KS_Chart_Spec 1.1）。

    每日按平台求和、保留原值，缺失日期为 None（不补零）；记录两期起止、天数、来源哈希，
    并检查平台键、重叠、活动期逐日之和与 total_volume 是否一致（不一致只记录）。
    """
    windows, volume, interaction = [], {}, {}
    sources = [("基准期", base), ("活动期", period)] if base and not base.get("unavailable") else [("活动期", period)]
    checks = {"missing_days": [], "overlap": False, "platform_keys_match": True, "totals_match": True}
    for name, bundle in sources:
        window = _bundle_window(bundle)
        days_v = daily_totals((bundle or {}).get("volume_trend") or {})
        days_i = daily_totals((bundle or {}).get("interaction_trend") or {})
        if not window and days_v:
            window = (min(days_v), max(days_v))
        if not window:
            continue
        days = _day_range(*window)
        windows.append({"name": name, "start": window[0], "end": window[1], "days": len(days)})
        for day in days:
            if day in volume:
                checks["overlap"] = True
            volume[day] = days_v.get(day)
            interaction[day] = days_i.get(day)
            if day not in days_v:
                checks["missing_days"].append(day)
    if len(sources) == 2:
        keys = [set(k for k in ((b or {}).get("volume_trend") or {}) if k != "_total") for _, b in sources]
        checks["platform_keys_match"] = keys[0] == keys[1]
    total = (period or {}).get("total_volume")
    if isinstance(total, (int, float)) and windows:
        last = windows[-1]
        observed = sum(volume.get(d) or 0 for d in _day_range(last["start"], last["end"]))
        checks["totals_match"] = observed == total
    dates = sorted(volume)
    return {"dates": dates, "volume": [volume[d] for d in dates],
            "interaction": [interaction[d] for d in dates], "periods": windows,
            "base_unavailable": len(sources) == 1,
            "source_sha": {"base": _sha((base or {}).get("volume_trend")) if len(sources) == 2 else None,
                           "period": _sha((period or {}).get("volume_trend"))},
            "checks": checks}


def _brand_trend_spec(two: dict) -> Optional[dict]:
    if len(two.get("dates") or []) < 2:
        return None
    areas = [{"name": f"{p['name']}（{p['start'][4:6]}.{p['start'][6:]}–{p['end'][4:6]}.{p['end'][6:]}，"
                      f"{p['days']}天）", "start": p["start"], "end": p["end"]}
             for p in two.get("periods") or []]
    return {"chart_id": "brand_trend", "title": "品牌·日声量与互动趋势（基准期+活动期）",
            "chart_type": "line", "metric": "声量", "dimension": "日期",
            "categories": two["dates"],
            "series": [_series("日声量", two["volume"], "条"), _series("日互动量", two["interaction"])],
            "mark_areas": areas, "mark_points": ["max"], "connect_nulls": False,
            "source_mode": "native"}


def _content_spec(cid: str, stats: dict) -> Optional[dict]:
    """内容类型分布环形图（UGC/PGC/未知）。全零或单一类型不出图。"""
    cd = (stats or {}).get("content_type_dist") or {}
    cats, vals = [], []
    for k in ("UGC", "PGC", "UKN"):
        v = int(cd.get(k) or 0)
        if v > 0:
            cats.append(_CONTENT_LABEL[k])
            vals.append(v)
    if len(cats) < 2:
        return None
    return {"chart_id": f"{cid}_content", "title": f"活动 {cid}·内容类型分布",
            "chart_type": "donut", "metric": "声量占比", "dimension": "内容类型",
            "categories": cats, "series": [_series("声量", vals)],
            "source_mode": "native"}


def _analysis_specs(cid: str, stats: dict) -> list[dict]:
    specs = []
    # FIX-18：退化时只画 活动相关→产品提及；主讨论对象方法画横向条形，不画漏斗。
    levels = mention_funnel.chart_levels((stats or {}).get("mention_funnel") or {})
    if levels:
        chart_type, cats, vals = levels
        title = ("主讨论对象分布" if chart_type == "rank_bar" else "提及漏斗")
        specs.append({
            "chart_id": f"{cid}_mention_funnel",
            "title": f"活动 {cid}·{title}",
            "chart_type": chart_type,
            "categories": cats,
            "series": [_series("声量", vals)],
            "source_mode": "native",
        })
    dimensions = [
        (name, item.get("value"))
        for name, item in ((stats or {}).get("dimension_nsr") or {}).items()
        if item.get("status") == "available" and item.get("value") is not None
    ]
    if dimensions:
        specs.append({
            "chart_id": f"{cid}_dimension_nsr",
            "title": f"活动 {cid}·分维度 NSR",
            "chart_type": "rank_bar",
            "categories": [name for name, _ in dimensions],
            "series": [_series("NSR", [value for _, value in dimensions])],
            "source_mode": "native",
        })
    kol = (stats or {}).get("kol_effectiveness") or {}
    levels = [level for level in ("head", "waist", "tail") if level in kol]
    # 粉丝数据全部缺失时不出图（FIX-16：改灰色斜体固定说明）。
    if levels and not (stats or {}).get("kol_notes", {}).get("all_unknown"):
        labels = (stats or {}).get("kol_level_labels") or {}
        specs.append({
            "chart_id": f"{cid}_kol_effectiveness",
            "title": f"活动 {cid}·达人层级效能",
            "chart_type": "bar",          # 双轴柱图：左轴声量占比，右轴条均互动量
            "categories": [labels.get(level, level) for level in levels],
            "series": [
                _series("声量占比", [kol[level]["volume_share"] for level in levels]),
                # 轴标题按客户要求：条均互动量（次/条）
                _series("条均互动量（次/条）",
                        [kol[level]["avg_interaction"] for level in levels]),
            ],
            "source_mode": "native",
        })
    ugc = (stats or {}).get("ugc_category_dist") or {}
    if ugc:
        specs.append({
            "chart_id": f"{cid}_ugc_category",
            "title": f"活动 {cid}·UGC 参与方式",
            "chart_type": "donut",
            "categories": list(ugc),
            "series": [_series("声量", [ugc[key] for key in ugc])],
            "source_mode": "native",
        })
    return specs


def _wordcloud_specs(wordcloud_data: dict, plan: Optional[dict] = None) -> list[dict]:
    """逐活动、维度生成词云规格；活动词云为综合已选平台的一张图（FIX-54，platform="all"）。"""
    specs = []
    for cid, dimensions in ((wordcloud_data or {}).get("campaigns") or {}).items():
        for dimension, bundle in (dimensions or {}).items():
            for plat, words in (bundle.get("platforms") or {}).items():
                if not isinstance(words, dict) or not words:
                    continue
                rows = (bundle.get("word_rows") or {}).get(plat)
                if rows is not None:
                    if ({r["term"]: r["value"] for r in rows} != words
                            or len(rows) != len(words)):
                        raise ValueError("词云原表顺序记录与原始词频不一致")
                    words = {row["term"]: row["value"] for row in rows}
                # Skill selects Top100 only at render time; analysis retains all.
                top = list(words.items())
                label = "综合已选平台" if plat == "all" else _PLATFORM_LABEL.get(plat, plat)
                specs.append({
                    "chart_id": f"wordcloud_{_safe(cid)}_{dimension}_{plat}",
                    "title": f"{cid}·{dimension}·{label}词云",
                    "chart_type": "wordcloud",
                    "metric": "词频",
                    "dimension": "关键词",
                    "categories": [word for word, _ in top],
                    "series": [_series("词频", [freq for _, freq in top])],
                    "source_mode": "native",
                    "wordcloud_input": wordcloud_adapter.input_payload(
                        cid, dimension, plat, words, plan or {},
                        bundle.get("metric") or "unknown"),
                    "wordcloud_scope": {"campaign": cid, "dimension": dimension, "platform": plat},
                })
    return specs


def _chart_kind(chart_id: str) -> str:
    for kind in SIZES:
        if chart_id.endswith("_" + kind):
            return kind
    return ""


def build_chart_specs(stats_bundle: dict, wordcloud_data: dict, plan: Optional[dict] = None,
                      requirement: Optional[dict] = None) -> list[dict]:
    specs = _build_chart_specs(stats_bundle, wordcloud_data, plan)
    for spec in specs:
        if spec.get("chart_type") == "wordcloud":
            # FIX-55：每张词云的 call-brief（调用层内部记录），由已保存需求确定性生成。
            scope = spec.get("wordcloud_scope") or {}
            spec["call_brief"] = wordcloud_adapter.build_call_brief(
                plan or {}, requirement or {}, scope.get("campaign", ""),
                scope.get("dimension", "overall"), scope.get("platform", ""))
        else:
            _style(spec, _chart_kind(spec["chart_id"]))
    return specs


def _build_chart_specs(stats_bundle: dict, wordcloud_data: dict, plan: Optional[dict] = None) -> list[dict]:
    """把统计产物翻译为图表 spec 列表（确定性、无副作用、可单测）。

    资格规则（简化自 echarts skill §1/§8）：剔除空图/全零/单值/单类别；多活动逐个出图；
    派生词云图不继承“图表占位”。返回顺序稳定，供真渲染与降级占位复用。
    """
    specs: list[dict] = []
    brand = (stats_bundle or {}).get("brand") or {}
    if brand:
        sentiment = brand.get("sentiment_dist") or {}
        if isinstance(sentiment.get("_total"), dict):
            sentiment = sentiment["_total"]
        content = brand.get("content_type_dist") or {}
        if isinstance(content.get("_total"), dict):
            content = content["_total"]
        brand_stats = {
            "sentiment_dist": sentiment,
            "platform_dist": brand.get("platform_dist") or {},
            "trend": brand.get("trend") or {},
            "interaction_trend": brand.get("interaction_trend") or {},
            "content_type_dist": content,
        }
        for builder in (_sentiment_spec, _platform_spec, _trend_spec, _content_spec):
            if builder is _trend_spec and brand.get("trend_two_period"):
                spec = _brand_trend_spec(brand["trend_two_period"])   # FIX-48
            else:
                spec = builder("brand", brand_stats)
            if spec:
                spec["title"] = spec["title"].replace("活动 brand", "品牌")
                specs.append(spec)
    camps = (stats_bundle or {}).get("campaigns") or {}
    for cid, stats in camps.items():
        for builder in (_sentiment_spec, _platform_spec, _trend_spec, _content_spec):
            spec = builder(cid, stats)
            if spec:
                specs.append(spec)
        specs.extend(_analysis_specs(cid, stats))
    stats_lift = (stats_bundle or {}).get("baseline_lift") or {}
    if stats_lift.get("status") == "available":
        daily = stats_lift.get("daily") or {}
        days = stats_lift.get("days") or {}
        categories, volume_series, interaction_series = [], [], []
        for label, key in (("活动期", "period"), ("基准期", "base")):
            point = daily.get(key) or {}
            if point.get("volume") is None:
                continue
            categories.append(f"{label}（{days.get(key)}天）")
            volume_series.append(point.get("volume"))
            interaction_series.append(point.get("interaction"))
        if categories:
            # 主指标用日均（FIX-15），总量只在数据表里作辅助，不画柱。
            specs.append({
                "chart_id": "baseline_lift",
                "title": "活动期与基准期日均声量对比",
                "chart_type": "bar",
                "categories": categories,
                "series": [
                    _series("日均声量", volume_series),
                    _series("日均互动量", interaction_series),
                ],
                # 图注按原版：基准期含节假日时须标明声量基数偏高。
                "note": stats_lift.get("holiday_text") or "",
                "source_mode": "native",
            })
    specs.extend(_wordcloud_specs(wordcloud_data, plan))
    if brand.get("wordcloud"):
        specs.extend(_wordcloud_specs({"campaigns": {"brand": {"overall": {
            "platforms": brand["wordcloud"],
            "word_rows": brand.get("wordcloud_rows") or {},
        }}}}, plan))
    return specs


# ── 文件名安全化 ────────────────────────────────────────────
def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\-]", "_", str(name or ""))


# ── 真路径：逐张渲染为本地 PNG ───────────────────────────────
def _render_png(spec: dict, out_png: str, width: int = 1200, height: int = 600) -> None:
    """调 render_from_spec.js 把单 spec 渲染为 PNG；逐张自检文件存在且非空。"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        import json as _json
        _json.dump(spec, f, ensure_ascii=False)
        spec_path = f.name
    try:
        subprocess.run(
            ["node", _RENDER_SCRIPT, "--spec", spec_path, "--output", out_png,
             "--width", str(width), "--height", str(height)],
            capture_output=True, timeout=120, check=True,
        )
    finally:
        try:
            os.unlink(spec_path)
        except OSError:
            pass
    if not (os.path.isfile(out_png) and os.path.getsize(out_png) > 0):
        raise RuntimeError(f"图表渲染产物为空: {spec.get('chart_id')}")


def _render_wordcloud(spec: dict, out_root: str) -> dict:
    # Explicit user override is optional. Otherwise use the customer's
    # platform-specific automatic font order, including packaged Noto Bold.
    font = os.environ.get("TOPIC5_WORDCLOUD_FONT")
    return wordcloud_adapter.render(spec, out_root, WORDCLOUD_DIR, font)


def _gen_cover(title: str, subtitle: str, out_png: str) -> Optional[str]:
    """封面真生成（best-effort）；成功返回本地路径，任何失败返回 None。"""
    if not cover_available():
        return None
    if os.path.join(COVER_DIR, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(COVER_DIR, "scripts"))
    try:
        from generate_cover_gpt import generate_report_cover_gpt  # noqa: E402
        r = generate_report_cover_gpt(
            title=title, subtitle=subtitle, title_en=title, subtitle_en=subtitle,
            output_path=out_png)
        return out_png if r.get("success") else None
    except Exception:
        return None


# FIX-39 视觉自检（确定性部分）：尺寸、非空、非黑底。可读性/乱码交独立 Validator 逐张查看。
PNG_MIN_OPAQUE = 0.05       # 非透明像素占比下限（排除空白图）
PNG_MIN_BRIGHTNESS = 60     # 平均亮度下限 0-255（排除黑底图）


def png_self_check(path: str) -> dict:
    """非空、非黑底。像素尺寸受渲染器 scale 与标题栏影响，不在此断言（尺寸由 spec 控制，见 FIX-53）。"""
    from PIL import Image, ImageStat
    with Image.open(path) as img:
        rgba = img.convert("RGBA")
        size = rgba.size
        alpha = rgba.getchannel("A")
        opaque = sum(alpha.histogram()[1:]) / float(size[0] * size[1])
        background = Image.new("RGBA", size, (255, 255, 255, 255))
        brightness = ImageStat.Stat(Image.alpha_composite(background, rgba).convert("L")).mean[0]
    problems = []
    if opaque < PNG_MIN_OPAQUE:
        problems.append(f"非透明像素 {opaque:.1%} < {PNG_MIN_OPAQUE:.0%}（疑似空白图）")
    if brightness < PNG_MIN_BRIGHTNESS:
        problems.append(f"平均亮度 {brightness:.0f} < {PNG_MIN_BRIGHTNESS}（疑似黑底）")
    return {"size": list(size), "opaque_ratio": round(opaque, 4), "brightness": round(brightness, 1),
            "ok": not problems, "problems": problems}


def render_charts(stats_bundle: dict, wordcloud_data: dict, *,
                  project: str, version: str, report_name: str,
                  outdir: str, plan: Optional[dict] = None) -> dict:
    """真路径：逐张渲染为本地 PNG（落 outdir），封面 best-effort。

    图片由飞书交付层（feishu.create_delivery）用 medias/upload_all 上传本地二进制。
    每张图返回 local_path（绝对路径）；url 置 None。
    """
    if not renderer_available():
        raise RuntimeError("图表渲染器不可用（缺 node_modules 或 node）")

    specs = build_chart_specs(stats_bundle, wordcloud_data, plan)
    os.makedirs(outdir, exist_ok=True)
    charts_out: list[dict] = []
    for spec in specs:
        png = os.path.join(outdir, f"{spec['chart_id']}.png")
        wordcloud_artifacts = None
        if spec.get("chart_type") == "wordcloud":
            wordcloud_artifacts = _render_wordcloud(spec, outdir)
            # FIX-54：双版都生成，报告引用白底版（KS 词云规格）。
            png = wordcloud_artifacts["white_path"]
        else:
            _render_png(spec, png, spec.get("width", 1200), spec.get("height", 600))   # FIX-53 尺寸
        check = png_self_check(png)
        if not check["ok"]:
            raise RuntimeError(f"图表自检未通过 {spec['chart_id']}：" + "；".join(check["problems"]))
        charts_out.append({
                           "self_check": check,"chart_id": spec["chart_id"], "title": spec["title"],
                           "chart_type": spec["chart_type"], "local_path": png,
                           "spec": spec,
                           **({"wordcloud_artifacts": wordcloud_artifacts} if wordcloud_artifacts else {}),
                           "url": None, "source_mode": spec.get("source_mode")})

    # FIX-39：客户要求封面非空——失败即抛错，由 n14 重试/暂停，不再降级。
    cover_png = _gen_cover(report_name, f"版本 {version}",
                           os.path.join(outdir, "cover.png"))
    if not cover_png or not os.path.isfile(cover_png) or os.path.getsize(cover_png) == 0:
        raise RuntimeError("封面生成失败（cover.png 为空或不可用）")
    cover = {"local_path": cover_png, "url": None, "degraded": False,
             "self_check": png_self_check(cover_png)}

    return {"charts": charts_out, "cover": cover, "project": project,
            "version": version, "report_name": report_name,
            "chart_count": len(charts_out), "degraded": False}


# ── 降级：同构 chart_urls（结构真实，local_path=None 表无图可嵌）────────
def fallback(stats_bundle: dict, wordcloud_data: dict, *, project: str,
             version: str, report_name: str) -> dict:
    """无渲染器/失败降级：返回同构 chart_urls（degraded=True）。

    每张图带确定性 spec 但 local_path=None / url=None（无渲染器即无图），结构可被
    write_report 与飞书交付无差别消费（缺图跳过，不塞死链）。封面同样降级。
    """
    specs = build_chart_specs(stats_bundle, wordcloud_data)
    charts_out = []
    for spec in specs:
        charts_out.append({
            "chart_id": spec["chart_id"], "title": spec["title"],
            "chart_type": spec["chart_type"],
            "local_path": None, "url": None,
            "source_mode": spec.get("source_mode"),
            "spec": spec,          # 降级保留 spec，便于后续在真环境补渲染
            "degraded": True,
        })
    return {"charts": charts_out,
            "cover": {"local_path": None, "url": None, "degraded": True},
            "project": project, "version": version, "report_name": report_name,
            "chart_count": len(charts_out), "degraded": True}
