#!/usr/bin/env python3
"""topic5-validator 第一段：报告内容层全量确定性扫描器。

把一份报告内容层 MD（n16 产出的 report_md.markdown）按活动复盘口径红线做全量检查，
产出结构化 qc_report（含分级 issues、p0_count、has_p0），供 n17_validate 关卡注入、
由 n18_qc_gate 按 pass_policy=p0_count==0 路由。

两段式审计中本脚本负责「全量扫描」（机器可查的硬口径）；语义层「全文检查」
由 topic5-validator Agent 依规则适配表补充（命中的问题以同 schema 合并）。

用法：
  python3 scan_report.py --report-md /path/report_md.json --out /path/qc_report.json
  python3 scan_report.py --markdown-file /path/report.md --out /path/qc_report.json

分级（severity）：
  P0 禁发（has_p0=True → 必须返修）：出现禁用措辞（人均/转化率/转化/性价比）、
      出现非白名单“合计声量/总声量”相加口径、内部 JSON/标注字段泄漏、
      报告为空/缺核心章节。
  P1 可披露放行：图表引用缺失、降级产物标注、字数偏短等（记录但不拦截交付）。
  P2 提示：风格/措辞建议。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# 本脚本可能被 validator.py 以 importlib.util.spec_from_file_location 直接加载，
# 那种方式不会把本目录加入 sys.path，因此显式补齐后再导入同目录的 policy。
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import policy  # noqa: E402  口径唯一事实源


# ── 口径红线常量：全部来自 policy.py，本文件不再重复定义（FIX-57）──────
FORBIDDEN_TERMS = policy.FORBIDDEN_METRIC_TERMS
METRIC_WHITELIST = policy.METRIC_WHITELIST
# 相加合计声量：多活动“不相加”，出现这些措辞视为口径违规（P0）。
AGGREGATE_SUM_PATTERNS = ("合计声量", "总声量", "声量总和", "累计声量", "声量合计")
# 否定/免责标记：同一行出现时说明是“不相加”口径声明而非违规断言，跳过。
AGGREGATE_DISCLAIMER_MARKERS = ("不相加", "不合计", "逐个", "分别统计", "不得相加", "勿")
# 章节结构（FIX-42）：四章 + 品牌 1.1~1.4 + 逐活动 2.N.1~2.N.5 + 2.{N+1} + 3.1~3.3，
# 全部来自 policy.REPORT_*，本文件不再写死标题。
MIN_BODY_CHARS = 200  # 内容层过短（降级空壳）记 P1。
RAW_JSON_PATTERN = re.compile(r"[\{\[]\s*\"[^\"\n]+\"\s*:")
INTERNAL_FIELD_NAMES = (
    "_annot_id", "_dedup_key", "_source_analysis_object",
    "ai_sentiment", "interaction_cnt", "discussion_dimension", "情感(AI)",
    "kol_level", "Producer", "Validator", "scan_report.py",
)
PLACEHOLDER_PATTERNS = (
    "[待补充]", "TODO", "TBD", "<待补充>", "待填", "占位符",
    "（事实/机制/建议分开，不越过数据边界。）",
    "以下结论仅基于已确认统计产物与证据链。",
    "补齐证据后再制定可验证的行动计划。",
)
CAUSALITY_PATTERNS = (
    re.compile(r"活动(?:直接)?(?:带来|导致|驱动|促成).{0,24}(?:增长|提升|下降)"),
    re.compile(r"归功于本次活动"),
)
PHONE_PATTERN = re.compile(r"(?<![A-Za-z0-9])1[3-9]\d{9}(?![A-Za-z0-9])")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
SMALL_SAMPLE_DISCLOSURE_PATTERN = (
    r"(?:样本(?:量|数)?(?:不足|少于|低于)"
    r"|样本(?:量|数)?\s*(?:<|＜)\s*30)"
)


def _issue(iid: str, severity: str, title: str, evidence: str, locus: str = "",
           artifact: str = "report_md", fix_required: bool = True,
           suggested_fix: str = "") -> dict:
    """质检问题条目。

    FIX-34：在客户要求的 severity/artifact/location/issue/fix_required/suggested_fix
    之上保留原有 id/title/evidence/locus 字段，保证 HITL 卡片与 fix_report 的旧读取路径不变。
      - title 即客户契约里的 issue
      - locus 即客户契约里的 location
    """
    return {"id": iid, "severity": severity, "title": title,
            "evidence": evidence[:300], "locus": locus,
            "artifact": artifact, "fix_required": bool(fix_required),
            "suggested_fix": suggested_fix[:200]}


_EVIDENCE_BLOCK = re.compile(
    r"<!-- report-module:start -->.*?<!-- report-module:end -->", re.S)


def _split_evidence_lines(md: str) -> tuple[str, int]:
    """把代码生成的证据模块（热门表、代表原文）整块挖空，用于“作者措辞”类扫描。

    证据里的原文引用属于事实，不是作者用词，不能被禁用词规则误判。
    挖空时保持行数不变（整块替换为等量空行），使后续行号仍可对应原文件。
    """
    count = 0

    def _blank(match):
        nonlocal count
        count += 1
        return "\n" * match.group(0).count("\n")

    return _EVIDENCE_BLOCK.sub(_blank, md or ""), count


def _section(md, prefix):
    match = re.search(rf"(?m)^#{{2,4}}\s+{re.escape(prefix)}(?=[^\d.]|$)[^\n]*", md)
    if not match:
        return ""
    tail = md[match.end():]
    end = re.search(r"(?m)^#{2,4}\s+", tail)
    return tail[:end.start()] if end else tail


def _tables(md):
    lines = md.splitlines()
    result = []
    i = 0
    while i + 1 < len(lines):
        split = lambda line: [s.strip().replace(r"\|", "|") for s in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if "|" in lines[i] and re.fullmatch(r"[\s|:\-]+", lines[i + 1]) and "---" in lines[i + 1]:
            rows = [split(lines[i])]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split(lines[i]))
                i += 1
            result.append(rows)
        else:
            i += 1
    return result


UGC_QUOTE_CHECK = 40
OPINION_QUOTE_CHECK = 40


def _quote_head(row, limit):
    return re.sub(r"\s+", " ", str(row.get("content") or row.get("title") or "")).strip()[:limit]


def _module_checks(md, requirement, stats, charts):
    issues = []
    def fail(key, title, evidence, locus):
        issues.append(_issue("P0-module-" + key, "P0", title, evidence, locus))
    description = re.search(r"(?m)^#{2,4}\s+.*数据说明[^\n]*", md)
    intro = md[description.end():] if description else ""
    next_heading = re.search(r"(?m)^#{2,4}\s+", intro)
    intro = intro[:next_heading.start()] if next_heading else intro
    if requirement:
        # FIX-43：KS §零 定稿版字段（数据平台/时间周期/分析对象/关键词/过滤词）。
        for term in ("数据平台", "时间周期", "分析对象", "关键词", "过滤词"):
            if term not in intro:
                fail("description-" + term, "数据说明缺必要字段", term, "数据说明")
        for term in policy.DESCRIPTION_FORBIDDEN_TERMS:
            if term in intro:
                issues.append(_issue(f"P2-description-{term}", "P2",
                                     "数据说明区出现方法论/版本/口径说明", term, "数据说明",
                                     fix_required=False))
        for key in ("brand_query", "activity_query", "product_query", "brand_filter", "activity_filter"):
            value = str(requirement.get(key) or "")
            if value and value.replace("|", r"\|") not in intro and value not in intro:
                fail("query-" + key, "数据说明未披露实际查询配置", key, "数据说明")
    chart_ids = {item.get("chart_id") for item in charts.get("charts") or []}
    objects = [("brand", stats.get("brand") or {}, "1.4")]
    objects += [(cid, bundle, f"2.{index}.5") for index, (cid, bundle) in enumerate((stats.get("campaigns") or {}).items(), 1)]
    for cid, bundle, hot_prefix in objects:
        trend = bundle.get("trend") or {}
        days = set()
        for key, value in trend.items():
            days.update(value.keys() if isinstance(value, dict) else [key])
        if len(days) >= 2 and cid + "_trend" not in chart_ids:
            fail("trend-" + cid, "缺应生成的趋势图", cid, "chart_urls")
        clouds = bundle.get("wordcloud") or {} if cid == "brand" else {}
        for platform, words in clouds.items():
            if words and f"wordcloud_brand_overall_{platform}" not in chart_ids:
                fail("cloud-brand-" + platform, "缺品牌大盘词云", platform, "1.3")
        hot = _section(md, hot_prefix)
        tables = _tables(hot)
        for kind, fields in (("pgc", list(policy.HOT_PGC_COLUMNS)),
                             ("ugc", list(policy.HOT_UGC_COLUMNS))):
            rows = (bundle.get("top_content") or {}).get(kind) or []
            if not rows:
                continue
            matching = [t for t in tables if t[0] == fields]
            if not matching:
                fail(f"hot-fields-{cid}-{kind}", "热门内容表字段缺失", "/".join(fields), hot_prefix)
            elif len(matching[0]) - 1 != min(10, len(rows)):
                fail(f"hot-count-{cid}-{kind}", "热门榜单行数不符合可用Top10", str(len(rows)), hot_prefix)
        if cid != "brand":
            index = hot_prefix.split(".")[1]
            opinion = _section(md, f"2.{index}.3")
            for direction, name in (("positive", "正面"), ("negative", "负面")):
                analysis = (bundle.get("opinion_analysis") or {}).get(direction) or {}
                sample = (bundle.get("sentiment_dist") or {}).get(direction, 0)
                if sample >= 10 and not analysis.get("top5"):
                    fail(f"opinion-data-{cid}-{direction}", "活动统计缺观点与原文证据", str(sample), "stats_bundle")
                if sample > 0 and f"{name}观点 TOP5" not in opinion:
                    fail(f"opinion-body-{cid}-{direction}", "活动口碑缺观点分析", name, f"2.{index}.3")
                for topic in analysis.get("top5") or []:
                    if topic["label"] not in opinion or not all(
                        _quote_head(row, OPINION_QUOTE_CHECK) in re.sub(r"\s+", " ", opinion)
                        for row in topic.get("representatives") or []
                    ):
                        fail(f"opinion-evidence-{cid}-{direction}-{topic['label']}", "观点未展示代表原文", topic["label"], f"2.{index}.3")
            ugc = _section(md, f"2.{index}.4")
            for category in (bundle.get("ugc_analysis") or {}).get("categories") or []:
                # UGC 代表原文正文只保留 40-60 字（KS 2.N.4②），按前 40 字（去空白）核对。
                if category["label"] not in ugc or not all(_quote_head(row, UGC_QUOTE_CHECK) in re.sub(r"\s+", " ", ugc)
                                                         for row in category.get("representatives") or []):
                    fail(f"ugc-evidence-{cid}-{category['label']}", "UGC类别缺代表内容", category["label"], f"2.{index}.4")
    for table in _tables(md):
        if any(len(row) != len(table[0]) for row in table):
            fail("table-columns", "Markdown表格列数错位", "正文竖线须转义", "tables")
    return issues


def _category_labels(bundle: dict) -> list[tuple[str, str]]:
    """(类别种类, 类别名)：观点 TOP5 与 UGC 参与方式。

    观点一帖多标签时 label 可能是「A,B」，按英文逗号拆开逐个判断。
    兜底类「其他」由标注环节统一填写，不属于发现出的类别名，跳过。
    """
    found: list[tuple[str, str]] = []
    for direction in ("positive", "negative"):
        analysis = ((bundle or {}).get("opinion_analysis") or {}).get(direction) or {}
        for topic in analysis.get("top5") or []:
            for name in str(topic.get("label") or "").split(","):
                found.append(("观点", name.strip()))
    for category in ((bundle or {}).get("ugc_analysis") or {}).get("categories") or []:
        found.append(("UGC 参与方式", str(category.get("label") or "").strip()))
    for name in ((bundle or {}).get("ugc_category_dist") or {}):
        found.append(("UGC 参与方式", str(name).strip()))
    return [(kind, name) for kind, name in found
            if name and name not in ("N/A", "其他")]


def _category_name_issues(campaigns: dict) -> list[dict]:
    """检查清单 3.9：类别名称带情感/观点倾向 → P0。"""
    issues: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for cid, bundle in (campaigns or {}).items():
        for kind, name in _category_labels(bundle):
            hits = policy.tendency_hits(name)
            if not hits or (kind, name) in seen:
                continue
            seen.add((kind, name))
            issues.append(_issue(
                f"P0-category-tendency-{kind}-{name}", "P0",
                "类别名称带情感/观点倾向（3.9）",
                f"{cid}: {kind}类别「{name}」命中 {hits}；观点应为讨论话题，"
                "参与方式应为动作或内容形式，情感由独立字段承载",
                locus="stats_bundle",
            ))
    return issues


# 检查清单 3.10 类别 MECE（P1）的确定性部分；包含/交叉等语义判断交 Agent 段。
_PAIR_SUFFIXES = (("好", "差"), ("高", "低"), ("多", "少"), ("快", "慢"))
_DIMENSION_WORDS = {"平台": ("小红书", "抖音", "微博", "B站", "快手", "知乎", "视频号"),
                    "人群": ("学生", "宝妈", "白领", "男生", "女生", "粉丝群体")}


def _category_mece_issues(campaigns: dict) -> list[dict]:
    issues: list[dict] = []
    for cid, bundle in (campaigns or {}).items():
        by_kind: dict[str, list[str]] = {}
        for kind, name in _category_labels(bundle):
            by_kind.setdefault(kind, [])
            if name not in by_kind[kind]:
                by_kind[kind].append(name)
        for kind, names in by_kind.items():
            for good, bad in _PAIR_SUFFIXES:
                stems = {n[:-1] for n in names if n.endswith(good)} & {n[:-1] for n in names if n.endswith(bad)}
                for stem in sorted(stems):
                    issues.append(_issue(f"P1-category-mece-pair-{cid}-{stem}", "P1", "类别 MECE：成对倾向类别（3.10）",
                                         f"{cid}: {kind}类别「{stem}{good}」与「{stem}{bad}」把情感拆成两类",
                                         locus="stats_bundle", fix_required=False))
            for dimension, words in _DIMENSION_WORDS.items():
                hits = [n for n in names if any(w in n for w in words)]
                if hits:
                    issues.append(_issue(f"P1-category-mece-dim-{cid}-{kind}-{dimension}", "P1",
                                         "类别 MECE：混入其他维度（3.10）",
                                         f"{cid}: {kind}类别 {hits} 混入{dimension}维度",
                                         locus="stats_bundle", fix_required=False))
    return issues


def _heading_present(md: str, number: str, title: str) -> bool:
    """标题行（# 或加粗编号）同时含编号与标题；容忍编号与标题间少量字符。"""
    pattern = rf"(?m)^(?:#{{1,6}}\s+|\*\*){re.escape(number)}(?![\d.]).{{0,16}}{re.escape(title)}"
    return bool(re.search(pattern, md))


