"""热帖成功配方解构（FIX-49，2.N.4④）。

原版依据 KS_Content_Analysis_Prompt「热帖成功配方解构 Prompt」：输入为 2.N.5 热门榜单
（Top10–15：平台、内容类型、标题、正文前 200 字、互动量、url）；输出高互动内容规律
（1-2 句核心洞察 + 2-3 句补充）与共性表（内容形式/核心主题/情感基调/代表帖互动量，3-5 行）。
代表帖必须来自输入集合；互动量由代码按代表帖回填，不采用模型数字。
"""
from __future__ import annotations

import json

import semantic

MAX_ATTEMPTS = 3
MIN_ROWS, MAX_ROWS = 3, 5
HOOK_TYPES = ("反差式", "惊叹式", "场景代入式", "价格锚点式", "问题式")

SYSTEM = (
    "## 角色\n你是一位资深社媒内容策略分析师，擅长从高互动内容中提炼可复用的内容规律。\n\n"
    "## 分析要求\n"
    "1. 识别内容来源分布（平台构成 + PGC/UGC 构成），说明哪类来源的内容互动表现更突出；"
    "无图文/视频字段时跳过形式维度，不得编造\n"
    "2. 归纳核心话题/主题（2-3个高频出现的内容方向）\n"
    "3. 总结情感基调（激励/趣味/种草/测评/情感共鸣等）\n"
    "4. 识别开篇钩子类型（" + "/".join(HOOK_TYPES) + "）\n"
    "5. 归纳叙事结构（步骤一 → 步骤二 → 步骤三）\n"
    "6. 提炼 1-2 句「高互动内容规律」作为核心洞察，含具体数据\n\n"
    "## 约束\n不引用帖子原文，禁止出现用户账号名；禁止自行计算平台对比比值；"
    "禁止出现「爆款配方」「解构」等内部分析语言；指标禁用人均/转化/性价比。\n\n"
    "## 输出（仅 JSON）\n"
    '{"callout":"一句：判断与数字证据，不以数字开头、不写建议",'
    '"insights":["核心洞察1-2句","补充说明2-3句"],'
    '"rows":[{"form":"内容形式","theme":"核心主题","tone":"情感基调","rep_id":"输入中的 id"}]}\n'
    f"rows 为 {MIN_ROWS}-{MAX_ROWS} 行，每行是一种典型内容模式的归纳，rep_id 必须取自输入。"
)


def _inputs(rows: list[dict]) -> list[dict]:
    return [{"id": f"post-{i}", "rank": i, "platform": row.get("platform"),
             "content_type": row.get("content_mode"), "title": row.get("title") or "",
             "content": row.get("content") or "", "interaction": row.get("interaction_cnt"),
             "url": row.get("url") or ""}
            for i, row in enumerate(rows, 1)]


def _validate(data, by_id: dict, forbidden: tuple[str, ...]) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["输出不是 JSON 对象"]
    rows = data.get("rows")
    if not isinstance(rows, list) or not MIN_ROWS <= len(rows) <= MAX_ROWS:
        errors.append(f"rows 须为 {MIN_ROWS}-{MAX_ROWS} 行")
        rows = rows if isinstance(rows, list) else []
    for row in rows:
        if not isinstance(row, dict) or row.get("rep_id") not in by_id:
            errors.append(f"rep_id 不在输入集合：{(row or {}).get('rep_id') if isinstance(row, dict) else row}")
        elif not all(str(row.get(k) or "").strip() for k in ("form", "theme", "tone")):
            errors.append(f"{row.get('rep_id')} 缺内容形式/主题/基调")
    insights = data.get("insights")
    if not isinstance(insights, list) or not insights or not str(data.get("callout") or "").strip():
        errors.append("缺 callout 或 insights")
    text = json.dumps(data, ensure_ascii=False)
    errors += [f"命中禁用措辞「{term}」" for term in forbidden if term in text]
    return errors


def analyze(campaign_name: str, rows: list[dict], forbidden: tuple[str, ...]) -> dict:
    """返回 {callout, insights, rows:[{form, theme, tone, rep_interaction, rep_url}]}。"""
    if len(rows) < MIN_ROWS:
        return {"not_applicable": True, "reason": f"热门内容仅 {len(rows)} 条，不足以归纳共性"}
    inputs = _inputs(rows)
    by_id = {item["id"]: item for item in inputs}
    user = (f"活动名称：{campaign_name}\n分析平台：{sorted({str(i['platform']) for i in inputs})}\n"
            "Top 帖子列表：\n" + json.dumps(inputs, ensure_ascii=False))
    feedback, errors = "", []
    for _ in range(MAX_ATTEMPTS):
        data = semantic.chat_json(SYSTEM, user + feedback)
        errors = _validate(data, by_id, forbidden)
        if not errors:
            return {
                "callout": str(data["callout"]).strip(),
                "insights": [str(x).strip() for x in data["insights"] if str(x).strip()],
                "rows": [{"form": r["form"], "theme": r["theme"], "tone": r["tone"],
                          "rep_interaction": by_id[r["rep_id"]]["interaction"],
                          "rep_url": by_id[r["rep_id"]]["url"]} for r in data["rows"]],
            }
        feedback = "\n\n上一次输出不合格，请修正：" + "；".join(errors)
    raise RuntimeError("hot_post_formula: 连续三次输出不合格：" + "；".join(errors))


def render(result: dict) -> str:
    """2.N.4④ 正文：💡 callout + 洞察 + 4 列共性表（列名按 KS_Report_Template §2.N.4）。"""
    title = "**④ 热帖成功配方**"
    if not result:
        return ""
    if result.get("not_applicable"):
        return f"{title}\n\n*{{{{muted}}}}{result.get('reason')}。*"
    lines = [title, f"> [!tip] {result['callout']}", "".join(result.get("insights") or [])]
    table = ["| 内容形式 | 核心主题 | 情感基调 | 代表帖互动量 |", "| --- | --- | --- | --- |"]
    table += [f"| {r['form']} | {r['theme']} | {r['tone']} | {r['rep_interaction']} |"
              for r in result.get("rows") or []]
    return "\n\n".join(lines + ["\n".join(table)])
