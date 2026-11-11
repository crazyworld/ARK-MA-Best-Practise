"""adapter 的统一契约:每个 topic 把自己的表映射成 TraceRef,其余逻辑共用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ..model import Span


@dataclass
class SessionRef:
    session_id: str
    role: str  # router | business


@dataclass
class TraceRef:
    """一次任务在数据库侧的全部已知信息;事件流由 service 按 sessions 再去拉。"""

    trace_id: str
    root_kind: str  # task | job
    scenario: str
    status: str
    started_at_ms: int
    finished_at_ms: Optional[int] = None
    last_error: str = ""
    # 任务所在的飞书会话(群或私聊);排障 Bot 据此限定"只能看当前群"。
    chat_id: str = ""
    sessions: list[SessionRef] = field(default_factory=list)
    db_spans: list[Span] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class TraceSource(Protocol):
    def resolve(self, ident: str) -> Optional[TraceRef]: ...

    def list_refs(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> list[TraceRef]: ...