def _structure_issues(md: str, campaign_ids: list[str]) -> list[dict]:
    """检查清单 1.5 / 5.7 / 5.8 / 5.12：固定四章结构（KS_Report_Template §一）。"""
    issues: list[dict] = []
    for chapter in policy.REPORT_CHAPTERS:
        if not re.search(rf"(?m)^#{{1,3}}\s+{re.escape(chapter)}", md):
            issues.append(_issue(f"P0-missing-chapter-{chapter}", "P0",
                                 f"缺固定章节「{chapter}」（5.7）",
                                 "报告固定四章：一品牌/二活动/三经验总结/四总结与建议",
                                 locus="structure"))
    for retired in policy.RETIRED_SECTION_TITLES:
        if retired in md:
            issues.append(_issue(f"P0-retired-section-{retired}", "P0",
                                 f"使用已废弃的章节「{retired}」（1.5/5.7）",
                                 "提及漏斗节标题为「提及漏斗分析」；拉升效果并入二章末 2.{N+1}",
                                 locus="structure"))
    for number, title in policy.BRAND_SECTIONS:
        if not _heading_present(md, number, title):
            issues.append(_issue(f"P0-missing-brand-section-{number}{title}", "P0",
                                 f"缺品牌子章节「{number} {title}」（5.7）",
                                 "品牌章固定 1.1~1.4", locus="structure"))
    if not campaign_ids:
        campaign_ids = [m.group(2).strip() for m in re.finditer(
            r"(?m)^#{2,5}\s+2\.(\d+)\s+(?:活动\s+)?(.+?)\s*$", md)
            if policy.LIFT_SECTION_TITLE not in m.group(2)]
    if not campaign_ids:
        issues.append(_issue("P0-missing-campaign-sections", "P0", "缺逐活动 2.N 分节（5.7）",
                             "未检出任何「2.N <活动名>」标题", locus="structure"))
    for index, campaign_id in enumerate(campaign_ids, 1):
        if campaign_id and campaign_id not in md:
            issues.append(_issue(f"P0-missing-campaign-{index}", "P0",
                                 f"缺活动分节「{campaign_id}」（5.7）",
                                 f"正文未检出活动名称「{campaign_id}」", locus="structure"))
        for suffix, title in policy.CAMPAIGN_SECTIONS:
            number = f"2.{index}.{suffix}"
            if not _heading_present(md, number, title):
                issues.append(_issue(f"P0-missing-campaign-section-{index}-{suffix}", "P0",
                                     f"缺活动子章节「{number} {title}」（5.7）",
                                     f"活动「{campaign_id}」缺固定结构", locus="structure"))
    lift = policy.lift_prefix(len(campaign_ids))
    if not _heading_present(md, lift, policy.LIFT_SECTION_TITLE):
        issues.append(_issue("P0-missing-lift-section", "P0",
                             f"缺「{lift} {policy.LIFT_SECTION_TITLE}」（5.8）",
                             "二章最后一节，编号 = 活动个数 + 1；基准不可用也须保留并披露",
                             locus=lift))
    for number, title in policy.SUMMARY_SECTIONS:
        if not _heading_present(md, number, title):
            issues.append(_issue(f"P0-missing-summary-{number}", "P0",
                                 f"经验总结缺少「{number} {title}」（5.7）",
                                 "三章固定 3.1 成功经验 / 3.2 待改进问题 / 3.3 后续行动计划",
                                 locus=number))
    issues.extend(_experience_issues(_chapter_body(md, policy.REPORT_CHAPTERS[2])))
    issues.extend(_callout_issues(md))
    final = _chapter_body(md, policy.REPORT_CHAPTERS[3])
    if "下次活动建议" in final:
        issues.append(_issue("P2-summary-duplicates-action-plan", "P2",
                             "「四、总结与建议」仍设下次活动建议（5.12）",
                             "行动建议统一写在 3.3，四章只保留核心成果表与待改善方向",
                             locus="四"))
    return issues


