"""topic6 case 的 Gateway:飞书触发词 → 长任务 Runner + HITL 卡片回调。

只处理 topic6 场景的入站消息与卡片回调;不涉及数字员工的 /new /role /remember 指令。
非触发词消息(以及未鉴权用户)给出用法提示。
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from arkagent.feishu import IncomingMessage
from arkagent.store import GatewayStore

from artifact_sync import Topic6ArtifactSync
from feishu_doc_publisher import FeishuDocPublisher
from job_notifier import JobNotifier
from pipeline_store import (
    STATUS_RUNNING,
    STATUS_WAIT_HC,
    PipelineJob,
    PipelineStore,
)
from topic6_hitl import Topic6Hitl, Topic6HitlDeps
from topic6_runner import (
    Topic6Config as RunnerConfig,
    Topic6Runner,
    Topic6RunnerError,
    normalize_user_text,
    parse_trigger,
)
from topic6_user_oauth import (
    FeishuOAuth,
    Topic6UserAuthorization,
    UserAuthorizationRequired,
)

from config import Topic6Config

log = logging.getLogger("arkagent.case.topic6")
TOKEN_KEEPALIVE_INTERVAL_S = 60.0
# in-chat 授权等待扫码的最大秒数；超过后取消 poll，用户需重新发送触发词。
IN_CHAT_AUTH_TIMEOUT_S = 300.0
CONTINUE_COMMAND_RE = re.compile(r"^/(?:continue|resume)(?:\s+(.*))?$", re.IGNORECASE)
LOG_COMMAND_RE = re.compile(r"^/log(?:\s+(\S+))?$", re.IGNORECASE)


class Topic6Gateway:
    """飞书 gateway-like 对象:实现 accept + on_card_action。"""

    def __init__(
        self,
        config: Topic6Config,
        store: GatewayStore,
        runner: Topic6Runner,
        hitl: Topic6Hitl,
        user_authorization: Topic6UserAuthorization,
        reply,
        loop: asyncio.AbstractEventLoop,
        pipeline_store: PipelineStore | None = None,
        artifact_sync: Topic6ArtifactSync | None = None,
        publisher: FeishuDocPublisher | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._runner = runner
        self._hitl = hitl
        self._user_authorization = user_authorization
        self._reply = reply
        self._loop = loop
        self._pipeline_store = pipeline_store
        self._artifact_sync = artifact_sync
        self._publisher = publisher
        self._authorized = set(config.authorized_open_ids)
        self._token_keepalive_tasks: dict[str, asyncio.Task] = {}
        # 每个 open_id 同时只允许一个 in-chat 授权流程
        self._pending_authorizations: dict[str, asyncio.Task] = {}

    def accept(self, message: IncomingMessage) -> bool:
        if not _should_handle_message(message):
            return False
        if not self._store.claim_event(message.event_id):
            return False
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._process(message))
        )
        return True

    def on_card_action(self, action: object) -> bool:
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._hitl.handle_card_action(action))
        )
        return True

    async def recover_active_jobs(self) -> None:
        """Gateway 启动后把本地活跃任务与 MA 历史事件对齐。

        恢复不会发送 ``/continue`` 或任意 user.message：MA 尚在运行时只重建 SSE
        订阅；已到 HC 时重建同一张待审卡；已终态时更新本地 SQLite 与进度卡。
        """
        # [P0-2] 启动时初始化飞书共享文件夹权限（幂等）
        if self._publisher is not None and self._publisher.enabled:
            try:
                await self._loop.run_in_executor(
                    None,
                    self._publisher.init_folder_permissions,
                    self._config.authorized_open_ids,
                )
            except Exception as err:  # noqa: BLE001
                log.warning("topic6 init docs folder permissions failed: %s", err)

        if self._pipeline_store is None:
            return
        active_jobs = self._pipeline_store.list_active_jobs()
        if not active_jobs:
            return

        log.info("topic6 startup recovery begins active_jobs=%s", len(active_jobs))
        results = await self._runner.recover_active_jobs()
        for job in active_jobs:
            current = self._pipeline_store.get_job(job.job_id)
            if current is None or current.status not in (STATUS_RUNNING, STATUS_WAIT_HC):
                continue
            if self._artifact_sync is not None:
                self._artifact_sync.start(current, self._loop)
            if getattr(self._config, "skip_miaoda_oauth", False):
                continue
            try:
                vault_id = await self._user_authorization.vault_id(current.user_open_id)
                self._start_token_keepalive(current, vault_id)
            except Exception as error:  # noqa: BLE001 - 过期授权不应阻断 MA 状态恢复
                log.warning(
                    "topic6 recovery token keepalive unavailable job=%s: %s",
                    current.job_id,
                    error,
                )
        log.info("topic6 startup recovery completed results=%s", results)

    async def _process(self, message: IncomingMessage) -> None:
        try:
            if self._authorized and message.user_open_id not in self._authorized:
                await self._reply(message.chat_id, "当前用户未授权。请联系管理员把你的 open_id 加入白名单。")
                self._store.complete_event(message.event_id, "completed")
                return

            text = normalize_user_text(message.text, message.mentioned_bot)

            if text == "/new":
                cancelled = await self._runner.cancel_active_job(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    user_open_id=message.user_open_id,
                )
                if cancelled is None:
                    await self._reply(
                        message.chat_id,
                        "[/new] 收到。当前会话没有活跃任务,可发送「热点周报 demo」「热点周报 full」或「热点周报 skip_sampling」开新一轮。",
                    )
                else:
                    self._stop_token_keepalive(cancelled.job_id)
                    if self._artifact_sync is not None:
                        self._artifact_sync.stop(cancelled.job_id)
                        try:
                            await self._artifact_sync.sync_once(
                                cancelled.job_id, cancelled.ma_session_id
                            )
                        except Exception as error:  # noqa: BLE001
                            log.warning(
                                "topic6 final artifact sync failed job=%s: %s",
                                cancelled.job_id,
                                error,
                            )
                    await self._reply(
                        message.chat_id,
                        f"[/new] 已取消当前任务 job_id={cancelled.job_id}(状态置为 failed:cancelled_by_user)。可再发触发词开新一轮。",
                    )
                self._store.complete_event(message.event_id, "completed")
                return

            continue_match = CONTINUE_COMMAND_RE.fullmatch(text)
            if continue_match:
                continuation_message = (continue_match.group(1) or "").strip() or "继续"
                resumed = await self._runner.continue_job(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    user_open_id=message.user_open_id,
                    message=continuation_message,
                )
                if resumed is None:
                    await self._reply(
                        message.chat_id,
                        "[/continue] 当前会话没有可续跑的任务。若要开新一轮，请发送「热点周报 demo」「热点周报 full」或「热点周报 skip_sampling」。",
                    )
                else:
                    message_hint = (
                        "补充消息已注入。"
                        if continue_match.group(1) and continue_match.group(1).strip()
                        else "已注入「继续」。"
                    )
                    await self._reply(
                        message.chat_id,
                        f"[/continue] {message_hint}已重新订阅进度 job_id={resumed.job_id}。若该 Session 已在方舟侧终止，稍后进度卡片会回落为失败。",
                    )
                self._store.complete_event(message.event_id, "completed")
                return

            log_match = LOG_COMMAND_RE.fullmatch(text)
            if log_match:
                await self._handle_log_command(
                    message.chat_id, message.user_open_id, log_match.group(1),
                )
                self._store.complete_event(message.event_id, "completed")
                return

            if await self._hitl.handle_remark_message(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                user_open_id=message.user_open_id,
                text=text,
            ):
                self._store.complete_event(message.event_id, "completed")
                return

            mode = parse_trigger(text)
            if mode is None:
                await self._reply(
                    message.chat_id,
                    "当前 Bot 仅启用 topic6 场景。发送「热点周报 demo」(50 条出报告)、「热点周报 full」(先 500 条校准再跑全量)或「热点周报 skip_sampling」(跳过 500 条采样,直接跑全量)触发;发送 /continue 续跑最近一次任务;发送 /new 可取消当前任务;发送 /log 查看当前任务日志，或 /log <session_id> 查看指定 Session 日志。",
                )
                self._store.complete_event(message.event_id, "completed")
                return

            try:
                if getattr(self._config, "skip_miaoda_oauth", False):
                    user_vault_id = ""
                    log.warning(
                        "topic6 user OAuth bypassed for temporary run user=%s; "
                        "Miaoda publishing is expected to fail without another credential",
                        message.user_open_id,
                    )
                else:
                    user_vault_id = await self._ensure_user_authorized(
                        message.chat_id, message.user_open_id
                    )
                job = await self._runner.start_job(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    user_open_id=message.user_open_id,
                    mode=mode,
                    user_message=text,
                    user_vault_id=user_vault_id,
                )
                # [P0-2] 创建 job 飞书子文件夹（继承根文件夹权限）
                if self._publisher is not None and self._publisher.enabled:
                    try:
                        folder_token = await self._loop.run_in_executor(
                            None, self._publisher.create_job_folder, job,
                        )
                        if folder_token and self._pipeline_store:
                            self._pipeline_store.update_docs_folder_token(job.job_id, folder_token)
                            job.docs_folder_token = folder_token
                    except Exception as err:  # noqa: BLE001
                        log.warning("topic6 create job docs folder failed: %s", err)
                if user_vault_id:
                    self._start_token_keepalive(job, user_vault_id)
                if self._artifact_sync is not None:
                    self._artifact_sync.start(job, self._loop)
            except (Topic6RunnerError, UserAuthorizationRequired) as error:
                await self._reply(message.chat_id, str(error))
            self._store.complete_event(message.event_id, "completed")
        except Exception as error:  # noqa: BLE001
            self._store.complete_event(message.event_id, "failed")
            # P0-1: 结构化错误信息，替代原来的纯文本截断。
            reason = str(error)[:300]
            phase_hint = ""
            session_hint = ""
            if hasattr(self, "_pipeline_store") and self._pipeline_store:
                active = self._pipeline_store.get_active_job()
                if active:
                    phase_hint = f"（job={active.job_id}, phase={active.current_phase}）"
                    session_hint = f"\nsession_id: {active.ma_session_id}（可用 /log {active.ma_session_id} 查看日志）"
            await self._reply(
                message.chat_id,
                f"❌ 执行异常{phase_hint}：{reason}{session_hint}\n"
                f"建议：发送 /continue 尝试恢复，或 /new 取消当前任务重新开始。",
            )

    async def _handle_log_command(
        self, chat_id: str, user_open_id: str, session_id_arg: str | None,
    ) -> None:
        """/log [session_id]: 获取 MA Session 执行日志。

        不带参数时使用当前活跃任务或最近一个任务的 session_id。
        """
        if not self._pipeline_store:
            await self._reply(chat_id, "[/log] Pipeline store 未初始化。")
            return

        # 解析 session_id
        session_id = (session_id_arg or "").strip()
        source_job = None
        if not session_id:
            # 无参数：从当前活跃任务或最近任务取
            source_job = self._pipeline_store.get_active_job()
            if source_job is None:
                jobs = self._pipeline_store.list_jobs(limit=1)
                source_job = jobs[0] if jobs else None
            if source_job is None:
                await self._reply(chat_id, "[/log] 没有找到任何任务记录。请指定 session_id：`/log <session_id>`")
                return
            session_id = source_job.ma_session_id
        else:
            # 有参数：先检查是否是 job_id
            source_job = self._pipeline_store.get_job(session_id)
            if source_job:
                session_id = source_job.ma_session_id
            else:
                source_job = self._pipeline_store.get_job_by_ma_session(session_id)

        job_hint = f"job={source_job.job_id} " if source_job else ""
        await self._reply(chat_id, f"[/log] 正在获取 {job_hint}session={session_id} 的执行日志…")

        try:
            from arkagent.ark import ArkClient
            ark: ArkClient = self._runner._ark  # noqa: SLF001
            events = await ark.list_session_events(session_id)
        except Exception as err:
            await self._reply(chat_id, f"[/log] 获取日志失败：{err!s}"[:500])
            return

        if not events:
            await self._reply(chat_id, f"[/log] session={session_id} 无事件记录。")
            return

        # 格式化事件为可读文本
        # 方舟事件结构：event["type"] + event["content"] (list of {type, text}) + event["name"] (tool_use)
        from arkagent.ark import event_text as _event_text

        lines: list[str] = [f"📋 Session 日志 ({len(events)} 条事件)"]
        lines.append(f"session_id: {session_id}")
        if source_job:
            lines.append(f"job_id: {source_job.job_id} · mode: {source_job.mode} · phase: {source_job.current_phase}")
        lines.append("---")
        shown = 0
        MAX_LOG_ITEMS = 80  # 飞书消息字数有限，控制条目数
        for event in events:
            if shown >= MAX_LOG_ITEMS:
                lines.append(f"\n…（已展示 {shown} 条，共 {len(events)} 条事件，后续省略）")
                break
            etype = event.get("type", "")
            # agent.message → 用 event_text 提取（与 SSE 消费一致）
            if etype == "agent.message":
                text = _event_text(event)
                if not text:
                    continue  # 跳过空消息
                preview = (text[:150] + "…") if len(text) > 150 else text
                lines.append(f"🤖 {preview}")
                shown += 1
            elif etype == "agent.tool_use":
                # event["name"] 是工具名，event["input"]["description"] 是简述
                name = event.get("name") or "unknown_tool"
                tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
                desc = str(tool_input.get("description") or "").strip()
                if desc:
                    lines.append(f"🔧 {name}: {desc[:100]}")
                else:
                    lines.append(f"🔧 {name}")
                shown += 1
            elif etype == "agent.tool_result":
                # 只展示出错的 tool_result
                if event.get("is_error") is True:
                    text = _event_text(event)
                    preview = (text[:100] + "…") if len(text) > 100 else (text or "(无详情)")
                    lines.append(f"❌ 工具出错: {preview}")
                    shown += 1
            elif etype == "user.message":
                text = _event_text(event)
                if not text:
                    continue
                preview = (text[:100] + "…") if len(text) > 100 else text
                lines.append(f"👤 {preview}")
                shown += 1
            elif etype in ("session.status_idle", "session.status_failed",
                           "session.status_terminated", "session.error",
                           "user.interrupt"):
                error_info = event.get("error")
                if isinstance(error_info, dict):
                    error_msg = error_info.get("message") or error_info.get("type") or ""
                elif isinstance(error_info, str):
                    error_msg = error_info
                else:
                    error_msg = ""
                if error_msg:
                    lines.append(f"⚠️ {etype}: {str(error_msg)[:150]}")
                else:
                    lines.append(f"📌 {etype}")
                shown += 1

        # 飞书消息有字数限制，超过则截断
        full_text = "\n".join(lines)
        if len(full_text) > 4000:
            full_text = full_text[:3900] + f"\n\n…（共 {len(events)} 条事件，已截断）"
        await self._reply(chat_id, full_text)

    async def _ensure_user_authorized(
        self, chat_id: str, user_open_id: str
    ) -> str:
        """获取用户 vault_id；若未授权则在聊天中发授权链接并等待扫码。

        防护措施：
        - 并发去重：同一 open_id 同时只有一个 Device Flow，后续请求复用
        - 超时控制：IN_CHAT_AUTH_TIMEOUT_S 后自动取消，不阻塞 _process
        - 授权完成后直接从 authorize 返回值构建 vault_id，避免冗余远程调用
        """
        try:
            return await self._user_authorization.vault_id(user_open_id)
        except UserAuthorizationRequired:
            pass

        # ---- 并发去重：复用已有的授权任务 ----
        existing = self._pending_authorizations.get(user_open_id)
        if existing is not None and not existing.done():
            await self._reply(
                chat_id,
                "正在等待你完成飞书授权，请点击之前发送的链接完成扫码。",
            )
            try:
                return await asyncio.shield(existing)
            except (asyncio.CancelledError, Exception) as error:
                raise UserAuthorizationRequired(
                    f"飞书授权失败：{error!s}。请重新发送触发词再试。"
                ) from error

        # ---- 发起新的 in-chat Device Flow ----
        task = asyncio.ensure_future(
            self._do_in_chat_authorize(chat_id, user_open_id)
        )
        self._pending_authorizations[user_open_id] = task
        try:
            return await asyncio.wait_for(task, timeout=IN_CHAT_AUTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            task.cancel()
            raise UserAuthorizationRequired(
                "授权链接已超时（5 分钟），请重新发送触发词获取新的授权链接。"
            )
        except Exception as error:
            raise UserAuthorizationRequired(
                f"飞书授权失败：{error!s}。请重新发送触发词再试。"
            ) from error
        finally:
            if self._pending_authorizations.get(user_open_id) is task:
                del self._pending_authorizations[user_open_id]

    async def _do_in_chat_authorize(
        self, chat_id: str, user_open_id: str
    ) -> str:
        """实际执行 Device Flow：发链接 → poll → 返回 vault_id。"""
        await self._reply(
            chat_id,
            "首次使用需要完成飞书账号授权，正在生成授权链接…",
        )
        open_id = await self._user_authorization.authorize(
            expected_open_id=user_open_id,
            on_device_ready=lambda device: self._reply(
                chat_id,
                f"请点击以下链接完成飞书授权（5 分钟内有效）：\n{device.verification_url}",
            ),
        )
        log.info("topic6 in-chat authorization completed user=%s", open_id)
        await self._reply(chat_id, "✅ 授权成功，正在继续执行任务…")
        # authorize 成功后 store 已写入，直接取 vault_id
        return await self._user_authorization.vault_id(open_id)

    def _start_token_keepalive(
        self, job: PipelineJob, expected_vault_id: str
    ) -> None:
        if self._pipeline_store is None or not expected_vault_id:
            return
        self._stop_token_keepalive(job.job_id)
        loop = self._loop or asyncio.get_running_loop()
        task = loop.create_task(
            self._keep_user_token_alive(job, expected_vault_id)
        )
        self._token_keepalive_tasks[job.job_id] = task
        task.add_done_callback(
            lambda completed, job_id=job.job_id: self._token_keepalive_done(
                job_id, completed
            )
        )

    def _stop_token_keepalive(self, job_id: str) -> None:
        task = self._token_keepalive_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _token_keepalive_done(
        self, job_id: str, task: asyncio.Task
    ) -> None:
        if self._token_keepalive_tasks.get(job_id) is task:
            self._token_keepalive_tasks.pop(job_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error(
                "topic6 token keepalive stopped unexpectedly job=%s: %s",
                job_id,
                error,
            )

    async def _keep_user_token_alive(
        self, job: PipelineJob, expected_vault_id: str
    ) -> None:
        """活跃任务期间原地轮换用户 Credential，不改变 Session 或 Vault。"""
        assert self._pipeline_store is not None
        while True:
            current = self._pipeline_store.get_job(job.job_id)
            if current is None or current.status not in (
                STATUS_RUNNING,
                STATUS_WAIT_HC,
            ):
                return
            try:
                vault_id = await self._user_authorization.vault_id(
                    job.user_open_id
                )
                if vault_id != expected_vault_id:
                    log.error(
                        "topic6 token keepalive vault changed job=%s "
                        "session=%s expected=%s actual=%s",
                        job.job_id,
                        job.ma_session_id,
                        expected_vault_id,
                        vault_id,
                    )
                    return
            except Exception as error:  # noqa: BLE001 - 临时刷新失败下轮继续
                log.warning(
                    "topic6 token keepalive refresh failed job=%s "
                    "session=%s: %s",
                    job.job_id,
                    job.ma_session_id,
                    error,
                )
            await asyncio.sleep(TOKEN_KEEPALIVE_INTERVAL_S)


def _should_handle_message(message: IncomingMessage) -> bool:
    if not message.text.strip():
        return False
    return message.chat_type == "p2p" or message.mentioned_bot


def build_gateway(config: Topic6Config, ark, sender, loop: asyncio.AbstractEventLoop):
    """Case 入口:装配 PipelineStore + Runner + HITL,返回 Topic6Gateway。"""
    from arkagent.paths import get_case_paths

    case_paths = get_case_paths("topic6")
    store = GatewayStore(case_paths.database_path)

    pipeline_db = config.pipeline_db_path or None
    pipeline_store = PipelineStore(pipeline_db)
    user_authorization = Topic6UserAuthorization(
        pipeline_store,
        ark,
        FeishuOAuth(config.feishu_app_id, config.feishu_app_secret),
    )
    # 统一通知器（@ 发起人通知 HC 审核/完成/失败/中断）——只依赖 sender，可以在 runner 前创建
    notifier = JobNotifier(sender)
    runner = Topic6Runner(
        ark,
        sender,
        pipeline_store,
        RunnerConfig(
            coordinator_agent_id=config.coordinator_agent_id,
            environment_id=config.environment_id,
            memory_store_id=config.memory_store_id,
            vault_ids=(config.vault_id,) if config.vault_id else (),
        ),
        loop=loop,
        extra_headers=(
            {f"X-User-Model-Header-{config.model_auth_header_name}": config.model_auth_header_value}
            if config.model_auth_header_name and config.model_auth_header_value
            else None
        ),
        notifier=notifier,
    )
    # [P0-2] 飞书文档发布器
    publisher = FeishuDocPublisher(sender, config.docs_folder_token)
    artifact_sync = Topic6ArtifactSync(
        ark,
        Path(config.artifact_sync_dir),
        pipeline_store.get_job,
        poll_interval_sec=config.artifact_poll_interval_sec,
    )
    # 注入 notifier 到 hitl（runner 已通过构造函数注入）
    hitl = Topic6Hitl(Topic6HitlDeps(
        store=pipeline_store,
        runner=runner,
        publisher=publisher,
        artifact_sync=artifact_sync,
        sync_root=Path(config.artifact_sync_dir),
        notifier=notifier,
    ))
    runner.bind_card_sender(hitl)

    async def reply(chat_id: str, text: str) -> None:
        await loop.run_in_executor(None, sender.send_to_chat, chat_id, text)

    return Topic6Gateway(
        config,
        store,
        runner,
        hitl,
        user_authorization,
        reply,
        loop,
        pipeline_store=pipeline_store,
        artifact_sync=artifact_sync,
        publisher=publisher,
    )
