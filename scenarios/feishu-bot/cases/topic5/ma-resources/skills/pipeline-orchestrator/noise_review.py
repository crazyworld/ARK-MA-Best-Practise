"""Step5.5 去噪审核的确定性部分（FIX-05，原版 KS_Noise_Review）。

  - 选样：每活动每平台互动 Top30；每日声量、互动 Top3 日期（并列全取）；
    互动高点当天主要贡献帖（累计 ≥80%）；声量高点按平台×PGC/UGC 分层每层最多 30 条，seed=42。
  - 窗口：在标题/正文/三类识别文本中定位活动关键词，取「前一句+当句+后一句」，不跨字段拼接。
  - 扩查：发现噪音后，相同命中分支或相同文案（hash）的内容加入待审，逐帖判定，不整批删除。
  - 出口：待审集合每个 ID 都有结论；保留集不含 noise ID。
"""
from __future__ import annotations

from interaction import interaction_value  # Q2 供应商互动量

import hashlib
import random
import re

TOP_INTERACTION = 30
PEAK_DAYS = 3
PEAK_CONTRIBUTION = 0.80
STRATUM_LIMIT = 30
SEED = 42
TEXT_FIELDS = ("title", "content", "video_cover_text", "audio_text", "video_text")
VERDICTS = ("keep", "noise", "uncertain")
_SENTENCE = re.compile(r"[^。！？!?\n]*[。！？!?\n]?")


def row_id(row: dict) -> str:
    return str(row.get("id") or row.get("item_id") or row.get("url") or row.get("_dedup_key") or "")


def _top_days(rows: list[dict], value) -> list[str]:
    totals: dict[str, float] = {}
    for row in rows:
        day = row.get("_publish_ymd")
        if day:
            totals[day] = totals.get(day, 0) + value(row)
    if not totals:
        return []
    ranked = sorted(set(totals.values()), reverse=True)[:PEAK_DAYS]
    return sorted(day for day, total in totals.items() if total in ranked)


def _interaction(row: dict) -> int:
    return interaction_value(row) or 0


def select_sample(rows: list[dict]) -> dict[str, list[str]]:
    """返回 {reason: [ids]}；同一 ID 可出现在多个原因下，审核只判一次。"""
    picked: dict[str, list[str]] = {"top_interaction": [], "interaction_peak": [], "volume_peak": []}
    by_platform: dict[str, list[dict]] = {}
    for row in rows:
        by_platform.setdefault(str(row.get("_platform") or ""), []).append(row)
    for group in by_platform.values():
        ranked = sorted(group, key=lambda r: (-_interaction(r), row_id(r)))[:TOP_INTERACTION]
        picked["top_interaction"] += [row_id(r) for r in ranked]
    for day in _top_days(rows, _interaction):
        same_day = sorted((r for r in rows if r.get("_publish_ymd") == day),
                          key=lambda r: (-_interaction(r), row_id(r)))
        total = sum(_interaction(r) for r in same_day) or 1
        running = 0
        for row in same_day:
            picked["interaction_peak"].append(row_id(row))
            running += _interaction(row)
            if running / total >= PEAK_CONTRIBUTION:
                break
    rng = random.Random(SEED)
    for day in _top_days(rows, lambda _r: 1):
        strata: dict[tuple, list[dict]] = {}
        for row in rows:
            if row.get("_publish_ymd") == day:
                key = (str(row.get("_platform") or ""), str(row.get("content_mode") or "UKN").upper())
                strata.setdefault(key, []).append(row)
        for key in sorted(strata):
            group = sorted(strata[key], key=row_id)
            chosen = group if len(group) <= STRATUM_LIMIT else rng.sample(group, STRATUM_LIMIT)
            picked["volume_peak"] += [row_id(r) for r in chosen]
    return {reason: list(dict.fromkeys(ids)) for reason, ids in picked.items()}


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.findall(text or "") if s.strip()]


def windows(row: dict, terms: list[str]) -> list[dict]:
    """每个命中句取前一句+当句+后一句；记录证据字段，不跨字段拼接。"""
    out = []
    lowered = [t.lower() for t in terms if t]
    for field in TEXT_FIELDS:
        sentences = _sentences(str(row.get(field) or ""))
        for index, sentence in enumerate(sentences):
            if any(term in sentence.lower() for term in lowered):
                text = "".join(sentences[max(0, index - 1):index + 2]).strip()
                if not out or out[-1]["field"] != field or out[-1]["window"] != text:
                    out.append({"field": field, "window": text})
    return out


def text_hash(row: dict) -> str:
    body = re.sub(r"\s+", "", f"{row.get('title') or ''}{row.get('content') or ''}")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] if body else ""


def expansion(rows: list[dict], noise: list[dict], reviewed: set[str]) -> list[str]:
    """同命中分支或同文案的未审内容加入下一轮逐帖判定。"""
    branches = {d.get("branch") for d in noise if d.get("branch")}
    hashes = {text_hash(r) for r in rows if row_id(r) in {d["id"] for d in noise}} - {""}
    extra = []
    for row in rows:
        rid = row_id(row)
        if rid in reviewed:
            continue
        if text_hash(row) in hashes or (branches and row.get("_hit_branch") in branches):
            extra.append(rid)
    return extra


def exit_check(sampled: set[str], decisions: list[dict], kept_ids: set[str]) -> None:
    decided = {d["id"] for d in decisions if d.get("verdict") in VERDICTS}
    missing = sorted(sampled - decided)
    if missing:
        raise RuntimeError(f"noise_review: {len(missing)} 条待审内容无结论，例如 {missing[:3]}")
    leaked = sorted(kept_ids & {d["id"] for d in decisions if d["verdict"] == "noise"})
    if leaked:
        raise RuntimeError(f"noise_review: 噪音仍在保留集内：{leaked[:3]}")


def peak_evidence(rows: list[dict]) -> dict:
    """高点证据（FIX-06，原版 validator_peak_relevance_review）：供独立 Validator 核查。

    声量 Top3 日期（当天按互动前 10 帖）；互动 Top3 日期的主要贡献帖（累计 ≥80%）。
    """
    def post(row: dict) -> dict:
        return {"id": row_id(row), "title": row.get("title") or "",
                "content": str(row.get("content") or "")[:200], "url": row.get("url") or "",
                "interaction_cnt": _interaction(row), "platform": row.get("_platform") or ""}

    def day_rows(day: str) -> list[dict]:
        return sorted((r for r in rows if r.get("_publish_ymd") == day),
                      key=lambda r: (-_interaction(r), row_id(r)))

    volume = [{"date": day, "posts": len(day_rows(day)),
               "top_posts": [post(r) for r in day_rows(day)[:10]]}
              for day in _top_days(rows, lambda _r: 1)]
    interaction = []
    for day in _top_days(rows, _interaction):
        same_day = day_rows(day)
        total = sum(_interaction(r) for r in same_day) or 1
        contributors, running = [], 0
        for row in same_day:
            contributors.append(post(row))
            running += _interaction(row)
            if running / total >= PEAK_CONTRIBUTION:
                break
        interaction.append({"date": day, "interaction": total,
                            "contributors": contributors, "covered_share": round(running / total, 4)})
    return {"volume_peaks": volume, "interaction_peaks": interaction}