_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def _callout_blocks(md: str, mark: str) -> list[tuple[int, str]]:
    """(行号, 正文)；正文为标记行之后连续的 `>` 行。"""
    lines, blocks = md.splitlines(), []
    for index, line in enumerate(lines):
        if line.strip().startswith(mark):
            body = [line.strip()[len(mark):].strip()]
            for follow in lines[index + 1:]:
                if not follow.startswith(">"):
                    break
                body.append(follow.lstrip("> ").strip())
            blocks.append((index + 1, "".join(body)))
    return blocks


def _callout_style_issues(text: str, key: str, severity: str) -> list[dict]:
    issues = []
    if re.match(r"\s*[\d+\-]", text):
        issues.append(_issue(f"{severity}-callout-number-first-{key}", severity,
                             "Callout 以数字开头", text[:60], key, fix_required=False))
    for group, label in ((policy.CALLOUT_ADVICE_TERMS, "建议"), (policy.CALLOUT_DEGREE_TERMS, "程度副词"),
                         (policy.CALLOUT_METAPHOR_TERMS, "比喻"), (policy.CALLOUT_CAUSAL_TERMS, "因果推断")):
        hits = [term for term in group if term in text]
        if hits:
            issues.append(_issue(f"{severity}-callout-{label}-{key}", severity,
                                 f"Callout 含{label}", "、".join(hits), key, fix_required=False))
    return issues


