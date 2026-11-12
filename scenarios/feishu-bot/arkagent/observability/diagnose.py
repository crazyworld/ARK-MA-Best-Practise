"""确定性的诊断:从 Trace 里找首个异常点,给出一句话原因。纯函数,不调模型。

排障 Bot 的结论必须有出处,所以这里只依据 Trace 里已有的信号归类,并指出对应 span;
LLM 只负责把这个结果讲给用户听,不负责在原始事件里自己找原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import (
    KIND_ERROR,
    KIND_HC,
    KIND_MODEL,
    KIND_TOOL,
    STATUS_ERROR,
    STATUS_PENDING,
    Span,
    Trace,
)

# 方舟会自动重试失败的模型调用,所以一次 is_error 不等于会话失败。
_EXHAUSTED = "exhausted"
# 工具未完成超过该时长才算"疑似卡住";bash_wait 轮询本来就可以很久。
STUCK_TOOL_MS = 15 * 60 * 1000
_FAILED_STATUSES = ("failed", "stopped", "unsupported", "busy")


@dataclass
class Finding:
    level: str  # error | warning | info
    code: str
    summary: str
    span_id: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "summary": self.summary,
            "span_id": self.span_id,
            "session_id": self.session_id,
        }


def diagnose(trace: Trace, *, now_ms: Optional[int] = None) -> list[Finding]:
    """按严重程度排序返回,首个即\"首个异常点\"的结论。"""
    findings: list[Finding] = []
    unavailable = [n for n in trace.sessions if not n.available]

    for node in trace.sessions:
        if not node.available:
            continue
        findings += _session_findings(node, now_ms)

    if trace.status in _FAILED_STATUSES and trace.last_error:
        findings.append(
            Finding("error", "task_failed", f"任务状态为 {trace.status}:{trace.last_error[:200]}")
        )
    for node in unavailable:
        findings.append(
            Finding(
                "info",
                "session_unavailable",
                f"会话 {node.session_id} 无事件数据:{node.unavailable_reason}",
                session_id=node.session_id,
            )
        )
    for span in trace.db_spans:
        if span.kind == KIND_HC and span.status == STATUS_PENDING:
            findings.append(Finding("info", "waiting_review", f"正在等待人工审核 {span.name}", span.span_id))

    # 不做任何合并或二次加工:每一次异常都按原始事件逐条列出,由使用者(人或模型)自行判断。
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(findings, key=lambda f: order[f.level])


def _session_findings(node, now_ms: Optional[int]) -> list[Finding]:
    found: list[Finding] = []
    for turn in node.turns:
        models = [s for s in turn.spans if s.kind == KIND_MODEL]
        failed = [s for s in models if s.status == STATUS_ERROR]
        exhausted = [
            s for s in turn.spans if s.kind == KIND_ERROR and s.attrs.get("retry_status") == _EXHAUSTED
        ]
        if exhausted:
            err = exhausted[0]
            found.append(
                Finding(
                    "error",
                    "model_retries_exhausted",
                    f"第 {turn.index} 轮模型调用失败且重试耗尽({err.name}:{err.attrs.get('message', '')[:120]})",
                    err.span_id,
                    node.session_id,
                )
            )
        elif failed:
            found.append(
                Finding(
                    "warning",
                    "model_retried",
                    f"第 {turn.index} 轮有 {len(failed)} 次模型调用失败,但方舟重试后继续运行",
                    failed[0].span_id,
                    node.session_id,
                )
            )
        for span in turn.spans:
            if span.kind == KIND_TOOL and span.status == STATUS_ERROR:
                found.append(
                    Finding(
                        "warning",
                        "tool_error",
                        f"第 {turn.index} 轮工具 {span.attrs.get('category', span.name)} 返回错误",
                        span.span_id,
                        node.session_id,
                    )
                )
        found += _stuck_tools(node, turn, now_ms)
    return found


def _stuck_tools(node, turn, now_ms: Optional[int]) -> list[Finding]:
    if now_ms is None:
        return []
    return [
        Finding(
            "warning",
            "tool_stuck",
            f"第 {turn.index} 轮工具 {s.attrs.get('category', s.name)} 已运行超过 {(now_ms - s.started_at_ms) // 60000} 分钟仍未返回",
            s.span_id,
            node.session_id,
        )
        for s in turn.spans
        if s.kind == KIND_TOOL
        and s.status == STATUS_PENDING
        and now_ms - s.started_at_ms > STUCK_TOOL_MS
    ]


def find_span(trace: Trace, span_id: str) -> Optional[Span]:
    return next((s for s in trace.all_spans() if s.span_id == span_id), None)
