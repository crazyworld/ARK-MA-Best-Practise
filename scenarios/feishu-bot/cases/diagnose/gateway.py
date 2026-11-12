"""diagnose case 的 Gateway:收到消息 → 绑定当前 chat_id → 让排障 Agent 调只读工具作答。

MVP:只开放群聊(私聊没有稳定的跨应用 user_id,无法判断\"自己名下\",暂不开放)。
范围规则:群聊内可排查本群全部任务,不区分发起人。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from arkagent.feishu import IncomingMessage
from arkagent.gateway.orchestrator import KeyedQueue, result_to_reply
from arkagent.observability.docscheduler import DocScheduler, SyncConfig, run_forever
from arkagent.observability.docsink import DocStore, SessionDocSink, make_sdk_api
from arkagent.observability.adapters.sqlite_sources import (
    ConversationSource,
    DigitalEmployeeSource,
    PipelineJobSource,
    TopicxSource,
)
from arkagent.paths import get_case_paths
from arkagent.store import GatewayStore

from arkagent.preflight import UserIdWatcher

from .config import DiagnoseConfig
from .tools import DiagnoseTools, Scope

log = logging.getLogger("arkagent.case.diagnose")



def build_sources(db_paths: dict) -> dict:
    jobs = {
        name: PipelineJobSource(name, path)
        for name, path in db_paths.items()
        if name in ("topic5", "topic6")
    }
    sources: dict = {}
    if "topicx" in db_paths:
        # topicx 在前:task 视图包含 router session,信息最全。
        sources["topicx"] = TopicxSource(db_paths["topicx"], jobs)
    sources.update(jobs)
    for name in ("analyst", "ada-pm"):  # 通用 Gateway:会话 → session
        if name in db_paths:
            sources[name] = ConversationSource(name, db_paths[name])
    if "digital-employee" in db_paths:
        sources["digital-employee"] = DigitalEmployeeSource("digital-employee", db_paths["digital-employee"])
    return sources


class DiagnoseGateway:
    def __init__(
        self, config: DiagnoseConfig, store: GatewayStore, ark, reply, loop, tools: DiagnoseTools,
        doc_scheduler: Optional[DocScheduler] = None,
    ) -> None:
        self._doc_scheduler = doc_scheduler
        self._config = config
        self._store = store
        self._ark = ark
        self._reply = reply
        self._loop = loop
        self._tools = tools
        self._queue = KeyedQueue()
        self._authorized = set(config.authorized_open_ids)
        self._watcher = UserIdWatcher(config.feishu_app_id)

    def start_background(self) -> None:
        """由 cli 在事件循环启动后调用:起文档同步的后台循环(未启用则什么都不做)。"""
        if self._doc_scheduler is None:
            return
        interval = self._config.doc_sync_interval_s
        asyncio.run_coroutine_threadsafe(run_forever(self._doc_scheduler, interval), self._loop)
        log.info("session 文档同步已启动 interval=%ss lookback=%sd", interval, self._config.doc_lookback_days)

    def accept(self, message: IncomingMessage) -> bool:
        if not message.text.strip():
            return False
        # 私聊直接响应;群聊必须 @ 了 bot。
        if message.chat_type != "p2p" and not message.mentioned_bot:
            return False
        if not self._store.claim_event(message.event_id):
            return False
        # 同一个会话内串行,避免并发问答互相覆盖同一个排障会话。
        self._loop.call_soon_threadsafe(
            lambda: self._queue.enqueue(_queue_key(message), lambda: self._run(message))
        )
        return True

    async def _run(self, message: IncomingMessage) -> None:
        try:
            await self._process(message)
            self._store.complete_event(message.event_id, "completed")
        except Exception as error:  # noqa: BLE001
            self._store.complete_event(message.event_id, "failed")
            log.exception("diagnose failed chat=%s event=%s", message.chat_id, message.event_id)
            await self._reply(message.chat_id, f"排查失败:{str(error)[:200]}")

    async def _process(self, message: IncomingMessage) -> None:
        if self._authorized and message.user_open_id not in self._authorized:
            await self._reply(message.chat_id, "当前用户未授权,请联系管理员。")
            return
        self._watcher.observe(message.user_id, message.chat_type)
        chat_id = message.chat_id
        # 范围只取自飞书消息本身,由网关绑定;工具参数里没有这些字段,模型无法改。
        scope = Scope(chat_id=chat_id, chat_type=message.chat_type, user_id=message.user_id)

        async def handle_tool(name: str, arguments: dict) -> tuple[str, bool]:
            result, is_error = await self._tools.call(scope, name, arguments)
            return json.dumps(result, ensure_ascii=False), is_error

        # 每个群一个排障会话;/new 之外不主动重置,上下文里保留着此前识别的任务,便于追问。
        key = _key(message)
        session_id = self._store.get_session(key)
        if not session_id:
            session_id = await self._ark.create_session(
                self._config.agent_id,
                self._config.environment_id,
                title=f"diagnose/{chat_id}",
            )
            self._store.save_session(key, session_id, self._config.agent_id)
        result = await self._ark.run(
            session_id,
            message.text,
            self._config.session_timeout_ms,
            custom_tool_handler=handle_tool,
        )
        await self._reply(chat_id, result_to_reply(result))


def _queue_key(message: IncomingMessage) -> str:
    # 私聊按人串行,群聊按群串行。
    return f"p2p:{message.user_id or message.user_open_id}" if message.chat_type == "p2p" else message.chat_id


def _key(message: IncomingMessage):
    from arkagent.store import ConversationKey

    # 群聊:群内共享一个排障会话(不带用户,与"群聊内皆共享"一致)。
    # 私聊:按人一个排障会话,避免不同人的私聊上下文串在一起。
    user = (message.user_id or message.user_open_id) if message.chat_type == "p2p" else ""
    return ConversationKey(message.tenant_key, message.chat_id, message.thread_id, user)


def build_gateway(config: DiagnoseConfig, ark, sender, loop: asyncio.AbstractEventLoop):
    case_paths = get_case_paths("diagnose")
    store = GatewayStore(case_paths.database_path)

    async def reply(chat_id: str, text: str) -> None:
        await loop.run_in_executor(None, sender.send_to_chat, chat_id, text)

    sources = build_sources(config.db_paths)
    tools = DiagnoseTools(sources, ark)
    scheduler = None
    if config.doc_parent_token:
        # 以排障应用自己的身份写;所有 bot 的 session 日志都落在同一个父节点下(MVP 不做隔离,见 docscheduler)。
        doc_store = DocStore(config.doc_db_path or f"{case_paths.case_dir}/session_docs.db")
        sink = SessionDocSink(doc_store, config.doc_parent_token, make_sdk_api(config.feishu_app_id, config.feishu_app_secret))
        async def fetch(path: str) -> dict:  # incremental 的 Fetch:GET 一个 path 返回 JSON
            return await ark._request("GET", path)

        scheduler = DocScheduler(
            sources, fetch, sink, doc_store,
            SyncConfig(
                interval_s=config.doc_sync_interval_s,
                lookback_days=config.doc_lookback_days,
                max_per_cycle=config.doc_max_per_cycle,
            ),
        )
    return DiagnoseGateway(config, store, ark, reply, loop, tools, scheduler)