def _callout_issues(md: str) -> list[dict]:
    """检查清单 5.5 / 5.14 / 5.15。只检查显式标记的 Callout。"""
    issues: list[dict] = []
    insights = _callout_blocks(md, policy.INSIGHT_MARK)
    if insights:
        line_no, text = insights[0]
        first_chapter = re.search(rf"(?m)^#{{1,2}}\s+{re.escape(policy.REPORT_CHAPTERS[0])}", md)
        if len(insights) > 1 or (first_chapter and md.find(policy.INSIGHT_MARK) > first_chapter.start()):
            issues.append(_issue("P0-insight-callout-position", "P0", "整体洞察 Callout 不在最前或不唯一（5.5）",
                                 f"L{line_no}", "整体洞察"))
        if len(_sentences(text)) != 3:
            issues.append(_issue("P1-insight-callout-sentences", "P1",
                                 "整体洞察不是固定三句（5.14）",
                                 f"{len(_sentences(text))} 句", "整体洞察", fix_required=False))
        issues += _callout_style_issues(text, "insight", "P1")
    for line_no, text in _callout_blocks(md, policy.TIP_MARK):
        key = f"L{line_no}"
        if len(_sentences(text)) > 1:
            issues.append(_issue(f"P2-tip-callout-sentences-{key}", "P2",
                                 "各节 Callout 超过一句（5.15）", text[:60], key, fix_required=False))
        issues += _callout_style_issues(text, key, "P2")
    for chapter in policy.REPORT_CHAPTERS[2:]:
        if policy.TIP_MARK in _chapter_body(md, chapter) or policy.INSIGHT_MARK in _chapter_body(md, chapter):
            issues.append(_issue(f"P2-summary-callout-{chapter}", "P2", "三、四章出现 callout（5.15）",
                                 chapter, chapter, fix_required=False))
    return issues


# 经验总结质量红线（KS_Content_Analysis_Prompt 经验总结合成 Prompt）。
VAGUE_ACTION_PATTERN = re.compile(r"持续优化|持续加强|进一步加强|加强\S{0,6}(?:建设|管理|力度)")
SOURCE_REF_PATTERN = re.compile(r"\d\.\d|\d\.\{?N")


def _experience_issues(body: str) -> list[dict]:
    """三章：经验/问题缺来源章节 → P1；行动计划空泛表述 → P2。"""
    issues: list[dict] = []
    for match in re.finditer(r"(?ms)^\*\*((?:经验|问题)\s*\d+[^*]*)\*\*(.*?)(?=^\*\*|^#|\Z)", body):
        if not SOURCE_REF_PATTERN.search(match.group(2)):
            issues.append(_issue(f"P1-experience-source-{match.group(1)[:6]}", "P1",
                                 "经验/问题未标注来源章节和数值", match.group(1).strip(), "三",
                                 fix_required=False))
    for line in body.splitlines():
        hit = VAGUE_ACTION_PATTERN.search(line)
        if hit:
            issues.append(_issue(f"P2-vague-action-{hit.group(0)}", "P2",
                                 "行动计划使用空泛表述", line.strip()[:80], "三",
                                 fix_required=False))
    return issues


def _chapter_body(md: str, chapter: str) -> str:
    match = re.search(rf"(?m)^#{{1,3}}\s+{re.escape(chapter)}[^\n]*", md)
    if not match:
        return ""
    tail = md[match.end():]
    end = re.search(r"(?m)^#{1,2}\s+[一二三四五六]、", tail)
    return tail[:end.start()] if end else tail


