"""把 MA session 事件渲染成飞书文档(XML),按 Turn → Span → 原始事件三级分块。

要求(用户,2026-11-12):
- **不过滤**:每一个事件都进文档,包括 thinking、成功的模型调用、session.usage 等,不截断;
- 既展示**原始事件 JSON 全文**,又展示每个阶段的**关键信息**(状态、耗时、token、线程);
- 区分分块:一个 turn、一个 span;样式参照 可观测demo.png(绿勾 / 红叉卡片、轮次头部、并行并排)。

唯一的加工是**密钥脱敏**(最初的决策"除密钥外均可见"):脱敏发生在渲染之前,原始事件对象不被修改。

飞书 XML 限制(2026-11-12 实测,回读确认):
- callout 里不能放代码块,所以"关键信息卡片"和"原始 JSON"是前后相邻的两个块;
- callout 的 ``background-color`` 取 ``light-*`` / ``medium-*`` 才会生效(基础色相会被渲染成灰底)。
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Optional

from .events import event_text, ts_ms
from .redact import redact_value

# 不属于任何 span 的状态类事件
_STATE_EVENTS = frozenset({
    "session.status_running", "session.status_idle", "session.usage",
    "session.thread_status_running", "session.thread_status_idle", "session.thread_created",
})

GREEN = ("✅", "light-green", "green")
RED = ("❌", "light-red", "red")
GRAY = ("⏳", "light-gray", "gray")
BLUE = ("🔹", "light-blue", "blue")
PURPLE = ("🔸", "light-purple", "purple")


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    return f"{seconds:.1f} 秒" if seconds < 120 else f"{seconds / 60:.1f} 分钟"


# ---- 分块模型 -----------------------------------------------------------------
@dataclass
class SpanBlock:
    kind: str  # model | tool | message | error | other
    title: str
    started_ms: int
    ended_ms: Optional[int] = None
    status: str = "ok"  # ok | error | pending
    tags: list[str] = field(default_factory=list)  # 关键信息:token、线程、工具名…
    events: list[dict] = field(default_factory=list)  # 原始事件,时间升序

    @property
    def duration_ms(self) -> Optional[int]:
        return None if self.ended_ms is None else max(0, self.ended_ms - self.started_ms)


@dataclass
class TurnBlock:
    index: int
    started_ms: int
    ended_ms: Optional[int] = None
    stop_reason: str = ""
    spans: list[SpanBlock] = field(default_factory=list)
    loose: list[dict] = field(default_factory=list)  # 不属于任何 span 的事件(状态、usage 等)
    head: list[dict] = field(default_factory=list)  # 用户消息


def build_turns(events: list[dict]) -> list[TurnBlock]:
    """把事件分进 Turn / Span,**保证每个事件恰好出现一次**(不丢、不重)。"""
    ordered = sorted((e for e in events if isinstance(e, dict)), key=lambda e: ts_ms(e.get("processed_at")))
    turns: list[TurnBlock] = []
    current: Optional[TurnBlock] = None
    # 第一条用户消息之前到达的状态类事件(status_running、thread_status_running、usage 等):
    # 它们属于紧随其后的那一轮,不能自己开一轮,否则轮次编号整体错位(实测会把 2 轮数成 3 轮)。
    preamble: list[dict] = []
    model_open: dict[str, SpanBlock] = {}  # model_request_start id → span
    tool_open: dict[str, SpanBlock] = {}  # tool_use id → span
    last_span: Optional[SpanBlock] = None

    def new_turn(at: int) -> TurnBlock:
        t = TurnBlock(index=len(turns) + 1, started_ms=at)
        turns.append(t)
        return t

    for e in ordered:
        at = ts_ms(e.get("processed_at"))
        etype = str(e.get("type") or "")
        if etype == "user.message":
            current = new_turn(at)
            current.head.append(e)
            current.loose.extend(preamble)
            preamble.clear()
            continue
        if current is None:
            if etype in _STATE_EVENTS:
                preamble.append(e)
                continue
            # 增量拉取时本批没有 user.message,但已有真正的工作事件:隐式开一轮,避免事件无处可放
            current = new_turn(at)
            current.loose.extend(preamble)
            preamble.clear()
        if etype == "span.model_request_start":
            sp = SpanBlock("model", "模型推理", at, events=[e], tags=_thread_tag(e))
            current.spans.append(sp)
            model_open[str(e.get("id"))] = sp
            last_span = sp
        elif etype == "span.model_request_end":
            sp = model_open.pop(str(e.get("model_request_start_id")), None)
            if sp is None:  # 找不到起点:单独成块,仍不丢
                sp = SpanBlock("model", "模型推理(缺起点事件)", at, events=[], tags=_thread_tag(e))
                current.spans.append(sp)
            sp.events.append(e)
            sp.ended_ms = at
            usage = e.get("model_usage") if isinstance(e.get("model_usage"), dict) else {}
            sp.tags += [
                f"in {usage.get('input_tokens', 0)}",
                f"out {usage.get('output_tokens', 0)}",
                *([f"cache {usage['cache_read_input_tokens']}"] if usage.get("cache_read_input_tokens") else []),
            ]
            if e.get("is_error"):
                sp.status = "error"
            last_span = sp
        elif etype == "agent.tool_use":
            sp = SpanBlock("tool", str(e.get("name") or "工具"), at, events=[e], status="pending", tags=_thread_tag(e))
            current.spans.append(sp)
            tool_open[str(e.get("id"))] = sp
            last_span = sp
        elif etype == "agent.tool_result":
            sp = tool_open.pop(str(e.get("tool_use_id")), None)
            if sp is None:
                sp = SpanBlock("tool", "工具结果(缺调用事件)", at, events=[], tags=_thread_tag(e))
                current.spans.append(sp)
            sp.events.append(e)
            sp.ended_ms = at
            sp.status = "error" if e.get("is_error") else "ok"
            last_span = sp
        elif etype in ("agent.thinking", "agent.message"):
            # 没有挂靠键:归属到最近的模型 span(思考与回复由它产生);没有就单独成块
            owner = last_span if last_span and last_span.kind == "model" else None
            if owner is not None:
                owner.events.append(e)
            else:
                current.spans.append(SpanBlock("message", "Agent 消息" if etype == "agent.message" else "思考", at, at, events=[e], tags=_thread_tag(e)))
        elif etype == "session.error":
            err = e.get("error") if isinstance(e.get("error"), dict) else {}
            current.spans.append(
                SpanBlock("error", f"错误 {err.get('type', '')}", at, at, "error",
                          [f"重试:{(err.get('retry_status') or {}).get('type', '-')}"], [e])
            )
        elif etype == "session.status_idle":
            current.ended_ms = at
            reason = e.get("stop_reason")
            current.stop_reason = reason.get("type") if isinstance(reason, dict) else ""
            current.loose.append(e)
        else:
            current.loose.append(e)  # usage、thread_*、status_running 等:原样保留在本轮末尾
    if preamble:  # 整批只有状态事件:仍然不丢,单独成一块
        t = new_turn(ts_ms(preamble[0].get("processed_at")))
        t.loose.extend(preamble)
    return turns


def _thread_tag(e: dict) -> list[str]:
    tid = e.get("session_thread_id")
    return [f"线程 …{str(tid)[-6:]}"] if tid else []


# ---- 渲染 ---------------------------------------------------------------------
def _json_block(event: dict, label: str) -> str:
    """原始事件 JSON 全文(已脱敏、不截断)。"""
    text = json.dumps(redact_value(event), ensure_ascii=False, indent=2)
    return f'<pre lang="json" caption="{esc(label)}"><code>{esc(text)}</code></pre>'


def _card(style: tuple, head: str, detail: str = "") -> str:
    emoji, bg, border = style
    body = f"<p>{head}</p>" + (f"<p>{detail}</p>" if detail else "")
    return f'<callout emoji="{emoji}" background-color="{bg}" border-color="{border}">{body}</callout>'


_KIND_LABEL = {"model": "模型推理", "tool": "工具调用", "message": "消息", "error": "错误", "other": "事件"}


def render_span(sp: SpanBlock) -> str:
    style = {"ok": GREEN, "error": RED, "pending": GRAY}[sp.status]
    label = _KIND_LABEL.get(sp.kind, "事件")
    head = f"<b>{esc(sp.title)}</b> · {label} · {fmt_ms(sp.duration_ms)}"
    if sp.status == "pending":
        head += " · 未完成"
    detail = " · ".join(esc(t) for t in sp.tags)
    blocks = [_card(style, head, detail)]
    for e in sp.events:
        blocks.append(_json_block(e, f"{e.get('type')} · {e.get('id', '')}"))
    return "".join(blocks)


def _parallel(spans: list[SpanBlock]) -> list[list[SpanBlock]]:
    """把时间上重叠的**工具** span 分成一组(图里的"并行");模型 span 与其他永远独占一组。"""
    groups: list[list[SpanBlock]] = []
    for sp in spans:
        if sp.kind == "tool" and groups and groups[-1][0].kind == "tool" and sp.ended_ms is not None:
            last_end = max((s.ended_ms or s.started_ms) for s in groups[-1])
            if sp.started_ms < last_end:
                groups[-1].append(sp)
                continue
        groups.append([sp])
    return groups


def render_turn(turn: TurnBlock) -> str:
    spans = turn.spans
    total = None if turn.ended_ms is None else max(0, turn.ended_ms - turn.started_ms)
    model_ms = sum(s.duration_ms or 0 for s in spans if s.kind == "model")
    tool_ms = sum(s.duration_ms or 0 for s in spans if s.kind == "tool")
    ratio = ""
    if model_ms + tool_ms:
        ratio = f" · 模型 {model_ms * 100 // (model_ms + tool_ms)}% / 工具 {tool_ms * 100 // (model_ms + tool_ms)}%"
    failed = any(s.status == "error" for s in spans)
    style = RED if failed else (GRAY if turn.ended_ms is None else GREEN)
    stop = f" · 结束原因 {esc(turn.stop_reason)}" if turn.stop_reason else (" · 进行中" if turn.ended_ms is None else "")
    out = ["<hr/>", _card(style, f"<b>第 {turn.index} 轮推理</b> · {len(spans)} 个 Span · {fmt_ms(total)}{ratio}{stop}")]
    for e in turn.head:
        out.append(_json_block(e, f"{e.get('type')} · {e.get('id', '')}"))
        text = event_text(e)
        if text:
            out.append(f"<blockquote><p>{esc(redact_value(text))}</p></blockquote>")
    for group in _parallel(spans):
        if len(group) == 1:
            out.append(render_span(group[0]))
            continue
        # 并行工具:图里"并行 N"并排;每列放卡片 + 原始 JSON
        out.append(f"<p><b>并行 {len(group)}</b></p>")
        ratio_w = f"{1 / len(group):.4f}"
        cols = "".join(f'<column width-ratio="{ratio_w}">{render_span(s)}</column>' for s in group)
        out.append(f"<grid>{cols}</grid>")
    for e in turn.loose:
        out.append(_json_block(e, f"{e.get('type')} · {e.get('id', '')}"))
    return "".join(out)


def render_events_xml(events: list[dict]) -> tuple[str, str, int, int]:
    """渲染一批事件。返回 (XML, 最后事件 id, 最后事件时间 ms, 事件总数)。无事件返回空串。"""
    turns = build_turns(events)
    if not turns:
        return "", "", 0, 0
    ordered = sorted((e for e in events if isinstance(e, dict)), key=lambda e: ts_ms(e.get("processed_at")))
    last = ordered[-1]
    return "".join(render_turn(t) for t in turns), str(last.get("id") or ""), ts_ms(last.get("processed_at")), len(ordered)
