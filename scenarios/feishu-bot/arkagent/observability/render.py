"""Trace 的文本渲染。JSON 输出直接用 ``Trace.to_dict()``。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .adapters.base import TraceRef
from .model import (
    KIND_ERROR,
    KIND_HC,
    KIND_MODEL,
    KIND_PHASE,
    KIND_THREAD_MESSAGE,
    KIND_TOOL,
    SOURCE_INFERRED,
    STATUS_ERROR,
    STATUS_PENDING,
    Span,
    Trace,
)


def _clock(ms: Optional[int]) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S") if ms else "-"


def _dur(ms: Optional[int]) -> str:
    if ms is None:
        return "   -  "
    if ms < 1000:
        return f"{ms:>4}ms"
    seconds = ms / 1000
    return f"{seconds:>5.1f}s" if seconds < 600 else f"{seconds / 60:>5.1f}m"


def _span_line(span: Span) -> str:
    mark = {STATUS_ERROR: "✗", STATUS_PENDING: "…"}.get(span.status, " ")
    thread = f" [{span.thread}{'~' if span.source == SOURCE_INFERRED else ''}]" if span.thread else ""
    head = f"    {_clock(span.started_at_ms)[6:]} {mark} {_dur(span.duration_ms)}  "
    a = span.attrs
    if span.kind == KIND_MODEL:
        body = f"model     in={a.get('input_tokens', 0)} out={a.get('output_tokens', 0)}"
        if a.get("cache_read_input_tokens"):
            body += f" cache={a['cache_read_input_tokens']}"
    elif span.kind == KIND_TOOL:
        body = f"tool      {a.get('category', span.name)}"
        if a.get("command_preview"):
            body += f"  {a['command_preview'][:90]!r}"
    elif span.kind == KIND_ERROR:
        body = f"error     {span.name}: {a.get('message', '')} (retry={a.get('retry_status')})"
    elif span.kind == KIND_PHASE:
        body = f"phase     → {span.name}"
    elif span.kind == KIND_THREAD_MESSAGE:
        body = f"thread    {span.name} ({a.get('chars', 0)} 字)"
    else:
        body = f"{span.kind:<9} {span.name}"
    return head + body + thread


def render_text(trace: Trace) -> str:
    total = (
        (trace.finished_at_ms - trace.started_at_ms)
        if trace.finished_at_ms
        else None
    )
    lines = [
        f"Trace {trace.trace_id}  ({trace.root_kind} · {trace.scenario})  状态={trace.status}",
        f"  {_clock(trace.started_at_ms)} → {_clock(trace.finished_at_ms)}  总耗时={_dur(total).strip()}",
    ]
    if trace.last_error:
        lines.append(f"  last_error: {trace.last_error[:200]}")
    if trace.meta:
        lines.append("  " + "  ".join(f"{k}={v}" for k, v in trace.meta.items() if v))
    for node in trace.sessions:
        lines.append(f"\n会话 [{node.role}] {node.session_id}  事件数={node.event_count}")
        if not node.available:
            lines.append(f"  (无事件数据:{node.unavailable_reason})")
            continue
        for turn in node.turns:
            lines.append(
                f"  Turn {turn.index}  {_clock(turn.started_at_ms)[6:]} → "
                f"{_clock(turn.ended_at_ms)[6:]}  stop={turn.stop_reason or '(未结束)'}  "
                f"用户消息={turn.user_text_chars}字"
            )
            lines.extend(_span_line(s) for s in turn.spans)
    if trace.db_spans:
        lines.append("\n数据库记录(HC 审核 / 控制事件)")
        for span in sorted(trace.db_spans, key=lambda s: s.started_at_ms):
            detail = f" 决定={span.attrs['decision']}" if span.attrs.get("decision") else ""
            state = "待处理" if span.status == STATUS_PENDING else "已处理"
            tail = f" {state}{detail}" if span.kind == KIND_HC else ""
            lines.append(f"  {_clock(span.started_at_ms)} {_dur(span.duration_ms)}  {span.kind} {span.name}{tail}")
    s = trace.summary()
    lines.append(
        f"\n摘要: turn={s['turns']}  模型调用={s['model_calls']}(失败{s['model_errors']})  "
        f"token in={s['input_tokens']} out={s['output_tokens']} cache={s['cache_read_tokens']}  "
        f"工具={s['tool_calls']}(失败{s['tool_errors']}, 未完成{s['tool_pending']})  错误事件={s['errors']}"
    )
    if s["slowest_tool"]:
        lines.append(f"最慢工具: {s['slowest_tool']['name']} {_dur(s['slowest_tool']['duration_ms']).strip()}")
    lines.append("注: [线程~] 表示由相邻事件推断的归属;未标注线程的 span 无法确定归属。")
    return "\n".join(lines)


def render_list(refs: list[TraceRef]) -> str:
    if not refs:
        return "没有匹配的任务。"
    rows = [
        f"{r.trace_id:<28} {r.root_kind:<4} {r.scenario:<8} {r.status:<10} {_clock(r.started_at_ms)}  "
        f"{(r.last_error or '')[:50]}"
        for r in refs
    ]
    return "\n".join([f"{'TRACE_ID':<28} {'根':<4} {'场景':<8} {'状态':<10} 开始时间", *rows])


def render_findings(findings) -> str:
    if not findings:
        return "诊断: 没有发现明确异常。"
    icon = {"error": "✗", "warning": "!", "info": "i"}
    return "诊断:\n" + "\n".join(f"  [{icon[f.level]}] {f.summary}" for f in findings)