def _consistency_issues(stats: dict) -> list[dict]:
    """检查清单 2.11 同一活动各表总数一致；2.12 讨论对象分组计数加总回全量。

    数据字段缺失时不判（FIX-34：数据未就绪不得误判 P0）。
    """
    issues: list[dict] = []
    for index, (cid, bundle) in enumerate(((stats or {}).get("campaigns") or {}).items(), 1):
        total = ((bundle or {}).get("metrics") or {}).get("活动相关条数")
        if not isinstance(total, int):
            continue
        sums = {}
        sentiment = (bundle.get("sentiment_dist") or {})
        if sentiment:
            sums["情感分布合计"] = sum(v for v in sentiment.values() if isinstance(v, int))
        platform = bundle.get("platform_dist") or {}
        if platform:
            sums["平台分布合计"] = sum(v for v in platform.values() if isinstance(v, int))
        content = bundle.get("content_type_dist") or {}
        if content:
            sums["内容类型合计"] = sum(v for v in content.values() if isinstance(v, int))
        for name, value in sums.items():
            if name == "情感分布合计":
                missing = int(bundle.get("sentiment_missing") or 0)
                value += missing
            if value != total:
                issues.append(_issue(f"P0-total-mismatch-{index}-{name}", "P0",
                                     f"同一活动总数不一致（2.11）：{name} {value} ≠ 活动相关 {total}",
                                     cid, f"2.{index}", artifact="stats_bundle",
                                     suggested_fix="核对聚合口径，所有分布须以活动相关内容为分母"))
        groups = bundle.get("dimension_counts")
        if isinstance(groups, dict) and groups:
            grouped = sum(v for v in groups.values() if isinstance(v, int))
            if grouped != total:
                issues.append(_issue(f"P0-dimension-sum-{index}", "P0",
                                     f"讨论对象分组计数加总 {grouped} ≠ 活动相关 {total}（2.12）",
                                     cid, f"2.{index}.3", artifact="stats_bundle",
                                     suggested_fix="三组讨论对象须互斥且覆盖全部活动相关内容"))
    return issues


def _content_length_issues(md: str) -> list[dict]:
    """检查清单 5.13：热门内容表「内容」列不超过 50 字（含省略号）。"""
    issues: list[dict] = []
    for table in _tables(md):
        header = table[0]
        if "内容" not in header or "总互动量" not in header:
            continue
        col = header.index("内容")
        for row in table[1:]:
            if col < len(row) and len(row[col].strip().rstrip("…")) > policy.HOT_EXCERPT_CHARS:
                issues.append(_issue("P1-hot-content-length", "P1", "热门内容列超过 50 字（5.13）",
                                     row[col][:60], "热门内容", fix_required=False))
                break
    return issues


def _ugc_redundant_table_issues(md: str) -> list[dict]:
    """检查清单 5.16（原清单重复编号的 5.12）：② UGC 参与方式分布饼图后不得再跟同数据表。"""
    match = re.search(r"(?m)^.*② *UGC 参与方式分布.*$", md)
    if not match:
        return []
    following = re.search(r"(?m)^(?:#{1,6}\s|\*\*③)", md[match.end():])
    block = md[match.end(): match.end() + following.start()] if following else md[match.end():]
    if re.search(r"(?m)^\|.*(占比|条数).*\|\s*$", block):
        return [_issue("P1-ugc-redundant-table", "P1", "UGC 参与方式饼图后有冗余数据表（5.16）",
                       "② UGC 参与方式分布 小节内出现数据表；饼图已含全部类别与占比",
                       "2.N.4②", fix_required=False)]
    return []


def _funnel_issues(md: str, stats: dict, has_product_query: bool) -> list[dict]:
    """检查清单 4.3 三级漏斗逻辑（FIX-18）。按 stats 的漏斗方法/退化状态确定性判定。

    - main_discussion：三组互斥，写成递进漏斗（出现 Tier 层级）→ P0；
    - 退化（Tier2/Tier1 ≥95%）却仍呈现 Tier2 / 品牌提及率 → P1；
    - 未退化且有 Tier3：缺「品牌内容中的产品提及占比 …（Tier3 N / Tier2 M）」分母写法 → P1；
    - 漏斗记录了产品词、未退化却缺产品层 → P0（以统计实际使用的产品词为准）。
    无漏斗统计（旧产物）时回退到全文检查「有产品词但缺递进漏斗」。
    """
    issues: list[dict] = []
    campaigns = (stats or {}).get("campaigns") or {}
    funnels = [(index, cid, (bundle or {}).get("mention_funnel"))
               for index, (cid, bundle) in enumerate(campaigns.items(), 1)]
    funnels = [(i, cid, f) for i, cid, f in funnels if isinstance(f, dict) and f.get("method")]
    if not funnels:
        if has_product_query and not re.search(
                r"三级.{0,8}漏斗|Tier\s*1.{0,80}Tier\s*2.{0,80}Tier\s*3", md, re.S):
            issues.append(_issue("P0-product-funnel", "P0", "有产品关键词但缺递进提及漏斗",
                                 "要求至少呈现活动相关→品牌提及→产品提及的递进关系",
                                 locus="2.N.2"))
        return issues
    for index, cid, funnel in funnels:
        body = _section(md, f"2.{index}.2")
        locus = f"2.{index}.2"
        if funnel["method"] == "main_discussion":
            if re.search(r"Tier\s*[23]", body):
                issues.append(_issue(f"P0-funnel-main-discussion-{cid}", "P0",
                                     "主讨论对象方法写成递进漏斗（4.3）",
                                     f"{cid}: 三个主对象组互斥，不构成 Tier 递进", locus=locus))
            continue
        if funnel.get("degraded"):
            if re.search(r"Tier\s*2|品牌提及率", body):
                issues.append(_issue(f"P1-funnel-degraded-{cid}", "P1",
                                     "品牌提及层已退化仍作为主指标呈现（4.3）",
                                     f"{cid}: Tier2/Tier1={funnel.get('tier2_ratio')} ≥ 95%",
                                     locus=locus))
            continue
        if funnel.get("tier3") is not None:
            if not re.search(r"品牌内容中的产品提及占比.{0,20}（Tier3\s*\d+\s*/\s*Tier2\s*\d+）", body):
                issues.append(_issue(f"P1-funnel-denominator-{cid}", "P1",
                                     "漏斗表产品提及占比未写明分母（4.3）",
                                     "应写「品牌内容中的产品提及占比 X%（Tier3 N / Tier2 M）」"
                                     "并另列占活动总量比例", locus=locus))
        elif funnel.get("product_keywords") and funnel.get("tier2") is not None:
            issues.append(_issue(f"P0-funnel-missing-product-{cid}", "P0",
                                 "有产品词、未退化却缺产品层（4.3）",
                                 f"{cid}: 漏斗统计缺 Tier3", locus=locus))
    return issues


