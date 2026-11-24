"""把 TraceRef(数据库侧)与 MA 事件流(方舟侧)组装成 Trace。"""
from __future__ import annotations

import asyncio
from typing import Optional, Protocol

from .adapters.base import SessionRef, TraceRef
from .events import parse_session_events
from .model import SessionNode, Trace
from .redact import redact_text


class EventSource(Protocol):
    """``ArkClient`` 满足该协议。"""

    async def list_session_events(self, session_id: str, limit: int = 200) -> list[dict]: ...


async def _load_session(
    ref: SessionRef, source: Optional[EventSource], redact: bool
) -> SessionNode:
    if source is None:
        return SessionNode(
            session_id=ref.session_id,
            role=ref.role,
            available=False,
            unavailable_reason="未拉取事件流(--no-events)",
        )
    try:
        events = await source.list_session_events(ref.session_id)
    except Exception as error:  # noqa: BLE001 - 单个 session 失败不应让整条 trace 不可用
        # 方舟在 session 14 天无新事件后会清理它,此时 404 是预期内的降级。
        reason = (
            "方舟侧已无该 session(超过保留期被清理,或 ID 不存在)"
            if getattr(error, "status_code", None) == 404
            else f"拉取事件失败:{error}"
        )
        return SessionNode(
            session_id=ref.session_id, role=ref.role, available=False, unavailable_reason=reason
        )
    return parse_session_events(
        events, session_id=ref.session_id, role=ref.role, redact=redact
    )


async def build_trace(
    ref: TraceRef, source: Optional[EventSource], *, redact: bool = True
) -> Trace:
    sessions = await asyncio.gather(
        *(_load_session(s, source, redact) for s in ref.sessions)
    )
    return Trace(
        trace_id=ref.trace_id,
        root_kind=ref.root_kind,
        scenario=ref.scenario,
        status=ref.status,
        started_at_ms=ref.started_at_ms,
        finished_at_ms=ref.finished_at_ms,
        last_error=redact_text(ref.last_error) if redact else ref.last_error,
        sessions=list(sessions),
        db_spans=ref.db_spans,
        meta=ref.meta,
    )
