"""把 MA session 事件渲染成飞书文档(XML):**原始事件严格按流顺序**,Turn / Span 卡片作为分组标注。

要求(用户,2026-11-12):
- **全量、不过滤**:每个事件都进文档,原始 JSON 全文,文档要满足用户"全量预览 MA 事件流";
- 同时展示每个阶段的**关键信息**(状态、耗时、token);区分分块:一轮对话、一个 span;样式参照 可观测demo.png;
- 事件的**顺序必须与 MA 流一致**:卡片只是插在事件之间的标注,**不把事件搬到别处**(早先把状态类事件
  集中放到轮末,实测 208 个事件里 163 个位置与原流不同,已推翻)。

唯一的加工是**凭据脱敏**(规则见 redact.py)。模型 token 用量是计数,不是机密,不脱敏。

术语:**一轮对话** = 用户一条消息 → 到 ``session.status_idle``;一轮对话里可以有**多次模型推理**(Span)和工具调用。

并行判定(2026-11-12 对 4 个真实 session、9 个批次实测):模型一次回复发出多个工具调用,事件流里表现为
**连续 N≥2 个 ``agent.tool_use``、中间没有任何别的事件**,它们的 ``tool_result`` 随后乱序返回。
这是因果性、结构性的判定,**不用时间重叠**(两个串行工具碰巧时间交叠不是并行),**也不用"时间戳相同"**
(实测并行批次里各 ``tool_use`` 的时间戳互不相同)。

飞书 XML 限制(实测):callout 里不能放代码块;callout 背景要用 ``light-*``。
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Optional

from .events import event_text, stream_order, ts_ms
from .redact import redact_value

GREEN = ("✅", "light-green", "green")
RED = ("❌", "light-red", "red")
GRAY = ("⏳", "light-gray", "gray")
AMBER = ("⚠️", "light-yellow", "yellow")

# stop_reason → 面向用户的说法。只收录实测见过的取值;没见过的原样显示,不编造含义。
_STOP_TEXT = {"end_turn": "已完成", "retries_exhausted": "模型调用失败,重试耗尽"}


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    return f"{seconds:.1f} 秒" if seconds < 120 else f"{seconds / 60:.1f} 分钟"


# ---- 分析:不移动任何事件,只在旁边记录"哪些事件属于哪个 Span / 并行批次" ------------------
@dataclass
class Span:
    kind: str  # model | tool
    name: str
    started_ms: int
    ended_ms: Optional[int] = None
    status: str = "ok"  # ok | error | pending
    tags: list[str] = field(default_factory=list)
    first_event_id: str = ""  # 卡片插在它之前
    event_ids: list[str] = field(default_factory=list)
    tool_use_id: str = ""

    @property
    def duration_ms(self) -> Optional[int]:
        return None if self.ended_ms is None else max(0, self.ended_ms - self.started_ms)


@dataclass
class Round:  # 一轮对话
    index: int
    started_ms: int
    ended_ms: Optional[int] = None
    stop_reason: str = ""
    errored: bool = False  # 出现 session.error
    retried_failures: int = 0  # 失败但后来恢复的模型调用数
    spans: list[Span] = field(default_factory=list)
    first_event_id: str = ""  # 轮头部卡片插在它之前


@dataclass
class Analysis:
    ordered: list[dict]  # 严格的原始流顺序
    rounds: list[Round]
    # 事件 id → 在它**之前**要插入的卡片(轮头部、Span 卡片、并行 grid)。事件本身位置不变。
    before: dict[str, list[str]]
    # 事件 id → 它在并行批次里的标签(用于 JSON 块的 caption)
    parallel_label: dict[str, str]


def analyse(events: list[dict], first_round: int = 1) -> Analysis:
    ordered = stream_order(events)   # 纳秒 + 输入位置;不能用毫秒(会让同一毫秒内的事件错位)
    rounds: list[Round] = []
    cur: Optional[Round] = None
    model_open: dict[str, Span] = {}
    tool_open: dict[str, Span] = {}
    spans_by_event: dict[str, Span] = {}
    pending_state: list[dict] = []  # 第一条 user.message 之前的状态事件,归入紧随其后的那一轮

    def new_round(at: int, first_id: str) -> Round:
        r = Round(index=first_round + len(rounds), started_ms=at, first_event_id=first_id)
        rounds.append(r)
        return r

    for e in ordered:
        at, etype, eid = ts_ms(e.get("processed_at")), str(e.get("type") or ""), str(e.get("id") or "")
        if etype == "user.message":
            first = pending_state[0]["id"] if pending_state else eid  # 开头的状态事件也在本轮卡片之后
            cur = new_round(pending_state and ts_ms(pending_state[0].get("processed_at")) or at, str(first))
            pending_state.clear()
            continue
        if cur is None:
            if etype in _STATE_EVENTS:
                pending_state.append(e)
                continue
            cur = new_round(at, eid)  # 增量批次没有 user.message 但已有工作事件
        if etype == "span.model_request_start":
            sp = Span("model", "模型推理", at, first_event_id=eid, event_ids=[eid], tags=_thread(e))
            cur.spans.append(sp)
            model_open[eid] = sp
            spans_by_event[eid] = sp
        elif etype == "span.model_request_end":
            sp = model_open.pop(str(e.get("model_request_start_id")), None)
            if sp is None:
                sp = Span("model", "模型推理(缺起点)", at, first_event_id=eid, tags=_thread(e))
                cur.spans.append(sp)
            sp.event_ids.append(eid)
            spans_by_event[eid] = sp
            sp.ended_ms = at
            u = e.get("model_usage") if isinstance(e.get("model_usage"), dict) else {}
            sp.tags += [f"in {u.get('input_tokens', 0)}", f"out {u.get('output_tokens', 0)}"]
            if u.get("cache_read_input_tokens"):
                sp.tags.append(f"cache {u['cache_read_input_tokens']}")
            if e.get("is_error"):
                sp.status = "error"
        elif etype == "agent.tool_use":
            sp = Span("tool", str(e.get("name") or "工具"), at, status="pending", first_event_id=eid,
                      event_ids=[eid], tags=_thread(e), tool_use_id=eid)
            cur.spans.append(sp)
            tool_open[eid] = sp
            spans_by_event[eid] = sp
        elif etype == "agent.tool_result":
            sp = tool_open.pop(str(e.get("tool_use_id")), None)
            if sp is None:
                sp = Span("tool", "工具结果(缺调用)", at, first_event_id=eid, tags=_thread(e))
                cur.spans.append(sp)
            sp.event_ids.append(eid)
            spans_by_event[eid] = sp
            sp.ended_ms = at
            sp.status = "error" if e.get("is_error") else "ok"
        elif etype == "session.error":
            cur.errored = True
        elif etype == "session.status_idle":
            cur.ended_ms = at
            reason = e.get("stop_reason")
            cur.stop_reason = str(reason.get("type") or "") if isinstance(reason, dict) else ""
    for r in rounds:
        r.retried_failures = _recovered_failures(r)
    return _place_cards(ordered, rounds, spans_by_event)


def _recovered_failures(r: Round) -> int:
    """失败的模型调用,且**之后**还有成功的模型调用 → 算"重试后恢复"。末尾连续失败不算(那是没恢复)。"""
    models = [s for s in r.spans if s.kind == "model"]
    last_ok = max((i for i, s in enumerate(models) if s.status == "ok"), default=-1)
    return sum(1 for i, s in enumerate(models) if s.status == "error" and i < last_ok)


# 不属于任何 Span 的状态类事件
_STATE_EVENTS = frozenset({
    "session.status_running", "session.status_idle", "session.usage",
    "session.thread_status_running", "session.thread_status_idle", "session.thread_created",
})


def _thread(e: dict) -> list[str]:
    tid = e.get("session_thread_id")
    return [f"线程 …{str(tid)[-6:]}"] if tid else []


# ---- 卡片 ------------------------------------------------------------------------------
def _card(style: tuple, head: str, detail: str = "") -> str:
    emoji, bg, border = style
    body = f"<p>{head}</p>" + (f"<p>{detail}</p>" if detail else "")
    return f'<callout emoji="{emoji}" background-color="{bg}" border-color="{border}">{body}</callout>'


def round_card(r: Round) -> str:
    """一轮对话的状态只看**最终结果**,不看过程里是否出现过失败(方舟会自动重试,失败后重试成功不是失败)。"""
    spans = r.spans
    total = None if r.ended_ms is None else max(0, r.ended_ms - r.started_ms)
    model_ms = sum(s.duration_ms or 0 for s in spans if s.kind == "model")
    tool_ms = sum(s.duration_ms or 0 for s in spans if s.kind == "tool")
    ratio = ""
    if model_ms + tool_ms:
        ratio = f" · 模型 {model_ms * 100 // (model_ms + tool_ms)}% / 工具 {tool_ms * 100 // (model_ms + tool_ms)}%"
    models = sum(1 for s in spans if s.kind == "model")
    tools = sum(1 for s in spans if s.kind == "tool")

    if r.ended_ms is None:
        style, state = GRAY, "进行中"
    elif r.stop_reason == "end_turn" and not r.errored:
        style, state = GREEN, _STOP_TEXT["end_turn"]
    elif r.stop_reason == "retries_exhausted" or r.errored:
        style, state = RED, _STOP_TEXT.get(r.stop_reason, f"出现错误({esc(r.stop_reason) or '无结束原因'})")
    else:
        style, state = AMBER, f"结束原因 {esc(r.stop_reason) or '未知'}"  # 没见过的取值:原样显示,不编造含义
    note = f"其中 {r.retried_failures} 次模型调用失败后重试成功" if r.retried_failures and style is GREEN else ""
    head = (f"<b>第 {r.index} 轮对话</b> · {state} · {fmt_ms(total)} · 模型推理 {models} 次 · 工具调用 {tools} 次{ratio}")
    return _card(style, head, note)


def span_card(sp: Span, label: str = "") -> str:
    style = {"ok": GREEN, "error": RED, "pending": GRAY}[sp.status]
    kind = "模型推理" if sp.kind == "model" else "工具调用"
    head = f"<b>{esc(sp.name)}</b> · {kind} · {fmt_ms(sp.duration_ms)}" + (" · 未完成" if sp.status == "pending" else "")
    if label:
        head = f"{esc(label)} " + head
    return _card(style, head, " · ".join(esc(t) for t in sp.tags))


def _place_cards(ordered: list[dict], rounds: list[Round], spans_by_event: dict[str, Span]) -> Analysis:
    before: dict[str, list[str]] = {}
    parallel_label: dict[str, str] = {}
    index_of = {str(e.get("id")): i for i, e in enumerate(ordered)}

    def put(event_id: str, card: str) -> None:
        before.setdefault(event_id, []).append(card)

    for r in rounds:
        put(r.first_event_id, "<hr/>" + round_card(r))
        groups = _parallel_groups(r.spans, ordered, index_of)
        grouped: set[int] = {id(s) for g in groups for s in g}
        for g in groups:
            first = min((s.first_event_id for s in g), key=lambda i: index_of.get(i, 10**9))
            cols = "".join(
                f'<column width-ratio="{1 / len(g):.4f}">{span_card(s, f"#{n}")}</column>' for n, s in enumerate(g, 1)
            )
            put(first, f"<p><b>并行 {len(g)}</b>(同一次模型回复发出的 {len(g)} 个工具调用)</p><grid>{cols}</grid>")
            for n, s in enumerate(g, 1):
                for eid in s.event_ids:
                    parallel_label[eid] = f"并行 #{n}/{len(g)}"
        for s in r.spans:
            if id(s) in grouped:
                continue
            put(s.first_event_id, span_card(s))
    return Analysis(ordered, rounds, before, parallel_label)


def _parallel_groups(spans: list[Span], ordered: list[dict], index_of: dict[str, int]) -> list[list[Span]]:
    """连续 N≥2 个 agent.tool_use(流中相邻,中间没有任何别的事件)= 一个并行批次。"""
    tool_use_pos = {s.tool_use_id: index_of.get(s.tool_use_id, -1) for s in spans if s.kind == "tool" and s.tool_use_id}
    by_pos = sorted((pos, s) for s in spans if s.kind == "tool" and s.tool_use_id for pos in [tool_use_pos[s.tool_use_id]] if pos >= 0)
    groups: list[list[Span]] = []
    run: list[tuple[int, Span]] = []
    for pos, sp in by_pos:
        if run and pos == run[-1][0] + 1:
            run.append((pos, sp))
        else:
            if len(run) >= 2:
                groups.append([s for _, s in run])
            run = [(pos, sp)]
    if len(run) >= 2:
        groups.append([s for _, s in run])
    return groups


# ---- 输出 ------------------------------------------------------------------------------
def _json_block(event: dict, label: str) -> str:
    text = json.dumps(redact_value(event), ensure_ascii=False, indent=2)
    return f'<pre lang="json" caption="{esc(label)}"><code>{esc(text)}</code></pre>'


def render_events_xml(events: list[dict], first_round: int = 1) -> tuple[str, str, int, int]:
    """返回 (XML, 最后事件 id, 最后事件时间 ms, 事件总数)。事件**严格按原始流顺序**,无事件返回空串。"""
    a = analyse(events, first_round)
    if not a.ordered:
        return "", "", 0, 0
    out: list[str] = []
    for e in a.ordered:
        eid = str(e.get("id") or "")
        out.extend(a.before.get(eid, []))
        label = f"{e.get('type')} · {eid}"
        if eid in a.parallel_label:
            label = f"{a.parallel_label[eid]} · {label}"
        out.append(_json_block(e, label))
        if e.get("type") == "user.message":
            text = event_text(e)
            if text:
                out.append(f"<blockquote><p>{esc(redact_value(text))}</p></blockquote>")
    last = a.ordered[-1]
    return "".join(out), str(last.get("id") or ""), ts_ms(last.get("processed_at")), len(a.ordered)


# 兼容旧的调用方 / 测试:按"分块"取轮列表
def build_turns(events: list[dict]) -> list[Round]:
    return analyse(events).rounds
