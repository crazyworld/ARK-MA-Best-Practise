"""topic5 case 的 Gateway:飞书触发词 → 长任务 Runner + HITL 卡片回调。

⚠️ host-only:import ``arkagent.feishu`` / ``arkagent.store`` 及下游 topic5_runner/hitl,
只能在装了 arkagent 框架的 Gateway 宿主进程运行,无法在 MA 云沙箱内独立 import 测试。

只处理 topic5 场景(复盘报告/营销复盘)的入站消息与卡片回调。与 topic6 的差异:
  - 触发词是「复盘报告」「营销复盘」「活动复盘」；full 走自然语言需求受理（原版 Phase1 v1.12），
    demo 用固定样例 G1 表单并对清洗后活动明细稳定抽样最多 500 条
  - 交付走飞书文档,**不需要** Miaoda 用户 OAuth / token-keepalive;过程文件经 Session
    输出目录同步到本地 ``data/artifacts/<job_id>/``(artifact_sync.py),可作测试回放来源
  - HC 门是 DAG 人工门 G1/G2/N10/G3/G4/HC7

非触发词消息(以及未鉴权用户)给出用法提示。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from arkagent.feishu import IncomingMessage
from arkagent.store import GatewayStore

try:
    from .artifact_sync import Topic5ArtifactSync
    from .pipeline_store import (
        STATUS_RUNNING, STATUS_STOPPING, STATUS_WAIT_HC, STATUS_WAIT_INPUT, PipelineStore,
        STATUS_WAIT_INTAKE,
    )
    from .topic5_hitl import Topic5Hitl, Topic5HitlDeps
    from .topic5_runner import (
        Topic5Config as RunnerConfig,
        Topic5Runner,
        Topic5RunnerError,
        Topic5AttachmentStateError,
        normalize_user_text,
        parse_trigger,
    )
    from .config import Topic5Config
except ImportError:  # pragma: no cover - standalone compatibility
    from artifact_sync import Topic5ArtifactSync
    from pipeline_store import (
        STATUS_RUNNING, STATUS_STOPPING, STATUS_WAIT_HC, STATUS_WAIT_INPUT, PipelineStore,
        STATUS_WAIT_INTAKE,
    )
    from topic5_hitl import Topic5Hitl, Topic5HitlDeps
    from topic5_runner import (
        Topic5Config as RunnerConfig,
        Topic5Runner,
        Topic5RunnerError,
        Topic5AttachmentStateError,
        normalize_user_text,
        parse_trigger,
    )
    from config import Topic5Config

log = logging.getLogger("arkagent.case.topic5")
# 过程文件同步持续到任务离开这些状态（终态/暂停后再做最后一次同步即停止）。
ACTIVE_SYNC_STATUSES = frozenset(
    {STATUS_RUNNING, STATUS_WAIT_HC, STATUS_WAIT_INPUT, STATUS_WAIT_INTAKE, STATUS_STOPPING}
)
CONTINUE_COMMAND_RE = re.compile(r"^/(?:continue|resume)(?:\s+(.*))?$", re.IGNORECASE)
# FIX-38：交接后修正（话题内，任务已完成时）。
FIX_COMMAND_RE = re.compile(r"^/fix(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)

# 开新任务的统一说法（full 走自然语言入口；demo 用固定样例演示）。
_NEW_TASK_HINT = "开新任务请在群主时间线 @我 发送「复盘报告」并附上活动描述。"
_USAGE = (
    "我负责营销活动复盘报告。在群主时间线 @我 发送「复盘报告」，并附上活动描述"
    "（品牌和活动、统计起止日期、平台即可，关键词和过滤词由我生成），例如：\n"
    "复盘报告 塔斯汀×Pingu联名活动，2026-10-22 至 2026-11-04，小红书和抖音\n"
    "只发「复盘报告」时，我会先发一份描述模板；信息不全时我会在任务话题里追问，"
    "直接在话题里回复即可，无需 @我。\n"
    "「复盘报告 demo」使用固定样例演示，质量标准与正式任务相同。\n"
    "在任务话题内：/continue 续跑任务，/new 取消任务。"
)


class Topic5Gateway:
    """飞书 gateway-like 对象:实现 accept + on_card_action。"""

    def __init__(
        self,
        config: Topic5Config,
        store: GatewayStore,
        runner: Topic5Runner,
        hitl: Topic5Hitl,
        reply,
        loop: asyncio.AbstractEventLoop,
        pipeline_store: PipelineStore | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._runner = runner
        self._hitl = hitl
        self._reply = reply
        self._loop = loop
        self._pipeline_store = pipeline_store
        self._authorized = set(config.authorized_open_ids)
        self._timeout_task: asyncio.Task | None = None

    def accept(self, message: IncomingMessage) -> bool:
        if not _should_handle_message(message, self._pipeline_store):
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
        """Gateway 启动后把本地活跃任务与 MA 历史事件对齐(只重建 SSE,不发 user.message)。"""
        self._start_timeout_monitor()
        if self._pipeline_store is None:
            return
        active_jobs = self._pipeline_store.list_active_jobs()
        if not active_jobs:
            return
        log.info("topic5 startup recovery begins active_jobs=%s", len(active_jobs))
        results = await self._runner.recover_active_jobs()
        log.info("topic5 startup recovery completed results=%s", results)

    def _start_timeout_monitor(self) -> None:
        if self._timeout_task is not None and not self._timeout_task.done():
            return
        self._timeout_task = asyncio.create_task(self._timeout_monitor())

    async def _timeout_monitor(self) -> None:
        while True:
            try:
                handled = await self._hitl.process_due_g4_timeouts()
                if handled:
                    log.info("topic5 G4 timeout actions handled=%s", handled)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("topic5 G4 timeout monitor failed")
            await asyncio.sleep(15 * 60)

    async def _process(self, message: IncomingMessage) -> None:
        try:
            if self._authorized and message.user_open_id not in self._authorized:
                await self._reply(message.chat_id, "当前用户未授权。请联系管理员把你的 open_id 加入白名单。")
                self._store.complete_event(message.event_id, "completed")
                return

            if message.resources:
                resource = next(
                    (item for item in message.resources if item.type == "file"),
                    message.resources[0],
                )
                try:
                    job = await self._runner.attach_activity_file(
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        user_open_id=message.user_open_id,
                        resource=resource,
                    )
                    await self._runner.resume_after_activity_file(job)
                except Topic5AttachmentStateError as error:
                    await self._runner.reply_to_message(message.message_id, str(error))
                except Topic5RunnerError as error:
                    await self._runner.reply_to_message(
                        message.message_id, f"数据文件未接收：{error}"
                    )
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
                    hint = (
                        "当前话题没有进行中的任务，无需取消。"
                        if message.thread_id
                        else "/new 用于取消某个任务，请在该任务的话题内发送；开新任务不需要 /new。"
                    )
                    await self._reply(message.chat_id, f"[/new] {hint}{_NEW_TASK_HINT}")
                else:
                    await self._reply(
                        message.chat_id,
                        f"[/new] 已取消当前任务（job_id={cancelled.job_id}）。{_NEW_TASK_HINT}",
                    )
                self._store.complete_event(message.event_id, "completed")
                return

            fix_match = FIX_COMMAND_RE.fullmatch(text)
            if fix_match:
                try:
                    fixed = await self._runner.post_handoff_fix(
                        chat_id=message.chat_id, thread_id=message.thread_id,
                        request=fix_match.group(1) or "")
                    reply = ("[/fix] 当前话题没有已交付的报告；/fix 只用于已完成任务的修正。"
                             if fixed is None else
                             f"[/fix] 已收到修正请求，将在原飞书文档上修改（链接不变），"
                             f"修改后重新质检并请你终验。job_id={fixed.job_id}")
                except Topic5RunnerError as error:
                    reply = f"[/fix] {error}"
                await self._reply(message.chat_id, reply)
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
                    hint = (
                        "当前话题没有可续跑的任务。"
                        if message.thread_id
                        else "/continue 用于续跑某个任务，请在该任务的话题内发送。"
                    )
                    await self._reply(message.chat_id, f"[/continue] {hint}{_NEW_TASK_HINT}")
                else:
                    message_hint = (
                        "补充消息已注入。"
                        if continue_match.group(1) and continue_match.group(1).strip()
                        else "已注入「继续」。"
                    )
                    await self._reply(
                        message.chat_id,
                        f"[/continue] {message_hint}已重新订阅进度 job_id={resumed.job_id}。若该 Session 已在方舟侧终止,稍后进度卡片会回落为失败。",
                    )
                self._store.complete_event(message.event_id, "completed")
                return

            if await self._hitl.restore_archived(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                user_open_id=message.user_open_id,
            ):
                self._store.complete_event(message.event_id, "completed")
                return

            # Phase1：等待需求补充时，话题内文字直接作为需求描述/补充（无需 @）。
            if await self._runner.handle_intake_reply(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                user_open_id=message.user_open_id,
                text=text,
            ):
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
                await self._reply(message.chat_id, _USAGE)
                self._store.complete_event(message.event_id, "completed")
                return

            try:
                job = await self._runner.start_job(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    user_open_id=message.user_open_id,
                    mode=mode,
                    user_message=text,
                    user_vault_id=self._config.vault_id,
                    root_message_id=message.message_id,
                )
                log.info("topic5 job started job=%s mode=%s", job.job_id, mode)
            except Topic5RunnerError as error:
                await self._reply(message.chat_id, str(error))
            self._store.complete_event(message.event_id, "completed")
        except Exception as error:  # noqa: BLE001
            self._store.complete_event(message.event_id, "failed")
            reason = str(error)
            await self._reply(message.chat_id, f"执行失败:{reason[:240]}")


def _should_handle_message(
    message: IncomingMessage, pipeline_store: PipelineStore | None = None
) -> bool:
    if message.resources:
        # 群聊主时间线的普通附件不抢占；话题内附件由当前关卡判断，无需再次 @bot。
        return message.chat_type == "p2p" or bool(message.thread_id)
    if not message.text.strip():
        return False
    if message.chat_type == "p2p" or message.mentioned_bot:
        return True
    # Phase1：任务话题处于「等待需求补充」时，话题内回复无需 @（仅限该话题）。
    return bool(
        message.thread_id
        and pipeline_store is not None
        and pipeline_store.get_wait_intake_job(
            message.chat_id, message.thread_id, message.user_open_id)
    )


_COORDINATOR_AGENT_JSON = Path(__file__).resolve().parent / "ma-resources" / "agents" / "coordinator.json"


def _byok_model_headers(config: Topic5Config, agent_json: Path = _COORDINATOR_AGENT_JSON):
    """对齐 topic6：coordinator 走 BYOK（model.provider 非空）时附加 relay 鉴权 Header。

    缺 BYOK_MODEL_AUTH_HEADER_* 时启动即失败（否则每次模型请求都会被 relay 拒绝）；
    方舟原生模型收到 X-User-Model-Header-* 会 400，因此原生模型时不附加。
    """
    try:
        model = json.loads(agent_json.read_text(encoding="utf-8")).get("model") or {}
    except (OSError, ValueError) as error:
        log.warning("topic5 cannot read %s (%s); keep BYOK header", agent_json, error)
        model = {"provider": "unknown"}
    if not model.get("provider"):
        log.info("topic5 coordinator uses Ark native model %s; BYOK header disabled", model.get("id"))
        return None
    if not (config.model_auth_header_name and config.model_auth_header_value):
        raise RuntimeError(
            f"coordinator 模型走 {model.get('provider')}，但未配置 BYOK_MODEL_AUTH_HEADER_NAME / "
            "BYOK_MODEL_AUTH_HEADER_VALUE（bmc-llm-relay 的鉴权 Header），Agent 请求会被拒绝"
        )
    return {f"X-User-Model-Header-{config.model_auth_header_name}": config.model_auth_header_value}


def build_gateway(config: Topic5Config, ark, sender, loop: asyncio.AbstractEventLoop):
    """Case 入口:装配 PipelineStore + Runner + HITL,返回 Topic5Gateway。"""
    from arkagent.paths import get_case_paths

    case_paths = get_case_paths("topic5")
    store = GatewayStore(case_paths.database_path)

    pipeline_db = config.pipeline_db_path or None
    pipeline_store = PipelineStore(pipeline_db)

    def _job_active(job_id: str) -> bool:
        job = pipeline_store.get_job(job_id)
        return job is not None and job.status in ACTIVE_SYNC_STATUSES

    artifact_sync = (
        Topic5ArtifactSync(
            ark,
            Path(config.artifact_sync_dir),
            _job_active,
            poll_interval_sec=config.artifact_poll_interval_sec,
        )
        if config.artifact_sync_dir else None
    )
    runner = Topic5Runner(
        ark,
        sender,
        pipeline_store,
        RunnerConfig(
            coordinator_agent_id=config.coordinator_agent_id,
            environment_id=config.environment_id,
            memory_store_id=config.memory_store_id,
            vault_ids=(config.vault_id,) if config.vault_id else (),
            artifact_sync_dir=config.artifact_sync_dir,
            replay_job_id=config.replay_job_id,
        ),
        loop=loop,
        artifact_sync=artifact_sync,
        extra_headers=_byok_model_headers(config),
    )
    log.info(
        "topic5 artifact sync dir=%s replay_job=%s",
        config.artifact_sync_dir or "off", config.replay_job_id or "-",
    )
    hitl = Topic5Hitl(Topic5HitlDeps(store=pipeline_store, runner=runner))
    runner.bind_card_sender(hitl)

    async def reply(chat_id: str, text: str) -> None:
        await loop.run_in_executor(None, sender.send_to_chat, chat_id, text)

    return Topic5Gateway(
        config,
        store,
        runner,
        hitl,
        reply,
        loop,
        pipeline_store=pipeline_store,
    )
