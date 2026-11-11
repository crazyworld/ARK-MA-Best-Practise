"""取某一步的输入输出正文。

Trace 里只放摘要;用户追问\"这一步的输入输出是什么\"时才回源取原事件,按事件 id 只取一条。
正文可见,但密钥必须脱敏(规则见 redact.py);过长内容截断并标注原长度,避免把整段灌进模型。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .events import event_text
from .redact import redact_value
from .service import EventSource

MAX_DETAIL_CHARS = 4000


def _clip(text: str) -> str:
    if len(text) <= MAX_DETAIL_CHARS:
        return text
    return f"{text[:MAX_DETAIL_CHARS]}\n…(已截断,原文共 {len(text)} 字符)"


def _render_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


async def get_step_detail(
    source: EventSource, session_id: str, event_id: str
) -> Optional[dict]:
    """event_id 为 tool_use 的 id 时,返回该调用的输入与对应结果;为 agent.message 等时返回正文。

    找不到返回 None。结果已脱敏、已截断。
    """
    events = await source.list_session_events(session_id)
    by_id = {e.get("id"): e for e in events if isinstance(e, dict)}
    target = by_id.get(event_id)
    if target is None:
        return None
    etype = target.get("type")

    if etype == "agent.tool_use":
        result = next(
            (
                e
                for e in events
                if e.get("type") == "agent.tool_result" and e.get("tool_use_id") == event_id
            ),
            None,
        )
        return {
            "kind": "tool_call",
            "event_id": event_id,
            "tool": target.get("name"),
            "input": _clip(_render_input(redact_value(target.get("input")))),
            "output": _clip(redact_value(event_text(result))) if result else None,
            "is_error": bool(result.get("is_error")) if result else None,
            "finished": result is not None,
        }
    if etype in ("agent.message", "user.message", "agent.thinking", "agent.thread_message_sent", "agent.thread_message_received"):
        return {
            "kind": etype,
            "event_id": event_id,
            "text": _clip(redact_value(event_text(target))),
        }
    if etype == "session.error":
        return {"kind": etype, "event_id": event_id, "error": redact_value(target.get("error"))}
    return {"kind": etype, "event_id": event_id, "text": _clip(redact_value(event_text(target)))}
