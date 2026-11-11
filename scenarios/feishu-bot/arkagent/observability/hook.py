"""把 SessionDocSink 接到通用 Gateway 的 ``after_turn`` 钩子上。

放在通用层,各 case 的 build_gateway 只需一行:
    Gateway(..., after_turn=make_after_turn(ark, sink))
未配置文件夹时返回 None,Gateway 行为与以前完全一致。
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable, Optional

from .docsink import DocStore, SessionDocSink
from .service import EventSource

ENV_PARENT_TOKEN = "SESSION_DOC_PARENT_TOKEN"  # 目标文件夹 token;空 = 不落文档
ENV_DB = "SESSION_DOC_DB_PATH"


def make_after_turn(
    events: EventSource,
    *,
    case_name: str,
    db_path: str,
    env: Optional[dict] = None,
) -> Optional[Callable[[str, str], Awaitable[None]]]:
    environ = os.environ if env is None else env
    parent = (environ.get(ENV_PARENT_TOKEN) or "").strip()
    if not parent:
        return None
    sink = SessionDocSink(DocStore(environ.get(ENV_DB) or db_path), parent)

    async def after_turn(session_id: str, user_open_id: str) -> None:
        records = await events.list_session_events(session_id)
        await sink.publish(session_id, records, title=f"[{case_name}] {session_id} 执行日志")

    return after_turn
