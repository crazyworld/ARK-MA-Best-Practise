"""Node handler registry + stub implementations.

每个 handler 签名：handler(node, state, ctx) -> dict
返回约定：
  code/tool/agent 节点：{"artifacts": {artifact_key: payload, ...}}
  decision 节点：      {"route": "<routes 键>"}
  human 节点：          raise HumanPause（由引擎捕获，置 awaiting_human）

stub 阶段：外部工具(SSJH/DataHub/飞书)和语义节点(发现/洞察/撰写/质检)
只产出占位 payload，证明编排骨架真实可跑。真实实现后续按 handler 名增量替换。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import semantic
import ssjh
import charts
import annotate as annotate_mod
import excel_source
import feishu
import validator as validator_mod
import report_contract
import label_review
import platforms
import lift  # FIX-15 活动拉升（日均口径）
import propagation_features  # 缺陷清单 Q1：旧6类改为传播特征参考
import annotation_structure as annotation_structure_mod  # FIX-29 结构预检
import review_plan as review_plan_mod  # FIX-30 标注抽检计划
import mention_funnel  # FIX-18 提及漏斗
import overview  # FIX-47 传播概况结构结论与高点话术
import hot_formula  # FIX-49 热帖成功配方
import noise_review  # FIX-05 Step5.5 去噪
import module_scope  # FIX-61 逐模块范围记录
import social_task  # FIX-04 活动原帖自动采集
import replay  # 测试回放：复用历史 Run 的外部数据与标注缓存

# ── 业务口径常量（源：飞书 wiki 活动复盘口径 + knowledge/_字段速查.md）────────
# 指标命名白名单：活动层只允许这些指标名；禁用“人均/转化率/性价比”等无数据支撑说法。
_POLICY = validator_mod.load_policy()
# 口径唯一事实源是 topic5-validator/scripts/policy.py（FIX-57）。定位失败时回退本地兼容
# 常量，并由 test_policy_single_source 断言两侧一致——不在导入期抛错。
METRIC_WHITELIST = tuple(
    getattr(_POLICY, "METRIC_WHITELIST", None)
    or ("条均互动量", "条均互动倍数", "品牌提及率", "品牌主导讨论占比",
        "产品提及占比", "声量贡献参考", "NSR")
)
FORBIDDEN_METRIC_TERMS = tuple(
    getattr(_POLICY, "FORBIDDEN_METRIC_TERMS", None)
    or ("人均", "转化率", "转化", "性价比")
)
FORBIDDEN_METRIC_PATTERNS = tuple(
    getattr(_POLICY, "FORBIDDEN_METRIC_PATTERNS", None) or ()
)
NSR_WRONG_DENOMINATOR_PATTERNS = tuple(
    getattr(_POLICY, "NSR_WRONG_DENOMINATOR_PATTERNS", None) or ()
)
DISCUSSION_DIMENSION_LABELS = dict(
    getattr(_POLICY, "DISCUSSION_DIMENSION_LABELS", None)
    or {"品牌": "品牌主导讨论", "产品": "产品主导讨论", "活动": "活动流程与获取主导讨论"}
)
DIMENSION_NSR_MIN_PN = int(
    getattr(_POLICY, "DIMENSION_NSR_MIN_POSITIVE_NEGATIVE", 30)
)
KOL_THRESHOLDS = dict(getattr(_POLICY, "KOL_THRESHOLDS", None) or {})
KOL_LEVEL_LABELS = dict(getattr(_POLICY, "KOL_LEVEL_LABELS", None) or {})
KOL_EFFECTIVENESS_LEVELS = tuple(
    getattr(_POLICY, "KOL_EFFECTIVENESS_LEVELS", None) or ("head", "waist", "tail")
)
RULES_VERSION = str(getattr(_POLICY, "RULES_VERSION", "") or "topic5-validator-qc-v1.9")
# content_scope → SSJH content_mode（FIX-03）。平台映射见 platforms.py。
CONTENT_MODE_BY_SCOPE = {"all": "ALL", "ugc": "UGC", "pgc": "PGC"}
# 内容类型规整：SSJH content_mode 的“未知”归一为 UKN。
_CONTENT_TYPE = {"UGC": "UGC", "PGC": "PGC", "未知": "UKN", "UKN": "UKN"}
# FIX-23：活动相关行 AI 情感缺失/非法比例上限（原版 aggregate --sentiment-missing-max 0.05）。
AI_SENTIMENT_MISSING_MAX = 0.05
# FIX-21：AI 判「不相关」占比超过此值 → 告警并要求复核（原版 aggregate --exclude-warn 0.10）。
AI_IRRELEVANT_WARN = 0.10
# 被判「不相关」的内容建议抽查条数（原版告警原文「建议随机 30 条」），计入轮次一审核额度。
AI_IRRELEVANT_REVIEW_SAMPLE = 30
# demo 只对清洗后的活动明细做稳定抽样；full 始终保留全量。
_DEMO_SAMPLE_LIMIT = 500
# SSJH publish_time 为 epoch 毫秒（CST）；统一按 UTC+8 转 YYYYMMDD（与声量趋势键对齐）。
_CST = timezone(timedelta(hours=8))


class HumanPause(Exception):
    """人工关卡：引擎捕获后暂停 run，等待 resume 注入人工产物。"""

    def __init__(self, prompt: str, expect: list[str]):
        super().__init__(prompt)
        self.prompt = prompt
        self.expect = expect  # 需要人工提供的 artifact 键


_REGISTRY: dict[str, Callable] = {}


def handler(name: str):
    def deco(fn: Callable) -> Callable:
        _REGISTRY[name] = fn
        return fn
    return deco


def get(name: str) -> Callable:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的 handler: {name}")
    return _REGISTRY[name]


def _stub_payload(node: dict, kind: str) -> dict:
    return {"_stub": True, "kind": kind, "node": node["id"], "name": node.get("name")}


# ── 清洗/聚合口径辅助（n07/n12 真实逻辑） ─────────────────────
def _iter_ssjh_rows(brand_stats: dict) -> list[dict]:
    """展平 SSJH articles（{platform: [row, ...]}），平台写入每行 _platform。"""
    arts = (brand_stats or {}).get("articles") or {}
    rows: list[dict] = []
    if isinstance(arts, dict):
        for platform, plist in arts.items():
            for r in (plist or []):
                rr = dict(r)
                rr.setdefault("_platform", platform)
                rows.append(rr)
    elif isinstance(arts, list):
        rows = [dict(r) for r in arts]
    return rows


def _normalize_platform(row: dict, fallback: str | None) -> str:
    """派生平台 slug（FIX-17）：优先行内 _platform，回退 site_name 映射。

    行内 _platform 在 excel_source 阶段已是 slug；这里兜中文名/别名与旧 Run 值，
    最后按站点名映射，未映射落 other。展示中文见 platforms.display()。
    """
    p = row.get("_platform") or fallback
    if p:
        text = str(p)
        if text in platforms.CANON:
            return platforms.to_slug(text)
        if text == platforms.OTHER:
            return platforms.OTHER_SLUG
        for canon, slug in platforms.CANON.items():
            if slug == text:
                return slug
        alias = platforms.ALIASES.get(text)
        if alias:
            return platforms.to_slug(alias)
    slug, _ = platforms.normalize_slug(str(row.get("site_name") or ""))
    return slug


# 达人层级平台阈值（FIX-16）：KS_Data_Processing.md:44-55 平台阈值表。
# 键为 platforms.CANON 的标准中文名；值为 (头部, 腰部, 尾部) 的下限。
# 「新闻」「公众号」不参与达人层级（原文 N/A）。
KOL_THRESHOLDS: dict[str, tuple[int, int, int] | None] = {
    "抖音": (1_000_000, 100_000, 10_000),
    "微博": (1_000_000, 100_000, 10_000),
    "B站": (1_000_000, 100_000, 10_000),
    "快手": (1_000_000, 100_000, 10_000),
    "小红书": (500_000, 50_000, 10_000),
    "视频号": (500_000, 50_000, 10_000),
    "知乎": (100_000, 10_000, 1_000),
    "新闻": None,
    "微信": None,
    "电商": None,
    "其他": None,
}

KOL_LEVEL_LABELS = {
    "head": "头部", "waist": "腰部", "tail": "尾部",
    "ordinary": "素人", "unknown": "未知", "n/a": "不适用",
}
# 只有这三层参与达人层级效能统计（排除素人与未知）。
KOL_EFFECTIVE_LEVELS = ("head", "waist", "tail")


def _kol_fans(row: dict) -> int | None:
    """粉丝数：缺失、空、0 一律视为未知（FIX-16，原版不把 0 当素人）。"""
    fans = row.get("fans") if row.get("fans") is not None else row.get("fans_count")
    if fans is None or (isinstance(fans, str) and not fans.strip()):
        return None
    try:
        value = int(fans)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _derive_kol_level(row: dict) -> str:
    """按平台阈值派生达人层级（FIX-16）。

    先取行内平台（FIX-17 已标准化为 slug），再查平台阈值表：
      - 新闻/公众号等不参与平台 → "n/a"；
      - 粉丝数缺失或为 0 → "unknown"；
      - 其余按该平台的头部/腰部/尾部下限分级，低于尾部下限 → 素人。
    """
    slug = row.get("_platform")
    if not slug:
        slug = _normalize_platform(row, None)
    canon = platforms.display(str(slug or ""))
    thresholds = KOL_THRESHOLDS.get(canon)
    if thresholds is None:
        return "n/a"
    fans = _kol_fans(row)
    if fans is None:
        return "unknown"
    head, waist, tail = thresholds
    if fans >= head:
        return "head"
    if fans >= waist:
        return "waist"
    if fans >= tail:
        return "tail"
    return "ordinary"


def _publish_ymd(row: dict) -> str | None:
    """日期口径统一取发表时间 publish_time（epoch ms，CST）→ YYYYMMDD；缺失返回 None。"""
    ts = row.get("publish_time")
    if ts is None:
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts > 1e12:  # 毫秒
        ts /= 1000.0
    return datetime.fromtimestamp(ts, _CST).strftime("%Y%m%d")


def _dedup_key(row: dict) -> str:
    """去重键：优先 fingerprint，回退 id / item_id / url；
    无 ID 按「作者+发布时间+正文前50字」（KS_Exception_Handling 常见异常表）。"""
    for k in ("fingerprint", "id", "item_id", "url"):
        v = row.get(k)
        if v:
            return str(v)
    if row.get("author") and row.get("publish_time"):
        return f"{row['author']}|{row['publish_time']}|{str(row.get('content') or '')[:50]}"
    return repr(sorted(row.items()))


def _stable_sample(rows: list[dict], limit: int) -> list[dict]:
    """按稳定去重键的 SHA-256 排序抽样，重跑及输入换序时样本不漂移。"""
    return sorted(
        rows,
        key=lambda row: (
            hashlib.sha256(_dedup_key(row).encode("utf-8")).hexdigest(),
            _dedup_key(row),
        ),
    )[:limit]


# ── code 节点 ───────────────────────────────────────────────
@handler("intake_check")
def intake_check(node, state, ctx):
    """生成 G1 待填写的需求模板。

    n00 发生在用户提交 G1 表单之前，因此这里只声明字段，不伪造 demo 值。用户输入由
    hc_bridge 在 resume 时二次校验并写成 requirement_confirmed。
    """
    return {"artifacts": {"requirement_snapshot": {
        "status": "awaiting_user_input",
        "required_fields": [
            "brand", "activity_name", "period_start", "period_end", "platforms",
            "brand_query", "activity_query", "product_query", "content_scope",
            "source_file",
        ],
        "optional_fields": ["brand_filter", "activity_filter"],
        "fields_complete": False,
    }}}


@handler("load_context")
def load_context(node, state, ctx):
    return {"artifacts": {"exec_context": _stub_payload(node, "lm_error_cases")}}


def _apply_filterword_decision(plan: dict, rows: list, state) -> tuple[list, int]:
    """按 filterword_decision 剔除命中活动过滤词的行（FIX-11）。"""
    decided = state.read_artifact("filterword_decision") or {}
    if str(decided.get("decision") or "").strip() not in ("剔除命中行", "drop", "exclude"):
        return rows, 0
    campaigns = [c for c in (plan.get("campaigns") or []) if isinstance(c, dict)]
    if not campaigns:
        campaigns = [{"id": plan.get("activity_name")}]
    drop: set[int] = set()
    for camp in campaigns:
        words = camp.get("activity_filter") or plan.get("activity_filter") or []
        if not words:
            continue
        name = str(camp.get("id") or camp.get("name") or "")
        scoped = [row for row in rows
                  if not name or str(row.get("analysis_object") or name) == name]
        result = excel_source.check_filterwords(scoped, words)
        for hit in result["hits"]:
            drop.add(id(scoped[hit["row_index"]]))
    if not drop:
        return rows, 0
    kept = [row for row in rows if id(row) not in drop]
    return kept, len(rows) - len(kept)


@handler("clean_campaign")
def clean_campaign(node, state, ctx):
    """活动数据清洗（本地确定性，无副作用）。

    口径（源：飞书 wiki + knowledge/01_输入与范围）：
      - 周期过滤：日期取**发表时间**，落在活动窗口 [period_start, period_end] 外剔除。
      - 去重：按 fingerprint（回退 id/item_id/url）。
      - 派生：规整平台 slug、KOL 层级、发表日 YYYYMMDD。
      - demo：清洗后超过 500 条时做稳定哈希抽样；full 保留全量。
      - 剔除比例只做留痕；需要抽查的是 AI 判「不相关」占比（FIX-21，见 aggregate.stats_scope
        与 review_plan 定向抽查），周期外数据在 excel_source 已报错，原阈值永不触发。
      - 全部缺发表时间 → 直接报错（日期口径无法建立）。
    """
    plan = state.read_artifact("confirmed_plan") or {}
    try:
        excel_data = excel_source.load_activity_rows(plan)
    except excel_source.SheetMergeRequired as merge:
        raise HumanPause(prompt=str(merge), expect=["data_exception_ack"]) from merge
    if excel_data is None:
        raise ValueError(
            "clean_campaign: confirmed_plan 缺少 G2 冻结的活动明细 source_file"
        )
    raw_rows = list(excel_data["rows"])
    meta = excel_data.get("meta") or {}
    source = "user_uploaded_excel"
    # FIX-11：用户在 filterword_decision 里选择剔除时，清洗阶段按同一套过滤词剔除命中行。
    # 决定由 n04 的 HumanPause 拿到；这里只在明确要求剔除时动手。
    raw_rows, filterword_removed = _apply_filterword_decision(plan, raw_rows, state)
    total_in = len(raw_rows) + filterword_removed

    # 活动窗口：优先 confirmed_plan，回退 SSJH meta 的 start/end（YYYY-MM-DD → YYYYMMDD）。
    def _ymd(s):
        return str(s).replace("-", "")[:8] if s else None
    period_start = _ymd(plan.get("period_start") or meta.get("start_date"))
    period_end = _ymd(plan.get("period_end") or meta.get("end_date"))

    # 全缺发表时间 → 报错（不兜底）。
    if total_in and all(_publish_ymd(r) is None for r in raw_rows):
        raise ValueError("clean_campaign: 所有记录缺发表时间（publish_time），日期口径无法建立")

    seen: set[str] = set()
    cleaned: list[dict] = []
    removed = {"out_of_period": 0, "duplicate": 0, "no_date": 0,
               "filterword": filterword_removed}  # FIX-11
    for r in raw_rows:
        ymd = _publish_ymd(r)
        if ymd is None:
            removed["no_date"] += 1
            continue
        if period_start and ymd < period_start:
            removed["out_of_period"] += 1
            continue
        if period_end and ymd > period_end:
            removed["out_of_period"] += 1
            continue
        key = _dedup_key(r)
        if key in seen:
            removed["duplicate"] += 1
            continue
        seen.add(key)
        platform = _normalize_platform(r, meta.get("platforms", [None])[0]
                                       if meta.get("platforms") else None)
        cleaned.append({
            **r,
            "_platform": platform,
            "_kol_level": _derive_kol_level(r),
            "_publish_ymd": ymd,
            "_dedup_key": key,
        })

    suspect_duplicates = _zero_fan_duplicates(cleaned)
    eligible_out = len(cleaned)
    run_mode = str(state.params.get("run_mode") or "full").lower()
    sample_applied = run_mode == "demo" and eligible_out > _DEMO_SAMPLE_LIMIT
    if sample_applied:
        cleaned = _stable_sample(cleaned, _DEMO_SAMPLE_LIMIT)
    # FIX-58：数据异常分级。严重项（有效量 <100）暂停告知用户，用户确认继续后带 ack 重跑。
    data_profile = excel_source.profile(cleaned, meta, run_mode)
    ack = state.read_artifact("data_exception_ack") or {}
    if data_profile["severe"] and ack.get("decision") != "continue":
        raise HumanPause(prompt="；".join(data_profile["severe"]), expect=["data_exception_ack"])
    total_out = len(cleaned)
    removed_total = sum(removed.values())
    remove_ratio = (removed_total / total_in) if total_in else 0.0
    cleaned_payload = {
        "rows": cleaned,
        "clean_record": {
            "source": source,
            "source_file": meta.get("file_name"),
            "total_in": total_in,
            "eligible_out": eligible_out,
            "total_out": total_out,
            "removed": {**removed, "date_error": int(meta.get("date_error_rows") or 0)},
            "remove_ratio": round(remove_ratio, 4),
            "profile": data_profile,
            # FIX-26：零粉丝且同文案重复 ≥5 次 → 只标记待人工决定，不自动剔除。
            "uncertain_zero_fan_duplicates": suspect_duplicates,
            # FIX-21：多活动时按活动周期剔除的条数，供 stats_scope 计算该活动的原始量。
            "period_dropped_by_campaign": dict(meta.get("period_dropped_by_campaign") or {}),
            "period": {"start": period_start, "end": period_end},
            "platforms": sorted({c["_platform"] for c in cleaned}),
            "sampling": {
                "applied": sample_applied,
                "mode": run_mode,
                "limit": _DEMO_SAMPLE_LIMIT if run_mode == "demo" else None,
                "population_size": eligible_out,
                "sample_size": total_out,
                "method": (
                    "stable_sha256_dedup_key" if sample_applied else "none"
                ),
                "disclosure": (
                    f"demo 模式从 {eligible_out} 条清洗后活动明细中稳定抽样 "
                    f"{total_out} 条，结果仅供流程演示与方向参考，不代表全量结论。"
                    if sample_applied else ""
                ),
            },
        },
    }
    return {"artifacts": {"cleaned_data": cleaned_payload}}


def _row_sentiment(row: dict) -> int | None:
    """取 AI 情感 ai_sentiment → {1,0,-1}；缺失或非法返回 None。

    FIX-23：活动层情感只认轮次一 AI 标注，不回退监测平台原始 sentiment
    （原版 aggregate.py:76「不回退到监测平台的原始『情感』列」）。
    缺失比例的容忍与报错由 aggregate 统一处理。
    """
    v = row.get("ai_sentiment")
    if v is None:
        return None
    if isinstance(v, str):
        m = {"正": 1, "正面": 1, "positive": 1, "中": 0, "中性": 0, "neutral": 0,
             "负": -1, "负面": -1, "negative": -1}
        if v in m:
            return m[v]
        try:
            v = int(v)
        except ValueError:
            return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    return iv if iv in (1, 0, -1) else None


def _is_relevant(row: dict) -> bool | None:
    """活动相关性（轮次一标注）。无 relevance 字段 → None（未标注）。"""
    if "relevance" not in row:
        return None
    v = row["relevance"]
    if isinstance(v, bool):
        return v
    return str(v) in ("是", "相关", "true", "True", "relevant", "1")


def _mentions(row: dict, words: list[str]) -> bool:
    """活动过滤词命中：标题/正文/话题任一命中（仅用于无 analysis_object 时切分活动）。

    提及漏斗不走这里，见 mention_funnel（主品牌名、大小写不敏感，FIX-18）。
    """
    if not words:
        return False
    hay = " ".join(str(row.get(k) or "") for k in ("title", "content"))
    kw = row.get("keywords") or []
    if isinstance(kw, list):
        hay += " " + " ".join(str(x) for x in kw)
    return any(w and w in hay for w in words)


def _campaign_stats(rows: list[dict], brand_name: str, product_words: list[str],
                    brand_avg_interaction: float | None = None,
                    funnel_method: str = mention_funnel.KEYWORD_MENTION,
                    content_scope: str = "all") -> dict:
    """单活动指标：只统计传入的活动相关行；指标名走白名单。

    brand_name 为主品牌名（漏斗 Tier2 只匹配主品牌名，原版 aggregate.py:51）。
    情感缺失行不计入情感分布与 NSR（容忍比例由 aggregate 校验，FIX-23）。
    """
    n = len(rows)
    # Q2：供应商互动量；缺失帖不计入总量/条均分母，单独披露 interaction_missing。
    inter = report_contract.interaction_stats(rows)
    interaction = inter["total"]
    pos = sum(1 for r in rows if _row_sentiment(r) == 1)
    neu = sum(1 for r in rows if _row_sentiment(r) == 0)
    neg = sum(1 for r in rows if _row_sentiment(r) == -1)
    # NSR 分母恒为「正面+负面」，不含中性（客户 KS_Metrics_Calculation v0.12 /
    # 错误案例 #13：含中性会把 NSR 严重稀释，历史事故 +57.6% 被算成 +15.2%）。
    nsr_denom = pos + neg
    # 提及漏斗（FIX-18）：Tier2=主品牌名、Tier3 嵌套于 Tier2、≥95% 退化，见 mention_funnel。
    funnel = mention_funnel.build(rows, method=funnel_method, brand_name=brand_name,
                                  product_words=product_words)

    trend: dict[str, int] = {}
    interaction_trend: dict[str, int] = {}
    platform_dist: dict[str, int] = {}
    kol_dist: dict[str, int] = {}
    kol_interactions: dict[str, int] = {}
    kol_interaction_n: dict[str, int] = {}
    all_level_dist: dict[str, int] = {}
    content_dist: dict[str, int] = {}
    ugc_dist: dict[str, int] = {}
    dimension_rows: dict[str, list[dict]] = {}
    trend_by_platform_type: dict[str, dict[str, dict[str, int]]] = {}
    interaction_by_platform_type: dict[str, dict[str, int]] = {}
    for r in rows:
        ymd = r.get("_publish_ymd")
        platform = r.get("_platform", "unknown")
        ctype = _CONTENT_TYPE.get(str(r.get("content_mode") or ""), "UKN")
        value = report_contract.interaction_value(r) or 0
        cell = interaction_by_platform_type.setdefault(platform, {})
        cell[ctype] = cell.get(ctype, 0) + value
        if ymd:
            trend[ymd] = trend.get(ymd, 0) + 1
            interaction_trend[ymd] = interaction_trend.get(ymd, 0) + value
            day = trend_by_platform_type.setdefault(ymd, {}).setdefault(platform, {})
            day[ctype] = day.get(ctype, 0) + 1
        platform_dist[r.get("_platform", "unknown")] = platform_dist.get(r.get("_platform", "unknown"), 0) + 1
        level = r.get("_kol_level", "unknown")
        all_level_dist[level] = all_level_dist.get(level, 0) + 1
        # 达人层级效能只统计活动相关 PGC 的头部/腰部/尾部（FIX-16）：
        # 排除素人与未知，分母是三层的帖子总数，不含 UGC。
        if (level in KOL_EFFECTIVE_LEVELS
                and _CONTENT_TYPE.get(str(r.get("content_mode") or ""), "UKN") == "PGC"):
            kol_dist[level] = kol_dist.get(level, 0) + 1
            if report_contract.interaction_value(r) is not None:
                kol_interactions[level] = (kol_interactions.get(level, 0)
                                           + report_contract.interaction_value(r))
                kol_interaction_n[level] = kol_interaction_n.get(level, 0) + 1
        ct = _CONTENT_TYPE.get(str(r.get("content_mode") or ""), "UKN")
        content_dist[ct] = content_dist.get(ct, 0) + 1
        category = str(r.get("ugc_category") or "").strip()
        if ct == "UGC" and category and category != "N/A":
            ugc_dist[category] = ugc_dist.get(category, 0) + 1
        dimension = str(r.get("discussion_dimension") or "").strip()
        if dimension and dimension != "N/A":
            dimension_rows.setdefault(dimension, []).append(r)

    # 主要讨论对象三分类（FIX-14）：键名按客户 Report 命名映射，
    # 禁止简称为「品牌NSR/产品NSR/活动NSR」。
    _DIM_KEY_MAP = {
        "品牌": "品牌主导讨论",
        "产品": "产品主导讨论",
        "活动": "活动流程与获取主导讨论",
    }
    dimension_nsr = {}
    for dim_key, dim_label in _DIM_KEY_MAP.items():
        group = dimension_rows.get(dim_key) or []
        gpos = sum(1 for row in group if _row_sentiment(row) == 1)
        gneu = sum(1 for row in group if _row_sentiment(row) == 0)
        gneg = sum(1 for row in group if _row_sentiment(row) == -1)
        # 分维 NSR：分母与门槛都按「正面+负面」，不按总行数
        # （客户错误案例 #35：KS 与脚本对中性占比高的维度结论相反）。
        gdenom = gpos + gneg
        enough = gdenom >= 30
        dimension_nsr[dim_label] = {
            "status": "available" if enough else "insufficient_sample",
            # sample_size 即门槛所用口径（正面+负面），row_count 保留该维度总行数。
            "sample_size": gdenom,
            "row_count": len(group),
            "positive": gpos,
            "negative": gneg,
            "neutral": gneu,
            "value": round((gpos - gneg) / gdenom, 4)
            if enough and gdenom else None,
        }
    # 三组覆盖之和（FIX-14）。原版 aggregate.py:161-164 的做法是打印警告并说明
    # 「分维度覆盖声量将偏低」，不报错 —— 这里同样只留痕交报告说明。
    dimension_nsr_total = sum(item["row_count"] for item in dimension_nsr.values())
    dimension_missing = max(n - dimension_nsr_total, 0)

    # 达人层级效能的分母：只算头部/腰部/尾部（FIX-16），不含素人、未知、不适用。
    kol_effective_total = sum(kol_dist.values())
    ugc_total = content_dist.get("UGC", 0)
    metrics = {
        "活动相关条数": n,
        "互动量": interaction,
        # 白名单指标：条均互动量 = 互动量 ÷ 活动相关条数（替代“人均”）
        "条均互动量": inter["avg"],
        # 品牌提及率 = Tier2/Tier1；产品提及占比 = Tier3/Tier2。无主品牌名、
        # 退化（Tier2 ≥95%）或主对象方法时不输出（原版 aggregate.py:187-199）。
        **mention_funnel.headline_metrics(funnel),
        # 条均互动倍数 = 活动条均互动量 ÷ 品牌条均互动量（KS_Metrics_Calculation:207）。
        # 分母取品牌期同一份数据的 互动量÷条数；品牌条均缺失或为 0 时不输出。
        "条均互动倍数": (
            round(inter["avg"] / brand_avg_interaction, 2)
            if inter["avg"] is not None and brand_avg_interaction else None
        ),
        # 品牌主导讨论占比 = 品牌主对象帖数 ÷ 活动相关总帖数（:204）。
        # 用 AI 主讨论对象口径，不是品牌名出现率（品牌提及率另列）。
        "品牌主导讨论占比": (
            round(len(dimension_rows.get("品牌", [])) / n, 4) if n else None
        ),
        # NSR = (正面-负面) ÷ (正面+负面)
        "NSR": (round((pos - neg) / nsr_denom, 4) if nsr_denom else None),
    }
    top_content = {kind.lower(): _top_rows(rows, kind) for kind in ("PGC", "UGC")}
    # 指标命名白名单硬校验：禁止出现“人均/转化/性价比”等措辞键。
    for key in metrics:
        for bad in FORBIDDEN_METRIC_TERMS:
            if bad in key:
                raise ValueError(f"aggregate: 指标名命中禁用措辞 '{bad}'：{key}")
    return {
        "row_count": n,
        "metrics": metrics,
        "sentiment_dist": {"positive": pos, "neutral": neu, "negative": neg},
        # FIX-34 / 检查清单 2.11：缺 AI 情感的行（≤5% 剔出分母）单独计数，供总数核对。
        "sentiment_missing": len(rows) - (pos + neu + neg),
        # 2.12：讨论对象分组计数（含未判定），加总须回到活动相关全量。
        "dimension_counts": {
            **{key: len(group) for key, group in dimension_rows.items()},
            "未判定": sum(1 for r in rows
                       if str(r.get("discussion_dimension") or "").strip() in ("", "N/A")),
        },
        "nsr_denominator": "正面+负面（不含中性）",
        "mention_funnel": funnel,
        "trend": dict(sorted(trend.items())),
        "interaction_trend": dict(sorted(interaction_trend.items())),
        # FIX-23：日期×平台×内容类型 / 平台×内容类型（也供 FIX-47 平台互动结构）。
        "trend_by_platform_type": dict(sorted(trend_by_platform_type.items())),
        # FIX-06：声量/互动高点及主要贡献帖，交独立 Validator 做高点相关性核查。
        "peak_evidence": noise_review.peak_evidence(rows),
        # FIX-47：2.N.1 结构结论与高点话术（确定性填空）。
        "overview": overview.campaign_overview(rows, content_scope),
        "interaction_by_platform_type": interaction_by_platform_type,
        "platform_dist": platform_dist,
        # 全量层级分布（含素人/未知/不适用）保留用于说明占比，不参与效能分母。
        "kol_level_dist": all_level_dist,
        "kol_level_labels": dict(KOL_LEVEL_LABELS),
        # 达人层级效能（FIX-16）：仅活动相关 PGC 的头部/腰部/尾部，分母为三层合计。
        "kol_effectiveness": {
            level: {
                "volume": count,
                "volume_share": round(count / kol_effective_total, 6)
                if kol_effective_total else None,
                "avg_interaction": round(
                    kol_interactions[level] / kol_interaction_n[level], 2
                ) if kol_interaction_n.get(level) else None,
            }
            for level, count in kol_dist.items()
        },
        "kol_effective_total": kol_effective_total,
        # Q2：供应商未返回互动量的帖数（不补零、不进互动 Top、不进条均分母）。
        "interaction_missing": inter["missing"],
        "kol_notes": {
            # UGC 内容以素人为主（占 XX%）—— 原版说明要求
            "ugc_amateur_share": (
                round(all_level_dist.get("ordinary", 0)
                      / ugc_total, 4) if ugc_total else None
            ),
            "unknown_share": (
                round(all_level_dist.get("unknown", 0) / n, 4) if n else None
            ),
            # 粉丝数据全部缺失 → 不出图，改灰色斜体固定说明
            "all_unknown": bool(n) and not kol_effective_total
            and all_level_dist.get("unknown", 0) >= n,
            "ugc_total": ugc_total,
        },
        "content_type_dist": content_dist,
        # FIX-26：带广告/推广标识的内容单列，区分自然声量；无标识列时 unknown 计数。
        "natural_vs_ad": _natural_vs_ad(rows),
        "ugc_category_dist": ugc_dist,
        "opinion_analysis": report_contract.opinion_analysis(rows, _row_sentiment),
        "ugc_analysis": report_contract.ugc_analysis(rows, _is_relevant),
        "dimension_nsr": dimension_nsr,
        # 主讨论对象三分类的覆盖情况（FIX-14）：三组 row_count 之和 vs 活动相关条数。
        "dimension_coverage": {
            "total": n,
            "covered": dimension_nsr_total,
            "missing": dimension_missing,
            "ratio": round(dimension_nsr_total / n, 4) if n else None,
            # 原版措辞：缺失/非法时提示先补标，分维覆盖声量偏低。
            "note": (
                f"{dimension_missing}/{n} 条活动相关内容的 discussion_dimension "
                "缺失/非法，分维度覆盖声量将偏低——请先补标再使用分组 NSR"
            ) if dimension_missing else None,
        },
        "top_content": top_content,
        # FIX-23 / FIX-49：热帖配方解构的输入就是热门榜单本身（原版 content_stats.py:5）。
        "content_formula_input": [
            {"content_mode": kind.upper(), **report_contract.formula_row(row)}
            for kind in ("pgc", "ugc") for row in top_content[kind]
        ],
    }


def _check_sentiment_coverage(relevant_rows: list[dict], warnings: list[str]) -> None:
    """活动相关行 AI 情感缺失/非法：≤5% 剔出情感分母并告警，>5% 报错（原版 aggregate.py:77-88）。

    不回退监测平台 sentiment；缺失行仍计入声量、互动等非情感指标。
    """
    total = len(relevant_rows)
    if not total:
        return
    missing = sum(1 for row in relevant_rows if _row_sentiment(row) is None)
    if not missing:
        return
    ratio = missing / total
    message = (f"活动相关内容中 {missing}/{total} 条（{ratio * 100:.1f}%）"
               "AI 情感缺失或不是 正面/中性/负面")
    if ratio > AI_SENTIMENT_MISSING_MAX:
        raise ValueError(f"aggregate: {message}，超过允许的 {AI_SENTIMENT_MISSING_MAX:.0%}；"
                         "请先完成/补跑轮次一情感标注（不使用监测平台原始情感）")
    warnings.append(message + "，这部分不计入情感分布与 NSR")


def _campaign_partitions(plan: dict, analysis_objects: list[str]) -> list[dict]:
    """活动切分：优先 Excel 权威字段 analysis_object；多活动禁止混池（原版 pick_campaign）。"""
    if analysis_objects:
        return [{"id": value, "analysis_object": value} for value in analysis_objects]
    campaigns = plan.get("campaigns") or []
    if len(campaigns) > 1 and not all(camp.get("filter_words") for camp in campaigns):
        raise ValueError("aggregate: 数据含多个活动但缺少『分析对象』或活动过滤词，"
                         "无法逐活动切分；不允许混池统计")
    return campaigns or [{"id": plan.get("activity_name") or "default", "filter_words": []}]


def _stats_scope(rows: list[dict], kept: list[dict], analysis_object: str | None,
                 filter_words: list[str], cleaned: dict) -> dict:
    """统计范围披露（FIX-21，原版 aggregate.py:89-92 stats_scope）。

    raw=监测检索原始（清洗输入）、noise_removed=清洗剔除、ai_irrelevant=轮次一判「否」、
    unlabeled=轮次一未标注、kept=活动相关（本报告统计口径）。
    """
    if analysis_object:
        scoped = [r for r in rows if str(r.get("analysis_object") or "").strip() == analysis_object]
    elif filter_words:
        scoped = [r for r in rows if _mentions(r, filter_words)]
    else:
        scoped = list(rows)
    record = (cleaned or {}).get("clean_record") or {}
    removed = record.get("removed") or {}
    if analysis_object:
        # 多活动：只有按该活动周期剔除的条数能归属到本活动（去重/过滤词按全表计）。
        noise_removed = int((record.get("period_dropped_by_campaign") or {}).get(analysis_object) or 0)
    elif filter_words:
        noise_removed = None
    else:
        noise_removed = sum(int(v or 0) for v in removed.values())
    ai_irrelevant = sum(1 for r in scoped if _is_relevant(r) is False)
    unlabeled = sum(1 for r in scoped if _is_relevant(r) is None) if any(
        _is_relevant(r) is not None for r in scoped) else 0
    # FIX-05：去噪剔除（Step5.5）与清洗剔除、AI 判否分开计数（KS_Noise_Review 出口核对）。
    campaign_key = analysis_object or "default"
    denoise_removed = int((((cleaned or {}).get("noise_by_campaign") or {}).get(campaign_key)) or 0)
    raw = len(scoped) + (noise_removed or 0) + denoise_removed
    ratio = ai_irrelevant / len(scoped) if scoped else 0.0
    return {
        "raw": raw,
        "noise_removed": noise_removed,
        "denoise_removed": denoise_removed,
        "ai_irrelevant": ai_irrelevant,
        "unlabeled": unlabeled,
        "kept": len(kept),
        "excluded": raw - len(kept),
        "ai_irrelevant_ratio": round(ratio, 4),
        # 原版告警：AI 判否占比 >10% 时先抽查被判否的内容（建议 30 条）再出报告。
        "irrelevant_review_required": ratio > AI_IRRELEVANT_WARN,
        "irrelevant_review_sample": AI_IRRELEVANT_REVIEW_SAMPLE,
    }


# FIX-24：赞助型活动（播客/节目/IP 合作）—— campaign_type 命中任一关键词即按赞助型处理
# （G1 选项「赛事赞助或合作」；KS_Classification_Rules 2.N.2「赞助型活动（播客/节目/IP合作）」）。
SPONSOR_TYPE_MARKERS = ("赞助", "播客", "节目")


def _is_sponsor_type(plan: dict) -> bool:
    """只看需求阶段已确认的 campaign_type（Q1：不从数据推断活动类型）。"""
    declared = str(plan.get("campaign_type") or "").strip()
    return any(marker in declared for marker in SPONSOR_TYPE_MARKERS)


def _sponsor_share(wordcloud_campaign: dict | None) -> dict:
    """品牌声量占比 B/A、产品声量占比 C/A（KS_Classification_Rules 2.N.2）。

    A/B/C 取 n13 同一时间/平台范围的三次 SSJH 查询 total_volume：
    overall=活动词、brand=活动词+品牌词、product=活动词+产品词。不额外取数。
    """
    dims = wordcloud_campaign or {}

    def volume(dim: str):
        source = (dims.get(dim) or {}).get("raw_source") or {}
        value = source.get("total_volume")
        return value if isinstance(value, (int, float)) and not source.get("degraded") else None

    a, b, c = volume("overall"), volume("brand"), volume("product")
    if not a:
        return {"status": "unavailable", "reason": "活动总声量（A）不可用",
                "activity_volume": a, "brand_volume": b, "product_volume": c,
                "brand_share": None, "product_share": None}
    return {"status": "available", "reason": None,
            "activity_volume": a, "brand_volume": b, "product_volume": c,
            "brand_share": round(b / a, 4) if b is not None else None,
            "product_share": round(c / a, 4) if c is not None else None}


# FIX-26：零粉丝重复内容的判定门槛（「零粉丝且大量重复」；FIX_PLAN 给定 ≥5 次）。
ZERO_FAN_DUPLICATE_MIN = 5


def _zero_fan_duplicates(rows: list[dict]) -> list[dict]:
    """按（作者、文案 hash）找出零粉丝且重复 ≥5 次的组；只列出，不删除。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row.get("fans") != 0:
            continue
        text = re.sub(r"\s+", "", f"{row.get('title') or ''}{row.get('content') or ''}")
        if not text:
            continue
        key = (str(row.get("author") or ""), hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
        groups.setdefault(key, []).append(row)
    return [{"author": author, "text_hash": digest, "count": len(items),
             "ids": [str(r.get("id") or r.get("_dedup_key") or "") for r in items],
             "status": "uncertain"}
            for (author, digest), items in groups.items()
            if len(items) >= ZERO_FAN_DUPLICATE_MIN]


def _merge_cloud_words(cloud_word: dict) -> tuple[dict, str | None]:
    """SSJH cloud_word {platform: {term: freq}} → 综合词表，按频次降序（同频保持首见顺序）。"""
    total = cloud_word.get("_total") if isinstance(cloud_word, dict) else None
    if isinstance(total, dict) and total:
        return dict(total), "ssjh_total"
    merged: dict[str, int] = {}
    for platform, words in (cloud_word or {}).items():
        if platform == "_total" or not isinstance(words, dict):
            continue
        for term, value in words.items():
            if isinstance(value, (int, float)):
                merged[term] = merged.get(term, 0) + value
    if not merged:
        return {}, None
    ordered = sorted(merged.items(), key=lambda kv: -kv[1])
    return dict(ordered), "sum_by_term_across_platforms"


def _natural_vs_ad(rows: list[dict]) -> dict:
    """FIX-26：自然/广告/未知计数。is_ad 兼容 Excel 布尔与 SSJH 的 0/1。"""
    counts = {"natural": 0, "ad": 0, "unknown": 0}
    for row in rows:
        flag = row.get("is_ad")
        if isinstance(flag, bool) or flag in (0, 1):
            counts["ad" if flag else "natural"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _top_rows(rows: list[dict], kind: str, limit: int = 10) -> list[dict]:
    """某内容类型按总互动量降序的 Top N（原版 content_stats.top_content）。"""
    pool = [row for row in rows
            if _CONTENT_TYPE.get(str(row.get("content_mode") or ""), "UKN") == kind]
    pool = [row for row in pool if report_contract.interaction_value(row) is not None]
    return sorted(pool, key=report_contract.interaction_value, reverse=True)[:limit]


def _voice_share(activity_n: int, brand_period: dict, start, end,
                 coverage_warnings: list) -> dict:
    """声量贡献参考 = 活动相关声量 / 品牌同期总声量（FIX-19）。

    原版口径（KS_Metrics_Calculation v0.15 / KS_Report_Template:491）：
      - 分母一律取第一章同一份 brand_stats 的逐日声量，按该活动自己的周期求和；
        不为单个活动另行取数（两次取数会让同一指标出现两个数值）。
      - 分母 < 分子时不输出，不得出现 >100%（LM #46）。
      - 品牌口径未覆盖活动口径时（FIX-08 coverage_warnings）也不输出。
    """
    s = str(start).replace("-", "")[:8] if start else ""
    e = str(end).replace("-", "")[:8] if end else ""
    out = {"value": None, "numerator": activity_n, "denominator": None,
           "period": {"start": s or None, "end": e or None},
           "source": "brand_stats.volume_trend", "reason": None}
    if coverage_warnings:
        out["reason"] = "品牌口径未覆盖活动口径"
        return out
    try:
        daily = charts.daily_totals((brand_period or {}).get("volume_trend") or {})
    except ValueError as exc:
        out["reason"] = f"品牌逐日声量格式无效：{exc}"
        return out
    if not daily:
        out["reason"] = "品牌逐日声量缺失"
        return out
    total = sum(v for day, v in daily.items() if (not s or day >= s) and (not e or day <= e))
    out["denominator"] = total
    if not activity_n:
        out["reason"] = "活动相关声量为 0"
    elif total < activity_n:
        out["reason"] = "分母小于分子（品牌同期总声量 < 活动相关声量）"
    else:
        out["value"] = round(activity_n / total, 4)
    return out


def _structure_config(
    opinion_labels: list[str] | None = None,
    ugc_categories: list[str] | None = None,
    ugc_not_applicable: bool = False,
    negative_labels: list[str] | None = None,
) -> dict:
    """构造结构预检的已批准类别配置（FIX-29，对齐原版 config 契约）。

    正负两套候选分别传入（FIX-31，原版按正负分别确认）；negative_labels 为 None
    时视为旧产物，两边共用 opinion_labels。UGC 仅 PGC 项目显式声明不适用。
    """
    labels = [str(x) for x in (opinion_labels or []) if str(x).strip()]
    negatives = ([str(x) for x in negative_labels if str(x).strip()]
                 if negative_labels is not None else labels)
    ugc = [str(x) for x in (ugc_categories or []) if str(x).strip()]
    # 原版 annot_r2.py:84/94 观点、UGC 均允许「其他」（占比由覆盖检查管控），结构门同口径。
    other = annotate_mod.OPINION_OTHER
    return {
        "positive_labels": labels + [other] if labels else [],
        "negative_labels": negatives + [other] if negatives else [],
        # 某方向无候选（原版 status!=ok，<10 条）→ 该方向观点填「无」，不检类别。
        "positive_applicable": bool(labels),
        "negative_applicable": bool(negatives),
        # 仅 PGC 项目：ugc_applicable=False，预检要求相关行一律 N/A。
        "ugc_labels": ugc + [annotate_mod.UGC_OTHER] if ugc else [],
        "ugc_applicable": not ugc_not_applicable,
    }


def _apply_structure_gate(
    result: dict, *, stage: str, config: dict, mode: str, label: str
) -> dict:
    """跑结构预检并按开关处理（FIX-29）。

    mode=block（默认）：errors 非空抛 ValueError，交引擎 retry/pause；
    mode=warn：只把报告写进产物，不阻断（用于历史 Run 过渡）。
    产物始终写 structure_check，供 FIX-30 审核与 Validator 第 7 项核对。
    """
    report = annotation_structure_mod.check_structure(
        result.get("rows") or [], config=config, stage=stage)
    result[f"structure_check_{stage}"] = report
    if report["status"] != "pass" and mode == "block":
        sample = report["errors"][:5]
        raise ValueError(
            f"annotate {label}: 结构预检未通过（{report['error_count']} 条）"
            f"reasons={report['errors_by_reason']} sample={sample}")
    return report


def _calibration_samples(review: dict, rows: list[dict]) -> list[dict]:
    """从审核结果里取判错样例，供重标时作为 few-shot 校准（FIX-30）。

    只使用带 expected 的逐帖判定；没有判定明细时返回空（不注入、不臆造正确值）。
    """
    verdicts = review.get("verdicts") if isinstance(review, dict) else None
    if not isinstance(verdicts, list):
        return []
    by_id = {str(r.get("_annot_id") or ""): r for r in rows}
    samples = []
    for item in verdicts:
        if not isinstance(item, dict) or str(item.get("verdict")) != "wrong":
            continue
        row = by_id.get(str(item.get("id") or ""))
        expected = str(item.get("expected") or "").strip()
        if row is None or not expected:
            continue
        field = str(item.get("field") or "判定")
        samples.append({
            "content": str(row.get("title") or "")[:60] + " " +
                       str(row.get("content") or "")[:60],
            "field": field,
            "wrong": str(row.get(field) or item.get("wrong") or "").strip(),
            "correct": expected,
        })
    return samples


def _history_by_stage(history) -> dict[str, list[str]]:
    """已审台账按轮次拆分（执行契约 §2：每轮各 500 额度）；旧格式只算轮次一。"""
    if not isinstance(history, dict):
        return {}
    by_stage = {k: list(v or []) for k, v in (history.get("by_stage") or {}).items()}
    if history.get("reviewed_ids") and not by_stage:
        by_stage["round1"] = list(history["reviewed_ids"])
    return by_stage


def _stage_reviewed_ids(history, stage: str) -> list[str]:
    return list(_history_by_stage(history).get(stage) or [])


def _rerun_base_rows(state, plan: dict, rows: list[dict]) -> list[dict]:
    """增量重标：以 rework 保存的上一版标注为底（按 _annot_id 对齐），找不到时用源行。"""
    ref = plan.get("base_artifact") if isinstance(plan, dict) else None
    path = None
    if isinstance(ref, dict):
        key, version = ref.get("key"), ref.get("version")
        meta = state.artifacts.get(key) or {}
        candidate = state.run_dir / "artifacts" / str(key) / f"v{version}" / "payload.json"
        path = candidate if candidate.exists() else (
            Path(meta["path"]) if meta.get("version") == version and meta.get("path") else None)
    if path is None or not Path(path).exists():
        return rows
    previous = json.loads(Path(path).read_text(encoding="utf-8"))
    prev_rows = previous.get("rows") if isinstance(previous, dict) else None
    if not prev_rows:
        return rows
    annotate_mod._tag_ids(rows)
    by_id = {str(r.get("_annot_id")): r for r in prev_rows if r.get("_annot_id")}
    return [dict(by_id.get(str(r.get("_annot_id")), r)) for r in rows]


def _rerun_scope(state, stage: str, review_key: str, rows: list[dict]):
    """读取重标计划，返回 (rerun_ids, calibration)。

    仅在 rework 节点的产物存在且阶段匹配时启用增量重标；
    否则返回 (None, []) 即全量标注。
    """
    plan = state.read_artifact("annotation_rework_plan") or {}
    if not isinstance(plan, dict) or plan.get("stage") != stage:
        return None, []
    rerun_ids = plan.get("rerun_ids") or None
    if plan.get("scope") == "all":
        rerun_ids = None
    review = state.read_artifact(review_key) or {}
    return rerun_ids, _calibration_samples(review, rows)


# ── Step5.5 去噪审核（FIX-05，原版 KS_Noise_Review）─────────────────────────
NOISE_BATCH = 15
NOISE_EXPANSION_ROUNDS = 2
NOISE_SYSTEM = (
    "你是营销活动去噪审核员。只判断内容是否属于本次活动分析范围，不做情感或相关性标注。\n"
    "先读给出的「前一句+关键词当句+后一句」窗口，不足时再读标题、全文与识别文本。\n"
    "判定表：同名、词义撞车，正文讨论其他对象→noise；正文与本次活动无关、只为引流挂活动话题→noise；"
    "其他期次、其他活动或只有品牌日常内容且不在本次活动范围→noise；属于用户已明确排除的范围→noise；"
    "真实参与、介绍、评价本次活动（包括投诉、差评）→keep；赞助型活动中讨论被赞助节目/IP且符合范围→keep；"
    "仅标签可见、正文缺失或纯图片、证据不足→uncertain（证据不足不等于噪音）。\n"
    "噪音指无关传播，不指负面情绪。识别文本可能错字或误识别，与正文冲突时不单凭识别文本判噪音。\n"
    '只输出 JSON：{"decisions":[{"id":"…","verdict":"keep|noise|uncertain","branch":"命中分支或词",'
    '"evidence_field":"title|content|video_cover_text|audio_text|video_text","reason":"一句理由"}]}'
)


def _noise_judge(rows: list[dict], context: dict, terms: list[str]) -> list[dict]:
    decisions: list[dict] = []
    for start in range(0, len(rows), NOISE_BATCH):
        batch = rows[start:start + NOISE_BATCH]
        payload = [{"id": noise_review.row_id(r), "title": r.get("title") or "",
                    "content": r.get("content") or "",
                    "windows": noise_review.windows(r, terms),
                    **{f: r.get(f) for f in ("video_cover_text", "audio_text", "video_text") if r.get(f)}}
                   for r in batch]
        expected = {item["id"] for item in payload}
        user = (f"活动：{context['campaign_name']}\n活动边界：{context['campaign_context']}\n"
                f"活动取数关键词：{context['activity_keywords']}\n待审内容：\n"
                + json.dumps(payload, ensure_ascii=False))
        for _ in range(semantic.CHAT_JSON_ATTEMPTS):
            data = semantic.chat_json(NOISE_SYSTEM, user)
            got = [d for d in (data or {}).get("decisions") or []
                   if isinstance(d, dict) and d.get("id") in expected and d.get("verdict") in noise_review.VERDICTS]
            if {d["id"] for d in got} == expected:
                window_of = {item["id"]: item["windows"] for item in payload}
                decisions += [{**d, "windows": window_of[d["id"]], "source": "producer"} for d in got]
                break
            user += "\n上一轮漏判或结论无效，请对全部 id 给出 keep/noise/uncertain。"
        else:
            raise RuntimeError("noise_review: 去噪判定连续三次未覆盖全部待审内容")
    return decisions


@handler("noise_review")
def noise_review_step(node, state, ctx):
    """逐活动去噪：选样→窗口→语义判定→扩查→剔除→出口核查。噪音默认剔除，不新增 HC。"""
    if not semantic.llm_available():
        raise RuntimeError("noise_review: 缺少语义能力，禁止跳过去噪")
    cleaned = state.read_artifact("cleaned_data") or {}
    rows = list(cleaned.get("rows") or [])
    plan = state.read_artifact("confirmed_plan") or {}
    context = _r1_context_from_plan(plan)
    if not context["campaign_name"] or not context["activity_keywords"]:
        raise RuntimeError("noise_review: 缺活动名或活动取数关键词，无法定位命中句")
    terms = [t for t in re.split(r"[|+()（）\s]+", context["activity_keywords"]) if t]
    validator_noise = set(map(str, (state.params.get("qc_noise_ids") or [])))
    previous = {d["id"]: d for d in ((state.read_artifact("noise_review") or {}).get("decisions") or [])}
    by_id = {noise_review.row_id(r): r for r in rows}
    review = {"campaigns": {}, "decisions": [], "removed_ids": [], "uncertain_ids": []}
    for cid, group in _rows_by_campaign(rows).items():
        sample = noise_review.select_sample(group)
        pending = list(dict.fromkeys(i for ids in sample.values() for i in ids))
        decided: dict[str, dict] = {}
        for _ in range(NOISE_EXPANSION_ROUNDS + 1):
            todo = [i for i in pending if i not in decided]
            fresh = []
            for rid in todo:
                if rid in validator_noise:          # FIX-06：Validator 高点核查确认的噪音
                    decided[rid] = {"id": rid, "verdict": "noise", "source": "validator",
                                    "reason": "独立 Validator 高点核查判定为噪音"}
                elif rid in previous:               # 增量：沿用上次结论，不重复调用模型
                    decided[rid] = previous[rid]
                else:
                    fresh.append(by_id[rid])
            for decision in _noise_judge(fresh, context, terms):
                decided[decision["id"]] = decision
            noise = [d for d in decided.values() if d["verdict"] == "noise"]
            extra = noise_review.expansion(group, noise, set(decided))
            if not extra:
                break
            pending += extra
        removed = [rid for rid, d in decided.items() if d["verdict"] == "noise"]
        review["campaigns"][cid] = {"sampled": sample, "reviewed": len(decided),
                                    "removed": len(removed),
                                    "removed_interaction": sum(report_contract.interaction_value(by_id[r]) or 0
                                                               for r in removed)}
        review["decisions"] += list(decided.values())
        review["removed_ids"] += removed
        review["uncertain_ids"] += [rid for rid, d in decided.items() if d["verdict"] == "uncertain"]
    removed_set = set(review["removed_ids"])
    kept = [r for r in rows if noise_review.row_id(r) not in removed_set]
    noise_review.exit_check({d["id"] for d in review["decisions"]}, review["decisions"],
                            {noise_review.row_id(r) for r in kept})
    denoised = {**cleaned, "rows": kept,
                "noise_record": {"cleaned": len(rows), "removed": len(removed_set), "kept": len(kept)},
                "noise_by_campaign": {cid: info["removed"] for cid, info in review["campaigns"].items()}}
    return {"artifacts": {"denoised_data": denoised, "noise_review": review}}


def _r1_context_from_plan(plan: dict) -> dict:
    """从 confirmed_plan 构造轮次一标注上下文（FIX-27）。

    三个必填项都来自需求确认单，缺任一项由 annotate 侧 fail-closed 抛错：
      - campaign_name：活动名（多活动时取活动列表拼成可读说明）；
      - campaign_context：活动边界（活动边界 / 实际举办期 / 活动类型）；
      - activity_keywords：活动取数表达式（保留 OR/AND 分支关系）。
    """
    plan = plan or {}
    campaigns = [c for c in (plan.get("campaigns") or []) if isinstance(c, dict)]
    name = str(plan.get("activity_name") or "").strip()
    if not name and campaigns:
        name = "、".join(str(c.get("name") or c.get("id") or "") for c in campaigns)

    context_parts = []
    for label, key in (("活动边界", "activity_boundary"),
                       ("实际举办期", "actual_event_period"),
                       ("活动类型", "campaign_type")):
        value = plan.get(key)
        if isinstance(value, dict):
            value = value.get("start") and f"{value.get('start')} ~ {value.get('end')}"
        text = str(value or "").strip()
        if text and text != "待数据推断":
            context_parts.append(f"{label}：{text}")
    window = (f"{plan.get('period_start')} ~ {plan.get('period_end')}"
              if plan.get("period_start") and plan.get("period_end") else "")
    if window:
        context_parts.append(f"统计窗口：{window}")

    keywords = str(plan.get("activity_query") or "").strip()
    return {
        "campaign_name": name,
        "campaign_context": "；".join(context_parts),
        "activity_keywords": keywords,
        "campaign_type": (str(plan.get("campaign_type") or "").strip()
                          if str(plan.get("campaign_type") or "") != "待数据推断" else ""),
    }


@handler("aggregate")
def aggregate(node, state, ctx):
    """分对象统计聚合（本地确定性，无副作用）。

    口径（源：飞书 wiki + knowledge/_字段速查）：
      - 逐活动独立统计，**不相加为合计声量**（多活动 campaigns 各算）。
      - 活动层所有指标**只统计活动相关**内容（relevance=是），分母=活动相关条数。
      - NSR 分母恒为「正面+负面」，不含中性（客户 KS_Metrics_Calculation v0.12）；
        分维 NSR 的样本门槛同样按「正面+负面合计 < 30」。无参数开关。
      - 指标命名白名单（条均互动量/品牌提及率/产品提及占比），禁用人均/转化/性价比。
      - 情感以 AI 标注为准；活动相关行缺 AI 情感 → 直接报错，不兜底。
    """
    annotated = state.read_artifact("annotated_data") or {}
    cleaned = state.read_artifact("denoised_data") or {}
    plan = state.read_artifact("confirmed_plan") or {}
    brand_period = state.read_artifact("brand_stats") or {}
    baseline = state.read_artifact("brand_stats_baseline") or {}

    # 行源只认标注产物（FIX-23 / FIX_PLAN 0.3：新产物缺失 fail-closed，不回退清洗 rows）。
    rows = list((annotated or {}).get("rows") or []) if isinstance(annotated, dict) else []

    # 活动相关过滤（原版 aggregate.py:62-74）：只统计 relevance=是；未标注行排除并告警；
    # 活动相关为 0 条时无法统计，直接报错。
    relevance_vals = [_is_relevant(r) for r in rows]
    relevant_rows = [r for r, v in zip(rows, relevance_vals) if v]
    if not relevant_rows:
        raise ValueError("aggregate: 活动相关内容为 0 条，无法统计"
                         "（检查轮次一『是否活动相关』是否已标注）")
    warnings: list[str] = []
    unlabeled = sum(1 for v in relevance_vals if v is None)
    if unlabeled:
        warnings.append(f"{unlabeled}/{len(rows)} 行『是否活动相关』缺失（轮次一未标注？），"
                        "已排除出统计")
    _check_sentiment_coverage(relevant_rows, warnings)

    analysis_objects = sorted({
        str(row.get("analysis_object") or "").strip()
        for row in relevant_rows
        if str(row.get("analysis_object") or "").strip()
    })
    campaigns = _campaign_partitions(plan, analysis_objects)
    # 漏斗 Tier2 只匹配主品牌名（G1 必填 brand），不用 brand_words 别名集合（原版 aggregate.py:51）。
    brand_name = str(plan.get("brand") or "").strip()
    product_words = plan.get("product_words") or []
    funnel_method = str(plan.get("funnel_method") or mention_funnel.KEYWORD_MENTION)
    if funnel_method not in mention_funnel.METHODS:
        raise ValueError(f"aggregate: 未知的提及漏斗方法 {funnel_method!r}")

    # 品牌条均互动量：同一份 brand_stats 的 互动量 ÷ 条数（FIX-04 / :207 分母口径）。
    _bp_volume = brand_period.get("total_volume") if isinstance(brand_period, dict) else None
    _bp_interaction = brand_period.get("total_interaction") if isinstance(brand_period, dict) else None
    brand_avg_interaction = (
        _bp_interaction / _bp_volume
        if isinstance(_bp_interaction, (int, float)) and isinstance(_bp_volume, (int, float))
        and _bp_volume > 0 else None
    )
    per_campaign: dict[str, dict] = {}
    for camp in campaigns:
        cid = camp.get("id", "default")
        analysis_object = camp.get("analysis_object")
        fw = camp.get("filter_words") or []
        if analysis_object:
            crows = [
                row for row in relevant_rows
                if str(row.get("analysis_object") or "").strip() == analysis_object
            ]
        else:
            crows = [r for r in relevant_rows if _mentions(r, fw)] if fw else list(relevant_rows)
        per_campaign[cid] = _campaign_stats(
            crows, brand_name, product_words, brand_avg_interaction, funnel_method,
            str(plan.get("content_scope") or "all").lower())
        per_campaign[cid]["stats_scope"] = _stats_scope(
            rows, crows, analysis_object, fw, cleaned)
        per_campaign[cid]["voice_share"] = _voice_share(
            len(crows), brand_period,
            camp.get("period_start") or plan.get("period_start"),
            camp.get("period_end") or plan.get("period_end"),
            plan.get("coverage_warnings") or [],
        )
        per_campaign[cid]["metrics"]["声量贡献参考"] = per_campaign[cid]["voice_share"]["value"]
        # FIX-61：逐模块范围记录（需求指纹/对象/周期/平台/方法/分母/数据源）。
        module_scope.attach(
            per_campaign[cid], campaign=cid, plan=plan,
            requirement=state.read_artifact("requirement_confirmed") or {},
            period=(camp.get("period_start") or plan.get("period_start"),
                    camp.get("period_end") or plan.get("period_end")),
            source_refs=[state.artifact_ref("denoised_data"), state.artifact_ref("annotated_data")])

    baseline_meta = baseline.get("meta") if isinstance(baseline, dict) else {}
    baseline_unavailable = bool(
        isinstance(baseline, dict)
        and (baseline.get("unavailable") or (baseline_meta or {}).get("unavailable"))
    )
    data_issues = []
    # 轮次二观点「其他」超门槛（annotate r2 记的 blocked_quality）：不降门槛、不强行归类，
    # 但把受限口径显式记成 DataIssue，报告数据说明区据此披露（原版 QC 协议:219）。
    for item in (annotated.get("blocked_quality") or [] if isinstance(annotated, dict) else []):
        if not isinstance(item, dict):
            continue
        data_issues.append({
            "id": f"opinion-quality-{item.get('field')}-{state.run_id}",
            "source": "annotation",
            "affected_module": "sentiment_opinion",
            "severity": "module_degraded",
            "reason": (f"观点「空值+其他」{item.get('other')}/{item.get('scope')}"
                       f"（{float(item.get('ratio') or 0):.1%}）未低于 10% 门槛"),
            "action": "disclose opinion coverage limitation in the opinion section",
        })
    if baseline_unavailable:
        data_issues.append({
            "id": f"baseline-unavailable-{state.run_id}",
            "source": "controlled-stat-tool",
            "affected_module": "baseline_lift",
            "severity": "module_degraded",
            "reason": (baseline_meta or {}).get("data_issue")
            or "brand baseline unavailable",
            "action": "suppress baseline comparison and disclose limitation",
        })

    # Q1：不再按旧6类矩阵推断 campaign_type；只给传播特征参考（不改变已确认活动类型）。
    propagation = propagation_features.propagation_features(
        relevant_rows, plan.get("brand_words") or ([brand_name] if brand_name else []))
    if _is_sponsor_type(plan):
        wordcloud = state.read_artifact("wordcloud_data") or {}
        for cid, bundle in per_campaign.items():
            bundle["sponsor_share"] = _sponsor_share((wordcloud.get("campaigns") or {}).get(cid))
            if bundle["sponsor_share"]["status"] != "available":
                data_issues.append({
                    "id": f"sponsor-share-unavailable-{cid}-{state.run_id}",
                    "source": "controlled-stat-tool", "affected_module": "sponsor_share",
                    "severity": "module_degraded",
                    "reason": bundle["sponsor_share"]["reason"],
                    "action": "omit sponsor share table and disclose limitation",
                })

    stats_bundle = {
        "schema_version": "1.1",
        # Preserve the controlled-stat-tool brand aggregate verbatim. It contains
        # daily volume/interaction, platform/content splits, sentiment, word
        # clouds and hot-content rows required by report sections 1.1-1.4.
        "brand": {
            "source": brand_period,
            "total_volume": brand_period.get("total_volume"),
            "total_interaction": brand_period.get("total_interaction"),
            "sentiment_dist": brand_period.get("sentiment_dist") or {},
            "platform_dist": brand_period.get("data_type") or {},
            "content_type_dist": brand_period.get("volume_type") or {},
            "trend": brand_period.get("volume_trend") or {},
            "interaction_trend": brand_period.get("interaction_trend") or {},
            # FIX-48：1.1 趋势图连续展示基准期+活动期。
            "trend_two_period": charts.two_period_trend(
                brand_period, baseline if isinstance(baseline, dict) else None),
            "wordcloud": brand_period.get("cloud_word") or {},
            "wordcloud_rows": brand_period.get("_wordcloud_tables") or {},
            "hot_content": brand_period.get("articles") or {},
            "top_content": report_contract.top_content(_iter_ssjh_rows(brand_period)),
            "peak_evidence": noise_review.peak_evidence(
                [{**r, "_publish_ymd": _publish_ymd(r)} for r in _iter_ssjh_rows(brand_period)]),
            "overview": overview.brand_overview(
                brand_period, charts.daily_totals, str(plan.get("content_scope") or "all").lower(),
                _publish_ymd),
            "degraded": bool(brand_period.get("degraded")),
        },
        "campaigns": per_campaign,
        # Q1：活动主类型沿用需求确认结果；传播特征仅作解释参考（待核实，不判类型）。
        "campaign_type": str(plan.get("campaign_type") or "").strip(),
        "propagation_features": propagation,
        "campaign_goal": list(plan.get("campaign_goal") or ["待数据推断"]),
        "warnings": warnings,
        "metric_whitelist": list(METRIC_WHITELIST),
        "baseline": (
            {"status": "unavailable", "total_volume": None,
             "total_interaction": None, "data_issue_id": data_issues[0]["id"]}
            if baseline_unavailable else
            ({"status": "available", "total_volume": baseline.get("total_volume"),
              "total_interaction": baseline.get("total_interaction")}
             if baseline else None)
        ),
        "baseline_lift": (
            lift.from_bundles(brand_period, baseline)
            if baseline and not baseline_unavailable else
            {"status": "unavailable", "value": None}
        ),
        "data_issues": data_issues,
        "reconciliation": {
            "relevant_rows": len(relevant_rows),
            "campaign_rows": {
                key: value.get("row_count", 0)
                for key, value in per_campaign.items()
            },
            "analysis_object_source": bool(analysis_objects),
        },
        "clean_record": cleaned.get("clean_record") if isinstance(cleaned, dict) else None,
    }
    return {"artifacts": {
        "stats_bundle": stats_bundle,
        "data_issues": {"issues": data_issues},
    }}


def merge_amendments(fix: dict, revision_id, report_version, validated: bool) -> list[dict]:
    """把进行中的交接后修正（pending）落为一条 amendment，接在历史记录之后。"""
    amendments = list((fix or {}).get("history") or [])
    if (fix or {}).get("pending"):
        amendments.append({**fix["pending"], "to_revision_id": revision_id,
                           "to_report_version": report_version, "validated": validated})
    return amendments


@handler("handoff")
def handoff(node, state, ctx):
    """交回 Supervisor 闭环（n24_handoff）：复读 revision_id → 校验版本链一致 → 写 handoff.json。

    消费 delivery_record + task_result（+ feishu_doc / user_confirmation）。
    校验（validate_handoff）：
      - 交付记录已提交、发布后检查通过；
      - 复读飞书文档当前 revision_id：若与交付绑定的 revision 不一致 → 标记 revision_changed（作废告警）；
      - 用户确认版本与报告版本一致（版本链对齐，指向同一 revision 而非 latest）。
    真路径复读失败/降级时以交付记录中的 revision 为准，validated 仍可成立（degraded 透传）。
    side_effect=none：本节点只读校验 + 写闭环产物，不产生对外副作用。
    """
    delivery_record = state.read_artifact("delivery_record") or {}
    task_result = state.read_artifact("task_result") or {}
    feishu_doc = state.read_artifact("feishu_doc") or {}
    receipts = state.read_artifact("external_receipts") or {}
    user_conf = state.read_artifact("user_confirmation") or {}

    doc_token = (feishu_doc.get("doc_token") if isinstance(feishu_doc, dict) else None) \
        or (receipts.get("doc_token") if isinstance(receipts, dict) else None)
    bound_rev = (feishu_doc.get("revision_id") if isinstance(feishu_doc, dict) else None)
    if bound_rev is None and isinstance(receipts, dict):
        bound_rev = receipts.get("revision_id")
    report_version = (receipts.get("report_version") if isinstance(receipts, dict) else None) \
        or (feishu_doc.get("report_version") if isinstance(feishu_doc, dict) else None)
    degraded = bool((feishu_doc or {}).get("degraded") or (receipts or {}).get("degraded"))

    # 真交付必须实时复读；None 表示无法验证，不得等同于“未变化”。
    live_rev = feishu.current_revision(doc_token) if not degraded else None
    revision_changed = bool(live_rev is not None and bound_rev is not None
                            and live_rev != bound_rev)
    live_revision_verified = bool(
        live_rev is not None and bound_rev is not None and live_rev == bound_rev
    )
    current_delivery_ref = state.artifact_ref("delivery_draft")

    checks = {
        "committed": bool(delivery_record.get("committed")),
        "post_publish_checks_passed":
            delivery_record.get("post_publish_checks") in ("passed", True, "pass"),
        "revision_bound": bound_rev is not None,
        "live_revision_verified": live_revision_verified,
        "revision_changed": revision_changed,
        "ownership_transferred": bool(delivery_record.get("ownership_transferred")),
        "collaborator_added": bool(delivery_record.get("collaborator_added")),
        "delivery_access_granted": bool(
            delivery_record.get("delivery_access_granted")
        ),
        "link_accessible": bool(delivery_record.get("link_accessible")),
        "version_chain_aligned": bool(report_version) and (
            isinstance(user_conf, dict)
            and user_conf.get("status") == "confirmed"
            and user_conf.get("report_version") == report_version
            and user_conf.get("revision_id") == bound_rev
            and user_conf.get("report_ref") == delivery_record.get("report_ref")
            and user_conf.get("delivery_ref") == current_delivery_ref
            and delivery_record.get("delivery_ref") == current_delivery_ref
        ),
        "validation_aligned": (
            delivery_record.get("validation_ref") == state.artifact_ref("qc_report")
            and delivery_record.get("report_ref") == state.artifact_ref("report_md")
        ),
        "degraded": degraded,
    }
    # 校验门：已提交 + 发布后检查过 + revision 已绑定且未被改动 + 版本链对齐。
    validated = (checks["committed"] and checks["post_publish_checks_passed"]
                 and checks["revision_bound"] and checks["live_revision_verified"]
                 and not checks["revision_changed"]
                 and checks["delivery_access_granted"] and checks["link_accessible"]
                 and checks["version_chain_aligned"]
                 and checks["validation_aligned"] and not degraded)
    handoff_payload = {
        "validated": validated,
        "checks": checks,
        "doc_token": doc_token,
        "bound_revision_id": bound_rev,
        "live_revision_id": live_rev,
        "report_version": report_version,
        "task_result": task_result,
        "user_confirmation": user_conf,
        "degraded": degraded,
        "terminal_accepted": validated,
    }
    # FIX-38：交接后修正——沿用原文档，追加 amendments[]（修正前后 revision 可追溯）。
    fix = state.read_artifact("post_handoff_fix") or {}
    amendments = merge_amendments(fix, bound_rev, report_version, validated)
    if amendments:
        handoff_payload["amendments"] = amendments
    # 落 handoff.json 到 run_dir/delivery（闭环证据，供 Gateway/Supervisor 复核）。
    try:
        hp = state.run_dir / "delivery" / "handoff.json"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps(handoff_payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except OSError as error:   # 闭环证据写盘失败：留痕，产物仍以 artifact 形式保存
        state.event(node.get("id", "n24_handoff"), "handoff_file_write_failed", str(error)[:300])
    out = {"handoff": handoff_payload}
    if fix.get("pending"):
        out["post_handoff_fix"] = {"pending": None, "history": amendments}
    return {"artifacts": out}


# ── tool 节点（外部副作用，接真：SSJH 取数 + 确定性降级）──────────
@handler("ssjh_pull")
def ssjh_pull(node, state, ctx):
    """SSJH（数说聚合）社媒统计取数。按 node.outputs 区分三种拉取语义：

      - n05_brand_pull     → brand_stats          （品牌活动期聚合，供 n07_clean 消费 articles）
      - n06_brand_baseline → brand_stats_baseline （基准期=活动前30天不重叠，供 n12_aggregate）
      - n13_wordcloud_pull → wordcloud_data        （词云，供 n14_charts / n15_wordcloud_insight）

    真路径调仓内 SSJH skill.run()；无凭据/失败降级到确定性 fallback（出口 schema 真实，
    带 degraded=True）。基准期(n06)与词云(n13)在 DAG 里 on_fail=exception_bypass，
    降级产物保证下游 join 不断链。
    """
    out = node.get("outputs", [])
    plan = state.read_artifact("confirmed_plan") or {}
    if replay.enabled():
        # 测试回放：复用录制 Run 的数说取数结果（已核对取数参数），不调用外部接口。
        payloads = {key: replay.load(state.run_dir, key, plan) for key in out}
        state.event(node["id"], "replayed", ",".join(out))
        return {"artifacts": payloads}
    baseline = "brand_stats_baseline" in out
    wordcloud = "wordcloud_data" in out
    # FIX-03：content_scope 必须贯通到 SSJH 的 content_mode（ALL/UGC/PGC），
    # 否则用户选「仅 UGC」时第一章仍统计全部内容，与第二章口径不一致。
    content_mode = CONTENT_MODE_BY_SCOPE.get(
        str(plan.get("content_scope") or "all").lower(), "ALL"
    )

    if wordcloud:
        campaigns = plan.get("campaigns") or [{
            "id": plan.get("activity_name") or "default",
            "filter_words": [],
        }]
        dimensions: dict[str, dict] = {}
        degraded_any = False
        for campaign in campaigns:
            cid = str(campaign.get("id") or "default")
            local_words = [
                str(word).strip()
                for word in (campaign.get("filter_words") or [])
                if str(word).strip()
            ]
            local_query = (
                campaign.get("activity_query")
                or campaign.get("query")
                or ("(" + "|".join(local_words) + ")" if local_words else None)
            )
            if len(campaigns) > 1 and not local_query:
                raise ValueError(
                    f"ssjh_pull: 多活动 {cid} 缺少活动级 query/filter_words"
                )
            activity_query = str(
                local_query
                or plan.get("activity_query")
                or cid
            )
            query_map = {
                "overall": activity_query,
                "brand": f"({activity_query})+({plan.get('brand_query') or plan.get('brand') or ''})",
            }
            if plan.get("product_query"):
                query_map["product"] = (
                    f"({activity_query})+({plan.get('product_query')})"
                )
            campaign_result: dict[str, dict] = {}
            for dimension, query in query_map.items():
                scoped_plan = {
                    **plan,
                    # FIX-01：brand_query 优先级最高，必须一并覆盖，
                    # 否则词云维度会退化成品牌整式查询。
                    "brand_query": query,
                    "brand_words": [query],
                    "keywords": [query],
                }
                try:
                    result = (
                        ssjh.pull(
                            scoped_plan,
                            baseline=False,
                            wordcloud=True,
                            filterword=str(plan.get("activity_filter") or ""),
                            content_mode=content_mode,
                        )
                        if ssjh.api_available()
                        else ssjh.fallback(scoped_plan, baseline=False)
                    )
                except Exception:
                    result = ssjh.fallback(scoped_plan, baseline=False)
                degraded = bool(result.get("degraded"))
                degraded_any = degraded_any or degraded
                # FIX-54：第二章活动词云综合已选平台——优先用 SSJH 的 _total，
                # 否则对各平台同词频次求和（对 SSJH 返回值的确定性合并，不本地重计）。
                merged_words, merge_method = _merge_cloud_words(result.get("cloud_word") or {})
                campaign_result[dimension] = {
                    "query": query,
                    "platforms": {"all": merged_words} if merged_words else {},
                    "platforms_by_source": result.get("cloud_word") or {},
                    "merge": merge_method,
                    "word_rows": {},
                    "raw_source": result,
                    "evidence_rows": [
                        {
                            "id": row.get("id") or row.get("item_id"),
                            "platform": row.get("_platform") or row.get("site_name"),
                            "title": row.get("title"),
                            "content": str(row.get("content") or ""),
                            "url": row.get("url"),
                        }
                        for row in _iter_ssjh_rows(result)[:100]
                    ],
                    "degraded": degraded,
                    "not_applicable": False,
                }
            if "product" not in query_map:
                campaign_result["product"] = {
                    "query": None,
                    "platforms": {},
                    "degraded": False,
                    "not_applicable": True,
                }
            dimensions[cid] = campaign_result
        if degraded_any:
            raise RuntimeError(
                "ssjh_pull: 逐活动/整体/品牌/产品词云取数不完整"
            )
        return {"artifacts": {"wordcloud_data": {
            "campaigns": dimensions,
            "degraded": degraded_any,
            "query_contract": "campaign x overall/brand/product",
        }}}

    def _get() -> dict:
        if ssjh.api_available():
            try:
                return ssjh.pull(
                    plan,
                    baseline=baseline,
                    wordcloud=wordcloud,
                    filterword=str(plan.get("brand_filter") or ""),
                    content_mode=content_mode,
                )
            except Exception:
                return ssjh.fallback(plan, baseline=baseline)
        return ssjh.fallback(plan, baseline=baseline)

    payload = _get()
    if payload.get("degraded"):
        if not baseline and not wordcloud:
            raise RuntimeError("ssjh_pull: 品牌活动期取数失败，禁止使用模拟数据")
        payload = {
            "articles": {},
            "cloud_word": {},
            "meta": {
                "unavailable": True,
                "data_issue": "controlled-stat-tool unavailable",
                "baseline": baseline,
                "wordcloud": wordcloud,
            },
            "degraded": True,
            "unavailable": True,
        }
    arts: dict[str, Any] = {}
    for k in out:
        if k == "wordcloud_data":
            # 品牌词云出口：逐平台词表（{platform: {word: freq}}）；1.3 按平台分组（KS）。
            arts[k] = {
                "platforms": payload.get("cloud_word") or {},
                "meta": payload.get("meta"),
                **({"degraded": True} if payload.get("degraded") else {}),
            }
        else:
            # brand_stats / brand_stats_baseline：原样透传 SSJH 11 维聚合（含 articles）。
            arts[k] = payload
    return {"artifacts": arts}


@handler("annotate")
def annotate(node, state, ctx):
    """AI 标注（轮次一 n09 / 轮次二 n11），按 node.outputs 分流。

      - outputs 含 annotated_round1 → 轮次一：情感 + 活动相关（消费 denoised_data.rows）。
      - outputs 含 annotated_data   → 轮次二：观点/UGC方式/讨论维度 三独立任务 + 覆盖检查
        （消费 annotated_round1.rows，注入 confirmed_opinion_labels + ugc_category_list）。

    路由：行数 ≥ params.annotation_routing_threshold 且 DataHub 可用 → 批量引擎；
    否则小引擎（semantic）；无凭据降级（degraded=True，出口 schema 一致）。
    口径：情感以 AI 标注为准；轮次二覆盖检查 ERROR（真路径缺口）→ raise 让引擎 pause 重试。
    side_effect=annotation-batch、idempotency、retry 由引擎处理；本 handler 不因缺凭据抛异常。
    """
    out = node.get("outputs", [])
    threshold = int(state.params.get("annotation_routing_threshold", 1000))
    if replay.enabled():
        replay.seed_annotation_cache(state.run_dir)

    if "annotated_round1" in out:
        cleaned = state.read_artifact("denoised_data") or {}
        src_rows = cleaned.get("rows") if isinstance(cleaned, dict) else None
        rows = [dict(r) for r in (src_rows or [])]
        plan = state.read_artifact("confirmed_plan") or {}
        context = _r1_context_from_plan(plan)
        rerun_ids, calibration = _rerun_scope(state, "round1", "r1_review", rows)
        # FIX-30 标注缓存放在 run_dir 下；键含 prompt 指纹，上下文/校准一变即失效。
        # FIX-33：≥阈值且 DataHub 不可用时先问用户；用户确认 small_engine 后才改走直连。
        decision = state.read_artifact("annotation_route_decision") or {}
        route = decision.get("route") if isinstance(decision, dict) else None
        try:
            result = annotate_mod.annotate_round1(
                rows, context=context, threshold=threshold,
                rerun_ids=rerun_ids, calibration=calibration,
                cache_dir=state.run_dir, route_decision=route)
        except annotate_mod.DataHubRouteRequired as required:
            raise HumanPause(prompt=str(required),
                             expect=["annotation_route_decision"]) from required
        if route:
            result["route_decision"] = route
        if result.get("degraded"):
            raise RuntimeError("annotate r1: 标注能力不可用，禁止确定性伪标注")
        coverage = result.get("coverage") or {}
        if (
            coverage.get("sentiment_missing", 0) > 0
            or coverage.get("relevance_missing", 0) > 0
        ):
            raise ValueError(
                "annotate r1: 覆盖检查存在缺口；"
                f"sentiment_missing={coverage.get('sentiment_missing', 0)}, "
                f"relevance_missing={coverage.get('relevance_missing', 0)}"
            )
        sc_mode = str(state.params.get("annotation_structure_check", "block")).lower()
        sc_config = _structure_config()  # round1 只检 id+域，不需要类别
        _apply_structure_gate(result, stage="round1", config=sc_config,
                              mode=sc_mode, label="r1")
        return {"artifacts": {"annotated_round1": result}}

    if "annotated_data" in out:
        r1 = state.read_artifact("annotated_round1") or {}
        src_rows = r1.get("rows") if isinstance(r1, dict) else None
        if not src_rows:
            cleaned = state.read_artifact("denoised_data") or {}
            src_rows = cleaned.get("rows") if isinstance(cleaned, dict) else None
        rows = [dict(r) for r in (src_rows or [])]

        confirmed = state.read_artifact("confirmed_opinion_labels") or {}
        opinion_labels = (confirmed.get("labels") or confirmed.get("candidates")
                          if isinstance(confirmed, dict) else None) or []
        ugc_list = state.read_artifact("ugc_category_list") or {}
        ugc_not_applicable = bool(
            isinstance(ugc_list, dict) and ugc_list.get("not_applicable")
        )
        ugc_categories = (
            confirmed.get("ugc_categories")
            if isinstance(confirmed, dict) else None
        ) or (
            ugc_list.get("categories") if isinstance(ugc_list, dict) else None
        ) or []

        r2_rerun, r2_calib = _rerun_scope(state, "round2", "r2_review", rows)
        if r2_rerun:
            rows = _rerun_base_rows(state, state.read_artifact("annotation_rework_plan"), rows)
        # 与轮次一共用用户的路由决定（FIX-33：DataHub 不可用时用户确认 small_engine）。
        decision = state.read_artifact("annotation_route_decision") or {}
        route2 = decision.get("route") if isinstance(decision, dict) else None
        # FIX-31：正面任务用 positive_labels，负面任务用 negative_labels。
        # 原版 annot_r2.py 直接读 opinion_candidates_positive.json / _negative.json，
        # 不依赖人工确认产物带不带分方向字段；确认产物的 note 追加类别两方向都加。
        positive_labels, negative_labels = _round2_direction_labels(
            confirmed, state.read_artifact("opinion_candidates") or {})
        try:
            result = annotate_mod.annotate_round2(
                rows, rerun_ids=r2_rerun, calibration=r2_calib,
                negative_labels=list(negative_labels) if negative_labels is not None else None,
                opinion_labels=list(positive_labels or opinion_labels),
                ugc_categories=list(ugc_categories), threshold=threshold,
                ugc_not_applicable=ugc_not_applicable,
                opinion_review_items=confirmed.get("opinion_review_items") or [],
                ugc_review_items=confirmed.get("ugc_review_items") or [],
                campaign_name=str(
                    (state.read_artifact("confirmed_plan") or {}).get("activity_name")
                    or ""),
                cache_dir=state.run_dir, route_decision=route2)
        except annotate_mod.DataHubRouteRequired as required:
            raise HumanPause(prompt=str(required),
                             expect=["annotation_route_decision"]) from required
        if result.get("degraded"):
            raise RuntimeError(
                "annotate r2: 标注能力不可用，禁止确定性伪标注"
                f"（{result.get('degraded_reason') or '未知原因'}）")
        # 覆盖检查缺口（真路径 ERROR）→ 直接报错，交引擎按 retry/on_fail=pause 处理。
        cov = result.get("coverage") or {}
        miss = {t: {k: cov[t].get(k) for k in ("scope_total", "missing", "other",
                                                "invalid_values") if k in cov[t]}
                for t in ("opinion", "ugc_category", "discussion_dimension")
                if cov.get(t, {}).get("status") == "ERROR"}
        if cov.get("has_error") and cov.get("has_retryable_error", True):
            # 可重试缺口（空值/非法值）：补标或重标能解决，交引擎重试。
            raise ValueError(
                "annotate r2: 覆盖检查未通过（缺失/非法，或观点「空值+其他」≥"
                f"{annotate_mod.OPINION_OTHER_BLOCK_RATIO:.0%}）：{miss}")
        if cov.get("has_error"):
            # 仅「其他」占比超门槛（质量阻断）：同一批数据重跑结果不变，重试无意义。
            # 原版 KS_Data_Processing:245 要求阻断并由人处置，QC 协议:219 明确「其他」
            # 不能为降比例强行归类 —— 因此默认记录 blocked_quality、继续出报告并在
            # 数据说明披露；opinion_other_policy=block 时保持硬阻断。
            policy = str(state.params.get("opinion_other_policy", "waive")).lower()
            if coverage_disposition(cov, policy) == "block":
                raise ValueError(
                    "annotate r2: 观点「空值+其他」占比超门槛（opinion_other_policy=block）："
                    f"{miss}")
            result["blocked_quality"] = list(cov.get("quality_blocked") or [])
            state.event(node.get("id", "n11_annotate_r2"), "quality_blocked",
                        f"观点「其他」超门槛，按遗留口径披露继续：{result['blocked_quality']}")
        sc_mode = str(state.params.get("annotation_structure_check", "block")).lower()
        sc_config = _structure_config(
            opinion_labels=list(positive_labels or opinion_labels),
            negative_labels=list(negative_labels) if negative_labels is not None else None,
            ugc_categories=list(ugc_categories),
            ugc_not_applicable=ugc_not_applicable)
        _apply_structure_gate(result, stage="full", config=sc_config,
                              mode=sc_mode, label="r2")
        return {"artifacts": {"annotated_data": result}}

    # 兜底：未知 outputs，保持 schema 可跑（不应触达）。
    return {"artifacts": {k: _stub_payload(node, "annotated") for k in out}}


@handler("charts_render")
def charts_render(node, state, ctx):
    """图表 + 封面渲染为本地 PNG（n14_charts）。

    消费 stats_bundle + wordcloud_data，产 chart_urls（含逐张 local_path + 封面）。
    图表规格确定性生成；ECharts渲染常规图表，wordcloud-insight v0.2.6
    渲染透明/白底双图及render.json（使用完整原表输入）。词云输出目录每次新建，
    chart_urls保留双图和范围记录供n15 compose，local_path默认引用透明图供飞书原生media。
    缺依赖或任一图失败必须抛错，由引擎重试/暂停，禁止无图交付。
    """
    stats = state.read_artifact("stats_bundle") or {}
    wc = state.read_artifact("wordcloud_data") or {}
    plan = state.read_artifact("confirmed_plan") or {}

    project = str(plan.get("project") or plan.get("brand") or "topic5")
    report_name = str(plan.get("report_name") or f"{project}营销活动复盘")
    # n14 在 n16 之前运行：封面标注即将写出的版本（下一次整篇写作 minor+1，patch=0）。
    version = f"v{state.counters.get('report_minor', 0) + 1}.0"
    outdir = str(state.run_dir / "charts")

    def _build() -> dict:
        # 渲染依赖（echarts/resvg 的 node_modules）不入包，首次运行由 charts.ensure_renderer
        # 现装；装不上直接走 fallback → 上图外抛错，禁止无图交付。
        try:
            charts.ensure_renderer()
        except Exception as error:
            result = charts.fallback(
                stats, wc, project=project, version=version,
                report_name=report_name)
            result["render_error"] = f"{type(error).__name__}: {error}（{charts.renderer_diagnostic()}）"
            return result
        if charts.renderer_available():
            try:
                return charts.render_charts(
                    stats, wc, project=project, version=version,
                    report_name=report_name, outdir=outdir, plan=plan)
            except Exception as error:
                result = charts.fallback(
                    stats, wc, project=project, version=version,
                    report_name=report_name)
                result["render_error"] = f"{type(error).__name__}: {error}"
                return result
        result = charts.fallback(
            stats, wc, project=project, version=version, report_name=report_name)
        result["render_error"] = (
            "renderer unavailable: " + charts.renderer_diagnostic()
        )
        return result

    result = _build()
    if result.get("degraded"):
        detail = str(result.get("render_error") or "unknown render error")
        raise RuntimeError(
            f"charts_render: 图表渲染失败，禁止无图交付: {detail}"
        )
    return {"artifacts": {"chart_urls": result}}


@handler("feishu_export")
def feishu_export(node, state, ctx):
    return {"artifacts": {"feishu_doc": {"_stub": True,
                                         "url": "https://feishu.example/doc/stub",
                                         "revision": 1}}}


# ── agent 节点（语义，接真：Ark Chat + 确定性降级）─────────────


def _sample_texts(rows: list[dict], limit: int = 40, maxlen: int = 120) -> list[str]:
    """从行取标题/正文样本供语义提示；去空、截断、限量，控制 token。"""
    out: list[str] = []
    for r in rows:
        t = str(r.get("title") or "").strip()
        c = str(r.get("content") or "").strip()
        s = (t + " " + c).strip()[:maxlen]
        if s:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _wordcloud_platforms(wc: dict) -> dict[str, list[str]]:
    """从词云产物抽各平台词表：兼容 {platform: {word: freq}} / {platform: [word]}。"""
    plats: dict[str, list[str]] = {}
    src = (wc or {}).get("platforms") or (wc or {}).get("wordclouds") or wc or {}
    if isinstance(src, dict):
        for plat, words in src.items():
            if plat in ("_stub", "kind", "node", "name"):
                continue
            if isinstance(words, dict):
                plats[plat] = list(words.keys())[:50]
            elif isinstance(words, list):
                plats[plat] = [str(w) for w in words][:50]
    return plats


# ── 类别发现（FIX-31）：原版 opinion/ugc-discovery run.py + pipeline_config.phase_discovery
DISCOVERY_MIN_POSTS = 10            # 该方向 < 10 条 → not_applicable，不调模型、无需人工确认
OPINION_DISCOVERY_SAMPLE = 200      # KS 区间 150~200，池不足取全量
UGC_DISCOVERY_SAMPLE = 150          # KS 区间 100~150


def _rows_by_campaign(rows: list[dict]) -> dict[str, list[dict]]:
    """按 analysis_object 逐活动分组（与 aggregate 同一权威字段）。

    没有 analysis_object 的行归入单活动键 "default"；原版 per_campaign=true，
    多活动时禁止混池抽样。
    """
    groups: dict[str, list[dict]] = {}
    for index, row in enumerate(rows):
        cid = str(row.get("analysis_object") or "").strip() or "default"
        groups.setdefault(cid, []).append({**row, "_review_source_index": index})
    return groups


def _discover(pool: list[dict], *, key: str, kind: str, size: int,
              campaign_name: str, max_items: int) -> dict:
    """单个（活动 × 方向）的类别发现。

    池 < DISCOVERY_MIN_POSTS → not_applicable（原版 status=not_applicable，不调模型）；
    否则 mixed 抽样 → 模型发现 → 引用核验 → 命名/数量告警（只告警不中断）。
    """
    if len(pool) < DISCOVERY_MIN_POSTS:
        return {
            "status": "not_applicable", "not_applicable": True,
            key: [], "review_items": [],
            "reason": f"该方向内容仅 {len(pool)} 条，不足 {DISCOVERY_MIN_POSTS} 条，不做类别发现",
            "sampling": {"pool_size": len(pool), "sampled": 0},
        }
    samples, catalog, sampling = label_review.source_catalog(pool, size)
    data = semantic.chat_json(
        label_review.discovery_prompt(key, kind=kind, campaign_name=campaign_name),
        json.dumps(samples, ensure_ascii=False), max_tokens=6144, lm_scope="discovery",
    )
    review = label_review.ground_candidates(data, key, catalog, max_items)
    review["sampling"].update(sampling)
    review["sampling"]["population_size"] = len(pool)
    review["warnings"] = label_review.lint_categories(review[key], kind)
    review["status"] = "ok"
    return review


@handler("ugc_discovery")
def ugc_discovery(node, state, ctx):
    """UGC 参与方式发现。

    口径（knowledge/_字段速查 + params）：
      - dynamic（默认）→ 从UGC内容样本发现参与方式；经标签关卡确认。
      - 无语义能力直接报错，不生成固定类别兜底（D5：固定四类已删除）。
    保留 categories[]；review_items 包含定义、边界、按 quote_id 核验的原帖引用。
    """
    plan = state.read_artifact("confirmed_plan") or {}
    selected = plan.get("selected_modules") if isinstance(plan, dict) else {}
    enabled = (
        selected.get("core_plus_conditional")
        if isinstance(selected, dict) else None
    ) or []
    if (
        str(plan.get("content_scope") or "").lower() == "pgc"
        or (enabled and "ugc_participation" not in enabled)
    ):
        return {"artifacts": {"ugc_category_list": {
            "mode": "not_applicable",
            "categories": [],
            "source": "confirmed_plan",
            "not_applicable": True,
            "reason": "content_scope=pgc; UGC participation module disabled",
        }}}

    # 缺陷清单 D5：UGC 参与方式只按本活动真实样本动态发现（R5-16/R5-18），固定四类已删除。
    mode = "dynamic"

    # FIX-31：样本取「活动相关 且 UGC」（原版 phase-ugc-discovery run.py:155-157），
    # 因此依赖轮次一的相关性判定。
    if not semantic.llm_available():
        raise RuntimeError("ugc_discovery: 动态类别发现能力不可用")
    r1 = state.read_artifact("annotated_round1") or {}
    rows = r1.get("rows") if isinstance(r1, dict) else None
    if not rows:
        raise RuntimeError("ugc_discovery: 缺少轮次一标注结果，无法按活动相关性抽样")

    campaigns: dict[str, dict] = {}
    for cid, crows in _rows_by_campaign(rows).items():
        pool = [row for row in crows
                if _is_relevant(row) is True
                and str(row.get("content_mode") or "").upper() == "UGC"]
        name = plan.get("activity_name") if cid == "default" else cid
        campaigns[cid] = {"ugc": _discover(
            pool, key="categories", kind="ugc", size=UGC_DISCOVERY_SAMPLE,
            campaign_name=str(name or ""),
            max_items=label_review.CATEGORY_COUNT["ugc"][1])}

    names, items, warnings = _merge_discoveries(campaigns, "categories")
    if not names:
        # 全部活动都不适用（无 UGC / UGC < 10 条）：ugc_category 全填 N/A，无需人工确认
        reasons = [c["ugc"].get("reason") for c in campaigns.values()]
        return {"artifacts": {"ugc_category_list": {
            "mode": "not_applicable", "categories": [], "source": "annotated_round1",
            "not_applicable": True, "reason": "；".join(r for r in reasons if r),
            "campaigns": campaigns,
        }}}
    return {"artifacts": {"ugc_category_list": {
        "categories": names,
        "review_items": items,
        "review_schema_version": label_review.REVIEW_SCHEMA_VERSION,
        "campaigns": campaigns,
        "warnings": warnings,
        "mode": mode,
        "source": "llm",
        "source_subject": state.artifact_ref("annotated_round1"),
    }}}


@handler("opinion_candidates")
def opinion_candidates(node, state, ctx):
    """观点类别发现（FIX-31，原版 blueai-opinion-discovery-skill）。

    - 按活动逐个运行；每个活动正面、负面各发现一次（相关 且 情感为该方向）。
    - mixed 抽样（互动 Top50% + 随机 50%，seed=42），每方向至多 200 条。
    - 某方向 < 10 条 → not_applicable，不调模型。
    - 产物：campaigns[cid].{positive, negative}；顶层 candidates / review_items
      为全部适用方向的合并去重结果，供 n10 / n11 沿用既有读取方式。
    """
    if not semantic.llm_available():
        raise RuntimeError("opinion_candidates: 观点发现能力不可用")
    r1 = state.read_artifact("annotated_round1") or {}
    rows = r1.get("rows") if isinstance(r1, dict) else None
    if not rows:
        raise RuntimeError("opinion_candidates: 缺少轮次一标注结果，无法按正负方向发现观点")
    plan = state.read_artifact("confirmed_plan") or {}

    campaigns: dict[str, dict] = {}
    for cid, crows in _rows_by_campaign(rows).items():
        relevant = [row for row in crows if _is_relevant(row) is True]
        name = plan.get("activity_name") if cid == "default" else cid
        campaigns[cid] = {
            polarity: _discover(
                [row for row in relevant if _row_sentiment(row) == sign],
                key="candidates", kind=polarity, size=OPINION_DISCOVERY_SAMPLE,
                campaign_name=str(name or ""),
                max_items=label_review.CATEGORY_COUNT[polarity][1])
            for polarity, sign in (("positive", 1), ("negative", -1))
        }

    merged, items, warnings = _merge_discoveries(campaigns, "candidates")
    return {"artifacts": {"opinion_candidates": {
        "candidates": merged,
        # 正负两套候选分开保存，供轮次二两个观点任务各自注入（FIX-28/31）
        "positive_candidates": _direction_names(campaigns, "positive", "candidates"),
        "negative_candidates": _direction_names(campaigns, "negative", "candidates"),
        "not_applicable_directions": sorted({
            f"{cid}/{direction}"
            for cid, directions in campaigns.items()
            for direction, result in directions.items()
            if result.get("not_applicable")}),
        "review_items": items,
        "review_schema_version": label_review.REVIEW_SCHEMA_VERSION,
        "evidence_count": sum(
            int(result.get("evidence_count") or 0)
            for camp in campaigns.values() for result in camp.values()),
        "campaigns": campaigns,
        "warnings": warnings,
        "source": "llm",
        "r1_ref": True,
        "source_subject": state.artifact_ref("annotated_round1"),
    }}}


def _direction_names(campaigns: dict, direction: str, key: str) -> list[str]:
    """某方向在全部活动里的类别名（按出现顺序去重）。"""
    names: list[str] = []
    for directions in campaigns.values():
        for name in (directions.get(direction) or {}).get(key) or []:
            if name not in names:
                names.append(name)
    return names


def _merge_discoveries(campaigns: dict, key: str) -> tuple[list[str], list[dict], list[str]]:
    """合并各活动各方向的发现结果，告警带上来源。

    names 为全部类别名去重（兼容旧读取方式）；review_items 按 (方向, 名称) 去重——
    正负两套各自独立，同名类别的定义可能不同，不能互相覆盖。
    """
    names: list[str] = []
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    warnings: list[str] = []
    for cid, directions in campaigns.items():
        for direction, result in directions.items():
            for warn in result.get("warnings") or []:
                warnings.append(f"[{cid}/{direction}] {warn}")
            for item in result.get("review_items") or []:
                name = item.get("name")
                if name not in names:
                    names.append(name)
                if (direction, name) not in seen:
                    seen.add((direction, name))
                    items.append({**item, "campaign": cid, "direction": direction})
    return names, items, warnings


def _opinion_count_reasons(opinion: dict, labels: list[str]) -> list[str]:
    """观点类别数量校验（FIX-31）：按方向分别检查，not_applicable 方向豁免。

    新产物带 positive_candidates / negative_candidates：正面 5-8、负面 3-5；
    旧产物只有一套合并候选时沿用 5-8。
    """
    if "positive_candidates" not in opinion and "negative_candidates" not in opinion:
        return [] if 5 <= len(labels) <= 8 else ["观点标签数量不在 5-8 个范围"]
    skipped = {item.split("/")[-1] for item in opinion.get("not_applicable_directions") or []}
    reasons = []
    for direction in ("positive", "negative"):
        names = opinion.get(f"{direction}_candidates") or []
        if direction in skipped and not names:
            continue
        low, high = label_review.CATEGORY_COUNT[direction]
        if not low <= len(names) <= high:
            label = label_review.POLARITY_LABEL[direction]
            reasons.append(f"{label}观点标签数量 {len(names)} 不在 {low}-{high} 个范围")
    return reasons


def _label_source_review(opinion: dict, ugc: dict) -> dict:
    """观点 + UGC 两套类别的来源审核，合并为一份 validator_review（FIX-30）。

    UGC 不适用（仅 PGC）时只审观点。
    """
    p0_opinion, opinion_meta = label_review.run_source_review("opinion", opinion)
    if ugc.get("not_applicable"):
        p0_ugc, ugc_meta = [], {}
    else:
        p0_ugc, ugc_meta = label_review.run_source_review("ugc", ugc)
    p0 = [f"观点：{x}" for x in p0_opinion] + [f"UGC：{x}" for x in p0_ugc]
    return {
        "actor_role": label_review.SOURCE_REVIEW_ACTOR,
        "mode": label_review.SOURCE_REVIEW_MODE,
        "rules_version": label_review.SOURCE_REVIEW_RULES_VERSION,
        "opinion": opinion_meta,
        "ugc": ugc_meta,
        "p0": p0,
        "has_p0": bool(p0),
    }


@handler("opinion_policy_gate")
def opinion_policy_gate(node, state, ctx):
    """Auto-confirm stable label sets; pause only when human judgment is needed."""
    opinion = state.read_artifact("opinion_candidates") or {}
    ugc = state.read_artifact("ugc_category_list") or {}
    labels = [
        str(value).strip()
        for value in (opinion.get("candidates") or [])
        if str(value).strip()
    ]
    ugc_categories = [
        str(value).strip()
        for value in (ugc.get("categories") or [])
        if str(value).strip()
    ]
    vague_labels = {"其他", "其它", "未知", "一般", "相关", "正面", "负面", "中性"}
    reasons = []
    if opinion.get("source") != "llm" or opinion.get("degraded"):
        reasons.append("观点候选不是独立语义模型的完整结果")
    reasons.extend(_opinion_count_reasons(opinion, labels))
    if len(set(labels)) != len(labels):
        reasons.append("观点标签存在重复")
    if any(len(label) > 20 or label in vague_labels for label in labels):
        reasons.append("观点标签过长或语义过于宽泛")
    # FIX-32：类别名带倾向（原版 SENTIMENT_WORDS / EVAL_SUFFIX）转人工
    tendency = [name for name in [*labels, *ugc_categories]
                if label_review.tendency_hits(name)]
    if tendency:
        reasons.append("类别名带倾向：" + "、".join(tendency))
    if int(opinion.get("evidence_count") or 0) < len(labels):
        reasons.append("候选标签缺少足够样本支撑")
    if (
        not ugc.get("not_applicable")
        and (
            ugc.get("degraded")
            or not ugc_categories
            or len(set(ugc_categories)) != len(ugc_categories)
            or ugc.get("mode") != "dynamic"
        )
    ):
        reasons.append("UGC 类别不完整或不稳定")
    for title, artifact in (("观点", opinion), ("UGC", ugc)):
        if artifact.get("review_schema_version"):
            expected_names = artifact.get("candidates") or artifact.get("categories") or []
            by_name = {item["name"]: item for item in artifact.get("review_items") or []}
            missing = [
                name for name in expected_names
                if not by_name.get(name, {}).get("definition")
                or not by_name.get(name, {}).get("boundary")
                or by_name.get(name, {}).get("evidence_status") != "grounded"
            ]
            if missing:
                reasons.append(f"{title}候选缺少定义、边界或代表原帖支撑：" + "、".join(missing))

    if reasons:
        raise HumanPause(
            prompt="观点与 UGC 标签需要人工审核：" + "；".join(reasons),
            expect=node.get("outputs", []),
        )

    # FIX-30：确定性策略通过后，委派独立 Validator 做 D1/D2 来源审核。
    # 审核通过即可自动确认（动态 UGC 不再一律转人工）；有 P0 转人工。
    validator_review = _label_source_review(opinion, ugc)
    if validator_review["has_p0"]:
        raise HumanPause(
            prompt="观点与 UGC 标签来源审核未通过：" + "；".join(validator_review["p0"]),
            expect=node.get("outputs", []),
        )

    return {"artifacts": {"confirmed_opinion_labels": {
        "validator_review": validator_review,
        "labels": labels,
        # FIX-31：正负两套候选分开冻结，供轮次二两个观点任务各自注入
        # 新产物：某方向候选为空 = 该方向不适用（原版 status!=ok），保持空表、不借用合并标签；
        # 旧产物（无分方向字段）两边共用 labels。
        "positive_labels": (list(opinion.get("positive_candidates") or [])
                            if "positive_candidates" in opinion else list(labels)),
        "negative_labels": (list(opinion.get("negative_candidates") or [])
                            if "negative_candidates" in opinion else list(labels)),
        "ugc_categories": ugc_categories,
        "ugc_not_applicable": bool(ugc.get("not_applicable")),
        **label_review.confirmed_review(opinion, ugc),
        "confirmed": True,
        "confirmed_by": "validator-policy",
        "automatic": True,
        "policy_version": "opinion-label-policy-v1",
        "subject": {
            "opinion_candidates": state.artifact_ref("opinion_candidates"),
            "ugc_category_list": state.artifact_ref("ugc_category_list"),
        },
    }}}


@handler("wordcloud_insight")
def wordcloud_insight(node, state, ctx):
    """Complete the customer's full-table analysis and grounded composition."""
    import wordcloud_adapter
    wc = state.read_artifact("wordcloud_data") or {}
    stats = state.read_artifact("stats_bundle") or {}
    chart_bundle = state.read_artifact("chart_urls") or {}
    if not semantic.llm_available():
        raise RuntimeError("wordcloud_insight: 缺少语义能力")
    expected = {
        spec["chart_id"]: spec for spec in charts.build_chart_specs(
            stats, wc, state.read_artifact("confirmed_plan") or {},
            state.read_artifact("requirement_confirmed") or {})
        if spec["chart_type"] == "wordcloud"
    }
    rendered = {
        chart["chart_id"]: chart for chart in chart_bundle.get("charts", [])
        if chart.get("chart_type") == "wordcloud"
    }
    if not expected or set(rendered) != set(expected):
        raise RuntimeError("wordcloud_insight: n14 词云范围不完整，禁止跳过分析")
    out = {}
    for chart in rendered.values():
        spec = chart.get("spec") or chart
        if spec.get("wordcloud_input") != expected[chart["chart_id"]]["wordcloud_input"]:
            raise RuntimeError("wordcloud_insight: n14 输入与当前词表/方案不一致")
        scope = (chart.get("wordcloud_artifacts") or {}).get("scope") or {}
        cid, dimension, platform = (scope.get("campaign"), scope.get("dimension"), scope.get("platform"))
        if not all((cid, dimension, platform)):
            raise RuntimeError("wordcloud_insight: 缺少 n14 新 Skill 范围记录")
        # FIX-55：call-brief 以当前已保存需求为准（不信任 n14 记录里的旧 brief）。
        chart = {**chart, "call_brief": expected[chart["chart_id"]]["call_brief"]}
        result = wordcloud_adapter.analyze(state, chart, charts.WORDCLOUD_DIR, FORBIDDEN_METRIC_TERMS)
        out.setdefault(cid, {}).setdefault(dimension, {"platforms": {}})["platforms"][platform] = result
    for cid, dimensions in (wc.get("campaigns") or {}).items():
        for dimension, bundle in dimensions.items():
            if bundle.get("not_applicable"):
                out.setdefault(cid, {})[dimension] = {"not_applicable": True, "platforms": {}}
    # FIX-49：热帖成功配方（2.N.4④），输入为 2.N.5 同一份热门榜单。
    formulas = {cid: hot_formula.analyze(cid, bundle.get("content_formula_input") or [],
                                         FORBIDDEN_METRIC_TERMS)
                for cid, bundle in (stats.get("campaigns") or {}).items()}
    return {"artifacts": {"wordcloud_insight": {
        "brand": out.pop("brand", {}), "campaigns": out, "hot_post_formula": formulas,
        "source": "wordcloud-insight-v0.2.6", "complete": True}}}


def _report_evidence(stats: dict) -> dict:
    """保留完整聚合证据，移除重复原始抓取数据及个人账号信息。

    不能截断 stats_bundle 的 JSON：brand.source 中的原帖可能挤掉后面的活动统计。
    """
    def compact(value, *, anonymous=False):
        if isinstance(value, list):
            return [compact(item, anonymous=anonymous) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key.startswith("_") and key != "_total":
                continue
            if key in {"source", "articles", "raw", "keywords", "hot_content"}:
                continue
            if key in {"author_id", "user_id", "nickname", "account_name"}:
                continue
            if key == "author" and (anonymous or str(value.get("content_mode") or "").upper() != "PGC"):
                continue
            if key == "content":
                result[key] = report_contract.excerpt(value)
                continue
            result[key] = compact(item, anonymous=anonymous or key == "ugc")
        return result
    return compact(stats)


# 报告撰写/经验总结/返修：原版推荐 claude-sonnet-4-6（KS_Content_Analysis_Prompt.md:142）；
# 长报告输出约 1-2 万 token，按 relay 吞吐需要数分钟，整体时限单独放宽，不受标注默认 180s 约束。
_REPORT_LLM = {"model": semantic.report_model(),
               "timeout": int(os.environ.get("TOPIC5_REPORT_TIMEOUT_SEC") or 900)}


@handler("write_report")
def _limitation_note(stats) -> str:
    """受限口径披露块（原版 QC 协议:183/219）：只对 module_degraded 的 DataIssue 生成。

    数据说明区只放平台/周期/关键词（KS_Report_Template §零），所以披露写在对应小节，
    既不降门槛也不隐藏。
    """
    limits = [issue for issue in ((stats or {}).get("data_issues") or [])
              if isinstance(issue, dict) and issue.get("severity") == "module_degraded"]
    if not limits:
        return ""
    rows = "\n".join(f"- {issue.get('affected_module')}：{issue.get('reason')}"
                     for issue in limits)
    return ("受限口径披露（必须遵守）：以下指标存在口径限制，报告对应小节须用一句客观"
            "说明其局限（不写方法论、不写清洗抽样说明、不放在数据说明区）：\n" + rows)


def write_report(node, state, ctx):
    """报告内容层撰写（MD，不含飞书布局）。

    口径（knowledge/04 报告结构 v2.6）：
      - 固定四章（FIX-42，结构常量见 policy.REPORT_*）：一品牌/二活动（末节 2.{N+1} 拉升）/
        三经验总结/四总结与建议。
      - 指标白名单（条均互动量/品牌提及率/产品提及占比/NSR），禁用“人均/转化/性价比”。
      - 活动层只讲“提及”不讲“转化”；多活动逐个处理不相加；竞品章已下线。
    消费 stats_bundle + chart_urls + wordcloud_insight。无凭据降级到确定性模板 MD。
    输出 schema：{markdown, chapters, version, source, degraded?}
    """
    # FIX-45：每次整篇（重）写报告 minor+1、patch 归零；首版即 1.0。
    state.counters["report_minor"] = state.counters.get("report_minor", 0) + 1
    state.counters["report_patch"] = 0
    version = _report_version(state)
    chapters = REPORT_CHAPTERS_MODE
    stats = state.read_artifact("stats_bundle") or {}
    charts = state.read_artifact("chart_urls") or {}
    wc_insight = state.read_artifact("wordcloud_insight") or {}

    if not semantic.llm_available() or not stats:
        raise RuntimeError("write_report: 缺少统计产物或语义能力")

    sys_p = (
        "你是资深营销复盘报告撰写人。依据统计数据与词云议题，写出活动复盘报告的内容层 Markdown"
        "（不含飞书布局）。严格遵守口径：\n"
        "1) 指标只用【条均互动量/品牌提及率/产品提及占比/NSR】，禁用“人均/转化/转化率/性价比”等无数据支撑说法；\n"
        "2) 活动层指标只统计活动相关内容，多活动逐个处理、不相加为合计声量；\n"
        "3) 提及漏斗只讲“提及”不讲“转化”；竞品分析已下线，不要写竞品章；\n"
        "4) " + _report_structure_rule(len(stats.get("campaigns") or {})) + "\n"
        "5) 所有数字必须来自给定数据，不得编造；证据不足就说明，不凑根因；\n"
        "6) 需插图时用 Markdown 图片语法 `![标题](local_path)`，local_path 取 chart_urls 中每张图的"
        "`local_path` 字段（本地 PNG 路径，交付时会上传进飞书文档）；local_path 为空的图跳过不插。\n"
        "7) 标题层级：四章用 `## 一、品牌传播表现` 等；1.N、2.N、2.{N+1}、3.N 用 `###`；"
        "活动标题为 `### 2.N 活动 {活动名}`，其五个分节用 `#### 2.N.1` 至 `#### 2.N.5`。"
        "每节顺序为分析文字→对应图→数据表；图必须放在对应章节，不能集中堆在热门内容后。\n"
        "8) " + _POLICY.CALLOUT_RULES + "成功经验、待改进问题、行动计划逐条回指章节和数字；"
        "有负面样本必须分析，不能用泛泛的证据不足代替；小样本<30标明样本不足，不下确定性结论。\n"
        "9) 不输出内部字段名、版本、模板生成说明、布局指令或原始 JSON；UGC 个人账号必须匿名，"
        "公开 PGC 机构或 KOL 作者仅可在热门内容表展示；词云解读必须为连贯叙事。\n"
        "10) 基准期30天和活动期长度不同，必须同时比较日均声量并披露假期影响；"
        "不把总量差异直接解释为活动效果；所有推导数值需写清分子、分母或计算方式。\n"
        "11) 篇头顺序：`# 标题` → `### 数据说明` → `## 整体洞察` → `## 一、品牌传播表现`；"
        "数据说明由系统按取数配置生成，你只需保留该标题，不要写方法论、口径、清洗或抽样说明。"
        "若 stats_bundle.clean_record.sampling.applied=true，系统会在整体洞察后插入抽样提示，"
        "正文不得把样本统计外推成全量活动结论。\n"
        "只输出 Markdown 正文。")
    sys_p += (
        "\n必需证据规范：品牌和活动分别使用自己的词云，不得混用。活动口碑依据 opinion_analysis "
        "给出正负观点TOP5、各自情感方向全部内容为分母的占比、每观点2-3条代表原文；"
        "UGC依据 ugc_analysis 逐类写占比、代表内容与基于样本的质量解读。"
        "PGC和UGC热门内容分别Top10，不得截短或丢无标题正文，字段以给定top_content为准。"
        "品牌口碑（1.2）与活动口碑（2.N.3）的情感来源不同，在对应小节用一句说明区分，不写在数据说明区。"
        "1.1/2.N.1 的平台与内容结构结论、声量/互动高点句由系统按统计数据生成并插在节首，"
        "你不要重复这些数字，只可在其后补充有帖子证据的归因。"
        "互动量口径：" + report_contract.INTERACTION_NOTE
        + "不写「点赞+评论+转发」等加总公式；按发布时间排序的内容不得称为互动量Top；"
        "metrics.互动量/条均互动量为 null 时写「不可得」。"
        "权威媒体：" + report_contract.AUTHORITY_MEDIA_RULE
    )
    # 受限口径披露：观点「其他」占比未达标等质量阻断记在 stats_bundle.data_issues。
    # 数据说明区只放平台/周期/关键词（KS_Report_Template §零），所以改在对应小节用一句
    # 说明披露，不降门槛也不隐藏（原版 QC 协议:183/219）。
    limitation_note = _limitation_note(stats)
    if limitation_note:
        sys_p += "\n\n" + limitation_note
    sys_p += "\n\n" + (Path(__file__).parent / "REPORT_CONTRACT.md").read_text(encoding="utf-8")
    user_p = ("统计数据(stats_bundle)：\n" + json.dumps(_report_evidence(stats), ensure_ascii=False)
              + "\n\n词云议题(wordcloud_insight)：\n"
              + json.dumps(wc_insight, ensure_ascii=False)
              + "\n\n图表(chart_urls，含每张图 local_path)：\n"
              + json.dumps(charts, ensure_ascii=False)
              + "\n\n冻结需求：\n"
              + json.dumps(state.read_artifact("confirmed_plan") or {}, ensure_ascii=False))
    try:
        md = semantic.chat(sys_p, user_p, max_tokens=16384, lm_scope="write_report", **_REPORT_LLM)
        if not md.strip():
            raise ValueError("模型返回空报告")
        # FIX-20：命中禁用措辞不再删除整句（删除会连带删掉数据与原文，且掩盖问题）。
        # 记录命中交 n17 质检判 P0、n19 定向改写；结构问题仍在节点内 fail-closed。
        guardrail_hits = _scan_forbidden(md)
        source = "llm"
        guardrail_reason = ""
        try:
            _assert_report_structure(
                md, list((stats.get("campaigns") or {}).keys())
            )
        except ValueError as error:
            raise ValueError(f"write_report: 报告未达结构或口径要求，禁止整篇降级替换: {error}") from error
        # FIX-44：全文写完后，按客户「经验总结合成 Prompt」单独重写三章 3.1~3.3。
        md = _synthesize_experience(md, state.read_artifact("confirmed_plan") or {}, stats)
        md = report_contract.assemble(
            md, plan=state.read_artifact("confirmed_plan") or {}, stats=stats,
            charts=charts, insight=wc_insight,
        )
        guardrail_hits = _scan_forbidden(md) or guardrail_hits
        _assert_report_structure(md, list((stats.get("campaigns") or {}).keys()))
        return {"artifacts": {"report_md": {
            "markdown": md,
            "chapters": chapters,
            "version": version,
            "source": source,
            "guardrail_hits": guardrail_hits,
            **(
                {"guardrail_fallback_reason": guardrail_reason}
                if guardrail_reason else {}
            ),
        }}}
    except Exception:
        raise


@handler("fix_report")
def fix_report(node, state, ctx):
    """修订完整报告；代码重建证据模块，结构/P0错误最多返修三次。"""
    # FIX-36：G4 内容返工只累加 user_rework_rounds（在 HB 中），qc_rounds 只计 G3 返修循环。
    qc_now = state.read_artifact("qc_report") or {}
    user_rework = any(isinstance(i, dict)
                      and (i.get("id") == "P0-g4-user-reject"
                           or str(i.get("id") or "").startswith("P0-postfix-"))   # FIX-38 交接后修正
                      and i.get("status") != "resolved" for i in qc_now.get("issues") or [])
    if not user_rework:
        state.counters["qc_rounds"] = state.counters.get("qc_rounds", 0) + 1
    state.counters["report_patch"] = state.counters.get("report_patch", 0) + 1   # FIX-45：P0 修复只加 patch
    version = _report_version(state)
    rnd = state.counters["qc_rounds"]
    qc = state.read_artifact("qc_report") or {}
    prev = state.read_artifact("report_md") or {}
    chapters = REPORT_CHAPTERS_MODE
    prev_md = prev.get("markdown", "") if isinstance(prev, dict) else ""
    issues = qc.get("issues") or []

    def _issue_text(items) -> str:
        parts = [str(i.get("msg") or i.get("evidence") or i) if isinstance(i, dict) else str(i)
                 for i in items]
        return "；".join(parts) or "按质检意见修订"

    if not semantic.llm_available() or not prev_md:
        raise RuntimeError("fix_report: 无法执行报告返修")

    stats = state.read_artifact("stats_bundle") or {}
    charts_payload = state.read_artifact("chart_urls") or {}
    requirement = state.read_artifact("requirement_confirmed") or {}
    assembly_args = {
        "plan": state.read_artifact("confirmed_plan") or {}, "stats": stats,
        "charts": charts_payload, "insight": state.read_artifact("wordcloud_insight") or {},
    }
    # Compare the same canonical structure on both sides, including old reports
    # whose generated Top10 modules used nested Markdown headings.
    canonical_previous = report_contract.assemble(prev_md, **assembly_args)
    sys_p = (
        "你是报告质检修复人。逐项关闭给定质检问题后输出完整 Markdown。"
        "每个 P0 都必须根据 id/title/evidence/locus 修复，不能只概括处理；保持未涉及章节和图表不变。"
        "保留所有编号章节；Top10表格及证据模块由代码按统计产物重建，不得改动其中数据。"
        "修订结果会立即由与独立 Validator 相同的确定性 scan_report 规则预检，仍有 P0 会在当前"
        "返修节点继续要求修改。口径不变：指标白名单、只讲提及不讲转化、数字不得编造。"
        "小样本披露可使用“样本不足”“样本量不足”“样本数不足”“样本量<30”等明确措辞，"
        "并且必须靠近对应维度名称，同时不得输出确定性结论。只输出修订后的完整 Markdown。"
        "\n" + _POLICY.CALLOUT_RULES
    )
    sys_p += "\n\n" + (Path(__file__).parent / "REPORT_CONTRACT.md").read_text(encoding="utf-8")

    def build_user_prompt(current_md: str, current_issues: list) -> str:
        return (
            "必须逐项关闭的质检问题（完整结构）：\n"
            + json.dumps(current_issues, ensure_ascii=False)
            + "\n\n当前报告 Markdown（完整正文，必须保留未涉及章节）：\n"
            + current_md
            + "\n\n完整聚合证据（不含原始抓取数据，UGC账号已去除）：\n"
            + json.dumps(_report_evidence(stats), ensure_ascii=False)
            + "\n\n图表与正确本地路径（按对应章节插入）：\n"
            + json.dumps(charts_payload, ensure_ascii=False)
            + "\n\n词云议题：\n"
            + json.dumps(state.read_artifact("wordcloud_insight") or {}, ensure_ascii=False)
            + "\n\n冻结需求：\n"
            + json.dumps(state.read_artifact("confirmed_plan") or {}, ensure_ascii=False)
        )

    current_md = canonical_previous
    current_issues = list(issues)
    last_preflight: dict = {}
    best_md = canonical_previous   # 最后一版结构完整的全文（返修失败时交付复检，不丢稿）
    try:
        for repair_attempt in range(1, 4):
            # 证据模块（Top10 表 / 代表原文 / 核心成果表等）由 assemble 按统计产物重建，
            # 不交给模型整段重写——否则返修输出比首写还长，易超时或被截断。
            md = semantic.chat(
                sys_p,
                build_user_prompt(_strip_generated_modules(current_md), current_issues),
                max_tokens=16384, lm_scope="fix_report", **_REPORT_LLM)
            try:
                if not md.strip():
                    raise ValueError("模型返回空修订")
                # FIX-20：返修节点保留禁用措辞硬校验（节点内最多 3 次返修 + Validator 预检兜底），
                # 但不删句——删句会掩盖问题并连带删数据和原文。
                md = report_contract.assemble(md, **assembly_args)
                _assert_no_forbidden_terms(md)
                _assert_report_structure(
                    md, list((stats.get("campaigns") or {}).keys()),
                )
                report_contract.assert_preserved_structure(canonical_previous, md)
                report_contract.assert_hot_tables(
                    md, stats,
                    str((assembly_args.get("plan") or {}).get("content_scope")
                        or "all").lower(),
                )
            except ValueError as error:
                # Keep the last valid full document. Never feed a truncated or
                # structurally invalid replacement forward as the new baseline.
                issue = {
                    "id": "P0-repair-structure", "severity": "P0",
                    "title": "返修结构或证据完整性未通过",
                    "evidence": str(error), "locus": "report_md",
                }
                current_issues = list(issues) + [issue]
                last_preflight = {"issues": [issue], "scan_mode": "structure"}
                continue
            last_preflight = validator_mod.validate_report(
                {"markdown": md, "degraded": False},
                requirement=requirement,
                stats=stats,
                charts=charts_payload,
            )
            # FIX-39：返修改动的数字须来自 stats；否则视为编造（P0「数字与统计不符」）。
            fix_record = report_contract.number_diff(canonical_previous, md, stats, charts_payload)
            if fix_record["unknown"]:
                last_preflight = {**last_preflight, "issues": list(last_preflight.get("issues") or []) + [{
                    "id": "P0-fix-number-mismatch", "severity": "P0", "title": "数字与统计不符",
                    "evidence": "、".join(fix_record["unknown"][:10]), "locus": "report_md"}]}
            remaining_p0 = [
                item
                for item in (last_preflight.get("issues") or [])
                if item.get("severity") == "P0"
            ]
            if not remaining_p0:
                return {"artifacts": {"report_md": {
                    "fix_record": fix_record["records"],
                    "markdown": md, "chapters": chapters, "version": version,
                    "fixed_round": rnd,
                    "source": "llm",
                    "guardrail_hits": _scan_forbidden(md),
                    "deterministic_preflight": {
                        "attempts": repair_attempt,
                        "p0_count": 0,
                        "scan_mode": last_preflight.get("scan_mode"),
                    },
                }}}
            current_md = md
            best_md = md
            current_issues = remaining_p0
            sys_p += (
                "\n上一版未通过确定性预检。下一次必须精确关闭以下剩余 P0，"
                "不得用仅语义近似但机器规则不接受的表达。"
            )
        # 原版 Producer v0.60:286「最多 3 轮，超限触发 HC-7」：不判 run 失败。返回最后一版
        # 结构完整的报告并标出未关闭的 P0，交 n17 独立复检、n18 按轮次路由（超限进 HC7 由用户处置）。
        unresolved = [item for item in (last_preflight.get("issues") or [])
                      if item.get("severity") == "P0"]
        state.event(node.get("id", "n19_fix"), "repair_unresolved",
                    ", ".join(str(i.get("id") or i.get("title") or "unknown")
                              for i in unresolved)[:500])
        return {"artifacts": {"report_md": {
            "markdown": best_md, "chapters": chapters, "version": version,
            "fixed_round": rnd, "source": "llm",
            "guardrail_hits": _scan_forbidden(best_md),
            "unresolved_p0": unresolved,
            "deterministic_preflight": {
                "attempts": 3, "p0_count": len(unresolved),
                "scan_mode": last_preflight.get("scan_mode"),
                "summary": _issue_text(unresolved)[:1000],
            },
        }}}
    except Exception:
        raise


_EVIDENCE_BLOCK = re.compile(
    r"<!-- report-module:start -->.*?<!-- report-module:end -->", re.S)


def _strip_generated_modules(md: str) -> str:
    """去掉代码生成的模块（assemble 会按统计产物原样重建），只把可编辑正文交给模型。"""
    return re.sub(r"\n{3,}", "\n\n", _EVIDENCE_BLOCK.sub("", md or ""))


def _mask_evidence(md: str) -> str:
    """挖空代码生成的证据模块（热门表 / 代表原文），保持行数不变。

    证据里的原文引用属于事实，不是作者措辞：产品讨论中的“性价比”等表达必须放行，
    否则会误删证据（FIX-20）。
    """
    return _EVIDENCE_BLOCK.sub(
        lambda m: "\n" * m.group(0).count("\n"), md or "")


def _scan_forbidden(md: str) -> list[dict]:
    """检测禁用措辞与越界指标名，返回逐条命中（不修改正文）。

    命中不再自动删句：删除会连带删掉数据与原文，且掩盖问题。
    交由 n17 质检判 P0、n19 改写（FIX-20）。
    """
    hits: list[dict] = []
    for ln_no, line in enumerate(_mask_evidence(md).splitlines(), 1):
        for term in FORBIDDEN_METRIC_TERMS:
            if term in line:
                hits.append({"line": ln_no, "term": term,
                             "text": line.strip()[:200], "kind": "term"})
        for pattern in FORBIDDEN_METRIC_PATTERNS:
            if pattern.search(line):
                hits.append({"line": ln_no, "term": pattern.pattern,
                             "text": line.strip()[:200], "kind": "metric_scope"})
    return hits


def _assert_no_forbidden_terms(md: str) -> None:
    """报告正文禁用措辞硬校验：命中即报错（证据模块内的原文引用豁免）。"""
    hits = _scan_forbidden(md)
    if hits:
        first = hits[0]
        raise ValueError(
            f"report: 正文命中禁用措辞 '{first['term']}'（L{first['line']}）"
        )


def _report_version(state) -> str:
    """报告版本号 {minor}.{patch}（原版：P0 修复递增 patch，重写递增 minor；只写在元数据/文件名）。"""
    return f"{state.counters.get('report_minor', 0)}.{state.counters.get('report_patch', 0)}"


# FIX-42：报告结构固定四章（KS_Report_Template §一「固定，不可调整」），不再随参数切换。
REPORT_CHAPTERS_MODE = "four"


# FIX-44：客户 KS_Content_Analysis_Prompt「经验总结合成 Prompt」（质量红线与输出格式原文）。
EXPERIENCE_SYSTEM = (
    "## 角色\n你是一位资深营销复盘分析师，擅长从数据中提炼可操作的战略结论。\n\n"
    "## 质量红线（P0，必须遵守）\n"
    "- 每条成功经验和待改进问题，必须显式标注来源章节和具体数值\n"
    "- 禁止空泛表述，如「达人策略效果良好，建议继续优化达人组合」\n"
    "- 行动计划项必须具体可执行，不允许「持续优化X」「加强Y」等模糊表述\n"
    "- 待改进问题只针对活动执行层问题（达人策略/活动机制/运营体验/内容形式等），"
    "不包含品牌外部负面事件\n"
    "- 禁止引用无来源的行业对比数据，所有比较基准必须来自报告输入数据\n"
    "- 活动目标含「（基于数据推断）」时，涉及「是否达成目标」的表述须同步加注「基于数据推断」\n"
    "- 指标只用条均互动量/品牌提及率/产品提及占比/NSR，禁用人均/转化/性价比\n"
    "- 没有成本数据时，不给预算配比、合作比例调整（如「某层级合作比例从X%提升至Y%」）或性价比/ROI结论；"
    "改为写数据支持的表现说明和可验证的建议（如「下次活动对比X与Y层级的条均互动量再定投放」）\n\n"
    "## 输出格式\n严格只输出以下三个小节（不要输出其他章节）：\n"
    "### 3.1 成功经验\n\n**经验 1：{策略/机制描述，10字以内}**\n"
    "数据支撑：{来源章节} 显示，{具体数值/比例/变化量}。\n可复制建议：{1句话}\n"
    "（共 3 条）\n\n### 3.2 待改进问题\n\n**问题 1：{短板描述，10字以内}**\n"
    "根因定位：{来源章节} 数据显示，{具体数值}。\n改进方向：{1-2句}\n（共 3 条）\n\n"
    "### 3.3 后续行动计划\n\n**立即行动**（下次活动前必须落实）\n· {具体事项}\n\n"
    "**短期优化**（1-2 个活动周期内改进）\n· {具体事项}\n\n**长期策略**\n· {方向性调整，1条}\n"
)


def _campaign_goal_text(plan: dict, stats: dict) -> str:
    """活动目标：用户填写直接引用；未填写时标注「基于数据推断」（客户 Prompt 输入说明）。"""
    goals = [g for g in (plan.get("campaign_goal") or []) if g and g != "待数据推断"]
    if goals:
        return "、".join(goals)
    # Q1：不再用数据推断的活动类型冒充活动目标。
    return "未填写（基于数据推断，须在相关表述中注明）"


def _synthesize_experience(md: str, plan: dict, stats: dict) -> str:
    """第二段生成：以第一段全文为洞察输入，重写「三、经验总结」的 3.1~3.3。"""
    names = "、".join((stats.get("campaigns") or {}).keys()) or str(plan.get("activity_name") or "")
    user = (f"品牌名：{plan.get('brand') or ''}\n活动名：{names}\n"
            f"活动目标：{_campaign_goal_text(plan, stats)}\n\n"
            "[全报告 Insight 内容]\n" + _chapter_text(md, 0, 2))
    summary = semantic.chat(EXPERIENCE_SYSTEM, user, max_tokens=4096, lm_scope="write_report", **_REPORT_LLM)
    for number, title in _POLICY.SUMMARY_SECTIONS:
        if not re.search(rf"(?m)^#{{1,4}}\s+{re.escape(number)}\s+{re.escape(title)}", summary or ""):
            raise ValueError(f"write_report: 经验总结合成缺少「{number} {title}」")
    chapter = re.search(rf"(?m)^#{{1,2}}\s+{re.escape(_POLICY.REPORT_CHAPTERS[2])}[^\n]*\n", md)
    nxt = re.search(rf"(?m)^#{{1,2}}\s+{re.escape(_POLICY.REPORT_CHAPTERS[3])}", md)
    if not chapter or not nxt:
        raise ValueError("write_report: 找不到三、四章边界，无法写入经验总结")
    body = re.sub(r"(?m)^#{1,6}\s+(3\.\d)", r"### \1", summary.strip())
    return md[:chapter.end()] + "\n" + body + "\n\n" + md[nxt.start():]


def _chapter_text(md: str, first: int, last: int) -> str:
    """取第 first..last 章（含）的正文，作为经验总结的洞察输入。"""
    start = re.search(rf"(?m)^#{{1,2}}\s+{re.escape(_POLICY.REPORT_CHAPTERS[first])}", md)
    end = re.search(rf"(?m)^#{{1,2}}\s+{re.escape(_POLICY.REPORT_CHAPTERS[last])}", md)
    return md[start.start() if start else 0:end.start() if end else len(md)]


def _report_structure_rule(campaign_count: int) -> str:
    """由 policy 结构常量渲染写作约束，避免 prompt 与质检各写一份。"""
    n = max(campaign_count, 1)
    campaign = "、".join(f"2.N.{k} {t}" for k, t in _POLICY.CAMPAIGN_SECTIONS)
    brand = "、".join(f"{k} {t}" for k, t in _POLICY.BRAND_SECTIONS)
    summary = "、".join(f"{k} {t}" for k, t in _POLICY.SUMMARY_SECTIONS)
    return (f"报告固定四章（{'、'.join(_POLICY.REPORT_CHAPTERS)}），标题后依次为数据说明与整体洞察；"
            f"品牌章含 {brand}；每个活动独立包含 {campaign}；"
            f"二章最后一节为「{_POLICY.lift_prefix(n)} {_POLICY.LIFT_SECTION_TITLE}」"
            "（与 2.N 同级，品牌整体口径，节首一句说明，基准不可用也保留标题并说明原因）；"
            f"三章含 {summary}（只写标题占位，系统在全文完成后单独合成）；"
            "四章只写 3-5 条有数据支撑的待改善方向（核心成果表由系统生成），不设下次活动建议；"
            "不得生成已下线的竞品章节或「综合表现定位」章；")


def _assert_report_structure(md: str, campaign_ids: list[str]) -> None:
    """节点内结构硬校验：直接复用质检的 _structure_issues（同一份 policy 常量与匹配规则）。

    另外要求数据说明与整体洞察存在（装配前后都会调用）。
    """
    scan = validator_mod._load_scan()
    if not scan or not hasattr(scan, "_structure_issues"):
        raise RuntimeError("report: 无法加载 topic5-validator 结构检查")
    problems = [issue["title"] for issue in scan._structure_issues(md, list(campaign_ids))
                if issue["severity"] == "P0"]
    problems += [f"缺「{name}」" for name in ("数据说明", "整体洞察") if name not in md]
    if problems:
        raise ValueError("report: 缺少必需章节或活动分节: " + "；".join(problems))


_REPORT_LABELS = {
    "positive": "正面",
    "neutral": "中性",
    "negative": "负面",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "weibo": "微博",
    "PGC": "PGC",
    "UGC": "UGC",
    "UKN": "未知",
    "UNK": "未知",
    "pgc": "PGC",
    "ugc": "UGC",
}


def _report_label(value: object) -> str:
    text = str(value or "").strip()
    return _REPORT_LABELS.get(text, text or "未知")


def _fmt_ratio(value: object) -> str:
    """比率 0-1 → 百分比文案；None 输出「不适用」（不补 0）。"""
    if not isinstance(value, (int, float)):
        return "不适用"
    return f"{value * 100:.1f}%"


def _pct_text(value: object) -> str:
    """变化率文案：None → 「不适用」（原版 fmt_pct 口径）。"""
    if not isinstance(value, (int, float)):
        return "不适用"
    return f"{value:+.1f}%"


def _format_report_number(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value or "N/A")


def _format_distribution(value: object) -> str:
    """Render internal distribution objects as compact user-facing prose."""
    data = value if isinstance(value, dict) else {}
    if isinstance(data.get("_total"), dict):
        data = data["_total"]
    parts = [
        f"{_report_label(key)} {_format_report_number(count)} 条"
        for key, count in data.items()
        if isinstance(count, (int, float)) and not isinstance(count, bool)
    ]
    return "；".join(parts) if parts else "暂无可用数据"


def _markdown_text(value: object, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("[", "【").replace("]", "】")
    return text[:limit] + ("…" if len(text) > limit else "")


def _format_hot_content(value: object, *, limit_per_group: int = 3) -> list[str]:
    """Keep only evidence fields suitable for the report; never expose raw rows."""
    if isinstance(value, list):
        groups = [("热门内容", value)]
    elif isinstance(value, dict):
        groups = [
            (_report_label(group), rows)
            for group, rows in value.items()
            if isinstance(rows, list)
        ]
    else:
        groups = []
    lines: list[str] = []
    for group, rows in groups:
        for row in rows[:limit_per_group]:
            if not isinstance(row, dict):
                continue
            title = _markdown_text(row.get("title") or row.get("content"), 70)
            if not title:
                title = "未命名内容"
            url = str(row.get("url") or "").strip()
            title_text = f"[{title}]({url})" if url.startswith("https://") else title
            details = []
            if row.get("author"):
                details.append(f"作者：{_markdown_text(row.get('author'), 30)}")
            if row.get("interaction_cnt") is not None:
                details.append(
                    f"互动量：{_format_report_number(row.get('interaction_cnt'))}"
                )
            suffix = f"（{'；'.join(details)}）" if details else ""
            lines.append(f"- {group}：{title_text}{suffix}")
    return lines or ["- 暂无可展示的热门内容证据。"]


def _fallback_report_md(stats, charts, wc_insight, chapters, version) -> str:
    """确定性降级报告：用真数据渲染四章骨架 MD（FIX-42），供下游质检/交付消费。

    嵌入 n14 产出的图表/封面（chart_urls={charts:[...], cover:{local_path}}）：封面置顶，
    各活动章节下嵌入该活动的情感/平台/趋势/内容图，词云派生图置 2.{N+1} 之后。图片以本地
    PNG 路径嵌入（`![](local_path)`），由 feishu.md_to_blocks 上传 media 进飞书文档；
    local_path 为空（降级无图）则跳过，不塞死链。
    """
    camps = (stats or {}).get("campaigns") or {}
    chart_items = (charts or {}).get("charts") or []
    cover = (charts or {}).get("cover") or {}

    def _img_src(c) -> str:
        """图片引用源：优先本地 PNG 路径（进飞书 media），回退 url（浏览器预览兼容）。"""
        return c.get("local_path") or c.get("url") or ""

    # 按 chart_id 前缀归类：{cid}_xxx 归该活动，wordcloud_xxx 归派生区。
    by_cid: dict[str, list] = {}
    derived: list = []
    for c in chart_items:
        cid_key = str(c.get("chart_id") or "")
        if cid_key.startswith("wordcloud_"):
            derived.append(c)
            continue
        owner = cid_key.rsplit("_", 1)[0] if "_" in cid_key else ""
        by_cid.setdefault(owner, []).append(c)

    lines = [f"# 营销活动复盘报告（v{version}）", ""]
    if _img_src(cover):
        lines += [f"![封面]({_img_src(cover)})", ""]
    lines += [f"> 结构：{chapters} · 指标口径：{'/'.join(METRIC_WHITELIST)}",
              "> 本报告由确定性模板生成，数字均取自统计产物。", ""]
    data_notes = [
        f"- 活动数：{len(camps)}（多活动逐个处理，不相加为合计声量）",
        "- 日期口径：发表时间；情感以 AI 标注为准。",
    ]
    sampling = ((stats or {}).get("clean_record") or {}).get("sampling") or {}
    if sampling.get("applied"):
        data_notes.append(
            "- " + (
                sampling.get("disclosure")
                or (
                    f"demo 模式从 {sampling.get('population_size')} 条清洗后活动明细中"
                    f"抽样 {sampling.get('sample_size')} 条，仅供参考，不代表全量结论。"
                )
            )
        )
    lines += ["### 数据说明", "", *data_notes, ""]
    lines += ["## 整体洞察", "",
              "以下结论仅基于已确认统计产物与证据链。", ""]
    brand = (stats or {}).get("brand") or {}
    lines += ["## 一、品牌传播表现", "",
              "### 1.1 传播概况",
              f"- 品牌声量：{brand.get('total_volume')}",
              f"- 品牌互动量：{brand.get('total_interaction')}", "",
              "### 1.2 口碑分析",
              f"- 情感分布：{_format_distribution(brand.get('sentiment_dist'))}", "",
              "### 1.3 内容分析",
              f"- 平台分布：{_format_distribution(brand.get('platform_dist'))}",
              f"- 内容类型：{_format_distribution(brand.get('content_type_dist'))}", "",
              "### 1.4 热门内容"]
    lines.extend(_format_hot_content(brand.get("hot_content")))
    lines.append("")
    lines += ["## 二、活动传播表现", ""]
    for index, (cid, s) in enumerate(camps.items(), 1):
        m = (s or {}).get("metrics", {})
        lines.append(f"### 2.{index} 活动 {cid}")
        lines.append(f"#### 2.{index}.1 传播概况")
        lines.append(f"- 活动相关条数：{m.get('活动相关条数')}")
        lines.append(f"- 条均互动量：{m.get('条均互动量')}")
        # 条均互动倍数：活动条均 ÷ 品牌条均，分母缺失时不输出（不补 1.0 冒充）。
        mult = m.get("条均互动倍数")
        if mult is not None:
            lines.append(f"- 条均互动倍数：{mult}（活动条均互动量 ÷ 品牌条均互动量）")
        lines.append(f"#### 2.{index}.2 提及漏斗分析")
        lines.append(f"- 品牌提及率：{_fmt_ratio(m.get('品牌提及率'))}")
        lines.append(f"- 产品提及占比：{_fmt_ratio(m.get('产品提及占比'))}")
        # 品牌主导讨论占比：AI 主对象口径，与品牌提及率是两种方法，须分别记录。
        dom = m.get("品牌主导讨论占比")
        if dom is not None:
            lines.append(
                f"- 品牌主导讨论占比：{_fmt_ratio(dom)}"
                "（AI 主讨论对象=品牌，非品牌名出现率）")
        coverage = (s or {}).get("dimension_coverage") or {}
        if coverage.get("note") and coverage.get("missing"):
            lines.append(f"- *{coverage['note']}*")
        lines.append(f"#### 2.{index}.3 口碑分析")
        lines.append(f"- NSR：{m.get('NSR')}")
        lines.append(f"#### 2.{index}.4 内容分析")
        lines.append(
            f"- 内容类型：{_format_distribution((s or {}).get('content_type_dist'))}"
        )
        lines.append(f"#### 2.{index}.5 热门内容")
        lines.extend(_format_hot_content((s or {}).get("top_content")))
        lines.append("")
        imgs = [c for c in by_cid.get(cid, []) if _img_src(c)]
        for c in imgs:
            lines.append(f"![{c.get('title')}]({_img_src(c)})")
        if imgs:
            lines.append("")
    lift = _POLICY.lift_prefix(len(camps))
    lines += [f"### {lift} {_POLICY.LIFT_SECTION_TITLE}", "",
              "*以下为品牌整体口径（合并窗口），不是单个活动的 2.N 指标。*", ""]
    baseline = (stats or {}).get("baseline_lift") or {}
    if baseline.get("status") == "available":
        days = baseline.get("days") or {}
        if baseline.get("holiday_text"):
            # 基准期落在节假日高峰月：用原版固定措辞，禁止直接写「下降 X%」。
            lines.append(
                "- " + str(baseline["holiday_text"]) + "：活动期日均 "
                f"{_format_report_number((baseline.get('daily') or {}).get('period', {}).get('volume'))} 条 vs "
                f"基准期日均 {_format_report_number((baseline.get('daily') or {}).get('base', {}).get('volume'))} 条"
                f"（{_pct_text(baseline.get('daily_volume_change_pct'))}）。"
            )
        elif baseline.get("base_insufficient"):
            lines.append("- 基准期声量不足 100 条，本节不输出变化率；建议延长基准期或跳过对比。")
        else:
            lines.append(
                f"- 活动期 {days.get('period')} 天、基准期 {days.get('base')} 天，"
                f"日均声量变化 {_pct_text(baseline.get('daily_volume_change_pct'))}，"
                f"日均互动量变化 {_pct_text(baseline.get('daily_interaction_change_pct'))}。"
            )
            if baseline.get("nsr_insufficient"):
                lines.append("- NSR 变化：样本不足（正面+负面合计 <30），不输出。")
            elif baseline.get("nsr_change_pp") is not None:
                lines.append(f"- NSR 变化：{baseline['nsr_change_pp']:+.1f}pp。")
    else:
        lines.append("- 基准期数据不可用，本节不输出确定性比较结论。")
    lines.append("")
    derived_imgs = [c for c in derived if _img_src(c)]
    for c in derived_imgs:
        lines.append(f"![{c.get('title')}]({_img_src(c)})")
    if derived_imgs:
        lines.append("")
    lines += [
        "## 三、经验总结", "",
        "### 3.1 成功经验", "",
        "- 证据不足，暂不形成确定性成功经验；后续结论需回指来源章节与数值。", "",
        "### 3.2 待改进问题", "",
        "- 证据不足，暂不形成确定性问题归因。", "",
        "### 3.3 后续行动计划", "",
        "- 证据补齐后按「立即行动 / 短期优化 / 长期策略」制定。", "",
    ]
    lines += ["## 四、总结与建议", "", "- 核心成果与待改善方向见上文各节数据。", ""]
    return "\n".join(lines)


# ── decision 节点 ───────────────────────────────────────────
# FIX-06：Validator 高点核查发现明确噪音 → 退回 Step5.5 去噪并全链重算，最多 2 次后走 HC7。
NOISE_ROLLBACK_MAX = 2
NOISE_ROLLBACK_INVALIDATE = [
    "denoised_data", "annotated_round1", "r1_review", "opinion_candidates", "confirmed_opinion_labels",
    "ugc_category_list", "annotated_data", "r2_review", "stats_bundle", "chart_urls",
    "wordcloud_insight", "report_md", "qc_report", "delivery_draft",
]


def qc_noise_ids(qc: dict) -> list[str]:
    return sorted({str(i) for issue in (qc.get("issues") or []) if isinstance(issue, dict)
                   for i in (issue.get("noise_ids") or [])})


@handler("qc_decision")
def qc_decision(node, state, ctx):
    qc = state.read_artifact("qc_report") or {}
    max_rounds = state.params.get("qc_max_rounds", 3)
    rounds = state.counters.get("qc_rounds", 0)
    if not qc.get("has_p0"):
        return {"route": "no_p0"}
    if qc_noise_ids(qc):
        if state.counters.get("noise_rollbacks", 0) >= NOISE_ROLLBACK_MAX:
            # 噪音回退次数上限为迁移版新增（原版无次数限制）：留痕，HC7 卡片据此说明进入原因。
            state.event(node.get("id", "n18_qc_gate"), "noise_rollback_maxed",
                        f"已回退 {NOISE_ROLLBACK_MAX} 次仍发现噪音，转 HC7 由用户处置")
            return {"route": "has_p0_maxed"}
        return {"route": "noise_found"}
    if rounds >= max_rounds:
        return {"route": "has_p0_maxed"}
    return {"route": "has_p0_retry"}


@handler("noise_rollback")
def noise_rollback(node, state, ctx):
    """把 Validator 判定的噪音 ID 交给 n07b 增量去噪，失效去噪及其下游全部产物（KS_Noise_Review 退回入口）。"""
    ids = qc_noise_ids(state.read_artifact("qc_report") or {})
    if not ids:
        raise RuntimeError("noise_rollback: qc_report 无 noise_ids")
    state.counters["noise_rollbacks"] = state.counters.get("noise_rollbacks", 0) + 1
    state.counters["rerun_epoch"] = state.counters.get("rerun_epoch", 0) + 1
    state.params["qc_noise_ids"] = sorted(set(map(str, state.params.get("qc_noise_ids") or [])) | set(ids))
    state.invalidate_artifacts(NOISE_ROLLBACK_INVALIDATE)
    return {"artifacts": {"noise_rollback_record": {
        "noise_ids": ids, "rollback": state.counters["noise_rollbacks"],
        "rerun_epoch": state.counters["rerun_epoch"], "invalidated": NOISE_ROLLBACK_INVALIDATE}}}


@handler("delivery_draft_decision")
def delivery_draft_decision(node, state, ctx):
    """Route content defects to repair and external permission failures to HC7."""
    draft = state.read_artifact("delivery_draft") or {}
    validation = draft.get("post_publish_validation") or {}
    # HC7 放宽（FIX-35）记在 qc_report.waivers；已豁免的发布后 P0 不再拦截。
    waived = {str(w.get("issue_id")) for w in
              ((state.read_artifact("qc_report") or {}).get("waivers") or [])
              if isinstance(w, dict)}
    issues = [i for i in (validation.get("issues") or [])
              if not (isinstance(i, dict) and str(i.get("id")) in waived)]
    open_p0 = [i for i in issues if isinstance(i, dict) and i.get("severity") == "P0"]
    if not open_p0 and (draft.get("state") == "ready" or waived):
        return {"route": "ready"}
    external_p0 = any(
        isinstance(issue, dict)
        and issue.get("severity") == "P0"
        and (
            issue.get("locus") == "feishu-permission"
            or issue.get("id") == "P0-postpublish-owner"
        )
        for issue in issues
    )
    if external_p0:
        return {"route": "has_p0_maxed"}

    max_rounds = state.params.get("qc_max_rounds", 3)
    rounds = state.counters.get("qc_rounds", 0)
    if rounds >= max_rounds:
        return {"route": "has_p0_maxed"}
    return {"route": "has_p0_retry"}


# ── v0.2 新增：方案 / Run 初始化 / 交付 draft·commit / 终验路由 ──────────
CAMPAIGN_TYPE_UNRESOLVED = ("", "待数据推断", "待确认")
CAMPAIGN_TYPE_PENDING_NOTE = ("活动类型尚未确认：视为未解决需求项，不由数据推断；依赖活动类型的"
                              "赞助型声量占比模块暂不执行。请在需求单选择类型（可参考关键词能力初判），"
                              "复合或表外类型选「其他」并写明。")


def unresolved_campaign_type(requirement: dict) -> bool:
    return str((requirement or {}).get("campaign_type") or "").strip() in CAMPAIGN_TYPE_UNRESOLVED


@handler("plan_compile")
def plan_compile(node, state, ctx):
    """Compile the complete G2 review contract from the confirmed requirement."""
    requirement = state.read_artifact("requirement_confirmed") or {}
    if not isinstance(requirement, dict) or not requirement.get("confirmed"):
        raise ValueError("plan_compile: requirement_confirmed 尚未确认")

    def stable_hash(value: Any) -> str:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    activity_name = str(requirement.get("activity_name") or "营销活动")
    campaigns = requirement.get("campaigns") or []
    activity_names = [
        str(item.get("id"))
        for item in campaigns
        if isinstance(item, dict) and item.get("id")
    ] or [activity_name]
    plan_platforms = [str(item) for item in (requirement.get("platforms") or [])]
    content_scope = str(requirement.get("content_scope") or "all")
    source_file = requirement.get("source_file") or {}
    plan_revision = int(
        (state.artifacts.get("analysis_plan") or {}).get("version", 0)
    ) + 1
    previous_confirmation = state.read_artifact("confirmed_plan") or {}
    revision_request = (
        previous_confirmation.get("note")
        if isinstance(previous_confirmation, dict)
        and previous_confirmation.get("confirmed") is False
        else ""
    )

    core_modules = [
        "brand_context",
        "activity_performance",
        "evidence_summary",
    ]
    conditional_modules = [
        "baseline_lift",
        "sentiment_opinion",
        "wordcloud_topics",
        "top_content",
        "kol_effectiveness",
    ]
    reasons = {
        "baseline_lift": "对比活动期与前置基准期，判断相对变化",
        "sentiment_opinion": "输出情感结构、观点标签与讨论维度",
        "wordcloud_topics": "品牌词云按平台提炼议题；活动词云综合已选平台（同词频次求和）",
        "top_content": "保留代表性内容作为结论证据",
        "kol_effectiveness": "按可用粉丝量派生达人层级，缺失时不强行出图",
    }
    if requirement.get("product_query"):
        conditional_modules.append("product_funnel")
        reasons["product_funnel"] = "需求提供了产品关键词，可计算品牌提及内的产品提及占比"
    if content_scope in ("all", "ugc"):
        conditional_modules.append("ugc_participation")
        reasons["ugc_participation"] = "范围包含 UGC，需要识别参与类型与内容贡献"
    if len(activity_names) > 1:
        conditional_modules.append("multi_activity_compare")
        reasons["multi_activity_compare"] = "包含多个活动，逐活动计算并横向比较，不合并相加"
    enabled_modules = core_modules + conditional_modules

    module_registry = {
        "brand_context": {
            "inputs": ["confirmed_plan.brand_query", "period", "platforms"],
            "data_source": "controlled-stat-tool",
            "route": "SSJH-Social-Stat",
            "outputs": ["brand_stats"],
            "failure_strategy": "fail-closed; degraded artifacts cannot enter the report chain",
        },
        "activity_performance": {
            "inputs": ["user-uploaded activity_detail XLSX"],
            "data_source": "user-uploaded-excel",
            "route": "excel_source + deterministic cleaning/aggregation",
            "outputs": ["cleaned_data", "stats_bundle"],
            "failure_strategy": "schema/date/range errors stop the affected run",
        },
        "evidence_summary": {
            "inputs": ["stats_bundle", "chart_urls", "wordcloud_insight"],
            "data_source": "versioned pipeline artifacts",
            "route": "Narrative handler",
            "outputs": ["report_md"],
            "failure_strategy": "missing evidence blocks report assembly",
        },
        "baseline_lift": {
            "inputs": ["brand_query", "30-day non-overlapping baseline"],
            "data_source": "controlled-stat-tool",
            "route": "SSJH-Social-Stat",
            "outputs": ["brand_stats_baseline"],
            "failure_strategy": "create DataIssue and suppress unsupported comparison",
        },
        "sentiment_opinion": {
            "inputs": ["cleaned_data", "confirmed label set"],
            "data_source": "activity_detail rows",
            "route": "small Ark engine or DataHub by threshold",
            "outputs": ["annotated_round1", "annotated_data"],
            "failure_strategy": "coverage ERROR stops; no silent AI sentiment fallback in full",
        },
        "wordcloud_topics": {
            "inputs": ["queries", "period", "platforms"],
            "data_source": "controlled-stat-tool",
            "route": "SSJH-Social-Stat → wordcloud-insight v0.2.6 render → full-table Insight handler + compose",
            "outputs": ["wordcloud_data", "wordcloud_insight"],
            "failure_strategy": "invalid table/render/provided-post evidence fails closed; optional absent posts use frequency analysis; never merge platforms",
        },
        "top_content": {
            "inputs": ["activity rows with URL/content/interaction"],
            "data_source": "activity_detail rows",
            "route": "deterministic ranking",
            "outputs": ["Top PGC/UGC evidence"],
            "failure_strategy": "missing fields are disclosed; no fabricated rows",
        },
        "kol_effectiveness": {
            "inputs": ["platform", "fans", "content type", "interaction"],
            "data_source": "activity_detail rows",
            "route": "deterministic platform-aware classification",
            "outputs": ["KOL tier metrics"],
            "failure_strategy": "omit module with explicit reason when fan data unavailable",
        },
        "product_funnel": {
            "inputs": ["brand_words", "product_words", "activity-related rows"],
            "data_source": "activity_detail rows",
            "route": "deterministic nested-set aggregation",
            "outputs": ["brand mention rate", "product mention share"],
            "failure_strategy": "enforce product subset of brand; zero denominator yields N/A",
        },
        "ugc_participation": {
            "inputs": ["activity-related UGC rows"],
            "data_source": "activity_detail rows",
            "route": "Discovery handler + confirmed categories",
            "outputs": ["ugc_category_list", "UGC distribution"],
            "failure_strategy": "PGC-only is explicit N/A; label changes invalidate downstream",
        },
        "multi_activity_compare": {
            "inputs": ["rows partitioned by confirmed activity"],
            "data_source": "activity_detail rows",
            "route": "deterministic per-activity aggregation",
            "outputs": ["per-activity metrics and comparison"],
            "failure_strategy": "never sum overlapping activities into one total",
        },
    }
    unknown_modules = [name for name in enabled_modules if name not in module_registry]
    questions = requirement.get("business_questions") or []
    if isinstance(questions, str):
        questions = [questions]
    objective_capability_map = []
    unsupported_objectives = []
    capability_keywords = {
        "activity_performance": (
            "活动表现", "表现", "传播", "声量", "互动", "提及", "效果", "口碑", "NSR",
        ),
        "sentiment_opinion": ("情感", "观点", "评价", "讨论"),
        "wordcloud_topics": ("内容", "话题", "议题", "关键词", "热点"),
        "top_content": ("热门", "高互动", "代表内容"),
        "kol_effectiveness": ("达人", "KOL", "博主"),
        "ugc_participation": ("UGC", "用户参与", "参与方式"),
        "evidence_summary": ("经验", "改进", "建议", "总结", "策略", "复盘"),
        "product_funnel": ("产品", "品牌提及"),
        "multi_activity_compare": ("多活动", "对比", "比较"),
    }
    unsupported_terms = (
        "销售转化", "转化率", "营销 ROI", "ROI", "GMV", "营收", "销量",
        "购买率", "成交",
    )
    for question in questions:
        text = str(question).strip()
        matched = [
            module for module, keywords in capability_keywords.items()
            if any(keyword.lower() in text.lower() for keyword in keywords)
            and module in enabled_modules
        ]
        reason = None
        if any(term.lower() in text.lower() for term in unsupported_terms):
            matched = []
            reason = "缺少销售/交易数据源和已登记转化或 ROI 模块"
        elif not matched:
            reason = "无法映射到当前已登记模块"
        objective_capability_map.append({
            "objective": text,
            "modules": matched,
            "supported": bool(matched),
            **({"reason": reason} if reason else {}),
        })
        if not matched:
            unsupported_objectives.append(text)
    delivery_supported = any(
        marker in str(requirement.get("delivery_expectation") or "")
        for marker in ("飞书", "报告", "复盘")
    )
    actions_supported = (
        "已登记" in str(requirement.get("allowed_actions") or "")
        or str(requirement.get("allowed_actions") or "").strip()
        in ("分析与生成报告", "只读分析")
    )

    required_fields = (
        "brand", "activity_name", "period_start", "period_end", "platforms",
        "brand_query", "activity_query", "content_scope", "business_questions",
        "delivery_expectation", "allowed_actions",
    )
    missing_fields = [key for key in required_fields if not requirement.get(key)]
    source_declared = bool(
        isinstance(source_file, dict)
        and source_file.get("name")
        and source_file.get("mount_path")
    )
    preflight = {
        "status": (
            ("READY" if source_declared else "READY_PENDING_INPUT")
            if (
                not missing_fields and not unknown_modules
                and not unsupported_objectives
                and delivery_supported and actions_supported
            ) else (
                "NEEDS_CAPABILITY_EXTENSION"
                if unsupported_objectives or not delivery_supported or not actions_supported
                else "BLOCKED"
            )
        ),
        "checks": [
            {"name": "required_requirement_fields", "ok": not missing_fields,
             "detail": missing_fields},
            {"name": "activity_detail_reference", "ok": source_declared,
             "deferred": not source_declared,
             "detail": source_file.get("name") if isinstance(source_file, dict) else None},
            {"name": "registered_modules_only", "ok": not unknown_modules,
             "detail": unknown_modules},
            {"name": "tool_routes_declared", "ok": all(
                module_registry[name].get("route") for name in enabled_modules
                if name in module_registry
            ), "detail": enabled_modules},
            {"name": "objectives_mapped_to_registered_modules",
             "ok": not unsupported_objectives,
             "detail": objective_capability_map},
            {"name": "delivery_supported", "ok": delivery_supported,
             "detail": requirement.get("delivery_expectation")},
            {"name": "allowed_actions_supported", "ok": actions_supported,
             "detail": requirement.get("allowed_actions")},
        ],
    }

    analysis_plan = {
        "schema_version": "1.0",
        "revision": plan_revision,
        "title": f"{activity_name}营销活动复盘分析方案",
        "scope": {
            "brand": requirement.get("brand"),
            "activities": activity_names,
            "period_start": requirement.get("period_start"),
            "period_end": requirement.get("period_end"),
            "platforms": plan_platforms,
            "content_scope": content_scope,
            # 业务属性字段。Q1：campaign_type 沿用需求阶段/关键词 Skill 确认结果，
            # 不由数据推断；「待数据推断」在 G2 前须回需求阶段澄清。
            "product_category": requirement.get("product_category") or "待数据推断",
            "campaign_type": requirement.get("campaign_type") or "待数据推断",
            # Q3：基准期沿用需求确认单的已确认起止（默认值也已在需求阶段写入）。
            "baseline_start": requirement.get("baseline_start"),
            "baseline_end": requirement.get("baseline_end"),
            "baseline_source": requirement.get("baseline_source"),
            # Q1：活动类型未确认 = 未解决需求项（G2 卡片展示，用户可打回需求阶段补充）。
            "campaign_type_pending": (CAMPAIGN_TYPE_PENDING_NOTE
                                      if unresolved_campaign_type(requirement) else ""),
            "campaign_goal": list(requirement.get("campaign_goal") or ["待数据推断"]),
            "activity_boundary": requirement.get("activity_boundary") or "",
            "actual_event_period": requirement.get("actual_event_period") or {},
            "brand_supplement_activity_terms": (
                requirement.get("brand_supplement_activity_terms") or "无"),
            "source_file_name": (
                source_file.get("name") if isinstance(source_file, dict) else None
            ),
        },
        "objectives": list(requirement.get("business_questions") or []),
        "objective_capability_map": objective_capability_map,
        "delivery_expectation": requirement.get("delivery_expectation"),
        "allowed_actions": requirement.get("allowed_actions"),
        "metrics": [
            {"name": "条均互动量",
             "definition": "供应商互动量之和 / 有互动量的活动相关内容数（缺失不补零，单独披露）",
             "source": report_contract.INTERACTION_NOTE},
            {"name": "条均互动倍数",
             "definition": "活动条均互动量 / 品牌条均互动量（活动内容单条互动相对品牌基线的倍数）"},
            {"name": "品牌提及率", "definition": "品牌提及内容数 / 活动相关内容数"},
            {"name": "品牌主导讨论占比",
             "definition": "品牌主导讨论帖数 / 活动相关帖数（以品牌为主要对象的内容比例）"},
            {"name": "产品提及占比", "definition": "产品提及内容数 / 品牌提及内容数"},
            {
                "name": "声量贡献参考",
                "definition": "活动相关声量 / 品牌同期总声量（分母<分子时不输出）",
            },
            {
                "name": "NSR",
                "definition": "(正面内容数 - 负面内容数) / (正面内容数 + 负面内容数)",
                "denominator": "正面+负面（不含中性）",
            },
        ],
        "dimensions": ["平台", "发表日期", "内容类型", "KOL 层级", "UGC 类别", "观点标签"],
        "comparisons": [
            "活动期与前置 30 天不重叠基准期对比",
            "多活动逐个计算和比较，不汇总相加",
        ],
        # FIX-42：固定四章，拉升效果位于二章末 2.{N+1}。
        "report_structure": list(_POLICY.REPORT_CHAPTERS),
        "data_requirements": [
            {
                "name": "activity_detail",
                "authority": "user-uploaded-excel",
                "required_fields": [
                    "平台", "发表时间", "内容类型", "标题/正文", "互动量",
                    "活动分析对象", "URL",
                ],
                "reference": {
                    "file_name": source_file.get("name"),
                    "mount_path": source_file.get("mount_path"),
                } if isinstance(source_file, dict) else {},
                "validation": "XLSX container, schema, period, grouping and row reconciliation",
            },
            {
                "name": "brand_period_and_baseline",
                "authority": "controlled-stat-tool",
                "parameters": ["brand_query", "brand_filter", "period", "platforms"],
            },
            {
                "name": "wordcloud",
                "authority": "controlled-stat-tool",
                "parameters": ["activity_query", "product_query", "period", "platforms"],
            },
        ],
        "chart_plan": [
            {"section": "品牌传播表现", "chart": "声量与互动趋势", "type": "dual-axis line",
             "condition": "daily series available"},
            {"section": "品牌传播表现", "chart": "平台声量分布", "type": "donut",
             "condition": "more than one platform"},
            {"section": "品牌传播表现", "chart": "PGC/UGC 结构", "type": "donut",
             "condition": "content_scope=all"},
            {"section": "口碑分析", "chart": "情感分布", "type": "donut",
             "condition": "AI sentiment coverage complete"},
            {"section": "提及漏斗分析", "chart": "品牌/产品提及漏斗", "type": "bar/funnel",
             "condition": "product query selects three-level funnel"},
            {"section": "内容分析", "chart": "逐平台词云/高频议题", "type": "wordcloud/rank",
             "condition": "platform data available"},
        ],
        "evidence_requirements": [
            "每个数字可回指输入 Artifact、口径版本和分母",
            "观点与建议必须关联代表内容或统计证据",
            "报告、图表和表格引用同一 stats_bundle 版本",
        ],
        "guardrails": [
            "活动层指标仅统计活动相关内容",
            "产品提及是品牌提及的子集，产品提及占比不超过 1",
            "指标仅使用条均互动量、品牌提及率、产品提及占比、NSR",
            "禁用人均、转化率、转化、性价比等无数据支撑表述",
            "竞品分析已下线，不生成竞品章节",
        ],
        "revision_request": revision_request or None,
    }
    execution_plan = {
        "schema_version": "1.0",
        "revision": plan_revision,
        "stages": [
            {"name": "数据准备", "work": "校验上传明细，拉取品牌活动期、基准期与词云数据"},
            {"name": "清洗标注", "work": "按发表时间过滤去重，完成相关性、情感、观点和 UGC 标注"},
            {"name": "统计洞察", "work": "按活动和平台计算白名单指标，生成图表与议题洞察"},
            {"name": "撰写质检", "work": "生成四章报告（拉升效果位于二章末 2.{N+1}），经独立 Validator 扫描并处理 P0 问题"},
            {"name": "发布交付", "work": "发布飞书文档，回读校验版本与内容后提交终验"},
        ],
        "human_gates": [
            {"gate": "G2", "purpose": "确认本分析与执行方案"},
            {"gate": "N10", "purpose": "确认观点候选标签"},
            {"gate": "G3", "purpose": "确认报告质检结果"},
            {"gate": "G4", "purpose": "确认最终交付文档"},
        ],
        "modules": [
            {"module": name, **module_registry[name]}
            for name in enabled_modules if name in module_registry
        ],
        "concurrency": [
            ["brand_period_pull", "brand_baseline_pull", "wordcloud_pull"],
            ["ugc_discovery", "round1_annotation"],
        ],
        "dependencies": [["chart_render", "wordcloud_insight", "report_write"]],
        "retry_and_timeout": {
            "external_unknown": "reconcile-before-retry",
            "side_effects": "idempotency key required",
            "g4": "2 business days reminder, then 1 business day escalation",
        },
        "preflight": preflight,
        "delivery": requirement.get("delivery_expectation"),
    }
    selected_modules = {
        "core": core_modules,
        "conditional": conditional_modules,
        "core_plus_conditional": core_modules + conditional_modules,
        "reasons": reasons,
        "excluded": {
            "competitor_sov": "竞品分析已按当前业务口径下线",
        },
        "preflight": preflight,
    }
    plan_bundle = {
        "analysis_plan": analysis_plan,
        "execution_plan": execution_plan,
        "selected_modules": selected_modules,
    }
    bundle_hash = stable_hash(plan_bundle)
    for payload in (analysis_plan, execution_plan, selected_modules):
        payload["plan_bundle_sha256"] = bundle_hash
    return {"artifacts": {
        "analysis_plan": analysis_plan,
        "execution_plan": execution_plan,
        "selected_modules": selected_modules,
    }}


def _check_activity_filterwords(plan: dict, rows: list, state) -> dict:
    """FIX-11 活动过滤词核对。

    原版口径：
      - 取品牌数据之前，先在标题/正文/话题里按字面（不区分大小写）搜索活动过滤词。
      - 有命中就告知用户并由用户决定；用户决定之前不取品牌数据。
      - 多个活动需交叉核对（每个活动用自己的过滤词集，且汇总命中）。
      - 决定写入 filterword_decision artifact，清洗环节据此剔除；重跑时读到已有决定即跳过。

    返回检查结果，供 input_schema_validation 留痕。
    """
    decided = state.read_artifact("filterword_decision") or {}
    campaigns = [c for c in (plan.get("campaigns") or []) if isinstance(c, dict)]
    if not campaigns:
        campaigns = [{"id": plan.get("activity_name")}]
    aggregate = {"hits": [], "hit_rows": 0, "total_rows": len(rows or []),
                 "ratio": 0.0, "per_campaign": {}}
    for camp in campaigns:
        words = camp.get("activity_filter") or plan.get("activity_filter") or []
        if not words:
            continue
        name = str(camp.get("id") or camp.get("name") or "")
        scoped = [row for row in (rows or [])
                  if not name or str(row.get("analysis_object") or name) == name]
        result = excel_source.check_filterwords(scoped, words)
        aggregate["per_campaign"][name] = {
            "hit_rows": result["hit_rows"],
            "total_rows": result["total_rows"],
            "ratio": result["ratio"],
            "words": result["words"],
        }
        aggregate["hits"].extend(
            {**hit, "campaign": name} for hit in result["hits"])
        aggregate["hit_rows"] = max(aggregate["hit_rows"], result["hit_rows"])
        aggregate["ratio"] = max(aggregate["ratio"], result["ratio"])
    if decided.get("decision"):
        return {**aggregate, "decision": decided.get("decision"),
                "source": "user_decision"}
    if not aggregate["hits"]:
        return {**aggregate, "decision": "no_hits", "source": "auto"}
    sample = [
        f"{hit['campaign']}／{hit['field']}／{hit['word']}：{hit['title'][:40]}"
        for hit in aggregate["hits"][:10]
    ]
    raise HumanPause(
        prompt=(
            f"活动明细中有 {aggregate['hit_rows']} 条命中活动过滤词"
            f"（占比 {aggregate['ratio']:.1%}），请确认是否剔除命中行：\n"
            + "\n".join(sample)
        ),
        expect=["filterword_decision"],
    )


# ── FIX-04 活动原帖自动采集（KS_Activity_Data_Acquisition，Step 2.5）────────────
SOCIAL_TASK_WAIT_SECONDS = 3600     # 单次节点内有界等待；超时暂停，原任务保留可恢复


def _campaign_list(plan: dict) -> list[dict]:
    campaigns = [c for c in plan.get("campaigns") or [] if isinstance(c, dict) and c.get("id")]
    return campaigns or [{"id": str(plan.get("activity_name") or "活动"),
                          "name": plan.get("activity_name")}]


@handler("activity_pull")
def activity_pull(node, state, ctx):
    """用户已提供活动明细 → 直接沿用；否则按活动调用 ssjh-social-task 抓原帖（只抓帖子）。

    首次只做数量预检并暂停请求采集授权（取消上传 HC ≠ 授权计费采集）；授权后创建/恢复原任务，
    下载后核验并适配 19 列工作簿，写入 confirmed_plan.source_file（transport=social_task）。
    """
    plan = dict(state.read_artifact("confirmed_plan") or {})
    if plan.get("source_file"):
        return {"artifacts": {"activity_detail": {"source": "user_uploaded_excel",
                                                  "source_file": plan["source_file"]}}}
    if replay.enabled():
        return _replay_activity_pull(state, plan)
    if not social_task.api_available():
        raise RuntimeError(f"activity_pull: 自动采集不可用（{social_task.unavailable_reason()}），"
                           "可在本话题上传活动明细 .xlsx 后重试")
    auth = state.read_artifact("collection_authorization") or {}
    workdir = state.run_dir / "activity_collection"
    registry = Path(os.environ.get("TOPIC5_SOCIAL_TASK_REGISTRY") or (state.run_dir.parent / "social_task_registry.json"))
    handoffs, all_rows, pending = [], [], []
    for campaign in _campaign_list(plan):
        cid = str(campaign["id"])
        rid = social_task.request_id(state.task_id, cid, str(plan.get("version") or ""))
        cdir = workdir / hashlib.sha256(cid.encode()).hexdigest()[:12]
        cdir.mkdir(parents=True, exist_ok=True)
        config = social_task.build_config(plan, campaign, rid=rid, output_file=str(cdir / "raw_export.xlsx"))
        social_task.config_scope_check(config)
        config_path = cdir / "task_config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        grant = (auth.get("campaigns") or {}).get(cid) if isinstance(auth, dict) else None
        if not grant:
            envelope = social_task.execute(config_path, cdir / "cache", registry)
            if social_task.is_budget_block(envelope):
                raise RuntimeError("activity_pull: " + str(envelope.get("message")))
            if not envelope.get("ok"):
                raise RuntimeError(f"activity_pull: 预检失败 {envelope.get('code')}")
            if envelope.get("state") == "AWAITING_QUANTITY_CONFIRMATION":
                data = envelope.get("data") or {}
                pending.append({"campaign": cid, "estimated_volume": data.get("estimated_volume"),
                                "estimate": data.get("estimate") or {}, "request_id": rid})
                continue
        else:
            envelope = social_task.execute(config_path, cdir / "cache", registry,
                                           max_records=int(grant["max_records"]),
                                           confirmation_ref=str(grant["confirmation_ref"]))
            if not envelope.get("ok"):
                raise RuntimeError(f"activity_pull: 创建任务失败 {envelope.get('code')}")
        if envelope.get("state") != "COMPLETED":
            envelope = social_task.wait_completed(config_path, cdir / "cache", registry,
                                                  SOCIAL_TASK_WAIT_SECONDS)
        run_dir = Path((envelope.get("data") or {}).get("run_dir") or "")
        scope = social_task.scope_check(run_dir)
        if not scope["ok"]:
            raise RuntimeError(f"activity_pull: 原任务范围不符（{scope['reason']}），已停止，不新建计费任务")
        records = json.loads(Path((envelope.get("data") or {})["raw_data"]).read_text(encoding="utf-8"))
        period = (campaign.get("period_start") or plan.get("period_start"),
                  campaign.get("period_end") or plan.get("period_end"))
        adapted = social_task.adapt(records, cid, period)
        if not adapted["verification"]["ok"]:
            raise RuntimeError("activity_pull: 下载核验未通过：" + "；".join(adapted["verification"]["issues"]))
        all_rows += adapted["rows"]
        handoffs.append({"campaign": cid, "request_id": rid, "job_id": envelope.get("job_id"),
                         "posts_only": True, "msg_type": {k: v.get("msg_type") for k, v in config["search"].items()
                                                          if isinstance(v, dict) and "msg_type" in v},
                         "scope_check": scope, "verification": adapted["verification"],
                         "retained_fields": adapted["retained_fields"], "missing_fields": adapted["missing_fields"],
                         "quality": (envelope.get("data") or {}).get("quality") or {},
                         "authorization_ref": (grant or {}).get("confirmation_ref", "")})
    if pending:
        raise HumanPause(prompt=json.dumps({"collection_authorization_required": pending}, ensure_ascii=False),
                         expect=["collection_authorization"])
    workbook = workdir / "activity_adapted.xlsx"
    sha = social_task.write_workbook(all_rows, workbook)
    source_file = {"name": "活动原帖（自动采集）.xlsx", "mount_path": workbook.name,
                   "transport": "social_task", "path": str(workbook), "sha256": sha}
    plan["source_file"] = source_file
    handoff = {"source": "ssjh_social_task", "skill_version": social_task.VERSION, "campaigns": handoffs,
               "workbook": str(workbook), "sha256": sha, "rows": len(all_rows), "status": "ready"}
    return {"artifacts": {"activity_detail": handoff, "confirmed_plan": plan}}


def _replay_activity_pull(state, plan: dict) -> dict:
    """测试回放：复用录制 Run 的自动采集结果，不创建计费任务（见 replay.py）。"""
    recorded = replay.load(state.run_dir, "activity_detail", plan)
    if recorded.get("source") != "ssjh_social_task" or recorded.get("status") != "ready":
        raise RuntimeError("activity_pull: 回放包中的活动原帖不是已完成的自动采集结果")
    old_workbook = Path(str(recorded.get("workbook") or ""))
    try:
        relative = old_workbook.relative_to(old_workbook.parents[1]).as_posix()
    except (IndexError, ValueError) as error:
        raise RuntimeError("activity_pull: 回放包工作簿路径非法") from error
    if not relative.startswith("activity_collection/"):
        raise RuntimeError("activity_pull: 回放包工作簿不在 activity_collection 下")
    workbook = replay.restore_file(state.run_dir, relative)
    sha = replay.sha256_file(workbook)
    if sha != recorded.get("sha256"):
        raise RuntimeError("activity_pull: 回放工作簿 sha256 与录制记录不一致")
    replay.seed_annotation_cache(state.run_dir)
    plan["source_file"] = {"name": "活动原帖（自动采集）.xlsx", "mount_path": workbook.name,
                           "transport": "social_task", "path": str(workbook), "sha256": sha}
    handoff = {**recorded, "workbook": str(workbook), "sha256": sha,
               "replayed_from": {"workbook": str(old_workbook)}}
    state.event("n03b_activity_pull", "replayed", f"rows={recorded.get('rows')}")
    return {"artifacts": {"activity_detail": handoff, "confirmed_plan": plan}}


@handler("run_init")
def run_init(node, state, ctx):
    plan = state.read_artifact("confirmed_plan") or {}
    if not isinstance(plan, dict) or not plan.get("confirmed"):
        raise ValueError("run_init: G2 ConfirmedPlan 未通过")
    for key in ("analysis_plan", "execution_plan", "selected_modules"):
        if (plan.get("subject") or {}).get(key) != state.artifact_ref(key):
            raise ValueError(f"run_init: ConfirmedPlan 引用的 {key} 已失效")

    source = plan.get("source_file") or {}
    mount_path = str(source.get("mount_path") or "")
    resolved_upload = excel_source.resolve_activity_xlsx(plan)
    source_path = next(
        (
            candidate for candidate in (
                resolved_upload,
                Path(mount_path),
                Path("/workspace") / mount_path,
                state.run_dir / mount_path,
            )
            if candidate is not None and candidate.is_file()
        ),
        None,
    )
    if source_path:
        activity_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()
    else:
        raise ValueError("run_init: 无法读取 G1 挂载的活动明细，不能冻结源数据哈希")
    # FIX-11：过滤词核查结果；无 source_path 时保持空结构，避免下游 KeyError。
    filterword_check: dict = {"hits": [], "hit_rows": 0, "total_rows": 0,
                              "ratio": 0.0, "decision": "no_source",
                              "source": "auto", "per_campaign": {}}

    if source_path:
        input_data = excel_source.load_activity_rows(plan)
        rows = (input_data or {}).get("rows") or []
        # FIX-11：取品牌数据之前先确认活动明细已按过滤词清洗。用户决定之前不往下走，
        # 决定写入 filterword_decision artifact，n07 据此剔除。
        filterword_check = _check_activity_filterwords(plan, rows, state)
        input_schema_validation = {
            "status": "PASSED",
            "row_count": len(rows),
            "platforms": (input_data or {}).get("meta", {}).get("platforms") or [],
            "campaigns": (input_data or {}).get("meta", {}).get("campaigns") or [],
            "source_row_count": (
                (input_data or {}).get("meta", {}).get("source_row_count")
            ),
            "accepted_row_count": (
                (input_data or {}).get("meta", {}).get("accepted_row_count")
            ),
            "period_start": (input_data or {}).get("meta", {}).get("period_start"),
            "period_end": (input_data or {}).get("meta", {}).get("period_end"),
            "source_file": source.get("name"),
            "sha256": activity_checksum,
        }
    def resource_fingerprint(path: Path) -> str:
        if not path.is_file():
            return "unavailable"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def combined_fingerprint(paths: list[Path]) -> str:
        values = [resource_fingerprint(path) for path in paths]
        if any(value == "unavailable" for value in values):
            return "unavailable"
        return hashlib.sha256(":".join(values).encode("ascii")).hexdigest()

    skills_root = Path(__file__).resolve().parent.parent
    package_root = Path(__file__).resolve().parent
    runtime_resources = package_root / "runtime_resources"
    default_resource_versions = {
        "dag-contract": resource_fingerprint(
            package_root / "contracts" / "pipeline.v0.2.yaml"
        ),
        "rules-implementation": combined_fingerprint([
            Path(__file__),
            Path(__file__).resolve().parent / "acceptance.py",
            Path(__file__).resolve().parent / "charts.py",
            Path(__file__).resolve().parent / "wordcloud_adapter.py",
            Path(__file__).resolve().parent / "ssjh.py",
        ]),
        "coordinator-agent": combined_fingerprint([
            runtime_resources / "agents" / "coordinator.json",
            runtime_resources / "agents" / "coordinator.system.md",
        ]),
        "validator-agent": combined_fingerprint([
            runtime_resources / "agents" / "validator.json",
            runtime_resources / "agents" / "validator.system.md",
        ]),
        "environment": resource_fingerprint(
            runtime_resources / "environment.json"
        ),
        "pipeline-orchestrator": resource_fingerprint(Path(__file__)),
        "topic5-validator": combined_fingerprint([
            skills_root / "topic5-validator" / "SKILL.md",
            skills_root / "topic5-validator" / "scripts" / "scan_report.py",
        ]),
        "SSJH-Social-Stat": resource_fingerprint(
            next((
                candidate for candidate in (
                    skills_root / "ssjh-social-stat" / "skill.py",
                    skills_root / "SSJH-Social-Stat" / "skill.py",
                ) if candidate.is_file()
            ), skills_root / "ssjh-social-stat" / "skill.py")
        ),
        "blueai-datahub-skill": resource_fingerprint(
            skills_root / "blueai-datahub-skill" / "sdk" / "client.py"
        ),
        "blueai-yuqing-echarts-skill-new": resource_fingerprint(
            skills_root / "blueai-yuqing-echarts-skill-new"
            / "scripts" / "render_from_spec.js"
        ),
        "report-cover-skill": resource_fingerprint(
            skills_root / "report-cover-skill" / "scripts" / "generate_cover_gpt.py"
        ),
        "wordcloud-insight": combined_fingerprint([
            Path(charts.WORDCLOUD_DIR) / "SKILL.md",
            Path(charts.WORDCLOUD_DIR) / "scripts" / "wordcloud_insight.py",
            Path(charts.WORDCLOUD_DIR) / "scripts" / "post_evidence.py",
            Path(charts.WORDCLOUD_DIR) / "scripts" / "field_source.py",
            Path(os.environ.get("TOPIC5_WORDCLOUD_FONT") or str(
                Path(charts.WORDCLOUD_DIR) / "assets/fonts/NotoSansCJK-Bold.ttc")),
        ]),
        "feishu-delivery-tool": resource_fingerprint(
            Path(__file__).resolve().parent / "feishu.py"
        ),
        "semantic-tool": resource_fingerprint(
            Path(__file__).resolve().parent / "semantic.py"
        ),
        "annotation-tool": resource_fingerprint(
            Path(__file__).resolve().parent / "annotate.py"
        ),
        "model": (
            os.environ.get("TOPIC5_MODEL_ID")
            or os.environ.get("ARK_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "configured-at-agent"
        ),
    }
    resource_versions = ctx.get("resource_versions") or {
        **default_resource_versions,
    }
    required_resource_keys = set(default_resource_versions)
    missing_resource_keys = sorted(required_resource_keys - set(resource_versions))
    unavailable_resources = sorted(
        key for key, value in resource_versions.items()
        if value in ("", "unavailable", "configured-at-agent")
    )
    if missing_resource_keys or unavailable_resources:
        raise ValueError("run_init: 无法冻结全部实际资源版本")

    lock_payload = {
        "confirmed_plan": {
            "version": plan.get("version"),
            "sha256": plan.get("plan_bundle_sha256"),
            "subject": plan.get("subject"),
        },
        "activity_detail_checksum": activity_checksum,
        "dag": f"{state.dag_slug}@{state.dag_version}",
        "input_schema_validation": input_schema_validation,
        "resources": resource_versions,
        "run_mode": state.params.get("run_mode", "full"),
    }
    lock = "lock_" + hashlib.sha256(
        json.dumps(
            lock_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()[:16]
    state.params["resource_lock"] = lock
    state.params["activity_detail_checksum"] = activity_checksum
    return {"artifacts": {
        "run_record": {
            "run_id": state.run_id,
            "task_id": state.task_id,
            "confirmed_plan_ref": state.artifact_ref("confirmed_plan"),
            "resource_lock": lock,
            "status": "initialized",
        },
        "resource_lock": {**lock_payload, "lock": lock, "frozen": True},
        "context_snapshot": {
            "confirmed_plan": plan,
            "confirmed_plan_ref": state.artifact_ref("confirmed_plan"),
            "params": dict(state.params),
        },
        "input_schema_validation": input_schema_validation,
        # FIX-11：活动过滤词核查结果（无命中时 decision=no_hits）。
        "filterword_check": filterword_check,
    }}


def _folder_issues(feishu_doc: dict) -> list[dict]:
    """FIX-41：项目文件夹。未配置根目录→P1；文件夹转移与协作者降级均失败→P0（外部权限，走 HC7）。"""
    if not feishu_doc.get("folder_configured"):
        return [{"id": "P1-postpublish-no-folder", "severity": "P1", "title": "未配置项目文件夹",
                 "evidence": "FEISHU_ROOT_FOLDER_TOKEN 未配置，文档建在应用空间", "locus": "feishu-docx"}]
    access = feishu_doc.get("folder_access") or {}
    if access.get("transferred"):
        return []
    if access.get("collaborator_added"):
        return [{"id": "P1-postpublish-folder-owner-fallback", "severity": "P1",
                 "title": "项目文件夹所有权转移失败，已降级为可管理协作者",
                 "evidence": str(access.get("transfer_error")), "locus": "feishu-permission"}]
    return [{"id": "P0-postpublish-owner", "severity": "P0",
             "title": "项目文件夹所有权转移和协作者降级均失败",
             "evidence": f"transfer_error={access.get('transfer_error')}; "
                         f"collaborator_error={access.get('collaborator_error')}",
             "locus": "feishu-permission"}]


@handler("delivery_draft")
def delivery_draft(node, state, ctx):
    """交付草稿（n20_delivery_draft，G4）：内容层 MD → 飞书文档 draft + 外部回执。

    真路径（feishu.create_delivery）：建 docx → 写 block → 转移所有权 →
    导出后回读核对（章节齐全/图引用生效/绑定报告版本一致）→ 记录 revision_id。
    无凭据/失败降级到确定性占位（feishu.fallback，degraded=True，schema 与真路径一致）。

    幂等（idempotency=feishu:{task_id}:{resource_lock}:{report_version} 由引擎处理）：
    本层命中已有回执（external_receipts.doc_token）则复用、不新建文档；side_effect=feishu-doc-create、
    retry max2 on_fail=pause 由引擎统一处理，本 handler 不因缺凭据抛异常（避免 pause）。
    """
    out = node.get("outputs", [])
    report = state.read_artifact("report_md") or {}
    report_md = report.get("markdown") if isinstance(report, dict) else ""
    # FIX-35：HC7 放宽项在数据说明区下方留痕（灰色斜体）。
    report_md = report_contract.waiver_note(report_md, (state.read_artifact("qc_report") or {}).get("waivers"))
    report_version = str(report.get("version") if isinstance(report, dict) else "") or "0.0"
    plan = state.read_artifact("confirmed_plan") or {}
    brand = str(plan.get("brand") or plan.get("project") or "立白")
    title = str(plan.get("report_name") or f"{brand}营销活动复盘报告 v{report_version}")
    task_id = state.task_id

    # FIX-38：返工时在上次发布的文档上原位修正；基线为上次发布的 MD。
    published = state.read_artifact("published_delivery") or {}
    # 幂等复用：只有报告与上次发布完全一致时才复用回执、跳过写入；报告已变（发布后返修
    # n19→n17→n20）必须原位修正，否则文档内容不变、回读比对新报告持续 P0。
    prev_receipts = state.read_artifact("external_receipts") or {}
    reused = (prev_receipts.get("doc_token")
              if isinstance(prev_receipts, dict) else "") or ""
    if reused and published.get("doc_token") and published.get("markdown") != report_md:
        reused = ""
    if feishu.api_available():
        try:
            feishu_doc = feishu.create_delivery(
                report_md,
                published=published if published.get("doc_token") else None, title=title, task_id=task_id,
                report_version=report_version, reused_doc_token=reused,
                key_numbers=report_contract.key_numbers(state.read_artifact("stats_bundle") or {}),
                project_name=str(plan.get("project") or plan.get("activity_name") or brand))
        except Exception:
            raise
    else:
        raise RuntimeError("delivery_draft: 飞书交付凭据不可用")

    readback = feishu_doc.get("readback") or {}
    ownership_transferred = bool(feishu_doc.get("ownership_transferred"))
    collaborator_added = bool(feishu_doc.get("collaborator_added"))
    delivery_access_granted = ownership_transferred or collaborator_added
    post_publish_issues = []
    if not readback.get("chapters_complete"):
        post_publish_issues.append({
            "id": "P0-postpublish-chapters",
            "severity": "P0",
            "title": "飞书文档回读缺少章节",
            "evidence": str(readback.get("missing_chapters") or []),
            "locus": "feishu-docx",
        })
    if not readback.get("images_referenced"):
        post_publish_issues.append({
            "id": "P0-postpublish-images",
            "severity": "P0",
            "title": "飞书文档图片块不完整",
            "evidence": (
                f"expected={readback.get('expected_images')}, "
                f"got={readback.get('got_images')}"
            ),
            "locus": "feishu-docx",
        })
    numbers = readback.get("numbers_check") or {}
    if numbers.get("missing"):
        post_publish_issues.append({
            "id": "P0-postpublish-numbers", "severity": "P0",
            "title": "飞书文档回读关键数字与报告不一致",
            "evidence": "；".join(f"{n.get('label')}={n.get('value_text')}" for n in numbers["missing"]),
            "locus": "feishu-docx",
        })
    table_layout = readback.get("table_layout") or {}
    if isinstance(table_layout, dict) and table_layout.get("ok") is False:
        post_publish_issues.append({
            "id": "P0-postpublish-table-layout", "severity": "P0",
            "title": "飞书表格布局回读不一致",
            "evidence": json.dumps({k: v for k, v in table_layout.items() if k != "ok"},
                                   ensure_ascii=False)[:500],
            "locus": "feishu-docx"})
    if readback.get("layout_complete") is False:
        post_publish_issues.append({
            "id": "P0-postpublish-layout", "severity": "P0",
            "title": "飞书Grid/Callout/表格布局回读不完整",
            "evidence": str(readback.get("layout_counts") or {}), "locus": "feishu-docx",
        })
    if not ownership_transferred:
        ownership_error = str(
            feishu_doc.get("ownership_error") or "unknown ownership error"
        )
        if collaborator_added:
            post_publish_issues.append({
                "id": "P1-postpublish-owner-fallback",
                "severity": "P1",
                "title": "所有权转移失败，已降级为可管理协作者",
                "evidence": (
                    "ownership_transferred=false; collaborator_added=true; "
                    "perm=full_access; transfer_error=" + ownership_error
                ),
                "locus": "feishu-permission",
            })
        else:
            collaborator_error = str(
                feishu_doc.get("collaborator_error")
                or "unknown collaborator error"
            )
            post_publish_issues.append({
                "id": "P0-postpublish-owner",
                "severity": "P0",
                "title": "飞书文档所有权转移和协作者降级均失败",
                "evidence": (
                    "ownership_transferred=false; collaborator_added=false; "
                    f"transfer_error={ownership_error}; "
                    f"collaborator_error={collaborator_error}"
                ),
                "locus": "feishu-permission",
            })
    post_publish_issues.extend(_folder_issues(feishu_doc))
    post_publish_p0_count = sum(
        1 for issue in post_publish_issues
        if issue.get("severity") == "P0"
    )
    post_publish_validation = {
        "validator": "topic5-validator",
        "rules_version": RULES_VERSION,
        "scope": "post_publish",
        "issues": post_publish_issues,
        "p0_count": post_publish_p0_count,
        "has_p0": post_publish_p0_count > 0,
        "layout_counts": readback.get("layout_counts") or {},
        "manual_checks": ["图表文字可读性、移动端视觉和议题表达质量"],
    }
    prior_qc = state.read_artifact("qc_report") or {}
    prior_issues = (
        list(prior_qc.get("issues") or [])
        if isinstance(prior_qc, dict) else []
    )
    prior_issues = [
        issue for issue in prior_issues
        if not (
            isinstance(issue, dict)
            and (
                str(issue.get("id") or "").startswith(
                    ("P0-postpublish-", "P1-postpublish-")
                )
                or issue.get("locus") in ("feishu-docx", "feishu-permission")
            )
        )
    ]
    merged_issues = []
    seen_issue_ids = set()
    for issue in prior_issues + post_publish_issues:
        issue_id = (
            str(issue.get("id") or "")
            if isinstance(issue, dict) else str(issue)
        )
        if issue_id and issue_id in seen_issue_ids:
            continue
        if issue_id:
            seen_issue_ids.add(issue_id)
        merged_issues.append(issue)
    # FIX-35：HC7 已豁免（waived）的 P0 不再计入。
    merged_p0_count = sum(
        1 for issue in merged_issues
        if isinstance(issue, dict) and issue.get("severity") == "P0" and issue.get("status") != "waived"
    )
    qc_report = {
        **(prior_qc if isinstance(prior_qc, dict) else {}),
        "issues": merged_issues,
        "p0_count": merged_p0_count,
        "has_p0": merged_p0_count > 0,
        "post_publish_validation": post_publish_validation,
    }
    # 发布后机器检查通过才进入 G4；原生 grid/callout 视觉仍由用户终验。
    draft_state = (
        "ready"
        if readback.get("ok") and delivery_access_granted
        else "needs_review"
    )
    delivery_draft_art = {
        "state": draft_state,
        "report_version": report_version,
        "doc_token": feishu_doc.get("doc_token"),
        "url": feishu_doc.get("url"),
        "revision_id": feishu_doc.get("revision_id"),
        "report_ref": state.artifact_ref("report_md"),
        "ownership_transferred": ownership_transferred,
        "ownership_target": feishu_doc.get("ownership_target"),
        "ownership_error": feishu_doc.get("ownership_error"),
        "collaborator_added": collaborator_added,
        "collaborator_perm": feishu_doc.get("collaborator_perm"),
        "collaborator_error": feishu_doc.get("collaborator_error"),
        "delivery_access_granted": delivery_access_granted,
        "readback": readback,
        "post_publish_validation": post_publish_validation,
        "degraded": bool(feishu_doc.get("degraded")),
    }
    external_receipts = {
        "doc_token": feishu_doc.get("doc_token"),
        "url": feishu_doc.get("url"),
        "revision_id": feishu_doc.get("revision_id"),
        "report_version": report_version,
        "ownership_transferred": bool(feishu_doc.get("ownership_transferred")),
        "collaborator_added": collaborator_added,
        "collaborator_perm": feishu_doc.get("collaborator_perm"),
        "delivery_access_granted": delivery_access_granted,
        "side_effect": "feishu-doc-create",
        "degraded": bool(feishu_doc.get("degraded")),
    }
    base = {
        "delivery_draft": delivery_draft_art,
        "external_receipts": external_receipts,
        "feishu_doc": feishu_doc,
        "qc_report": qc_report,
    }
    artifacts = {k: base.get(k, _stub_payload(node, "delivery")) for k in out}
    # FIX-38：记录本次发布基线（不在返工失效列表中），下次返工在同一文档上原位修正。
    if feishu_doc.get("doc_token") and not feishu_doc.get("degraded"):
        artifacts["published_delivery"] = {
            "doc_token": feishu_doc["doc_token"], "folder_token": feishu_doc.get("folder_token") or "",
            "markdown": report_md, "report_version": report_version,
            "revision_id": feishu_doc.get("revision_id")}
    return {"artifacts": artifacts}


@handler("delivery_commit")
def delivery_commit(node, state, ctx):
    """交付提交（n23_delivery_commit，发布后检查）。

    control_plane=blueflare-ada 未上线：本地 stub 执行，但产出结构对齐真实契约——
    绑定交付文档 revision_id 与 report_version（版本链对齐，指向同一 revision 而非 latest），
    发布后检查涵盖图片/布局/所有权/链接/版本一致。上线后只换 handler 不改 schema。
    side_effect=delivery-commit、idempotency=commit:{task_id}:{resource_lock} 由引擎处理。
    """
    feishu_doc = state.read_artifact("feishu_doc") or {}
    receipts = state.read_artifact("external_receipts") or {}
    user_conf = state.read_artifact("user_confirmation") or {}
    readback = (feishu_doc.get("readback") if isinstance(feishu_doc, dict) else {}) or {}
    revision_id = (feishu_doc.get("revision_id") if isinstance(feishu_doc, dict) else None) \
        or (receipts.get("revision_id") if isinstance(receipts, dict) else None)
    report_version = (feishu_doc.get("report_version") if isinstance(feishu_doc, dict) else None) \
        or (receipts.get("report_version") if isinstance(receipts, dict) else None)
    checks_ok = bool(readback.get("ok", True))
    user_confirmed = bool(
        isinstance(user_conf, dict) and user_conf.get("status") == "confirmed"
    )
    current_report_ref = state.artifact_ref("report_md")
    current_delivery_ref = state.artifact_ref("delivery_draft")
    url = str((feishu_doc or {}).get("url") or (receipts or {}).get("url") or "")
    link_accessible = bool(
        url.startswith("https://")
        and "/docx/" in url
        and readback.get("ok")
    )
    ownership_transferred = bool((feishu_doc or {}).get("ownership_transferred"))
    collaborator_added = bool((feishu_doc or {}).get("collaborator_added"))
    delivery_access_granted = ownership_transferred or collaborator_added
    acceptance_aligned = bool(
        user_confirmed
        and user_conf.get("report_version") == report_version
        and user_conf.get("revision_id") == revision_id
        and user_conf.get("report_ref") == current_report_ref
        and user_conf.get("delivery_ref") == current_delivery_ref
    )
    if (
        not checks_ok
        or not acceptance_aligned
        or not revision_id
        or not delivery_access_granted
        or not link_accessible
        or bool((feishu_doc or {}).get("degraded"))
    ):
        raise ValueError(
            "delivery_commit: 发布后检查、用户终验绑定、revision、所有权、链接"
            "或真实交付状态不满足"
        )
    delivery_record = {
        "committed": (
            checks_ok and acceptance_aligned and bool(revision_id)
            and delivery_access_granted and link_accessible
            and not bool((feishu_doc or {}).get("degraded"))
        ),
        "post_publish_checks": "passed" if checks_ok else "needs_review",
        "doc_token": (feishu_doc.get("doc_token") if isinstance(feishu_doc, dict) else None),
        "url": url,
        "revision_id": revision_id,
        "report_version": report_version,
        "report_ref": current_report_ref,
        "delivery_ref": current_delivery_ref,
        "validation_ref": state.artifact_ref("qc_report"),
        "user_acceptance_ref": state.artifact_ref("user_confirmation"),
        "ownership_transferred": ownership_transferred,
        "collaborator_added": collaborator_added,
        "collaborator_perm": (feishu_doc or {}).get("collaborator_perm"),
        "delivery_access_granted": delivery_access_granted,
        "link_accessible": link_accessible,
        "acceptance_aligned": acceptance_aligned,
        "user_confirmed": user_confirmed,
        "degraded": bool((feishu_doc or {}).get("degraded")),
        "control_plane": node.get("control_plane"),
    }
    return {"artifacts": {
        "delivery_record": delivery_record,
        "task_result": {"status": "delivered", "report_version": report_version,
                        "revision_id": revision_id,
                        "doc_token": delivery_record["doc_token"]},
    }}


@handler("user_decision")
def user_decision(node, state, ctx):
    uc = state.read_artifact("user_confirmation") or {}
    status = uc.get("status", ctx.get("user_decision"))
    if status not in node["routes"]:
        raise ValueError(f"user_decision: 非法或缺失的用户终验状态 {status!r}")
    return {"route": status}


# ── human 节点 ──────────────────────────────────────────────
def _g2_auto_confirm_allowed(node, state, ctx) -> bool:
    """G2 代用户确认的判定（FIX-09）。

    默认关闭（`Y.params.g2_auto_confirm_when_no_cost=false`），由业务方决定是否打开。
    打开后仍需同时满足：
      - 当前节点是 G2（方案确认）；
      - 本次运行不涉及计费（没有付费取数 / 预估费用为 0）；
      - preflight 结果为 READY（无阻塞项）。
    G2 本身不删除：冻结 plan_bundle_sha256 的机制必须保留，这里只是放行主体变化。
    """
    params = (ctx or {}).get("params") or {}
    if not _truthy(params.get("g2_auto_confirm_when_no_cost")):
        return False
    if str(node.get("gate") or node.get("id")) not in ("G2", "n03_hc_plan"):
        return False
    preflight = state.read_artifact("preflight") or {}
    if str(preflight.get("status") or "").upper() != "READY":
        return False
    cost = preflight.get("estimated_cost")
    if isinstance(cost, (int, float)) and cost > 0:
        return False
    if preflight.get("billable") is True:
        return False
    if preflight.get("requires_payment") is True:
        return False
    return True


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")


@handler("human_gate")
def human_gate(node, state, ctx):
    if str(node.get("gate") or "") == "G1":
        preset = state.read_artifact("requirement_preset")
        if isinstance(preset, dict) and preset.get("note"):
            # 原版 Phase1 v1.12 Step5：Skill ready 后直接采用，HC-T5-0 只展示不确认。
            import hc_bridge
            return {
                "artifacts": {"requirement_confirmed": hc_bridge.confirmed_requirement(
                    preset["note"], authority=hc_bridge.G1_AUTHORITY_SKILL)},
            }
    if _g2_auto_confirm_allowed(node, state, ctx):
        # n02_plan_compile 产出 analysis_plan；G2 冻结后即为 confirmed_plan
        plan = state.read_artifact("analysis_plan") or {}
        return {
            "artifacts": {"confirmed_plan": plan},
            "authority": "user-delegated",
            "reason": "g2_auto_confirm_when_no_cost：不涉及计费且 preflight=READY，代用户确认",
        }
    raise HumanPause(prompt=node.get("name", node["id"]), expect=node.get("outputs", []))


@handler("validator_gate")
def validator_gate(node, state, ctx):
    """Pause for the independent Validator Agent, never for end-user review."""
    raise HumanPause(
        prompt=node.get("name", node["id"]),
        expect=node.get("outputs", []),
    )


# ── v0.2 新增：标注审核（FIX-30，独立 Validator 抽检）────────────────────
# 口径依据：tools/plan_annotation_review.py（抽样）、KS_Annotation_Review（审核职责）、
# QC §5/§7（正确率阈值 90%/目标 95%、额度用完置 blocked_quality 并停下下游）。
ANNOTATION_REVIEW_DEFAULT_ACCURACY = 0.90
ANNOTATION_REVIEW_DEFAULT_ROUNDS = 2
# 重标范围判定：错误类型属于这些「系统性」类别时重标全部受影响行
SYSTEMATIC_ERROR_TYPES = ("relevance", "sentiment", "global")


def _review_rows(node, state) -> tuple[list[dict], str]:
    """取待审行与产物键：轮次一读 annotated_round1，轮次二读 annotated_data。"""
    key = "annotated_round1" if "r1_review" in (node.get("outputs") or []) else "annotated_data"
    art = state.read_artifact(key) or {}
    rows = art.get("rows") if isinstance(art, dict) else None
    return [dict(r) for r in (rows or [])], key


@handler("annotation_review")
def annotation_review(node, state, ctx):
    """标注抽检（FIX-30）：生成确定性分层抽样计划，交独立 Validator 逐帖判定。

    - 关闭开关（annotation_review_enabled=false）时直接放行并留痕 P1，不暂停。
    - 开启时先写抽样计划再 HumanPause，等 Validator 提交本节点产物；
      已审 ID 跨轮次累计在 annotation_review_history，额度按剩余额度收敛。
    - 只读：不改标注字段、不改数据。
    """
    stage = "round1" if "r1_review" in (node.get("outputs") or []) else "round2"
    enabled = state.params.get("annotation_review_enabled", True)
    enabled = (str(enabled).strip().lower() not in ("false", "0", "no", "off")
               if not isinstance(enabled, bool) else enabled)
    if not enabled:
        return {"artifacts": {f"{stage[0]}{'1' if stage == 'round1' else '2'}_review": {
            "enabled": False, "stage": stage, "decision": "skipped",
            "p1_notes": ["未执行标注审核（annotation_review_enabled=false）"],
        }}}

    rows, _key = _review_rows(node, state)
    plan = state.read_artifact("confirmed_plan") or {}
    labels = state.read_artifact("confirmed_opinion_labels") or {}
    ugc_list = state.read_artifact("ugc_category_list") or {}
    config = _structure_config(
        opinion_labels=(labels.get("labels") or labels.get("candidates")
                        if isinstance(labels, dict) else None) or [],
        ugc_categories=(ugc_list.get("categories")
                        if isinstance(ugc_list, dict) else None) or [],
        ugc_not_applicable=bool(
            isinstance(ugc_list, dict) and ugc_list.get("not_applicable")))

    history = state.read_artifact("annotation_review_history") or {}
    reviewed = _stage_reviewed_ids(history, stage)
    sample_plan = review_plan_mod.plan_review(
        rows, stage="round1" if stage == "round1" else "round2",
        reviewed_ids=reviewed, config=config)
    sample_plan["campaign"] = str(plan.get("activity_name") or "")
    plan_path = _write_review_plan(state, stage, _key, rows, sample_plan)
    raise HumanPause(
        prompt=(f"{node.get('name', node['id'])}：{sample_plan['sample_size']} 帖待抽检；"
                f"抽检计划 {plan_path}"),
        expect=node.get("outputs", []),
    )


# 各轮独立审核的字段（原版 QC Protocol §5：轮次一情感+相关性分别判定；轮次二观点）。
REVIEW_FIELDS = {"round1": ("sentiment", "relevance"), "round2": ("opinion",)}
_REVIEW_LABEL_KEYS = ("ai_sentiment", "relevance", "opinion", "ugc_category",
                      "discussion_dimension", "content_mode")


def review_plan_path(run_dir: Path, stage: str) -> Path:
    return Path(run_dir) / "validation" / f"annotation_review_{stage[0]}{stage[-1]}_plan.json"


def _write_review_plan(state, stage: str, key: str, rows: list[dict], sample_plan: dict) -> Path:
    """抽检计划落盘：独立 Validator 逐帖判定的唯一输入，绑定当前标注版本。"""
    by_id = {str(r.get("_annot_id") or r.get("id") or ""): r for r in rows}
    samples = []
    for item in sample_plan.get("samples") or []:
        row = by_id.get(str(item.get("id")), {})
        samples.append({
            **item,
            "title": row.get("title") or "",
            "content": row.get("content") or "",
            "url": row.get("url") or "",
            "platform": row.get("_platform") or row.get("site_name") or "",
            "labels": {k: row.get(k) for k in _REVIEW_LABEL_KEYS if k in row},
        })
    payload = {
        **{k: v for k, v in sample_plan.items() if k != "samples"},
        "stage": stage,
        "fields": list(REVIEW_FIELDS[stage]),
        "subject": state.artifact_ref(key),
        "samples": samples,
    }
    path = review_plan_path(state.run_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@handler("annotation_review_decision")
def annotation_review_decision(node, state, ctx):
    """审核路由（FIX-30）：正确率达标放行；未达标且额度有余回重标；额度耗尽升级。

    正确率按字段分别算（sentiment/relevance 或 opinion/ugc_category/discussion_dimension），
    任一字段低于 annotation_accuracy_min（默认 0.90）即视为未达标。
    字段无判定结果按「未达标」处理：没有证据不能当作通过。
    """
    stage = "round1" if "n09r" in node.get("id", "") else "round2"
    key = "r1_review" if stage == "round1" else "r2_review"
    review = state.read_artifact(key) or {}
    if not isinstance(review, dict) or not review.get("enabled", True):
        return {"route": "pass"}

    accuracy_min = float(state.params.get(
        "annotation_accuracy_min", ANNOTATION_REVIEW_DEFAULT_ACCURACY))
    accuracy = review.get("accuracy") or {}
    verdicts = {k: v for k, v in accuracy.items() if isinstance(v, (int, float))}
    under = (
        [field for field, value in verdicts.items() if value < accuracy_min]
        if verdicts else ["no_verdict"]
    )
    if not under:
        return {"route": "pass"}

    rounds = state.counters.get(f"annotation_review_rounds_{stage}", 0)
    max_rounds = int(state.params.get(
        "annotation_review_max_rounds", ANNOTATION_REVIEW_DEFAULT_ROUNDS))
    quota_plan = review.get("plan") or {}
    remaining = quota_plan.get("remaining_after_plan")
    quota_left = (remaining is None) or int(remaining) > 0
    if rounds >= max_rounds or not quota_left:
        review["blocked_quality"] = True
        review["blocked_reason"] = (
            "rounds_exhausted" if rounds >= max_rounds else "quota_exhausted")
        return {"route": "maxed"}
    return {"route": "retry"}


def _round2_direction_labels(confirmed, candidates) -> tuple:
    """轮次二正/负观点任务各自的候选表（原版 annot_r2.py 正负各一套）。

    取数顺序：确认产物的分方向字段 → opinion_candidates 的分方向候选 → None（调用方
    退回合并的 labels）。分方向候选就是该方向自己的表，不能把另一方向的类别并进来：
    合并的 13 个标签含负面专属类别，混装后正面行会被标成负向话题（实测 3.05%）。
    """
    confirmed = confirmed if isinstance(confirmed, dict) else {}
    candidates = candidates if isinstance(candidates, dict) else {}
    pos = confirmed.get("positive_labels")
    neg = confirmed.get("negative_labels")
    if pos is None:
        pos = candidates.get("positive_candidates")
    if neg is None:
        neg = candidates.get("negative_candidates")
    return (list(pos) if pos is not None else None,
            list(neg) if neg is not None else None)


def coverage_disposition(coverage: dict, policy: str = "waive") -> str:
    """覆盖检查不通过时怎么处置：retry（可补标）/ waive（记录披露继续）/ block（硬阻断）。

    「其他」占比超门槛是质量结论而非偶发错误：同一批数据重跑标注结果不变，重试只会
    重复同样结论（job_436842fb74da n11 连烧三次）。原版 KS_Data_Processing:245 把它
    定为阻断并由人处置，QC 协议:219 又明确不能为降比例强行归类，所以默认 waive：
    记录 blocked_quality、报告披露，门槛值本身不改。
    """
    if not isinstance(coverage, dict) or not coverage.get("has_error"):
        return "ok"
    if coverage.get("has_retryable_error", True):
        return "retry"
    if not coverage.get("quality_blocked"):
        return "retry"
    return "block" if str(policy).strip().lower() == "block" else "waive"


def _is_systematic_error(affected_fields: list[str], error_types) -> bool:
    """错误是否属系统性（需重标全部行）。

    判为系统性的情形：没有任何可定位的错误字段（无从增量重标），
    或受影响字段/错误类型命中 SYSTEMATIC_ERROR_TYPES。
    依据 FIX-30「系统性错误需要修正边界后，重处理受影响的全部数据」。
    """
    if not affected_fields:
        return True
    if any(field in SYSTEMATIC_ERROR_TYPES for field in affected_fields):
        return True
    if isinstance(error_types, dict):
        return any(str(value) in SYSTEMATIC_ERROR_TYPES
                   for value in error_types.values())
    if isinstance(error_types, (list, tuple)):
        return any(str(value) in SYSTEMATIC_ERROR_TYPES for value in error_types)
    return False


@handler("annotation_rework")
def annotation_rework(node, state, ctx):
    """重标准备（FIX-30）：判定重标范围、invalidate 该轮产物、rerun_epoch 递增。

    - 系统性错误（relevance/sentiment/global 或错误类型缺失）→ 重标全部行；
      局部错误 → 只重标 wrong_ids。
    - invalidate 范围为该轮标注产物及其下游全部 artifact；gate 是 decision 节点
      不能执行 invalidate，故由本 code 节点执行后 next 指回 n09/n11。
    """
    stage = "round1" if "n09r" in node.get("id", "") else "round2"
    key = "r1_review" if stage == "round1" else "r2_review"
    review = state.read_artifact(key) or {}
    wrong_ids = review.get("wrong_ids") or {}
    error_types = review.get("error_types") or {}

    affected_fields = sorted(field for field, ids in wrong_ids.items() if ids)
    systematic = _is_systematic_error(affected_fields, error_types)
    rerun_ids = [] if systematic else sorted(
        {str(i) for ids in wrong_ids.values() for i in (ids or [])})

    epoch_key = f"annotation_rerun_epoch_{stage}"
    state.counters[epoch_key] = state.counters.get(epoch_key, 0) + 1
    state.counters[f"annotation_review_rounds_{stage}"] = (
        state.counters.get(f"annotation_review_rounds_{stage}", 0) + 1)

    # 轮次一重标 → 轮次二及其下游全部失效；轮次二重标 → 只失效轮次二及下游。
    # wordcloud_data 来自外部 SSJH（只由 n02 扇出的 n13 产出一次），与标注无关，不失效；
    # 否则 n12 永远等不到输入、Run 判 stuck。
    downstream = (["annotated_round1", "opinion_candidates", "confirmed_opinion_labels",
                   "ugc_category_list", "annotated_data", "stats_bundle",
                   "chart_urls", "wordcloud_insight",
                   "report_md", "qc_report", "delivery_draft"]
                  if stage == "round1" else
                  ["annotated_data", "stats_bundle", "chart_urls",
                   "wordcloud_insight", "report_md", "qc_report", "delivery_draft"])
    # 增量重标的底稿：失效前先保存本轮标注（执行契约 §3：只修对应行，其余行保留原标注）。
    labeled_key = "annotated_round1" if stage == "round1" else "annotated_data"
    base_ref = None if systematic else state.artifact_ref(labeled_key)
    state.invalidate_artifacts(downstream)

    history = state.read_artifact("annotation_review_history") or {}
    by_stage = _history_by_stage(history)
    plan = review.get("plan") or {}
    by_stage[stage] = sorted(set(by_stage.get(stage, [])) | {
        str(s.get("id")) for s in (plan.get("samples") or []) if s.get("id")})

    payload = {
        "stage": stage,
        "rerun_epoch": state.counters[epoch_key],
        "scope": "all" if systematic else "wrong_ids",
        "rerun_ids": rerun_ids,
        "affected_fields": affected_fields,
        "error_types": error_types,
        "review_rounds": state.counters[f"annotation_review_rounds_{stage}"],
        "invalidated": downstream,
        "base_artifact": base_ref,
        "history": {"by_stage": by_stage},
    }
    return {"artifacts": {"annotation_rework_plan": payload,
                          "annotation_review_history": payload["history"]}}