def scan_markdown(
    markdown: str, *, degraded: bool = False,
    expected_campaigns: Optional[list[str]] = None,
    requirement: Optional[dict] = None,
    stats: Optional[dict] = None,
    charts: Optional[dict] = None,
) -> dict:
    """全量扫描内容层 MD，产出分级 issues 与 qc_report。"""
    issues: list[dict] = []
    md = markdown or ""
    lines = md.splitlines()

    # 1) 禁用措辞（P0）——逐行定位，给行号证据。
    #    FIX-20： unconditionally 禁用的词用精确词表；语境相关的（性价比/渗透率）用正则，
    #    避免把产品讨论里的“性价比”误判为指标越界。
    #    代码生成的证据模块（hot tables / 观点代表原文）里的原文引用不属作者措辞，先剔除再扫。
    scan_md, evidence_lines = _split_evidence_lines(md)
    for ln_no, line in enumerate(scan_md.splitlines(), 1):
        for term in FORBIDDEN_TERMS:
            if term in line:
                issues.append(_issue(
                    f"P0-forbidden-{term}", "P0", f"出现禁用措辞「{term}」",
                    f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}"))
        for pattern in policy.FORBIDDEN_METRIC_PATTERNS:
            if pattern.search(line):
                issues.append(_issue(
                    f"P0-forbidden-metric-{ln_no}", "P0", "指标名称或解释越界（3.8）",
                    f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}"))

    # 1b) NSR 分母口径写错（P0）——客户公式分母为正+负，不含中性（FIX-13 / 检查清单 2.4）。
    for ln_no, line in enumerate(scan_md.splitlines(), 1):
        if any(p.search(line) for p in policy.NSR_WRONG_DENOMINATOR_PATTERNS):
            issues.append(_issue(
                f"P0-nsr-denominator-{ln_no}", "P0",
                "NSR 分母口径错误（应为正面+负面，不含中性）",
                f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}"))

    # 1c) 声量贡献参考越界（P0）——分母<分子时不输出，不得出现 >100%（FIX-19 / LM #46）。
    for ln_no, line in enumerate(scan_md.splitlines(), 1):
        if "声量贡献参考" not in line:
            continue
        for pct in re.findall(r"(\d+(?:\.\d+)?)\s*%", line):
            if float(pct) > 100.0:
                issues.append(_issue(
                    f"P0-voice-share-overflow-{ln_no}", "P0",
                    "声量贡献参考超过 100%（分母小于分子时不应输出该指标）",
                    f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}"))
                break

    # 2) 多活动相加/合计声量（P0）——同行含“不相加/逐个”等免责标记则视为口径声明，跳过。
    single_campaign = len((stats or {}).get("campaigns") or {}) == 1 or len(expected_campaigns or []) == 1
    for ln_no, line in enumerate(lines, 1):
        if any(mk in line for mk in AGGREGATE_DISCLAIMER_MARKERS):
            continue
        mixed_sum = re.search(
            r"多活动|跨活动|各活动.*(?:合计|相加|总计)|"
            r"(?:品牌.{0,15}活动|活动.{0,15}品牌).{0,15}(?:相加|合计|总计|加总|\+)",
            line,
        )
        if single_campaign and not mixed_sum:
            continue
        for pat in AGGREGATE_SUM_PATTERNS:
            if pat in line:
                issues.append(_issue(
                    f"P0-aggregate-{pat}", "P0", f"疑似多活动相加口径「{pat}」",
                    f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}"))

    # 3) 内部 JSON / 标注字段泄漏（P0）。
    for ln_no, line in enumerate(lines, 1):
        leaked_fields = [name for name in INTERNAL_FIELD_NAMES if name in line]
        if RAW_JSON_PATTERN.search(line) or leaked_fields:
            evidence = (
                f"内部字段: {', '.join(leaked_fields)}；" if leaked_fields else ""
            )
            issues.append(_issue(
                f"P0-raw-json-{ln_no}", "P0",
                "报告正文泄漏内部 JSON 或标注字段",
                f"{evidence}L{ln_no}: {line.strip()}",
                locus=f"L{ln_no}",
            ))

    # 4) 占位符、错误因果与明显隐私泄漏（P0）。
    for ln_no, line in enumerate(lines, 1):
        placeholders = [value for value in PLACEHOLDER_PATTERNS if value in line]
        if placeholders:
            issues.append(_issue(
                f"P0-placeholder-{ln_no}", "P0", "报告残留占位符",
                f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}",
            ))
        if any(pattern.search(line) for pattern in CAUSALITY_PATTERNS):
            issues.append(_issue(
                f"P0-causality-{ln_no}", "P0", "活动效果使用无依据因果表述",
                f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}",
            ))
        if PHONE_PATTERN.search(line):
            issues.append(_issue(
                f"P0-privacy-phone-{ln_no}", "P0", "报告泄漏手机号",
                f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}",
            ))
        if re.search(r"\bUGC\b.*(?:作者|账号|昵称)[：:]", line):
            issues.append(_issue(
                f"P0-privacy-ugc-{ln_no}", "P0", "UGC 个人账号未匿名",
                f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}",
            ))
        if re.search(r"确定性模板生成|结构[：:]\s*(?:four|five)|报告（v\d", line):
            issues.append(_issue(
                f"P0-internal-template-{ln_no}", "P0", "报告暴露内部模板或版本",
                f"L{ln_no}: {line.strip()}", locus=f"L{ln_no}",
            ))

    # 5) 报告为空 / 缺核心章节（P0）。
    body = md.strip()
    if not body:
        issues.append(_issue("P0-empty", "P0", "报告内容为空", "markdown 为空字符串"))
    else:
        issues.extend(_structure_issues(md, list(expected_campaigns or [])))

    # 6) 需求一致性与当前项目适配规则。
    requirement = requirement or {}
    stats = stats or {}
    charts = charts or {}
    issues.extend(_module_checks(md, requirement, stats, charts))
    content_scope = str(requirement.get("content_scope") or "all").lower()
    if content_scope != "all" and re.search(r"PGC\s*[/与和对比]+\s*UGC|PGC/UGC", md):
        issues.append(_issue(
            "P0-scope-pgc-ugc", "P0", "报告超出 content_scope 输出 PGC/UGC 对比",
            f"content_scope={content_scope}", locus="requirement",
        ))
    issues.extend(_funnel_issues(md, stats, bool(requirement.get("product_query"))))
    issues.extend(_consistency_issues(stats))
    issues.extend(_content_length_issues(md))
    issues.extend(_ugc_redundant_table_issues(md))
    if re.search(r"竞品\s*(?:SOV|分析|声量)", md, re.I):
        issues.append(_issue(
            "P0-competitor-retired", "P0", "报告包含已下线的竞品分析",
            "当前 Topic5 权威口径已下线 competitor_sov", locus="竞品",
        ))
    if "整体洞察" in md and "品牌传播" in md:
        if md.find("整体洞察") > md.find("品牌传播"):
            issues.append(_issue(
                "P0-overall-insight-position", "P0", "整体洞察位置错误",
                "整体洞察必须位于标题之后、第一章之前", locus="structure",
            ))

    # 7) 统计产物可确定验证的口径。
    campaigns = stats.get("campaigns") if isinstance(stats, dict) else {}

    # 7a) 检查清单 3.9（P0）：观点 / UGC 参与方式的类别名带情感或评价倾向。
    #     只判断类别名本身；代表原文里的用户措辞不在此列（清单原文）。
    issues.extend(_category_name_issues(campaigns))
    issues.extend(_category_mece_issues(campaigns))
    for cid, bundle in (campaigns or {}).items():
        metrics = (bundle or {}).get("metrics") or {}
        product_share = metrics.get("产品提及占比")
        if isinstance(product_share, (int, float)) and not 0 <= product_share <= 1:
            issues.append(_issue(
                f"P0-funnel-range-{cid}", "P0", "产品提及占比超出 0~100%",
                f"{cid}: 产品提及占比={product_share}", locus="stats_bundle",
            ))
        for dimension, values in ((bundle or {}).get("dimension_nsr") or {}).items():
            if not isinstance(values, dict):
                continue
            sample = values.get("sample_size", values.get("sample"))
            dimension_text = re.escape(str(dimension))
            presented = bool(re.search(
                rf"{dimension_text}(?:维度)?[\s*：:，,（）()=为的]*(?:NSR|净情绪指数)"
                rf"|(?:NSR|净情绪指数)[\s*：:，,（）()=为的]*{dimension_text}",
                md, re.I,
            )) or any(
                any("NSR" in header.upper() or "净情绪指数" in header for header in table[0])
                and any(str(dimension) in [cell.strip("* ") for cell in row] for row in table[1:])
                for table in _tables(md)
            )
            if isinstance(sample, int) and sample < 30 and presented:
                dimension_text = re.escape(str(dimension))
                disclosed = re.search(
                    (
                        rf"{dimension_text}.{{0,120}}{SMALL_SAMPLE_DISCLOSURE_PATTERN}"
                        rf"|{SMALL_SAMPLE_DISCLOSURE_PATTERN}.{{0,120}}{dimension_text}"
                    ),
                    md,
                    re.S,
                )
                if not disclosed:
                    issues.append(_issue(
                        f"P0-small-sample-{cid}-{dimension}", "P0",
                        "分维 NSR 小样本未披露",
                        f"{cid}/{dimension}: sample={sample}<30",
                        locus="dimension_nsr",
                    ))

    # 8) 图表 Artifact 完整性；内容层允许受控本地 PNG，发布后再检查 media。
    chart_items = charts.get("charts") if isinstance(charts, dict) else []
    known_paths = {str(item.get("local_path") or item.get("url") or "") for item in chart_items or []}
    cover = charts.get("cover") or {}
    if isinstance(cover, dict):
        known_paths.add(str(cover.get("local_path") or ""))
    for path in IMAGE_PATTERN.findall(md):
        if path.startswith(("/", "./", "../")) and (not Path(path).is_file() or (charts and path not in known_paths)):
            issues.append(_issue("P0-image-invalid", "P0", "报告引用不存在或未登记的本地图片", path, "report_md"))
    for item in chart_items or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("local_path") or item.get("url") or "")
        if not source:
            issues.append(_issue(
                f"P0-chart-missing-{item.get('chart_id', 'unknown')}", "P0",
                "启用图表缺少可交付文件",
                str(item.get("title") or item.get("chart_id") or "unknown"),
                locus="chart_urls",
            ))
        elif item.get("local_path") and not Path(source).is_file():
            issues.append(_issue(
                f"P0-chart-file-{item.get('chart_id', 'unknown')}", "P0",
                "图表本地文件不存在",
                source, locus="chart_urls",
            ))
        if source and source not in IMAGE_PATTERN.findall(md):
            issues.append(_issue(
                f"P0-chart-unreferenced-{item.get('chart_id', 'unknown')}", "P0",
                "已启用图表未进入报告",
                str(item.get("title") or item.get("chart_id") or "unknown"),
                locus="report_md",
            ))

    # 9) 内容过短（P1）。
    if body and len(body) < MIN_BODY_CHARS:
        issues.append(_issue(
            "P1-too-short", "P1", "内容层偏短（疑似降级空壳）",
            f"正文仅 {len(body)} 字，低于 {MIN_BODY_CHARS}", locus="length"))

    # 10) 图表引用缺失（P1）——五章报告通常应含图/封面。
    if body and "![" not in md and "http" not in md:
        issues.append(_issue(
            "P1-no-chart", "P1", "报告未引用任何图表/封面链接",
            "正文中未检出 ![](...) 或图表 URL", locus="charts"))

    # 11) 报告降级禁发；不得静默以整篇简化模板代替业务报告。
    if degraded:
        issues.append(_issue(
            "P0-degraded-source", "P0", "报告来源含降级产物",
            "report_md.degraded=True 或上游降级", locus="provenance"))

    p0 = [i for i in issues if i["severity"] == "P0"]
    p1 = [i for i in issues if i["severity"] == "P1"]
    p2 = [i for i in issues if i["severity"] == "P2"]
    return {
        "has_p0": bool(p0),
        "p0_count": len(p0),
        "p1_count": len(p1),
        "p2_count": len(p2),
        "issues": issues,
        "metric_whitelist": list(METRIC_WHITELIST),
        "forbidden_terms": list(FORBIDDEN_TERMS),
        "scan_mode": "full_scan",
        "degraded": bool(degraded),
        "rules_version": policy.RULES_VERSION,
    }


