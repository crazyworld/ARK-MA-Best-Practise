"""AI 标注适配层 — 供 annotate handler 接真（轮次一 n09 / 轮次二 n11）。

设计（类比 ssjh.py / charts.py）：
  - 真路径按 `annotation_routing_threshold` 路由：
      行数 ≥ 阈值 且 DataHub 可用 → datahub.batch_annotate（批量引擎）；
      否则 → 小引擎（semantic.py，分批 chat_json）。
    两条真路径 **出口 schema 完全一致**（每行回填相同标注字段）。
  - 降级路径：无任何凭据时，产出结构真实、带 `degraded=True` 的确定性标注，
    使下游（opinion_candidates / aggregate）无差别消费，本地可端到端跑通。

口径红线：
  - 轮次一：情感(ai_sentiment ∈ {1,0,-1}) + 活动相关(relevance: bool)。情感以 AI 标注为准。
  - 轮次二：观点 / UGC 参与方式 / 讨论维度 **三个独立任务** + 覆盖检查；
    覆盖检查有 ERROR（真路径下解析失败/缺口）才阻断（raise→引擎 pause 重试）；
    “不适用”情形标注为 N/A 跳过、不阻断。
  - 多活动逐个处理由上层 aggregate 负责；本层只做逐行标注与覆盖核对。
"""
from __future__ import annotations

import json
from typing import Any, Optional

import annotation_cache
import semantic
import datahub
import label_review

# 情感标签 ↔ int（与 aggregate._row_sentiment 口径一致）。
_SENTI_TO_INT = {
    "正面": 1, "正": 1, "positive": 1, "积极": 1,
    "中性": 0, "中": 0, "neutral": 0,
    "负面": -1, "负": -1, "negative": -1, "消极": -1,
}
_INT_TO_SENTI = {1: "正面", 0: "中性", -1: "负面"}

# 轮次二“不适用”标记（不阻断覆盖检查）。
_NA = "N/A"
# 主要讨论对象三分类（FIX-14）：KS_Metadata_Structure.md:110。
# 枚举之外的值（如「价格」「功效」）视为缺失，_apply_attr 丢弃并等待重试。
_DISCUSSION_DIMENSIONS = ("品牌", "产品", "活动")
_DISCUSSION_DIM_DEF = (
    "品牌=品牌形象/价值观/历史/联名意义；"
    "产品=功能/使用体验/成分材质/外观/性价比；"
    "活动=活动机制/参与体验/获取方式"
)
# prompt 用的三类定义（原版 annot_r2.py 的 discussion_dimension 段落，逐条对应）。
_DISCUSSION_DIM_PROMPT = (
    "- 品牌：主要讨论品牌形象、价值观、合作背景、联名意义\n"
    "- 产品：主要讨论具体产品功能、使用体验、外观设计、口味、周边实物\n"
    "- 活动：主要讨论活动机制、参与流程、抢购、打卡、规则、奖励\n"
    "多维度并存取篇幅最重者。"
)
_ROUND2_TASKS = ("opinion", "ugc_category", "discussion_dimension")


