"""Trace 数据模型。纯数据类,不含任何 IO。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Span.source:直接来自事件 / 数据库,还是由相邻事件推断(线程归属、阶段)。
SOURCE_EVENT = "event"
SOURCE_DB = "db"
SOURCE_INFERRED = "inferred"

KIND_MODEL = "model_call"
KIND_TOOL = "tool_call"
KIND_THREAD_MESSAGE = "thread_message"
KIND_PHASE = "phase"
KIND_ERROR = "error"
KIND_HC = "hc_wait"
KIND_CONTROL = "control_event"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"


@dataclass
class Span:
    span_id: str
    kind: str
    name: str
    started_at_ms: int
    duration_ms: Optional[int] = None
    status: str = STATUS_OK
    thread: Optional[str] = None
    source: str = SOURCE_EVENT
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    index: int
    user_event_id: Optional[str]
    started_at_ms: int
    ended_at_ms: Optional[int] = None
    stop_reason: Optional[str] = None
    user_text_chars: int = 0
    spans: list[Span] = field(default_factory=list)


@dataclass
class SessionNode:
    session_id: str
    role: str  # router | business
    available: bool = True
    unavailable_reason: str = ""
    event_count: int = 0
    turns: list[Turn] = field(default_factory=list)


@dataclass
class Trace:
    trace_id: str
    root_kind: str  # task | job
    scenario: str
    status: str
    started_at_ms: int
    finished_at_ms: Optional[int] = None
    last_error: str = ""
    sessions: list[SessionNode] = field(default_factory=list)
    db_spans: list[Span] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def all_spans(self) -> list[Span]:
        spans = [s for node in self.sessions for t in node.turns for s in t.spans]
        return spans + self.db_spans

    def summary(self) -> dict[str, Any]:
        models = [s for s in self.all_spans() if s.kind == KIND_MODEL]
        tools = [s for s in self.all_spans() if s.kind == KIND_TOOL]
        slowest = max(
            (s for s in tools if s.duration_ms is not None),
            key=lambda s: s.duration_ms or 0,
            default=None,
        )

        def usage(key: str) -> int:
            return sum(int(s.attrs.get(key) or 0) for s in models)

        return {
            "turns": sum(len(n.turns) for n in self.sessions),
            "model_calls": len(models),
            "model_errors": sum(1 for s in models if s.status == STATUS_ERROR),
            "input_tokens": usage("input_tokens"),
            "output_tokens": usage("output_tokens"),
            "cache_read_tokens": usage("cache_read_input_tokens"),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for s in tools if s.status == STATUS_ERROR),
            "tool_pending": sum(1 for s in tools if s.status == STATUS_PENDING),
            "errors": sum(1 for s in self.all_spans() if s.kind == KIND_ERROR),
            "slowest_tool": (
                {"name": slowest.name, "duration_ms": slowest.duration_ms}
                if slowest
                else None
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["summary"] = self.summary()
        return data