def _load_markdown(args) -> tuple[str, bool]:
    if args.markdown_file:
        return Path(args.markdown_file).read_text(encoding="utf-8"), False
    data = json.loads(Path(args.report_md).read_text(encoding="utf-8"))
    md = data.get("markdown") if isinstance(data, dict) else str(data)
    return md or "", bool(isinstance(data, dict) and (data.get("degraded") or data.get("source") == "deterministic_guardrail"))


def _input_ref(path_value: str, key: str) -> dict:
    path = Path(path_value)
    raw = path.read_bytes()
    version = None
    if path.name == "payload.json" and path.parent.name.startswith("v"):
        try:
            version = int(path.parent.name[1:])
        except ValueError:
            version = None
    return {
        "artifact_id": f"{key}@v{version}" if version is not None else key,
        "key": key,
        "version": version,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-md", help="report_md artifact payload.json 路径")
    ap.add_argument("--markdown-file", help="纯 .md 文件路径（二选一）")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--execution-source",
        default=os.environ.get("MA_SESSION_ID", ""),
        help="独立 Validator 子 Agent 的平台 Session/Execution ID（必填）",
    )
    ap.add_argument("--requirement", required=True)
    ap.add_argument("--stats", required=True)
    ap.add_argument("--charts", required=True)
    ap.add_argument("--rules", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--attempt-id", required=True)
    ap.add_argument(
        "--validator-version",
        default=os.environ.get("TOPIC5_VALIDATOR_VERSION", "topic5-validator@1.6"),
    )
    args = ap.parse_args()
    if not (args.report_md or args.markdown_file):
        ap.error("需提供 --report-md 或 --markdown-file")
    if not args.execution_source:
        ap.error("需提供 --execution-source 绑定独立 Validator 执行来源")
    md, degraded = _load_markdown(args)
    requirement_payload = json.loads(Path(args.requirement).read_text(encoding="utf-8"))
    stats_payload = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    charts_payload = json.loads(Path(args.charts).read_text(encoding="utf-8"))
    expected_campaigns = list((stats_payload.get("campaigns") or {}).keys())
    qc = scan_markdown(
        md,
        degraded=degraded,
        expected_campaigns=expected_campaigns,
        requirement=requirement_payload,
        stats=stats_payload,
        charts=charts_payload,
    )
    subject = None
    if args.report_md:
        report_path = Path(args.report_md)
        try:
            version = int(report_path.parent.name.lstrip("v"))
            subject = {
                "artifact_id": f"report_md@v{version}",
                "key": "report_md",
                "version": version,
                "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }
        except (TypeError, ValueError):
            ap.error("--report-md 路径必须位于 artifacts/report_md/vN/payload.json")
    qc.update({
        "validator": "topic5-validator",
        "validation_id": f"validation_{uuid.uuid4().hex}",
        "subject": subject,
        "rules_version": policy.RULES_VERSION,
        "execution_source": args.execution_source,
        "validator_version": args.validator_version,
        "execution_ref": {
            "task_id": args.task_id,
            "run_id": args.run_id,
            "attempt_id": args.attempt_id,
        },
        "checked_at": int(time.time()),
        "checkable_scope": [
            "forbidden_terms", "multi_activity_sum", "core_chapters",
            "raw_json_leakage", "body_length", "chart_references",
            "degraded_provenance", "requirement_scope", "product_funnel",
            "competitor_retired", "placeholder_leakage", "causality",
            "phone_privacy", "small_sample_disclosure", "chart_files",
            "required_trends", "brand_wordcloud", "hot_table_schema",
            "opinion_representatives", "ugc_representatives", "query_disclosure",
        ],
        "uncheckable_scope": [
            "semantic_evidence", "live_feishu_layout", "document_ownership",
            "external_link_access",
        ],
        "qc_metadata": {
            "actor": "topic5-validator",
            "mode": "independent-agent",
            "validator_id": "topic5-validator",
            "generated_at": int(time.time()),
            "input_artifacts": [
                item for item in (
                    subject,
                    _input_ref(args.requirement, "requirement_confirmed"),
                    _input_ref(args.stats, "stats_bundle"),
                    _input_ref(args.charts, "chart_urls"),
                    _input_ref(args.rules, "validator_rules"),
                ) if item
            ],
        },
    })
    Path(args.out).write_text(json.dumps(qc, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"[validator] p0={qc['p0_count']} p1={qc['p1_count']} "
          f"has_p0={qc['has_p0']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
