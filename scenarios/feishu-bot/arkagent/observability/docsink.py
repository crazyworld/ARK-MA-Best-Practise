"""把 session 的执行日志渲染成飞书文档并增量追加,文档可直接交付用户。

定位:**交付视图**,不是分析依据——写入可能失败或缺段,分析仍以 Trace(事件 + 库)为准。

设计要点:
- 写入失败永远不影响主流程:调用方 ``await sink.publish(...)`` 不会抛异常,失败只记日志。
- 幂等:每个 session 的\"已写到哪个事件\"记在本机 SQLite(同机部署,同一 session 只有一个
  gateway 进程持有),重放或重启不会重复追加。
- 只追加:正文用 ``docs +update --command append``;摘要区不覆写,避免依赖 block id。
- 权限继承自父节点:本模块不调用任何\"加协作者\"接口(那是 high-risk 写操作,应由人事先配好)。
- 以**业务应用自己的身份**写(用 gateway 进程里已有的 app_id / app_secret 经 SDK 直调 OpenAPI),
  不依赖 lark-cli:lark-cli 只绑定一个应用,它的 bot 身份不是业务应用,给业务应用加的权限它用不上。
- 密钥在渲染时脱敏(规则见 redact.py);正文其余内容原样呈现,与\"除密钥外均可见\"的决策一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .events import event_text, ts_ms
from .redact import redact_text, redact_value

log = logging.getLogger("arkagent.observability.docsink")

MAX_BODY_CHARS = 3000  # 单个事件正文在文档里的上限,超出截断并标注原长度
API_TIMEOUT_S = 60
# 进入文档的事件类型;thinking、usage、状态类事件体量大且对交付无价值。
_RENDERED = (
    "user.message",
    "agent.message",
    "agent.tool_use",
    "agent.tool_result",
    "session.error",
    "span.model_request_end",
    "agent.thread_message_sent",
    "agent.thread_message_received",
)

# (method, path, body) -> 响应 JSON;可注入,测试不起真请求。
Api = Callable[[str, str, dict], Awaitable[dict]]
CREATE_PATH = "/open-apis/docs_ai/v1/documents"


def make_sdk_api(app_id: str, app_secret: str) -> Api:
    """用业务应用自己的 tenant token 直调 OpenAPI;同步 SDK 丢线程池,不阻塞事件循环。"""
    import lark_channel as lark
    from lark_channel import AccessTokenType, BaseRequest, HttpMethod

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def _call(method: str, path: str, body: dict) -> dict:
        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.POST if method == "POST" else HttpMethod.PUT)
            .uri(path)
            .token_types({AccessTokenType.TENANT})
            .body(body)
            .build()
        )
        response = client.request(request)
        raw = response.raw.content if getattr(response, "raw", None) else None
        payload = json.loads(raw) if raw else {}
        if not response.success():
            # 缺权限等错误带着飞书给的原因,直接抛出,由 publish 记日志;不吞掉也不重试。
            raise RuntimeError(f"飞书接口失败 code={response.code}: {str(response.msg)[:300]}")
        return payload

    async def api(method: str, path: str, body: dict) -> dict:
        return await asyncio.wait_for(asyncio.to_thread(_call, method, path, body), API_TIMEOUT_S)

    return api


@dataclass(frozen=True)
class DocRecord:
    session_id: str
    doc_token: str
    doc_url: str
    last_event_id: str
    last_event_ms: int


class DocStore:
    """session → 文档 的映射与写入进度。本机 SQLite。"""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS session_docs (
                session_id TEXT PRIMARY KEY,
                doc_token TEXT NOT NULL,
                doc_url TEXT NOT NULL DEFAULT '',
                last_event_id TEXT NOT NULL DEFAULT '',
                last_event_ms INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._db.commit()

    def get(self, session_id: str) -> Optional[DocRecord]:
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, doc_token, doc_url, last_event_id, last_event_ms "
                "FROM session_docs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return DocRecord(*row) if row else None

    def create(self, session_id: str, doc_token: str, doc_url: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO session_docs (session_id, doc_token, doc_url, updated_at) VALUES (?,?,?,?)",
                (session_id, doc_token, doc_url, int(time.time())),
            )
            self._db.commit()

    def advance(self, session_id: str, event_id: str, event_ms: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE session_docs SET last_event_id=?, last_event_ms=?, updated_at=? WHERE session_id=?",
                (event_id, event_ms, int(time.time()), session_id),
            )
            self._db.commit()


def _clip(text: str) -> str:
    text = redact_text(text)
    if len(text) <= MAX_BODY_CHARS:
        return text
    return f"{text[:MAX_BODY_CHARS]}\n…(已截断,原文共 {len(text)} 字符)"


def _fence(text: str) -> str:
    # 正文里自带 ``` 时用更长的围栏,避免提前闭合。
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}\n{text}\n{ticks}"


def render_events(events: list[dict]) -> tuple[str, str, int]:
    """渲染事件为 Markdown。返回 (文本, 最后一个事件 id, 最后一个事件时间 ms);无可写内容时文本为空。"""
    tool_names: dict[str, str] = {}
    blocks: list[str] = []
    last_id, last_ms = "", 0
    for event in events:
        etype = event.get("type")
        if etype not in _RENDERED:
            continue
        at = time.strftime("%H:%M:%S", time.localtime(ts_ms(event.get("processed_at")) / 1000))
        eid = str(event.get("id") or "")
        if etype == "user.message":
            blocks.append(f"### {at} 用户\n\n{_clip(event_text(event))}")
        elif etype == "agent.message":
            blocks.append(f"### {at} Agent\n\n{_clip(event_text(event))}")
        elif etype == "agent.tool_use":
            tool_names[eid] = str(event.get("name") or "")
            tool_input = event.get("input")
            body = tool_input.get("command") if isinstance(tool_input, dict) and "command" in tool_input else json.dumps(
                redact_value(tool_input), ensure_ascii=False, indent=2
            )
            blocks.append(f"#### {at} 调用工具 `{event.get('name')}`\n\n{_fence(_clip(str(body)))}\n\n`event_id: {eid}`")
        elif etype == "agent.tool_result":
            name = tool_names.get(str(event.get("tool_use_id")), "")
            flag = " ⚠️ 返回错误" if event.get("is_error") else ""
            blocks.append(f"#### {at} 工具结果 `{name}`{flag}\n\n{_fence(_clip(event_text(event)))}")
        elif etype == "session.error":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            blocks.append(
                f"### {at} ❌ 错误 `{err.get('type', '')}`\n\n{_clip(str(err.get('message', '')))}"
                f"(重试状态:{(err.get('retry_status') or {}).get('type', '-')})\n\n`event_id: {eid}`"
            )
        elif etype == "span.model_request_end":
            if event.get("is_error"):
                blocks.append(f"#### {at} ⚠️ 模型调用失败\n\n`event_id: {eid}`")
            else:
                continue  # 成功的模型调用只有 token 数,放进文档只会淹没正文
        else:
            blocks.append(f"#### {at} 子 Agent 消息\n\n{_clip(event_text(event))}")
        last_id, last_ms = eid, ts_ms(event.get("processed_at"))
    return "\n\n".join(blocks), last_id, last_ms


class SessionDocSink:
    def __init__(
        self,
        store: DocStore,
        parent_token: str,
        api: Api,
    ) -> None:
        self._store = store
        self._parent = parent_token
        self._api = api
        # 同一 session 的追加必须串行,否则并发追加会乱序。
        self._locks: dict[str, asyncio.Lock] = {}

    async def publish(self, session_id: str, events: list[dict], *, title: str) -> Optional[str]:
        """把 ``events`` 里尚未写入的部分追加到该 session 的文档;返回文档链接。**不抛异常**。"""
        try:
            return await self._publish(session_id, events, title)
        except Exception as error:  # noqa: BLE001 - 文档只是交付视图,不得影响主流程
            log.warning("session 文档写入失败 session=%s: %s", session_id, error)
            return None

    async def _publish(self, session_id: str, events: list[dict], title: str) -> Optional[str]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            record = self._store.get(session_id)
            ordered = sorted((e for e in events if isinstance(e, dict)), key=lambda e: ts_ms(e.get("processed_at")))
            pending = _after(ordered, record)
            text, last_id, last_ms = render_events(pending)
            if not text:
                return record.doc_url if record else None
            if record is None:
                record = await self._create(session_id, title, text)
                # 创建时正文已一并写入
            else:
                await self._append(record.doc_token, text)
            self._store.advance(session_id, last_id, last_ms)
            return record.doc_url

    async def _create(self, session_id: str, title: str, first_body: str) -> DocRecord:
        body: dict = {"content": f"<title>{_escape_title(title)}</title>\n{first_body}", "format": "markdown"}
        if self._parent:
            body["parent_token"] = self._parent
        out = await self._api("POST", CREATE_PATH, body)
        document = (out.get("data") or {}).get("document") or out.get("document") or {}
        token = str(document.get("document_id") or document.get("doc_token") or "")
        url = str(document.get("url") or "")
        if not token:
            raise RuntimeError(f"创建文档成功但没有返回文档 id:{str(out)[:200]}")
        self._store.create(session_id, token, url)
        return DocRecord(session_id, token, url, "", 0)

    async def _append(self, token: str, text: str) -> None:
        # block_id=-1 即文末;revision_id=-1 取最新版本,不依赖读文档。
        await self._api(
            "PUT",
            f"{CREATE_PATH}/{token}",
            {"command": "block_insert_after", "block_id": "-1", "content": text, "format": "markdown", "revision_id": -1},
        )


def _escape_title(title: str) -> str:
    return title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _after(ordered: list[dict], record: Optional[DocRecord]) -> list[dict]:
    """取 record 之后的事件。优先按事件 id 定位;找不到(被清理)时按时间,避免重复追加。"""
    if record is None or not record.last_event_id:
        return ordered
    for index, event in enumerate(ordered):
        if event.get("id") == record.last_event_id:
            return ordered[index + 1:]
    return [e for e in ordered if ts_ms(e.get("processed_at")) > record.last_event_ms]
