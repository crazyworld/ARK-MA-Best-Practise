"""diagnose case 的 Gateway:收到消息 → 绑定当前 chat_id → 让排障 Agent 调只读工具作答。

MVP:只开放群聊(私聊没有稳定的跨应用 user_id,无法判断\"自己名下\",暂不开放)。
范围规则:群聊内可排查本群全部任务,不区分发起人。
"""
from __future__ import annotations

import asyncio
import json
import logging

from arkagent.feishu import IncomingMessage
from arkagent.gateway.orchestrator import KeyedQueue, result_to_reply
from arkagent.observability.adapters.sqlite_sources import PipelineJobSource, TopicxSource
from arkagent.paths import get_case_paths
from arkagent.store import GatewayStore

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
    return sources


class DiagnoseGateway:
    def __init__(self, config: DiagnoseConfig, store: GatewayStore, ark, reply, loop, tools: DiagnoseTools) -> None:
        self._config = config
        self._store = store
        self._ark = ark
        self._reply = reply
        self._loop = loop
        self._tools = tools
        self._queue = KeyedQueue()
        self._authorized = set(config.authorized_open_ids)

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

    tools = DiagnoseTools(build_sources(config.db_paths), ark)
    return DiagnoseGateway(config, store, ark, reply, loop, tools)
