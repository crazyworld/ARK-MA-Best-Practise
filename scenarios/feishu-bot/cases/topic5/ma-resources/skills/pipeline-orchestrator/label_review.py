"""Versioned label review metadata; quotes are copied from source rows, never LLM prose."""
from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any

REVIEW_SCHEMA_VERSION = 1


# 类别发现抽样口径（FIX-31）：原版 opinion/ugc-discovery run.py 的 draw_sample。
DISCOVERY_SEED = 42
DISCOVERY_TOP_RATIO = 0.5
# 摘录长度：标题 200 字 + 正文 800 字（与轮次一标注输入一致，FIX-27/31）
EXCERPT_TITLE_CHARS = 200
EXCERPT_CONTENT_CHARS = 800
# 正文按段切分 quote，单段长度；段越小定位越准，模型只能引用其中某段
EXCERPT_SEGMENT_CHARS = 200


def _interaction(row: dict) -> int:
    """总互动量；缺失/非法按 0（只用于排序，不参与任何统计）。"""
    try:
        return int(float(row.get("interaction_cnt") or 0))
    except (TypeError, ValueError):
        return 0


def _identity_digest(row: dict) -> str:
    identity = str(
        row.get("_dedup_key") or row.get("fingerprint") or row.get("id")
        or row.get("item_id") or row.get("url")
        or json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def draw_mixed_sample(
    pool: list, size: int, *, seed: int = DISCOVERY_SEED,
    top_ratio: float = DISCOVERY_TOP_RATIO, key=_interaction,
) -> tuple[list, dict]:
    """互动 Top 50% + 其余随机 50%（原版 draw_sample 逐行对应）。

    池子不超过 size 时取全部（mode=all）。排序按互动降序，同互动量以输入顺序
    作为 tie-break（调用方先按 sha256 稳定排序，保证可复现）。
    """
    ranked = sorted(pool, key=key, reverse=True)
    if len(ranked) <= size:
        return ranked, {"mode": "all", "top_n": len(ranked), "random_n": 0, "seed": None}
    top_n = int(round(size * top_ratio))
    rest = ranked[top_n:]
    picked = random.Random(seed).sample(rest, size - top_n)
    return ranked[:top_n] + picked, {
        "mode": "mixed", "top_n": top_n, "random_n": size - top_n, "seed": seed}


def _quote_spans(field: str, text: str) -> list[tuple[int, str]]:
    """标题整段一个 quote；正文按段切，总长不超过 EXCERPT_CONTENT_CHARS。"""
    if field == "title":
        return [(0, text[:EXCERPT_TITLE_CHARS])]
    body = text[:EXCERPT_CONTENT_CHARS]
    return [(start, body[start:start + EXCERPT_SEGMENT_CHARS])
            for start in range(0, len(body), EXCERPT_SEGMENT_CHARS)]


def source_catalog(
    rows: list[dict], size: int = 200, *, seed: int = DISCOVERY_SEED,
) -> tuple[list[dict], dict]:
    """Mixed sample with field-bound contiguous excerpts and unambiguous quote IDs.

    抽样：互动 Top 50% + 随机 50%（seed=42），池不足 size 取全部。
    返回 (samples, catalog, sampling)；sampling 记录 mode/top_n/random_n/seed/
    pool_size/sampled（原版 discovery 结果里的同名字段）。
    """
    sources = []
    for index, row in enumerate(rows):
        if not any(str(row.get(field) or "").strip() for field in ("title", "content")):
            continue
        source_index = row.get("_review_source_index", index)
        sources.append((_identity_digest(row), source_index, row))
    # sha256 稳定序作为同互动量的 tie-break，保证抽样可复现
    sources.sort(key=lambda item: (item[0], item[1]))
    picked, sampling = draw_mixed_sample(
        sources, size, seed=seed, key=lambda item: _interaction(item[2]))
    samples, catalog = [], {}
    for digest, index, row in picked:
        # Index disambiguates repeated source IDs without merging their original fields.
        post_ref = f"p-{digest[:16]}-{index}"
        quotes = []
        for field in ("title", "content"):
            text = str(row.get(field) or "")
            for start, excerpt in _quote_spans(field, text):
                if not excerpt.strip():
                    continue
                quote_id = f"{post_ref}:{field}:{start}"
                catalog[quote_id] = {
                    "quote_id": quote_id,
                    "post_ref": post_ref,
                    "post_id": str(row.get("id") or row.get("item_id") or ""),
                    "source_row_index": index,
                    "platform": str(row.get("_platform") or row.get("site_name") or ""),
                    "content_type": str(row.get("content_mode") or "UKN"),
                    "title": str(row.get("title") or "")[:EXCERPT_TITLE_CHARS],
                    "url": str(row.get("url") or ""),
                    "source_field": field,
                    "start": start,
                    "end": start + len(excerpt),
                    "excerpt": excerpt,
                }
                quotes.append({"quote_id": quote_id, "excerpt": excerpt})
        samples.append({
            "post_ref": post_ref,
            "content_type": str(row.get("content_mode") or "UKN"),
            "interaction": _interaction(row),
            "quotes": quotes,
        })
    sampling.update({"pool_size": len(sources), "sampled": len(picked)})
    return samples, catalog, sampling


# 类别命名口径（FIX-31/32）：原版 opinion/ugc-discovery run.py 的同名常量，逐字照抄。
SENTIMENT_WORDS = ("好评", "差评", "吐槽", "避雷", "失望", "满意", "不满", "喜欢", "种草",
                   "安利", "夸赞", "点赞", "批评", "抱怨", "推荐", "踩雷", "惊艳", "翻车",
                   "正面", "负面", "积极", "消极")
CATCHALL_NAMES = ("其他", "其它", "未分类", "杂项", "无法归类", "其余")
EVAL_SUFFIX = re.compile(r"(好|差|棒|赞|烂|高|低|贵|便宜)$")
# 类别数量区间（pipeline_config.phase_discovery.num_categories / ugc 3-6）
CATEGORY_COUNT = {"positive": (5, 8), "negative": (3, 5), "ugc": (3, 6)}
POLARITY_LABEL = {"positive": "正面", "negative": "负面"}


def tendency_hits(name: str) -> list[str]:
    """类别名命中的倾向词；以评价词结尾记为「评价后缀」（原版 lint_names）。"""
    text = str(name or "").strip()
    hits = [word for word in SENTIMENT_WORDS if word in text]
    if EVAL_SUFFIX.search(text):
        hits.append("评价后缀")
    return hits


def lint_categories(names: list[str], kind: str) -> list[str]:
    """命名与数量告警（原版 lint_names + lint_count：只告警，不中断）。

    kind ∈ positive / negative / ugc。告警交 Validator 核查后改名或合并。
    """
    label = "观点" if kind in POLARITY_LABEL else "参与方式"
    subject = "讨论的话题" if label == "观点" else "用户做了什么动作、采用什么内容形式"
    warns, seen = [], set()
    for name in names:
        hits = tendency_hits(name)
        if hits:
            warns.append(f"类别「{name}」含情感/评价措辞{hits}：{label}应只描述{subject}，"
                         "请提交 Validator 核查后改名")
        if name in CATCHALL_NAMES:
            warns.append(f"类别「{name}」是兜底类：不应单设，无法归类的内容由标注环节统一填「其他」")
        if name in seen:
            warns.append(f"类别名称重复：「{name}」")
        seen.add(name)
    low, high = CATEGORY_COUNT[kind]
    if not low <= len(names) <= high:
        warns.append(f"{label}类别数为 {len(names)}，要求 {low}-{high}：请提交 Validator "
                     "核查是否需要合并相近类别或补充遗漏类别")
    return warns


_GROUNDING_RULES = (
    "每项 name、definition、boundary 必填；evidence_refs 最多2项，优先一个代表原帖，"
    "必要时一个边界/反例。只选择输入已有 quote_id，usage 为 representative 或 boundary，"
    "reason 最多60字。找不到代表帖时 evidence_refs 填空数组，禁止写原帖摘录、链接或发明ID。"
    "输入 excerpt 是原字段的连续片段，不得当作完整帖子。"
)


def discovery_prompt(key: str, *, kind: str, campaign_name: str = "") -> str:
    """类别发现 prompt（原版两份 run.py 的 prompt 逐条移植，输出格式改为可核验引用）。

    kind ∈ positive / negative / ugc。
    """
    campaign = f"「{campaign_name}」" if campaign_name else "本次"
    low, high = CATEGORY_COUNT[kind]
    if kind == "ugc":
        body = (
            f"阅读以下{campaign}活动的UGC样本，识别用户参与该活动的主要方式，归纳{low}-{high}个类别。\n\n"
            "## 类别的定义（必须遵守）\n"
            "- 参与方式 = 用户【做了什么动作】、【采用什么内容形式】（如：到店打卡、开箱测评、晒单分享、"
            "二创改编、话题接龙、提问求助），类别名称2-4字，用动作或内容形式命名\n"
            "- 类别可以是动作或内容形式，两者可在同一套类别中并列；但类别之间不得重叠——一条帖子同时"
            "符合多类时，按其主要参与方式归入一类，并在 boundary 里写清区分点；类别中不要混入话题、"
            "情感或人群维度\n"
            "- 不得按情感、态度或观点倾向分类，类别名称与定义中都不要出现好评、吐槽、避雷、种草、安利、"
            "喜欢、失望等倾向词；同样的动作和形式，无论内容夸还是骂，归入同一类（情感另有独立字段）\n\n"
            "## MECE 要求\n"
            "- 互斥：任取两类，应能用一句话说清区别；一条帖子按其主要参与方式只归入一类\n"
            "- 穷尽：类别合起来应覆盖样本中绝大多数内容，个别无法归类的留给‘其他’，不要单独设‘其他’类\n"
            "- 输出前自查是否满足以上要求\n\n"
            "样本包含高互动帖和随机抽取的帖子两部分，请同等对待，不要只关注高互动帖；"
            "低频但边界清晰的参与方式也应保留。\n"
        )
    else:
        polarity = POLARITY_LABEL[kind]
        body = (
            f"阅读以下{campaign}活动的{polarity}内容样本，归纳用户讨论的观点主题，"
            f"输出{low}-{high}个观点类别。\n\n"
            "## 观点类别的定义（必须遵守）\n"
            "- 观点类别 = 用户【讨论的话题/对象/方面】（如：口感风味、价格优惠、联名周边、门店排队、"
            "包装设计），用名词性短语命名，4-10字\n"
            f"- 这些内容的情感倾向已由独立字段判定为{polarity}，类别不要再表达情感或态度：名称与定义中"
            "不要出现好评、差评、吐槽、喜欢、失望、满意、推荐等倾向词，也不要写成‘XX好’‘XX差’‘XX贵’"
            "这类评价句；需要说明倾向时，写在判断标准里，而不是写在类别名里\n"
            "- 同一套类别使用同一个分类标准（都按话题归类），不要混用话题、人群、平台等不同维度\n\n"
            "## MECE 要求\n"
            "- 互斥：各类的话题边界清晰，任取两类应能用一句话说清区别，不出现包含或交叉关系\n"
            "- 穷尽：类别合起来应覆盖样本中绝大多数内容，个别无法归类的留给‘其他’，不要单独设‘其他’类\n"
            "- 占比按‘主要讨论焦点’估算；同一条内容兼有两个话题时才允许同时归入两类\n"
            "- 输出前自查是否满足以上要求\n\n"
            "样本包含高互动帖和随机抽取的帖子两部分，请同等对待，不要只关注高互动帖。\n"
        )
    return (
        body + "证据不足时允许更少，禁止为了凑数量编造。" + _GROUNDING_RULES + "仅输出 JSON：\n"
        f'{{"{key}":[{{"name":"类别名称","definition":"判断标准",'
        '"boundary":"排除情形、与相近类别的区分点及同时命中时的归类规则",'
        '"evidence_refs":[{"quote_id":"输入中的ID","usage":"representative",'
        '"reason":"此片段为何支持该类别"}]}]}}'
    )


def _required_text(item: dict, field: str, max_length: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"标签 {field} 必须是非空字符串且不超过 {max_length} 字")
    return value.strip()


def ground_candidates(data: Any, key: str, catalog: dict, max_items: int) -> dict:
    """Reject fabricated/cross-field references; missing support stays explicit for HC."""
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or len(items) > max_items:
        raise ValueError(f"{key} 必须是 1-{max_items} 个包含审核定义的对象")
    names, review_items = [], []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("候选须包含 name、definition、boundary、evidence_refs")
        name = _required_text(item, "name", 20)
        if name in names:
            raise ValueError(f"重复候选：{name}")
        definition = _required_text(item, "definition", 100)
        boundary = _required_text(item, "boundary", 180)
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or len(refs) > 2:
            raise ValueError(f"{name}: evidence_refs 必须是至多2项的数组")
        evidence, seen = [], set()
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError(f"{name}: 非法原帖引用")
            quote_id = ref.get("quote_id")
            if not isinstance(quote_id, str) or quote_id not in catalog:
                raise ValueError(f"{name}: 未知 quote_id")
            if quote_id in seen:
                raise ValueError(f"{name}: 重复 quote_id")
            seen.add(quote_id)
            source = catalog[quote_id]
            # Models may echo locator fields, but they cannot override the source.
            for field in (
                "excerpt", "post_ref", "post_id", "source_field", "url", "start", "end",
            ):
                if field in ref and ref[field] != source[field]:
                    raise ValueError(f"{name}: 原帖 {field} 与 quote_id 冲突")
            usage = ref.get("usage")
            if usage not in ("representative", "boundary"):
                raise ValueError(f"{name}: 非法证据用途")
            evidence.append({
                **source,
                "usage": usage,
                "reason": _required_text(ref, "reason", 60),
            })
        supported = any(item["usage"] == "representative" for item in evidence)
        names.append(name)
        review_items.append({
            "name": name,
            "definition": definition,
            "boundary": boundary,
            "evidence": evidence,
            "evidence_status": "grounded" if supported else "missing",
        })
    return {
        key: names,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "review_items": review_items,
        "evidence_count": len({item["post_ref"] for item in catalog.values()}),
        "sampling": {
            "method": "mixed_top50_random50",
            "sample_size": len({item["post_ref"] for item in catalog.values()}),
            "is_population_count": False,
            "excerpt_scope": (f"title_first{EXCERPT_TITLE_CHARS}_"
                              f"content_first{EXCERPT_CONTENT_CHARS}"),
        },
    }


def confirmed_review(opinion: dict, ugc: dict) -> dict:
    """Carry exact reviewed metadata into the confirmed artifact, preserving old arrays."""
    return {
        "opinion_review_items": list(opinion.get("review_items") or []),
        "ugc_review_items": list(ugc.get("review_items") or []),
        "review_schema_version": max(
            int(opinion.get("review_schema_version") or 0),
            int(ugc.get("review_schema_version") or 0),
        ),
    }


def annotation_rules(items: list[dict] | None, names: list[str]) -> str:
    """Only matching confirmed definitions/boundaries enter either annotation engine."""
    rules = [
        {
            "name": item["name"],
            "definition": item.get("definition") or "",
            "boundary": item.get("boundary") or "",
        }
        for item in (items or [])
        if isinstance(item, dict) and item.get("name") in names
    ]
    if not rules:
        return ""
    # Avoid DataHub's {{column}} template syntax in model-written prose.
    text = json.dumps(rules, ensure_ascii=False)
    return "\n确认的定义与边界（按边界的单标签优先规则执行）：" + re.sub(
        r"\{\{|\}\}", lambda match: "｛｛" if match[0] == "{{" else "｝｝", text
    )


# ── D1/D2 来源审核（FIX-30）──────────────────────────────────────
# 方案：确定性策略先过（handler 侧 reasons），再委派独立语义审核，
# 逐个类别核对「定义/boundary 与原帖 grounded quote 是否一致」。
# 结果写入 confirmed_opinion_labels.validator_review，有 P0 时转人工。
SOURCE_REVIEW_ACTOR = "Validator"
SOURCE_REVIEW_MODE = "independent-agent"
SOURCE_REVIEW_RULES_VERSION = "label-source-review-v1"


def source_review_prompt(key: str, items: list[dict]) -> str:
    """来源审核 prompt：每项给出类别名、定义、边界与代表原帖原文。"""
    title = "观点类别" if key == "opinion" else "UGC 参与方式类别"
    lines = [
        f"你是独立 Validator，审核{title}的 D1/D2 来源是否成立。",
        "对每个类别判断：给定的定义与边界，是否与它引用的原帖原文一致。",
        "只输出 JSON 数组，每项 "
        '{"name":"类别名","verdict":"pass|fail","severity":"P0|P1",'
        '"reason":"判断依据"}；P0 表示定义或边界与原帖矛盾、或原帖根本不支持该类别。',
        "不要改写类别，也不要凭空补充证据。",
        "",
    ]
    for item in items:
        name = str(item.get("name") or "")
        definition = str(item.get("definition") or "").strip()
        boundary = str(item.get("boundary") or "").strip()
        # 原帖原文来自 ground_candidates 回填的 evidence[].excerpt（代码复制，非模型文本）
        quotes = [
            f"[{ev.get('usage')}] {str(ev.get('excerpt') or '').strip()[:160]}"
            for ev in (item.get("evidence") or [])
            if isinstance(ev, dict) and str(ev.get("excerpt") or "").strip()
        ]
        lines.append(f"- 类别：{name}")
        lines.append(f"  定义：{definition or '（空）'}")
        lines.append(f"  边界：{boundary or '（空）'}")
        lines.append(f"  原帖：{'；'.join(quotes) if quotes else '（空）'}")
    return "\n".join(lines)


def _review_subject(key: str, artifact: dict) -> list[dict]:
    """取需审核的类别项。

    缺证据（evidence_status != grounded）的项已被确定性策略拦下转人工，
    这里只审有原帖可对照的项，不对没有证据的类别下结论。
    """
    return [
        item for item in (artifact.get("review_items") or [])
        if isinstance(item, dict) and item.get("name")
        and item.get("evidence_status") == "grounded"
    ]


def apply_source_review(
    key: str, artifact: dict, verdicts: list[dict]
) -> tuple[list[str], dict]:
    """把审核判定落到 artifact 副本上。

    返回 (p0_reasons, review_metadata)；p0_reasons 非空时调用方应转人工。
    判定不合法的项按「未审核」处理，不当作通过。
    """
    by_name = {
        str(v.get("name")): v for v in verdicts
        if isinstance(v, dict) and v.get("name")
    }
    p0: list[str] = []
    decisions: dict[str, dict] = {}
    for item in _review_subject(key, artifact):
        name = str(item.get("name"))
        verdict = by_name.get(name)
        if not isinstance(verdict, dict):
            decisions[name] = {"verdict": "unreviewed"}
            p0.append(f"{name} 未获来源审核结论")
            continue
        status = str(verdict.get("verdict") or "").strip().lower()
        severity = str(verdict.get("severity") or "P1").strip().upper()
        decisions[name] = {
            "verdict": status or "unreviewed",
            "severity": severity,
            "reason": str(verdict.get("reason") or "").strip()[:300],
        }
        if status == "fail" and severity == "P0":
            p0.append(f"{name} 来源审核不通过：{decisions[name]['reason'] or 'P0'}")
    metadata = {
        "actor_role": SOURCE_REVIEW_ACTOR,
        "mode": SOURCE_REVIEW_MODE,
        "rules_version": SOURCE_REVIEW_RULES_VERSION,
        "reviewed": len(decisions),
        "decisions": decisions,
        "p0": p0,
        "has_p0": bool(p0),
    }
    return p0, metadata


def _empty_review() -> dict:
    """无需审核（没有可审类别）时的审核元数据。"""
    return {
        "actor_role": SOURCE_REVIEW_ACTOR,
        "mode": SOURCE_REVIEW_MODE,
        "rules_version": SOURCE_REVIEW_RULES_VERSION,
        "reviewed": 0,
        "decisions": {},
        "p0": [],
        "has_p0": False,
    }


def _unreviewed(items: list[dict], reason: str, detail: str) -> tuple[list[str], dict]:
    """审核无法完成：全部类别记 P0 转人工（没有证据不能当作通过）。"""
    p0 = [f"{item.get('name')} {detail}" for item in items]
    return p0, {**_empty_review(), "reviewed": len(items), "p0": p0,
                "has_p0": True, "reason": reason}


def run_source_review(key: str, artifact: dict) -> tuple[list[str], dict]:
    """执行 D1/D2 来源审核：逐类别核对定义/边界与原帖 quote 是否一致。

    返回 (p0_reasons, review_metadata)。语义能力缺失、调用异常或没有
    有效结论时，全部类别按「未审核」计 P0，由调用方转人工。
    """
    items = _review_subject(key, artifact)
    if not items:
        return [], _empty_review()

    import semantic
    if not semantic.llm_available():
        return _unreviewed(items, "llm_unavailable", "语义能力缺失，无法完成来源审核")
    try:
        data = semantic.chat_json(
            source_review_prompt(key, items),
            json.dumps([item.get("name") for item in items], ensure_ascii=False),
            max_tokens=2048)
    except Exception as error:  # 审核调用异常：转人工，不猜结论
        return _unreviewed(items, f"llm_error:{type(error).__name__}", "来源审核调用失败")
    verdicts = parse_verdicts(data)
    if not verdicts:
        return _unreviewed(items, "no_verdicts", "来源审核无有效结论")
    return apply_source_review(key, artifact, verdicts)


def parse_verdicts(data) -> list[dict]:
    """解析审核输出；非数组或非 dict 项一律丢弃（不猜结论）。"""
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("name")]