def _senti_to_int(v: Any) -> Optional[int]:
    """情感归一化到 {1,0,-1}；无法识别返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        iv = int(v)
        return iv if iv in (1, 0, -1) else None
    s = str(v).strip()
    if s in _SENTI_TO_INT:
        return _SENTI_TO_INT[s]
    try:
        iv = int(s)
        return iv if iv in (1, 0, -1) else None
    except ValueError:
        return None


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip() in ("是", "相关", "true", "True", "1", "yes", "relevant")


def _tag_ids(rows: list[dict]) -> None:
    """为每行写入稳定回填键 `_annot_id`（用去重键/索引）。"""
    for i, r in enumerate(rows):
        if not r.get("_annot_id"):
            r["_annot_id"] = str(r.get("_dedup_key") or r.get("id") or r.get("fingerprint") or i)


# ── 轮次一：情感 + 活动相关 ───────────────────────────────────
# 输入长度（FIX-27）：标题前 200 字 + 正文前 800 字，合计不超过 1000 字。
# 依据：annot_r1.py 的 text() 与 annotation_prompt.md「每条输入」。
R1_TITLE_CHARS = 200
R1_CONTENT_CHARS = 800
# 每批 15 条、最多重试 3 次（annot_r1.py BATCH=15 / range(3)）。
R1_BATCH = 15
R1_MAX_ATTEMPTS = 3
# 三个识别字段（KS_Data_Processing.md:185：非空时必须传入）
R1_MEDIA_FIELDS = ("video_cover_text", "audio_text", "video_text")
R1_MEDIA_CHARS = 600
# 正文与标题都为空 → 不送模型，按原版写「标注跳过_无内容」。
SKIP_EMPTY_LABEL = "标注跳过_无内容"

_R1_PROMPT_DH = (
    "请判断以下社媒内容的【情感倾向】与【是否与活动相关】。\n"
    "文本：{{content}}\n"
    "标题：{{title}}\n"
    "返回 JSON：{\"sentiment\": \"正面/中性/负面\", \"campaign_relevant\": \"是/否\"}"
)
# DataHub 路径的识别字段列（KS_Data_Processing.md:185：非空必须传入）。
_R1_DH_COLUMNS = ["content", "title", *R1_MEDIA_FIELDS]


def _r1_dh_prompt(context: dict) -> str:
    """DataHub 轮次一 prompt：把活动上下文前置，再拼正文模板（FIX-27）。"""
    ctx = _r1_context(context)
    header = (f"活动名称：{ctx['campaign_name']}\n"
              f"活动背景：{ctx['campaign_context']}\n"
              f"活动取数表达式：{ctx['activity_keywords']}\n"
              "相关性判定：明确提及/参与/响应/评价本次活动为「是」；"
              "赞助型活动提及被赞助内容也算相关；品牌一般内容与行业泛讨论为「否」；"
              "负面不等于噪音，真实投诉仍为「是」。\n\n")
    return header + _R1_PROMPT_DH


def _r1_context(context: dict) -> dict:
    """校验并规整轮次一的注入上下文（FIX-27，缺项 fail-closed）。

    annotation_prompt.md「项目配置区」：campaign_name / campaign_context /
    activity_keywords 未填入时禁止开始标注。
    """
    context = context or {}
    missing = [
        key for key in ("campaign_name", "campaign_context", "activity_keywords")
        if not str(context.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"annotate_round1: 缺少标注前置上下文 {missing}，"
            "按标注合约禁止开始标注")
    return {
        "campaign_name": str(context["campaign_name"]).strip(),
        "campaign_context": str(context["campaign_context"]).strip(),
        "activity_keywords": str(context["activity_keywords"]).strip(),
        "campaign_type": str(context.get("campaign_type") or "").strip(),
    }


def _calibration_block(calibration: list[dict] | None) -> str:
    """校准样例（FIX-30）：「原判定 → 正确判定」的 few-shot 段落。

    calibration 每项 {content, wrong, correct, field}；为空时返回空串，
    保证与未校准时的 prompt 完全一致（原版无此段）。
    """
    items = [c for c in (calibration or []) if isinstance(c, dict)]
    if not items:
        return ""
    lines = ["\n## 校准样例（上一轮抽检判错的原帖，务必照此纠正）"]
    for item in items[:8]:
        text = str(item.get("content") or "").strip()[:120]
        field = str(item.get("field") or "判定")
        wrong = str(item.get("wrong") or "").strip()
        correct = str(item.get("correct") or "").strip()
        lines.append(f"- 原帖摘录：{text}")
        lines.append(f"  {field} 原判「{wrong}」是错的，应为「{correct}」；"
                     "同类表述按后者判定。")
    return "\n".join(lines) + "\n"


def _r1_sys(context: dict | None = None, calibration: list[dict] | None = None) -> str:
    """轮次一 system prompt；context 为 None 时返回无上下文的兼容版本。"""
    if not context:
        return ("你是营销舆情标注员。对每条内容判定：1) 情感倾向(正面/中性/负面)；"
                "2) 是否与活动相关(是/否)。只输出 JSON 数组，每项 "
                "{\"id\": \"行id\", \"sentiment\": \"正面/中性/负面\", "
                "\"campaign_relevant\": \"是/否\"}，顺序与输入一致，不要多余文字。")
    ctx = _r1_context(context)
    type_line = f"活动类型：{ctx['campaign_type']}\n" if ctx["campaign_type"] else ""
    return (
        "你是专业的社媒文本标注员，对每条内容独立标注两个字段。\n"
        f"\n## 活动背景\n本次复盘活动：「{ctx['campaign_name']}」。"
        f"{ctx['campaign_context']}\n{type_line}"
        "\n## 1. sentiment（情感倾向）\n"
        "- 正面：明确表达积极评价、推荐、喜爱、支持\n"
        "- 中性：纯信息传递、无明显情感、内容展示/介绍\n"
        "- 负面：抱怨、批评、质疑、失望、负面情绪\n"
        "正负并存时以主导情感为准。\n"
        "\n## 2. campaign_relevant（是否活动相关）\n"
        f"- 是：内容明确提及、参与、响应或评价「{ctx['campaign_name']}」本次活动；"
        "赞助型活动（播客/节目/IP 合作）中提及被赞助内容也视为相关，无需同时提及品牌名\n"
        "- 否：品牌一般内容、其他品牌讨论、行业泛讨论等与本次活动无关\n"
        "\n## 相关性上下文规则\n"
        f"活动取数表达式（保留 OR/AND 分支关系，不要求 AND 词连续或同句）：{ctx['activity_keywords']}\n"
        "- 先定位每处关键词命中，查看命中当句、前一句、后一句；三句不足再扩读标题与全文。\n"
        "- 任一窗口存在真实活动关联即判相关；负面不等于噪音，真实投诉/差评仍为「是」。\n"
        "- 同名歧义、无关正文挂活动话题、其他期次活动判「否」。\n"
        "- 正文很少或只有标签时，结合视频封面/音频/视频文本识别结果判断；"
        "识别字段缺失只记录无信息，不据此判噪音。\n"
        "\n只输出 JSON 数组，每个元素 "
        "{\"id\":\"行id\",\"sentiment\":\"正面|中性|负面\","
        "\"campaign_relevant\":\"是|否\"}，不附加解释。"
        + _calibration_block(calibration))


def _apply_r1(row: dict, parsed: dict) -> None:
    """把轮次一解析结果回填到行（ai_sentiment / relevance）。

    输出字段以 campaign_relevant 为准；兼容旧缓存的 relevant。
    非法值不落库，留空由覆盖检查判缺口后重试。
    """
    si = _senti_to_int(parsed.get("sentiment"))
    if si is not None:
        row["ai_sentiment"] = si
        row["ai_sentiment_label"] = _INT_TO_SENTI[si]
    for key in ("campaign_relevant", "relevant"):
        if key in parsed and parsed.get(key) not in (None, ""):
            row["relevance"] = _to_bool(parsed.get(key))
            break
    if _to_bool(parsed.get("uncertain")):
        # 文本不足以判断：交轮次一审核优先抽取（FIX-30），不判为噪音。
        row["r1_uncertain"] = True


def _r1_text(row: dict) -> tuple[str, str, str]:
    """(标题, 正文, 识别文本)；识别文本按字段分别保留，不跨字段拼接。"""
    title = str(row.get("title") or "")[:R1_TITLE_CHARS]
    content = str(row.get("content") or "")[:R1_CONTENT_CHARS]
    media = []
    budget = R1_MEDIA_CHARS
    for field in R1_MEDIA_FIELDS:
        text = str(row.get(field) or "").strip()
        if text and budget > 0:
            piece = text[:budget]
            media.append(f"{field}：{piece}")
            budget -= len(piece)
    return title, content, "\n".join(media)


def _r1_payload_row(row: dict) -> dict | None:
    """构造单条入参；标题与正文都为空时返回 None（不送模型）。"""
    title, content, media = _r1_text(row)
    if not title.strip() and not content.strip() and not media:
        return None
    item = {"id": row["_annot_id"], "platform": str(row.get("_platform") or "")}
    if title.strip():
        item["title"] = title
    if content.strip():
        item["content"] = content
    if media:
        item["media_text"] = media
    return item


def _r1_skip_empty(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """分离空内容行；空内容行不送模型，标「标注跳过_无内容」（FIX-27）。

    依据：annot_r1.py 的 todo = [...str().strip() 判断] 与
    KS_Exception_Handling.md 内容空值条目。
    """
    to_annotate, skipped = [], []
    for r in rows:
        title, content, media = _r1_text(r)
        if title.strip() or content.strip() or media:
            to_annotate.append(r)
        else:
            r.setdefault("annotation_skipped", SKIP_EMPTY_LABEL)
            skipped.append(r)
    return to_annotate, skipped


def _r1_labeled(row: dict) -> bool:
    """轮次一两字段都已合法落值。"""
    return row.get("ai_sentiment") in (1, 0, -1) and "relevance" in row


def _small_engine_r1(
    rows: list[dict],
    context: dict | None = None,
    calibration: list[dict] | None = None,
    batch: int = R1_BATCH,
    max_attempts: int = R1_MAX_ATTEMPTS,
    cache=None,
) -> None:
    """小引擎轮次一（FIX-27）。

    - 每批 15 条、最多 3 次（原版 annot_r1.py BATCH=15 / range(3)）。
    - 每条传 platform + 标题前 200 字 + 正文前 800 字 + 识别字段（非空）。
    - 空内容行已由 _r1_skip_empty 过滤，不再出现在 pending 里。
    - 合法值：情感 ∈ {正面,中性,负面}，campaign_relevant ∈ {是,否}；非法值丢弃等重试。
    - cache（FIX-30，可选）：命中则不送模型；只缓存合法结果（原版 checkpoint 口径）。
    """
    sys_prompt = _r1_sys(context, calibration)
    if cache is not None:
        for row in rows:
            item = _r1_payload_row(row)
            hit = cache.get(item) if item else None
            if hit:
                _apply_r1(row, hit)
        rows = [row for row in rows if not _r1_labeled(row)]
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        pending = list(chunk)
        for _attempt in range(max_attempts):
            items = {r["_annot_id"]: item for r in pending
                     if (item := _r1_payload_row(r))}
            if not items:
                break
            data = semantic.chat_json(
                sys_prompt,
                json.dumps(list(items.values()), ensure_ascii=False),
                max_tokens=2048,
            )
            by_id = (
                {str(d.get("id")): d for d in data if isinstance(d, dict)}
                if isinstance(data, list) else {}
            )
            for row in pending:
                parsed = by_id.get(row["_annot_id"])
                if isinstance(parsed, dict):
                    _apply_r1(row, parsed)
                    if cache is not None and _r1_labeled(row):
                        cache.put(items[row["_annot_id"]], parsed)
            pending = [row for row in pending if not _r1_labeled(row)]
            if not pending:
                break
    if cache is not None:
        cache.save()


def annotate_round1(
    rows: list[dict], *, context: dict, threshold: int = 1000,
    rerun_ids: list[str] | None = None,
    calibration: list[dict] | None = None,
    cache_dir=None,
) -> dict:
    """轮次一标注。返回 {rows, round:1, routed_via, coverage, degraded?, skipped_empty}。

    context 必填（FIX-27）：campaign_name / campaign_context / activity_keywords，
    缺任一项抛错（annotation_prompt.md「项目配置区」：未填入禁止开始标注）。
    rerun_ids：只重标这些 _annot_id（FIX-30 局部错误重标），为空则标注全部行。
    calibration：上轮抽检判错的样例，作为 few-shot 注入 prompt（FIX-30 校准）。
    """
    ctx = _r1_context(context)
    # 缓存在上下文校验之后才建（缺上下文由 _r1_context 统一 fail-closed）。
    cache = (annotation_cache.AnnotationCache(
        cache_dir, "round1", _r1_sys(ctx, calibration))
        if cache_dir is not None else None)
    _tag_ids(rows)
    if rerun_ids:
        wanted = {str(x) for x in rerun_ids}
        # 增量重标：只重标指定 ID，其余行保留原标注。
        to_run = [r for r in rows if r.get("_annot_id") in wanted]
    else:
        to_run = rows
    to_annotate, skipped = _r1_skip_empty(to_run)
    routed_via, degraded = _route_and_run(
        to_annotate, threshold=threshold,
        dh_prompt=_r1_dh_prompt(ctx), dh_apply=_apply_r1,
        small_engine=lambda batch: _small_engine_r1(
            batch, ctx, calibration, cache=cache),
        degrade=_degrade_r1, dh_columns=_R1_DH_COLUMNS)
    cov = _coverage_r1(to_annotate)
    cov["skipped_empty"] = len(skipped)
    out = {
        "rows": rows, "round": 1, "routed_via": routed_via,
        "coverage": cov, "context": ctx,
        "uncertain_ids": [r["_annot_id"] for r in to_annotate if r.get("r1_uncertain")],
        "cache_hits": cache.hits if cache is not None else 0,
    }
    if degraded:
        out["degraded"] = True
    return out


def _degrade_r1(rows: list[dict]) -> None:
    """轮次一降级：情感取现有 SSJH sentiment（无则按 0 中性），相关性按品牌/关键词命中。

    说明：这是无凭据时的确定性兜底以跑通链路，显式标 degraded；真实接真走模型。
    """
    for r in rows:
        si = _senti_to_int(r.get("sentiment"))
        r["ai_sentiment"] = si if si is not None else 0
        r["ai_sentiment_label"] = _INT_TO_SENTI[r["ai_sentiment"]]
        # 相关性降级：有 content/title 即视为相关（跑通用；真路径由模型判定）。
        r.setdefault("relevance", True)


def _coverage_r1(rows: list[dict]) -> dict:
    total = len(rows)
    with_senti = sum(1 for r in rows if r.get("ai_sentiment") in (1, 0, -1))
    with_rel = sum(1 for r in rows if "relevance" in r)
    return {"total": total, "sentiment_covered": with_senti, "relevance_covered": with_rel,
            "sentiment_missing": total - with_senti, "relevance_missing": total - with_rel}


# ── 轮次二：三个独立任务（观点正面 / 观点负面 / 属性标注）──────────
# 原版依据：Agent.Projects/.../_scripts/annot_r2.py
#   ① 正面观点：只送「活动相关 且 情感=正面」的行
#   ② 负面观点：只送「活动相关 且 情感=负面」的行
#   ③ 属性标注：送全部活动相关行（含中性），一次输出 ugc_category + discussion_dimension
# 标注前过滤（由代码完成，不交模型）：
#   - 不相关行：三个字段全部 N/A，不送模型；
#   - 中性相关行：opinion 填「无」，不进入观点任务；
#   - PGC 行：ugc_category 填 N/A，属性任务仍送该行但只要求维度。
OPINION_NONE = "无"
OPINION_OTHER = "其他"
UGC_OTHER = "其他"
R2_BATCH = 15
R2_MAX_ATTEMPTS = 3
ROUND2_TASKS = _ROUND2_TASKS

# 观点：空值 + 「其他」占比达到该阈值即阻断（FIX-28，默认 0.10）
OPINION_OTHER_BLOCK_RATIO = 0.10
# UGC：「其他」超过该占比时告警并建议重新做类别发现（FIX-28，默认 0.20）
UGC_OTHER_WARN_RATIO = 0.20


def _is_relevant_row(row: dict) -> bool:
    """活动相关=是。缺 relevance 视为未判定，不算相关。"""
    return row.get("relevance") is True


def _row_sentiment_label(row: dict) -> str:
    """行情感标签（正面/负面/中性/空）。"""
    return _INT_TO_SENTI.get(row.get("ai_sentiment"), "")


def _split_opinion_labels(value: str) -> list[str]:
    """「标签1,标签2」→ ['标签1','标签2']；兼容中文逗号与顿号。"""
    text = str(value or "").replace("，", ",").replace("、", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _r2_task_prompts(
    campaign_name: str,
    opinion_labels: list[str],
    ugc_categories: list[str],
    ugc_not_applicable: bool,
    opinion_review_items: list[dict] | None,
    ugc_review_items: list[dict] | None,
    calibration: list[dict] | None = None,
    negative_labels: list[str] | None = None,
) -> dict[str, str]:
    """三个子任务各自的 system prompt（原版 annot_r2.py 的三段 sysmsg）。

    opinion / opinion_negative 分别对应正面、负面两套候选；negative_labels
    为 None 时两方向共用 opinion_labels（单套候选的旧产物）。
    """
    ops = _label_table(opinion_labels, opinion_review_items)
    ugcs = _label_table(ugc_categories, ugc_review_items)
    ugc_part = (
        "## ugc_category（UGC 参与方式）\n"
        "分类依据是用户做了什么动作、采用什么内容形式，不依据情感/观点倾向——"
        "同样的动作和形式，无论夸还是骂，归同一类。选 1 个；都不符合填「其他」。\n"
        f"{ugcs}"
        if not ugc_not_applicable else
        "## ugc_category\n本项目仅 PGC，一律填 N/A。"
    )
    dim_part = (
        "## discussion_dimension（主要讨论对象，选 1 个主导项）\n"
        + _DISCUSSION_DIM_PROMPT
    )
    calib = _calibration_block(calibration)
    output_opinion = ('只输出 JSON 数组：[{"id":"行id","opinion":"标签1,标签2"}]，不附加解释。')
    output_attr = (
        '只输出 JSON 数组：[{"id":"行id","ugc_category":"…",'
        '"discussion_dimension":"品牌|产品|活动"}]，不附加解释。'
    )
    def opinion_prompt(polarity: str, table: str) -> str:
        return (
            f"你是专业文本标注员。以下是「{campaign_name}」活动的{polarity}内容。"
            "候选观点标签是【讨论话题】，不含情感；该内容的情感已确定为"
            f"{polarity}，只需判断它在讨论哪个话题。\n"
            f"## 候选标签\n{table}\n"
            "## 规则\n"
            "从候选标签中选 1 个最匹配的；仅当内容兼有两个不同话题时选 2 个"
            "（用英文逗号分隔）。优先篇幅最重的部分；都不匹配输出「其他」。\n"
            + output_opinion + calib
        )

    neg_table = (_label_table(negative_labels, opinion_review_items)
                 if negative_labels is not None else ops)
    return {
        "opinion": opinion_prompt("正面", ops),
        "opinion_negative": opinion_prompt("负面", neg_table),
        "attr": (
            f"你是专业文本标注员。对「{campaign_name}」活动的每条内容标注字段"
            "（与情感无关，中性内容同样标注）。\n"
            f"{ugc_part}\n\n{dim_part}\n"
            + output_attr + calib
        ),
    }


def _label_table(labels: list[str], review_items: list[dict] | None) -> str:
    """标签清单 → 「- 名称：定义（例：…）」。定义与例子来自已确认类别（FIX-31）。"""
    items = {str((item or {}).get("name") or ""): item
             for item in (review_items or []) if isinstance(item, dict)}
    rows = []
    for name in labels or []:
        meta = items.get(name) or {}
        definition = str(meta.get("definition") or "").strip()
        examples = [str(q)[:40] for q in (meta.get("examples") or [])[:2] if str(q).strip()]
        line = f"- {name}"
        if definition:
            line += f"：{definition}"
        if examples:
            line += f"（例：{'；'.join(examples)}）"
        rows.append(line)
    return "\n".join(rows) if rows else "（无预设）"


def _apply_opinion(row: dict, parsed: dict, allowed: list[str]) -> None:
    """回填观点；候选之外的值丢弃（视为漏标，交覆盖检查重试）。"""
    value = str(parsed.get("opinion") or "").strip()
    if not value:
        return
    labels = _split_opinion_labels(value)
    if not labels or any(label not in (*allowed, OPINION_OTHER) for label in labels):
        return
    # 原版：最多 2 个标签，用英文逗号连接（check_annotation_structure 按 "," 拆分）。
    row["opinion"] = ",".join(labels[:2])


def _apply_attr(row: dict, parsed: dict, allowed_ugc: list[str], use_ugc: bool) -> None:
    """回填属性任务；维度不在三枚举内时丢弃（交覆盖检查判 ERROR）。"""
    if use_ugc:
        ugc = str(parsed.get("ugc_category") or "").strip()
        if ugc and ugc in (*allowed_ugc, UGC_OTHER, _NA):
            row["ugc_category"] = ugc
    dim = str(parsed.get("discussion_dimension") or "").strip()
    if dim in (*_DISCUSSION_DIMENSIONS, _NA):
        row["discussion_dimension"] = dim


def _apply_r2(row: dict, parsed: dict) -> None:
    """兼容入口：单条结果按三任务规则分别回填。

    保留该函数是为了兼容旧调用点与 DataHub 路径；新代码应直接用
    `_apply_opinion` / `_apply_attr`，按任务的适用范围处理。
    """
    allowed_opinion = list(parsed.get("_allowed_opinion") or [])
    allowed_ugc = list(parsed.get("_allowed_ugc") or [])
    if "opinion" in parsed or "观点" in parsed:
        _apply_opinion(row, parsed, allowed_opinion)
    if "discussion_dimension" in parsed or "ugc_category" in parsed:
        _apply_attr(row, parsed, allowed_ugc, use_ugc=True)


def _preset_r2_defaults(
    rows: list[dict], ugc_not_applicable: bool
) -> dict[str, list[dict]]:
    """标注前过滤（FIX-28，原版 annot_r2.py:64-68）。

    返回三个子任务各自的行集合：
      - opinion_pos / opinion_neg：活动相关且情感为正/负的行；
      - attr：活动相关的全部行（含中性）。
    不相关行与中性行的 opinion 在此直接定值，不送模型。
    """
    opinion_pos, opinion_neg, attr = [], [], []
    for row in rows:
        if not _is_relevant_row(row):
            # 不相关：三字段全部 N/A，不送模型。
            row["opinion"] = _NA
            row["ugc_category"] = _NA
            row["discussion_dimension"] = _NA
            continue
        attr.append(row)
        sentiment = _row_sentiment_label(row)
        if sentiment == "正面":
            opinion_pos.append(row)
        elif sentiment == "负面":
            opinion_neg.append(row)
        else:
            # 中性（或未判定）：观点填「无」，不进入观点任务。
            row["opinion"] = OPINION_NONE
    if ugc_not_applicable:
        for row in rows:
            row["ugc_category"] = _NA
    return {"opinion_pos": opinion_pos, "opinion_neg": opinion_neg, "attr": attr}


def _task_payload(rows: list[dict], fields: tuple[str, ...]) -> list[dict]:
    """构造某子任务的入参：id + 平台 + 标题200 + 正文800 + 识别文本。"""
    payload = []
    for row in rows:
        title, content, media = _r1_text(row)
        item: dict = {"id": row["_annot_id"],
                      "platform": str(row.get("_platform") or "")}
        if title.strip():
            item["title"] = title
        if content.strip():
            item["content"] = content
        if media:
            item["media_text"] = media
        payload.append(item)
    return payload


def _run_task(
    rows: list[dict],
    *,
    prompt: str,
    apply_fn,
    is_missing,
    batch: int = R2_BATCH,
    max_attempts: int = R2_MAX_ATTEMPTS,
) -> None:
    """跑一个子任务：每批 15 条、最多重试 3 次，只重试仍缺失的行。"""
    for start in range(0, len(rows), batch):
        pending = list(rows[start:start + batch])
        for _attempt in range(max_attempts):
            if not pending:
                break
            data = semantic.chat_json(
                prompt,
                json.dumps(_task_payload(pending, ()), ensure_ascii=False),
                max_tokens=2560,
            )
            by_id = (
                {str(item.get("id")): item for item in data if isinstance(item, dict)}
                if isinstance(data, list) else {}
            )
            for row in pending:
                parsed = by_id.get(row["_annot_id"])
                if isinstance(parsed, dict):
                    apply_fn(row, parsed)
            pending = [row for row in pending if is_missing(row)]
            if not pending:
                break


def annotate_round2(
    rows: list[dict],
    *,
    opinion_labels: list[str],
    ugc_categories: list[str],
    threshold: int = 1000,
    ugc_not_applicable: bool = False,
    opinion_review_items: list[dict] | None = None,
    ugc_review_items: list[dict] | None = None,
    campaign_name: str = "",
    rerun_ids: list[str] | None = None,
    calibration: list[dict] | None = None,
    negative_labels: list[str] | None = None,
) -> dict:
    """轮次二标注：三个独立子任务 + 按适用范围的覆盖检查（FIX-28）。

    子任务与客户 annot_r2.py 一致，DAG 仍是一个 n11 节点：三个任务的产物都写在
    同一行上，物理拆成多个节点只会增加 join，不改变结果。
    UGC 类别未注入且不是「仅 PGC」时抛错（原版禁止在无类别时标注）。
    """
    if not ugc_not_applicable and not [c for c in (ugc_categories or []) if str(c).strip()]:
        raise ValueError(
            "annotate_round2: 未注入 ugc_category_list，禁止开始 UGC 方式标注")
    _tag_ids(rows)
    groups = _preset_r2_defaults(rows, ugc_not_applicable)
    if rerun_ids:
        # 增量重标（FIX-30）：只保留指定 ID，其余行保留原标注。
        wanted = {str(x) for x in rerun_ids}
        groups = {key: [r for r in group if r.get("_annot_id") in wanted]
                  for key, group in groups.items()}
    prompts = _r2_task_prompts(
        campaign_name, opinion_labels, ugc_categories,
        ugc_not_applicable, opinion_review_items, ugc_review_items,
        calibration, negative_labels)
    neg_labels = opinion_labels if negative_labels is None else negative_labels

    opinion_missing = lambda row: not str(row.get("opinion") or "").strip()
    attr_missing = lambda row: (
        not str(row.get("discussion_dimension") or "").strip()
        or (not ugc_not_applicable and not str(row.get("ugc_category") or "").strip())
    )

    routed_via, degraded = "small_engine", False
    if semantic.llm_available():
        try:
            # 正负各用各自的 prompt 与候选（原版 annot_r2.py 两个独立观点任务）
            for group, prompt, allowed in (
                (groups["opinion_pos"], prompts["opinion"], opinion_labels),
                (groups["opinion_neg"], prompts["opinion_negative"], neg_labels),
            ):
                if group:
                    _run_task(
                        group, prompt=prompt,
                        apply_fn=lambda row, parsed, allowed=allowed: _apply_opinion(
                            row, parsed, allowed),
                        is_missing=opinion_missing)
            if groups["attr"]:
                _run_task(
                    groups["attr"], prompt=prompts["attr"],
                    apply_fn=lambda row, parsed: _apply_attr(
                        row, parsed, ugc_categories, not ugc_not_applicable),
                    is_missing=attr_missing)
        except Exception:
            degraded = True
    else:
        degraded = True
    if degraded:
        _degrade_r2(rows, opinion_labels, ugc_categories)

    cov = check_coverage(
        rows, degraded=degraded, ugc_not_applicable=ugc_not_applicable, groups=groups)
    out = {"rows": rows, "round": 2, "routed_via": routed_via, "coverage": cov,
           "tasks": {
               "opinion_positive": len(groups["opinion_pos"]),
               "opinion_negative": len(groups["opinion_neg"]),
               "attribute": len(groups["attr"]),
           }}
    if ugc_not_applicable:
        out["not_applicable"] = ["ugc_category"]
    if degraded:
        out["degraded"] = True
    return out


def _degrade_r2(rows: list[dict], opinion_labels, ugc_categories) -> None:
    """轮次二降级：确定性派生三标签（命中候选或 N/A），显式标 degraded。"""
    op0 = opinion_labels[0] if opinion_labels else _NA
    for r in rows:
        hay = (str(r.get("title") or "") + " " + str(r.get("content") or ""))
        if str(r.get("opinion") or "").strip():
            continue  # 已由标注前过滤定值（N/A / 无），不覆盖
        r["opinion"] = next((o for o in opinion_labels if o and o in hay), op0)
        r.setdefault("ugc_category",
                     next((u for u in ugc_categories if u and u in hay),
                          (ugc_categories[0] if ugc_categories else _NA)))
        # FIX-14 降级时也固定用三枚举（降级整体以 DEGRADED 标记，不影响 Validator 阻断）。
        r.setdefault("discussion_dimension",
                     next((d for d in _DISCUSSION_DIMENSIONS if d in hay), _NA))


def check_coverage(
    rows: list[dict],
    *,
    degraded: bool = False,
    ugc_not_applicable: bool = False,
    groups: dict[str, list[dict]] | None = None,
) -> dict:
    """按各字段的适用范围检查覆盖（FIX-28，原版 check_coverage.py 口径）。

    每项输出 {scope_total, labeled, na, other, missing, status}，其中 scope_total
    是该字段真正的适用范围（观点=相关且非中性；维度/UGC=活动相关行）。
      - DEGRADED：降级产物，不阻断；
      - OK：无缺口；
      - WARN：UGC「其他」超过 20%，建议重做类别发现（不阻断）；
      - ERROR：存在缺口，或观点「空值+其他」达到 10%，或维度越界。
    """
    total = len(rows)
    relevant = [r for r in rows if _is_relevant_row(r)]
    opinion_scope = [
        r for r in relevant if _row_sentiment_label(r) in ("正面", "负面")
    ]

    def stat(scope: list[dict], field: str, other_value: str | None = None) -> dict:
        labeled = sum(1 for r in scope if str(r.get(field) or "").strip())
        na = sum(1 for r in scope if str(r.get(field) or "").strip() == _NA)
        other = (sum(1 for r in scope if str(r.get(field) or "").strip() == other_value)
                 if other_value else 0)
        return {"scope_total": len(scope), "labeled": labeled, "na": na,
                "other": other, "missing": len(scope) - labeled}

    cov: dict[str, dict] = {}
    invalid_dim = [
        r for r in relevant
        if str(r.get("discussion_dimension") or "").strip()
        and str(r.get("discussion_dimension") or "").strip()
        not in (*_DISCUSSION_DIMENSIONS, _NA)
    ]

    def status_of(item: dict, *, block_ratio: float | None = None,
                  warn_ratio: float | None = None) -> str:
        if degraded:
            return "DEGRADED"
        if item["missing"]:
            return "ERROR"
        scope = item["scope_total"]
        if block_ratio and scope:
            if (item["missing"] + item["other"]) / scope >= block_ratio:
                return "ERROR"
        if warn_ratio and scope and item["other"] / scope > warn_ratio:
            return "WARN"
        return "OK"

    for task, field, other_value, block, warn in (
        ("opinion", "opinion", OPINION_OTHER, OPINION_OTHER_BLOCK_RATIO, None),
        ("discussion_dimension", "discussion_dimension", None, None, None),
        ("ugc_category", "ugc_category", UGC_OTHER, None,
         None if ugc_not_applicable else UGC_OTHER_WARN_RATIO),
    ):
        if task == "opinion":
            item = stat(opinion_scope, field, other_value)
        elif task == "ugc_category" and ugc_not_applicable:
            item = {"scope_total": len(relevant), "labeled": len(relevant),
                    "na": len(relevant), "other": 0, "missing": 0}
        else:
            item = stat(relevant, field, other_value)
        item["status"] = status_of(item, block_ratio=block, warn_ratio=warn)
        cov[task] = item

    if invalid_dim:
        cov["discussion_dimension"]["status"] = (
            "DEGRADED" if degraded else "ERROR")
        cov["discussion_dimension"]["invalid_values"] = sorted({
            str(r.get("discussion_dimension") or "").strip() for r in invalid_dim})

    # 按情感拆分的缺失统计：若观点任务全为中性行 → 说明被错并入正负任务。
    cov["opinion_by_sentiment"] = {
        label: sum(1 for r in relevant if _row_sentiment_label(r) == label)
        for label in ("正面", "负面", "中性")
    }
    if groups is not None:
        cov["scope"] = {
            "total": total, "relevant": len(relevant),
            "opinion_positive": len(groups["opinion_pos"]),
            "opinion_negative": len(groups["opinion_neg"]),
            "attribute": len(groups["attr"]),
        }
    cov["ugc_rediscovery_suggested"] = (
        cov["ugc_category"]["status"] == "WARN")
    cov["has_error"] = any(
        item.get("status") == "ERROR"
        for item in cov.values() if isinstance(item, dict)
    )
    return cov


# ── 路由 + 执行（真路径 DataHub / 小引擎；无凭据降级）────────────
def _route_and_run(rows, *, threshold, dh_prompt, dh_apply, small_engine, degrade,
                   dh_columns=None) -> tuple[str, bool]:
    """按阈值与可用凭据选择执行路径。返回 (routed_via, degraded)。

    - 行数 ≥ threshold 且 DataHub 可用 → datahub 批量；
    - 否则小引擎（semantic）可用 → 小引擎；
    - 都不可用 / 真路径异常 → 确定性降级。
    """
    n = len(rows)
    use_datahub = n >= threshold and datahub.api_available()
    if use_datahub:
        try:
            _run_datahub(rows, dh_prompt, dh_apply, dh_columns)
            return "datahub", False
        except Exception:
            pass  # 批量任务级失败 → 落小引擎/降级
    if semantic.llm_available() and n:
        try:
            small_engine(rows)
            return "small_engine", False
        except Exception:
            pass
    degrade(rows)
    return "fallback", True


def _run_datahub(rows, prompt, apply_fn, content_columns=None) -> None:
    """DataHub 批量标注并回填。行级解析失败不抛，留待覆盖检查判定。"""
    results = datahub.batch_annotate(
        rows, prompt, content_columns=content_columns or ["content", "title"])
    for r in rows:
        parsed = results.get(r["_annot_id"])
        if isinstance(parsed, dict) and not parsed.get("_error"):
            apply_fn(r, parsed)
