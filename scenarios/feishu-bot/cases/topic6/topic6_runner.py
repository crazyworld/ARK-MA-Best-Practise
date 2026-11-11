"""topic6 长任务运行器:飞书触发 → 方舟 MA Session → 事件桥接。

职责:

  - ``start_job``:接飞书 `/start [模式]` 命令,创建 MA Session(挂 topic6-coordinator),
    落 ``pipeline_jobs``,启动异步 SSE 消费任务。
  - ``resume_job``:HITL 卡片回调后,把 HC 决策以 ``user.message`` 注入原 MA Session,
    再启一个 SSE 消费任务把 pipeline 从 wait_hc 拉回 running。
  - SSE 消费:``agent.tool_use`` / ``agent.message.delta`` 汇成一句进度文本发飞书;
    ``session.status_idle`` 时检查末尾消息是否是 HC 结构化 JSON,是则把主卡切到 HITL,
    否则视为流程终态(成功/失败)落库并推最终链接。

不重写 SSE / SSE 解析,直接复用 :mod:`arkagent.ark` 里的 ``_open_event_stream`` 与
事件抽取工具。飞书发消息全部走 ``FeishuSender.send_to_chat``(同步),外面用
``run_in_executor`` 转异步。

同一 Session 同时只允许一个 SSE 消费任务:任务 ID 存在 ``_active_streams``,
resume 前先 cancel 前一个,避免重复订阅导致 429/事件重复。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from arkagent.ark import ArkClient, event_error, event_progress, event_requires_action, event_text
from arkagent.feishu import FeishuSender
from arkagent.session_state import is_session_resumable, session_status_from_payload
try:
    from .job_notifier import JobNotifier
    from .pipeline_store import (
        FAILURE_SCOPE_RECOVERABLE,
        FAILURE_SCOPE_SESSION,
        FAILURE_SCOPE_TASK,
        HC_KINDS,
        STATUS_DONE,
        STATUS_FAILED,
        STATUS_RUNNING,
        STATUS_STOPPING,
        STATUS_STOPPED,
        STATUS_WAIT_HC,
        PipelineJob,
        PipelineStore,
    )
    from .topic6_progress_card import MAX_TOOL_LINES, build_progress_card
except ImportError:  # pragma: no cover - standalone compatibility
    from job_notifier import JobNotifier
    from pipeline_store import (
        FAILURE_SCOPE_RECOVERABLE,
        FAILURE_SCOPE_SESSION,
        FAILURE_SCOPE_TASK,
        HC_KINDS,
        STATUS_DONE,
        STATUS_FAILED,
        STATUS_RUNNING,
        STATUS_STOPPING,
        STATUS_STOPPED,
        STATUS_WAIT_HC,
        PipelineJob,
        PipelineStore,
    )
    from topic6_progress_card import MAX_TOOL_LINES, build_progress_card

log = logging.getLogger("arkagent.topic6.runner")

# 启动命令:`/start [模式] [周期]`。只认命令，不再按「热点报告/热点周报」子串触发——
# 普通聊天里提到这两个词(如「W45的热点周报给我开个权限」)曾误启动 full 全流程。
START_COMMAND_RE = re.compile(r"^/start(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)

# 模式别名(`/start` 后第一个词精确匹配;不带模式默认 full)。
MODE_DEMO_KEYWORDS = ("demo", "演示")
MODE_FULL_KEYWORDS = ("full", "完整", "正式")
# 旧 test 等别名映射到 smoke。
MODE_SMOKE_KEYWORDS = ("smoke", "冒烟", "冒烟测试", "快速测试", "test", "小样本", "试跑")

# ---- idle 自动续跑 ----
# Agent 在 pipeline 中途偶发 end_turn 后，Runner 通过 MA send_message 注入催促
# 消息唤醒 idle session。最多重试 MAX_IDLE_NUDGES 次，超限才判终态。
MAX_IDLE_NUDGES = 3
# Agent 需要用户做决定时输出的行首标记（coordinator.system.md R14）；Gateway 不催促。
NEED_DECISION_MARKER = "[need_decision]"
# need_decision 收口时写入 last_error 的固定片段；Gateway 据此识别「待裁决」任务(N2 引导)。
NEED_DECISION_REASON_MARK = "等待用户裁决"
# /republish:已过 HC3 的任务只重跑 Phase G(网页)+ H(妙搭),用于 HC3 后在飞书文档改正文、补/换营销观察配图。
# 消息自包含步骤,不依赖 Session 创建时的 coordinator 版本(旧 Session 也能执行)。
REPUBLISH_MESSAGE = (
    "[republish] 用户已在 HC3 定稿的飞书文档中修改了正文和/或补充、替换了营销观察配图，请重新发布网页：\n"
    "1. 不要重跑 Phase A~F，不要修改报告内容和飞书文档。\n"
    "2. 输出 `[phase] G`，按 Phase G 规定从飞书文档(status.f.feishu_doc_url)重新读取最新正文"
    "(fetch_report_md.py)和配图(fetch_case_images.py)，重新生成并校验网页。\n"
    "3. 输出 `[phase] H`，按 Phase H 规定更新同一个妙搭应用(status.h.app_id)，不要新建应用。\n"
    '4. 成功后最后一条消息只输出发布 JSON：{"phase":"H","release_status":"finished","online_url":"<发布 API 返回的链接>"}'
)
NEED_DECISION_REPLY_LIMIT = 3000
# HC 卡点的 idle 是正常业务行为（等人审），不做自动催促。
_HC_PHASE_PREFIXES = tuple(HC_KINDS)

# model_request_failed_error 的自动重试上限和退避策略。
# Provider 侧临时故障（速率限制/超载）不应直接终止 pipeline。
_MAX_PROVIDER_RETRIES = 5
MODE_SKIP_SAMPLING_KEYWORDS = (
    "skip_sampling",
    "skip-sampling",
    "跳过采样",
    "跳过抽样",
)

# 进度卡片 patch 节流:同一卡片至少间隔多少秒才发一次 patch,避免飞书频控。
PROGRESS_MIN_INTERVAL_SEC = 4.0
# SSE 网络断线后的有限重连：1/2/4/8/16 秒，加抖动避免大量 Gateway 同时重连。
SSE_RECONNECT_MAX_ATTEMPTS = 5
SSE_RECONNECT_BASE_DELAY_SEC = 1.0
SSE_RECONNECT_MAX_DELAY_SEC = 16.0

# Topic6 业务项目只能落在该目录的直属子目录。Coordinator 负责按周期契约生成子目录名。
PROJECTS_ROOT = PurePosixPath("/workspace/Projects")


def normalize_user_text(text: str, mentioned_bot: bool = False) -> str:
    """群聊入站先去掉开头的 @机器人，避免机器人名称参与命令/触发词判断。"""
    normalized = text.strip()
    if mentioned_bot:
        normalized = re.sub(r"^(?:@\S+\s*)+", "", normalized).strip()
    return normalized


def is_start_command(text: str) -> bool:
    return bool(text) and START_COMMAND_RE.fullmatch(text.strip()) is not None


def parse_trigger(text: str) -> Optional[str]:
    """`/start [模式] [周期]` → mode(demo|full|skip_sampling|smoke)。

    - 不是 `/start` 命令返回 None(普通消息不触发任务)。
    - `/start` 后第一个词按别名精确匹配模式；没有参数、或第一个词是周期写法
      (W45 / 09-01到09-14 / 双周 / 继续W45 等，交给 init_project.py 解析)时默认 full。
    - 第一个词是纯字母但不是已知模式(如 `/start ful`)视为模式写错，返回 None，
      由调用方用 is_start_command 区分后回复用法提示，避免静默按 full 启动。
    """
    if not text:
        return None
    match = START_COMMAND_RE.fullmatch(text.strip())
    if match is None:
        return None
    args = (match.group(1) or "").split()
    if not args:
        return "full"
    first = args[0].lower()
    for mode, aliases in (
        ("skip_sampling", MODE_SKIP_SAMPLING_KEYWORDS),
        ("demo", MODE_DEMO_KEYWORDS),
        ("smoke", MODE_SMOKE_KEYWORDS),
        ("full", MODE_FULL_KEYWORDS),
    ):
        if first in aliases:
            return mode
    if re.fullmatch(r"[a-z_\-]+", first):
        return None
    return "full"


# ---- 事件解析 --------------------------------------------------------------


def extract_hc_payload(text: str) -> Optional[dict]:
    """从 agent.message 正文里抠出 HC 结构化 JSON;抠不到返回 None。

    coordinator.system.md 已强制约定 HC1/HC2/HC3 在 end_turn 前必须落一个如下 JSON:

        {"hc": "HC1", "mode": "full", "project_dir": "...", ...}

    正文中允许穿插其它文本,但 JSON 块本身必须完整可解析。逐个尝试正文中的 ``{``
    起点并交给 ``JSONDecoder.raw_decode``，从而正确处理嵌套对象；找不到合法 HC
    对象时返回 None,由上层视为普通完成事件。
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    payload: Optional[dict] = None
    for start in (match.start() for match in re.finditer(r"\{", text)):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("hc") in HC_KINDS:
            payload = value
            break
    return payload


def normalize_project_dir(value: object) -> Optional[str]:
    """校验 Topic6 项目绝对路径，只允许 ``/workspace/Projects`` 的直属子目录。"""
    raw = str(value or "").strip().strip("`")
    if not raw:
        return None
    candidate = PurePosixPath(raw)
    if candidate.parent != PROJECTS_ROOT or candidate.name in {"", ".", ".."}:
        return None
    return str(candidate)


def extract_project_dir(text: str) -> Optional[str]:
    """解析 Coordinator 的 ``[project_dir]`` 状态标记。"""
    if not text:
        return None
    match = re.search(r"(?m)^\[project_dir\]\s+([^\r\n]+?)\s*$", text)
    return normalize_project_dir(match.group(1)) if match else None


def _idle_stop_reason(event: dict) -> str:
    reason = event.get("stop_reason")
    if isinstance(reason, dict):
        reason = reason.get("type")
    return str(reason or "")


def _error_retry_status(event: dict) -> str:
    """session.error 的 error.retry_status.type：retrying | exhausted | terminal | ""。"""
    error = event.get("error")
    if not isinstance(error, dict):
        return ""
    status = error.get("retry_status")
    if isinstance(status, dict):
        status = status.get("type")
    return str(status or "").strip().lower()


def _session_failure_scope(etype: str, event: dict) -> str:
    """按方舟语义判定失败是否不可恢复，返回 failure_scope。

    只有 Session 真正进入终态(status_failed / status_terminated，或 session.error 的
    retry_status=terminal)才是 session 级；retry_status=exhausted 表示本轮结束但仍可继续
    发消息，缺省未知时也按可恢复处理——/continue 前 continue_job 会用 GET /sessions 复核
    真实状态，真终止时在那里改判 session 级，不会把死 Session 误当活的。
    """
    if etype in ("session.status_failed", "session.status_terminated"):
        return FAILURE_SCOPE_SESSION
    if etype == "session.error" and _error_retry_status(event) == "terminal":
        return FAILURE_SCOPE_SESSION
    return FAILURE_SCOPE_RECOVERABLE


# 方舟沙箱故障(平台侧)：工具结果的固定错误文本。连续出现即判定沙箱不可用，
# 不再自动续跑(续跑只会让 Agent 反复探测、白耗上下文)。
_SANDBOX_ERROR_MARKERS = ("task service unavailable", "sandbox abnormal")
SANDBOX_ERROR_STREAK_LIMIT = 3

# /continue 回复了进度卡片、但卡片对应任务已不可续跑时的提示(不回落到会话键查找)。
_UNRESUMABLE_CARD_HINTS = {
    STATUS_DONE: "已完成",
    STATUS_STOPPED: "已停止",
    STATUS_STOPPING: "正在停止",
}


def _is_sandbox_tool_error(event: dict) -> bool:
    if event.get("type") != "agent.tool_result" or event.get("is_error") is not True:
        return False
    text = event_text(event).lower()
    return any(marker in text for marker in _SANDBOX_ERROR_MARKERS)


def validate_hc_payload(payload: dict) -> Optional[str]:
    """返回 HC payload 的契约错误；合法时返回 None。

    P1-4 扩展：
    - HC3: 保留 feishu_doc_url 必填。Validator BLOCK 不在此处 reject（已有
      build_hc_card 红色警示 + "忽略阻断并通过"按钮处理）。
    - HC1/HC2: 检查 wide_table_path。
    """
    hc = payload.get("hc")

    if hc == "HC3":
        if not str(payload.get("feishu_doc_url") or "").strip():
            return "HC3 缺少 feishu_doc_url：Phase F 飞书发布未完成"

    if hc in ("HC1", "HC2"):
        wide_table = str(payload.get("wide_table_path") or "").strip()
        if not wide_table:
            return f"{hc} 缺少 wide_table_path"

    return None


# ---- Runner ----------------------------------------------------------------


@dataclass
class Topic6Config:
    """topic6 运行器所需的方舟资源常量(来自 ma-resources/skill_ids.json 汇总)。"""

    coordinator_agent_id: str
    environment_id: str
    memory_store_id: str = ""  # 以 memory 工具形式提供给 Agent(非文件系统挂载);为空则不挂
    vault_ids: tuple[str, ...] = ()
    # Session 首次消息发送后,SSE 消费的整体超时(秒)。给足 3.5h 主线 + 富余。
    session_timeout_sec: int = 6 * 3600


@dataclass
class _ProgressState:
    """一次 pipeline 的进度卡片本地状态(runner 内部使用)。

    - ``tool_lines``: 环形缓冲,存"HH:MM:SS · name · desc"字符串;超过 MAX_TOOL_LINES 自动挤旧。
    - ``overflow``: 已被环形缓冲挤掉的历史条数,渲染时用作"(更早 N 条已省略)"提示。
    - ``last_patch_at``: 单调时钟时间戳,用于节流。0 表示卡片刚发出、还没 patch 过。
    - ``started_at_wall``: 墙钟时间(秒),用于算 elapsed_sec。
    - ``last_signature``: 上一次渲染快照的哈希指纹,内容没变就不 patch。
    """

    started_at_wall: float = field(default_factory=time.time)
    tool_lines: deque = field(default_factory=lambda: deque(maxlen=MAX_TOOL_LINES))
    overflow: int = 0
    last_patch_at: float = 0.0
    last_signature: str = ""
    idle_nudge_count: int = 0
    provider_retry_count: int = 0
    # provider 重试已发出唤醒消息、尚未看到其后的 idle：紧随 session.error 的
    # retries_exhausted idle 属于同一次故障，不能再走 nudge（否则一次故障吃两份额度）。
    provider_retry_pending: bool = False
    # 连续出现方舟沙箱故障的工具结果条数；任一正常工具结果清零。
    sandbox_error_streak: int = 0
    # 只在该下标之后的 Session 事件里找 Phase H online_url:/republish 前的旧链接不能把
    # 本轮发布失败误判为完成。
    history_floor: int = 0
    # 终态已调度过程关键文件归档（同一终态多次 patch 只归档一次；/continue 新一轮重建 state）
    process_files_scheduled: bool = False

    def push_tool_line(self, line: str) -> None:
        if len(self.tool_lines) == self.tool_lines.maxlen:
            self.overflow += 1
        self.tool_lines.append(line)


class Topic6Runner:
    """飞书 ↔ 方舟 MA topic6 pipeline 的桥接器。"""

    def __init__(
        self,
        ark: ArkClient,
        feishu: FeishuSender,
        store: PipelineStore,
        config: Topic6Config,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        card_sender: Optional["Topic6CardSenderProtocol"] = None,
        extra_headers: Optional[dict] = None,
        notifier: Optional["JobNotifier"] = None,
        lease_validator: Optional[Callable[[str, str], bool]] = None,
        lease_activator: Optional[Callable[[str, str, str, str], bool]] = None,
    ):
        self._ark = ark
        self._feishu = feishu
        self._store = store
        self._config = config
        self._loop = loop
        self._card_sender = card_sender  # 由 topic6_hitl 注入
        # BYOK Direct 模式：按请求动态附加的 bmc-llm-relay 鉴权 Header；Agent 走方舟原生模型时为 None。
        # 只通过 _send_message 使用，不得记日志/写库/拼进消息正文（Header 值即 relay Key）。
        self._extra_headers = extra_headers
        self._notifier = notifier  # 由 gateway 装配时注入
        self._lease_validator = lease_validator
        self._lease_activator = lease_activator
        # 串行化“检查活跃任务 → 创建 Session → 落 Job”，避免两个同时到达的触发
        # 都在落库前通过检查并各自创建一条长任务。
        self._start_lock = asyncio.Lock()
        # ma_session_id → 正在消费的 SSE 任务;续跑前 cancel 前一个。
        self._active_streams: dict[str, asyncio.Task] = {}
        # job_id → 进度卡片本地状态(tool 环形缓冲 + 上次 patch 时间)。
        self._progress: dict[str, "_ProgressState"] = {}
        # 同一任务的进度事件与 HITL 回调可能并发 patch；按 job 串行，避免旧快照覆盖新状态。
        self._card_locks: dict[str, asyncio.Lock] = {}
        # ma_session_id → 该 Session 的 Agent 是否走 BYOK(创建时的 agent 快照决定,之后不变)。
        self._session_byok: dict[str, bool] = {}

    def add_vault_id(self, vault_id: str) -> None:
        """追加一个每个新 Session 都要挂的 Vault（如 Gateway 异步置备的 bot token Vault）。"""
        if vault_id and vault_id not in self._config.vault_ids:
            self._config.vault_ids = (vault_id, *self._config.vault_ids)

    def bind_card_sender(self, sender: "Topic6CardSenderProtocol") -> None:
        self._card_sender = sender

    def bind_process_files(self, delivery, artifact_sync) -> None:
        """终态时归档过程关键文件到飞书（由 gateway 装配时注入）。"""
        self._process_files = delivery
        self._process_artifact_sync = artifact_sync

    async def _deliver_process_files(self, job_id: str) -> None:
        """终态：先补一次产物同步，再归档关键文件；有新上传才 @ 发起人。失败只记日志。"""
        delivery = getattr(self, "_process_files", None)
        if delivery is None or not getattr(delivery, "enabled", False):
            return
        job = self._store.get_job(job_id)
        if job is None:
            return
        sync = getattr(self, "_process_artifact_sync", None)
        try:
            if sync is not None and job.ma_session_id:
                await sync.sync_once(job.job_id, job.ma_session_id)
            result = await asyncio.get_running_loop().run_in_executor(None, delivery.deliver, job)
        except Exception as err:  # noqa: BLE001 - 归档失败不影响终态
            log.warning("topic6 terminal process files delivery failed job=%s: %s", job_id, err)
            return
        if result.get("uploaded") and result.get("folder_url") and self._notifier is not None:
            try:
                self._notifier.notify_process_files(
                    job, result["folder_url"], int(result["uploaded"]) + int(result.get("skipped") or 0))
            except Exception as err:  # noqa: BLE001
                log.warning("topic6 process files notification failed job=%s: %s", job_id, err)

    # ---- 入口 --------------------------------------------------------------

    async def start_job(
        self,
        *,
        chat_id: str,
        thread_id: str,
        user_open_id: str,
        mode: str,
        user_message: str,
        user_vault_id: str = "",
    ) -> PipelineJob:
        """全局串行检查并启动任务；同一 Gateway 只允许一个活跃 Job。"""
        async with self._start_lock:
            return await self._start_job_locked(
                chat_id=chat_id,
                thread_id=thread_id,
                user_open_id=user_open_id,
                mode=mode,
                user_message=user_message,
                user_vault_id=user_vault_id,
            )

    async def start_routed_job(
        self,
        *,
        task_id: str,
        lease_token: str,
        chat_id: str,
        thread_id: str,
        user_open_id: str,
        mode: str,
        user_message: str,
        root_message_id: str,
        user_vault_id: str = "",
        existing_session_id: str = "",
        on_session_created: Optional[Callable[[str], None]] = None,
        on_job_created: Optional[Callable[[PipelineJob], None]] = None,
    ) -> PipelineJob:
        if mode not in ("demo", "full", "skip_sampling"):
            raise Topic6RunnerError(f"不支持的 Topic6 mode: {mode}")
        if self._lease_validator is None or not self._lease_validator(
            task_id, lease_token
        ):
            raise Topic6RunnerError("Topic6 lease token 无效或 task 不是当前 owner")
        async with self._start_lock:
            return await self._start_job_locked(
                chat_id=chat_id,
                thread_id=thread_id,
                user_open_id=user_open_id,
                mode=mode,
                user_message=user_message,
                user_vault_id=user_vault_id,
                task_id=task_id,
                root_message_id=root_message_id,
                existing_session_id=existing_session_id,
                skip_global_guard=True,
                lease_token=lease_token,
                on_session_created=on_session_created,
                on_job_created=on_job_created,
            )

    async def _start_job_locked(
        self,
        *,
        chat_id: str,
        thread_id: str,
        user_open_id: str,
        mode: str,
        user_message: str,
        user_vault_id: str = "",
        task_id: str = "",
        root_message_id: str = "",
        existing_session_id: str = "",
        skip_global_guard: bool = False,
        lease_token: str = "",
        on_session_created: Optional[Callable[[str], None]] = None,
        on_job_created: Optional[Callable[[PipelineJob], None]] = None,
    ) -> PipelineJob:
        """创建 MA Session、落库并启动 SSE 消费任务。

        - 整个 Gateway 任意 ``running`` / ``wait_hc`` Job 都会阻止新建任务。
        - Session 挂载 Memory Store(Agent 经 memory_read 工具读取)承载 topic6 的 lm/ 内容。
        - 首个 user.message 拼「触发词 + 模式 + 用户原始消息」,由 coordinator 解析并按
          system prompt 的 14 步执行。
        """
        if task_id:
            task_job = self._store.get_job_by_task(task_id)
            if task_job:
                return task_job
        existing = self._store.get_active_job()
        if existing and not skip_global_guard:
            state = "等待审核" if existing.status == STATUS_WAIT_HC else "运行中"
            raise Topic6RunnerError(
                f"当前已有热点周报任务{state}"
                f"（mode={existing.mode}，phase={existing.current_phase}）。"
                "请等待前一个任务结束后再发起。"
            )

        resources = []
        if self._config.memory_store_id:
            resources.append(
                {
                    "type": "memory_store",
                    "memory_store_id": self._config.memory_store_id,
                    "access": "read_only",
                }
            )
        env_overrides = {"FEISHU_USER_OPEN_ID": user_open_id}
        if self._config.memory_store_id:
            # 方舟不把 memstore 挂成目录,Agent 用 memory_read /{memstore_id}/topic6/xxx.md 读取;
            # 注入 ID 便于 Agent 拼出 memory 路径(非密钥)。
            env_overrides["TOPIC6_MEMORY_STORE_ID"] = self._config.memory_store_id
        if task_id:
            env_overrides["TOPICX_TASK_ID"] = task_id
        vault_ids = list(
            dict.fromkeys(
                vault_id
                for vault_id in (*self._config.vault_ids, user_vault_id)
                if vault_id
            )
        )
        create_kwargs = {
            "vault_ids": vault_ids,
            "env_overrides": env_overrides,
            "resources": resources,
        }
        if task_id:
            create_kwargs["title"] = f"topicx/{task_id}/topic6"
        session_id = existing_session_id or await self._ark.create_session(
            self._config.coordinator_agent_id,
            self._config.environment_id,
            **create_kwargs,
        )
        if on_session_created is not None:
            on_session_created(session_id)
        # 业务目录名依赖 Coordinator 解析出的周期。创建 Job 时保持未知，Coordinator
        # 创建目录后必须输出 [project_dir] 标记，SSE 消费器会立即回写真值。
        project_dir = ""
        job = self._store.create_job(
            chat_id=chat_id,
            thread_id=thread_id,
            user_open_id=user_open_id,
            ma_session_id=session_id,
            mode=mode,
            project_dir=project_dir,
            root_message_id=root_message_id,
            task_id=task_id,
        )
        if on_job_created is not None:
            on_job_created(job)
        if task_id and (
            self._lease_activator is None
            or not self._lease_activator(
                task_id, lease_token, session_id, job.job_id
            )
        ):
            raise Topic6RunnerError("Topic6 lease 激活失败")
        log.info(
            "topic6 start_job job=%s session=%s mode=%s chat=%s user=%s",
            job.job_id, session_id, mode, chat_id, user_open_id,
        )

        prompt = (
            f"[topic6] 触发词=热点报告 mode={mode} "
            f"workspace_root={PROJECTS_ROOT}\n用户原始消息:{user_message}"
        )
        # 先发一张进度卡片当作"启动确认",拿到 message_id 落库,后续 patch 覆写这张卡即可,
        # 不再对每条 tool 事件单独 send_to_chat 刷屏。发失败也不阻塞主流程,只是这一次没有卡片。
        state = _ProgressState()
        self._progress[job.job_id] = state
        self._card_locks[job.job_id] = asyncio.Lock()
        card = build_progress_card(
            job=job,
            status=STATUS_RUNNING,
            elapsed_sec=0,
            tool_lines=[],
            overflow=0,
        )
        try:
            if root_message_id:
                message_id, topic_thread_id = await self._loop_run(
                    lambda: self._feishu.send_interactive_card_in_thread(
                        root_message_id, card
                    )
                )
                if topic_thread_id:
                    self._store.set_topic_thread_id(job.job_id, topic_thread_id)
                    job.topic_thread_id = topic_thread_id
            else:
                message_id = await self._loop_run(
                    lambda: self._feishu.send_interactive_card(chat_id, card)
                )
        except Exception as error:  # noqa: BLE001
            log.warning("topic6 progress card init failed job=%s: %s", job.job_id, error)
            message_id = None
        if message_id:
            self._store.set_progress_card_message_id(job.job_id, message_id)
            job.progress_card_message_id = message_id  # 内存态同步,后续路径不用再查库
            state.last_signature = self._signature(card)

        # SSE 消费任务在后台跑,不阻塞当前请求线程(飞书 3 秒响应窗口)。
        task = self._spawn_stream(job, first_user_message=prompt)
        self._active_streams[session_id] = task
        return job

    async def _send_message(self, session_id: str, text: str) -> None:
        """向 MA Session 注入 user.message 的唯一入口。

        BYOK 鉴权 Header 是按请求生效的(不写入 Agent/Session 持久配置)：任何一条
        user.message 漏带，方舟触发的那次模型请求就会以 model_request_failed_error
        (input=0/output=0)失败。所以首条消息、HC 续跑、idle 催促、provider 重试、
        重启恢复都必须经过这里，禁止直接调用 self._ark.send_message。
        Header 按 Session 决定:方舟原生模型的 Session 收到 X-User-Model-Header-* 会 400
        InvalidModelHeader,所以切换模型前创建的旧 Session 不带 Header。
        """
        headers = await self._headers_for(session_id)
        await self._ark.send_message(session_id, text, extra_headers=headers)

    async def _headers_for(self, session_id: str) -> Optional[dict]:
        if not self._extra_headers:
            return None
        byok = self._session_byok.get(session_id)
        if byok is None:
            try:
                session = await self._ark.get_session(session_id)
            except Exception as error:  # noqa: BLE001 - 查不到快照时按当前配置带 Header
                log.warning(
                    "topic6 get_session failed session=%s, keep BYOK header: %s",
                    session_id, error,
                )
                return self._extra_headers
            model = (session.get("agent") or {}).get("model") or {}
            byok = bool(model.get("provider"))
            self._session_byok[session_id] = byok
        return self._extra_headers if byok else None

    async def resume_job(self, job: PipelineJob, decision_message: str) -> None:
        """HITL 卡片回调路径:注入 user.message 后重新订阅 SSE。"""
        # cancel 前一个消费任务(通常已经因 idle 退出,但兜底)。
        prev = self._active_streams.get(job.ma_session_id)
        if prev is not None and not prev.done():
            prev.cancel()
        # HC 通过后 phase 应脱离 HC 前缀，否则后续 idle 会被误判为"等人审"而跳过 nudge。
        # 用 "post-HCx" 标记过渡态，Agent 实际输出 [phase] 后会被 _maybe_update_phase 覆盖。
        next_phase = job.current_phase
        if next_phase in HC_KINDS:
            next_phase = f"post-{next_phase}"
        self._store.resume_running(job.job_id, next_phase=next_phase)
        # 复用同一张进度卡片(state 若已回收就重建,避免续跑丢卡)。
        if job.job_id not in self._progress:
            self._progress[job.job_id] = _ProgressState()
        # 审核回调刚把唯一主卡 patch 成“已通过/已备注”终态。必须在启动下一段 SSE 前
        # 立即恢复运行卡，否则下一段开头若只有 status/thinking 事件、迟迟没有可展示的
        # progress，卡片会一直停留在 HC 已处理状态，历史工具进度也看不到。
        await self._render_and_patch(job.job_id, status=STATUS_RUNNING, force=True)
        task = self._spawn_stream(job, first_user_message=decision_message)
        self._active_streams[job.ma_session_id] = task

    async def continue_job(
        self,
        chat_id: str,
        thread_id: str,
        user_open_id: str,
        message: str = "继续",
        *,
        card_message_id: str = "",
        admin_open_ids: tuple[str, ...] = (),
    ) -> Optional[PipelineJob]:
        """/continue 续跑:把一条 user.message 注入最近一个可续跑的 Session,重订 SSE。

        - 无可续跑 job → 返回 None(调用方给“没有可续跑任务”的提示)。
        - 命中(running/wait_hc/failed)→ 复用 resume_job 注入 message 并重新
          订阅 SSE,返回该 job。
        - stopped 是用户确认的不可逆停止，不允许通过 /continue 恢复。

        主场景:卡片已显示“执行失败，可重试”,但 MA Session 仍是 idle/running。
        真正 failed/terminated 的 Session 不允许继续。
        """
        job = None
        if card_message_id:
            # 回复了进度卡片：按卡片精确定位，只允许发起人或管理员续跑(与卡片按钮口径一致)。
            # 卡片对应的任务已不可续跑时直接提示，不回落会话键——否则会续跑同一用户的另一个任务。
            job = self._store.get_job_by_progress_card(chat_id, card_message_id)
            if job is not None:
                if user_open_id not in {job.user_open_id, *admin_open_ids}:
                    raise Topic6RunnerError(
                        f"任务 {job.job_id} 由其他人发起，仅发起人或管理员可以继续。"
                    )
                unresumable = _UNRESUMABLE_CARD_HINTS.get(job.status)
                if unresumable:
                    raise Topic6RunnerError(
                        f"你回复的卡片对应任务 {job.job_id} {unresumable}，不能继续；"
                        "如需重新生成请发送 `/start`。"
                    )
        if job is None:
            job = self._store.get_resumable_job_by_session_key(
                chat_id, thread_id, user_open_id
            )
        if job is None:
            return None
        # Phase H 已在 Session 历史里交付过 online_url（如 Gateway 当时误判失败）：
        # 直接按历史收口为 done，不再注入「继续」——注入后 Agent 只会回复收尾/裁决
        # 文本，不会重复输出发布 JSON，反而被 P0-4 再判一次失败。
        if job.status != STATUS_DONE and job.ma_session_id:
            published = await self._find_published_online_url(
                job.ma_session_id, self._history_floor(job.job_id)
            )
            if published:
                log.info(
                    "topic6 /continue found published url in history job=%s url=%s",
                    job.job_id, published,
                )
                self._store.mark_done(job.job_id, online_url=published)
                await self._render_and_patch(
                    job.job_id, status=STATUS_DONE, online_url=published, force=True
                )
                self._progress.pop(job.job_id, None)
                return self._store.get_job(job.job_id) or job
        if job.failure_scope not in ("", "recoverable"):
            raise Topic6RunnerError("当前任务已终止，不能继续")
        session = await self._ark.get_session(job.ma_session_id)
        session_status = session_status_from_payload(session)
        if not is_session_resumable(session_status):
            self._store.mark_failed(
                job.job_id,
                f"session_not_resumable:{session_status or 'unknown'}",
                failure_scope=FAILURE_SCOPE_SESSION,
                retry_strategy="",
            )
            raise Topic6RunnerError(
                f"业务 Session 状态为 {session_status or 'unknown'}，不能继续当前任务"
            )
        log.info(
            "topic6 /continue resume job=%s session=%s session_status=%s "
            "prev_status=%s phase=%s",
            job.job_id,
            job.ma_session_id,
            session_status,
            job.status,
            job.current_phase,
        )
        # 用户手动续跑 = 人已介入：给后续阶段全新的自动续跑 / provider 重试额度
        state = self._progress.get(job.job_id)
        if state is not None:
            state.idle_nudge_count = 0
            state.provider_retry_count = 0
            state.provider_retry_pending = False
        await self.resume_job(job, message)
        return job

    async def _find_published_online_url(self, session_id: str, floor: int = 0) -> str:
        """倒序扫描 Session 第 floor 条之后的 agent.message，返回最近一次有效的 Phase H online_url。"""
        try:
            events = await self._ark.list_session_events(session_id)
        except Exception as error:  # noqa: BLE001 - 查不到历史就按原逻辑续跑
            log.warning("topic6 list_session_events failed session=%s: %s", session_id, error)
            return ""
        for event in reversed((events or [])[floor:]):
            if event.get("type") != "agent.message":
                continue
            url = extract_final_online_url(event_text(event))
            if url:
                return url
        return ""

    def _history_floor(self, job_id: str) -> int:
        state = self._progress.get(job_id)
        return state.history_floor if state is not None else 0

    def _hc3_passed(self, job: PipelineJob) -> bool:
        if job.status == STATUS_DONE:  # 已妙搭发布 ⇒ 必然过了 HC3(含审核记录缺失的早期任务)
            return True
        hc = self._store.latest_hc_for_job(job.job_id, "HC3")
        return hc is not None and hc.user_decision == "pass"

    def find_republishable_job(self, chat_id: str, ref: str = "") -> PipelineJob:
        """/republish 的目标:指定 job_id/session_id,否则取本会话最近一个已过 HC3 的任务。"""
        if ref:
            job = self._store.get_job(ref) or self._store.get_job_by_ma_session(ref)
            if job is None:
                raise Topic6RunnerError(f"未找到任务 {ref}(可传 job_id 或 session_id)。")
            if job.chat_id != chat_id:
                raise Topic6RunnerError(f"任务 {job.job_id} 不属于当前会话，请在原会话里发送。")
            if not self._hc3_passed(job):
                raise Topic6RunnerError(
                    f"任务 {job.job_id} 还没通过 HC3,网页尚未生成，无需重新发布。"
                )
            return job
        for job in self._store.list_jobs_for_chat(chat_id):
            if self._hc3_passed(job):
                return job
        raise Topic6RunnerError("当前会话没有已通过 HC3 的任务，无法重新发布网页。")

    async def republish_job(self, job: PipelineJob) -> PipelineJob:
        """在原 Session 里只重跑 Phase G+H(重新读取飞书文档配图 → 网页 → 更新同一妙搭应用)。"""
        async with self._start_lock:
            active = self._store.get_active_job()
            if active is not None:
                raise Topic6RunnerError(
                    f"当前已有任务在运行(job={active.job_id},phase={active.current_phase}),"
                    "请等它结束后再重新发布。"
                )
            try:
                session = await self._ark.get_session(job.ma_session_id)
                events = await self._ark.list_session_events(job.ma_session_id)
            except Exception as error:  # noqa: BLE001
                raise Topic6RunnerError(f"读取 Session {job.ma_session_id} 失败：{error!s}"[:300]) from error
            status = str(session.get("status") or "")
            if status != "idle":
                raise Topic6RunnerError(
                    f"Session {job.ma_session_id} 当前状态为 {status or '未知'},只有空闲的 Session 才能重新发布。"
                )
            model = (session.get("agent") or {}).get("model") or {}
            self._session_byok[job.ma_session_id] = bool(model.get("provider"))

            self._store.resume_running(job.job_id, next_phase="G")
            job = self._store.get_job(job.job_id) or job
            state = _ProgressState(history_floor=len(events or []))
            self._progress[job.job_id] = state
            self._card_locks.setdefault(job.job_id, asyncio.Lock())
            # 原进度卡在聊天记录里很靠上:重发一张新卡,后续进度与结果都 patch 到这张卡
            card = build_progress_card(
                job=job, status=STATUS_RUNNING, elapsed_sec=0, tool_lines=[], overflow=0
            )
            try:
                message_id = await self._loop_run(
                    lambda: self._feishu.send_interactive_card(job.chat_id, card)
                )
            except Exception as error:  # noqa: BLE001
                log.warning("topic6 republish card failed job=%s: %s", job.job_id, error)
                message_id = None
            if message_id:
                self._store.set_progress_card_message_id(job.job_id, message_id)
                job.progress_card_message_id = message_id
                state.last_signature = self._signature(card)

            prev = self._active_streams.get(job.ma_session_id)
            if prev is not None and not prev.done():
                prev.cancel()
            log.info(
                "topic6 republish job=%s session=%s byok=%s floor=%d",
                job.job_id, job.ma_session_id, self._session_byok[job.ma_session_id],
                state.history_floor,
            )
            task = self._spawn_stream(job, first_user_message=REPUBLISH_MESSAGE)
            self._active_streams[job.ma_session_id] = task
            return job

    async def cancel_active_job(
        self, chat_id: str, thread_id: str, user_open_id: str
    ) -> Optional[PipelineJob]:
        """取消该会话键上的活跃 job(SSE task + status → failed),让 /new 可以马上开新任务。

        - 无活跃 job → 返回 None(调用方给"当前没有可取消的任务"的提示)。
        - 有活跃 job → cancel SSE task、mark_failed(reason=cancelled_by_user)、
          刷一次进度卡片到失败终态,返回该 job。
        """
        job = self._store.get_active_job_by_session_key(chat_id, thread_id, user_open_id)
        if job is None:
            return None

        task = self._active_streams.pop(job.ma_session_id, None)
        if task is not None and not task.done():
            task.cancel()

        reason = "cancelled_by_user"
        self._store.mark_failed(
            job.job_id, reason, failure_scope=FAILURE_SCOPE_TASK
        )
        await self._render_and_patch(
            job.job_id, status=STATUS_FAILED, error=reason, force=True
        )
        return job

    async def recover_active_jobs(self) -> dict[str, str]:
        """Gateway 重启后对账所有活跃任务，并恢复必要的 SSE 订阅。

        恢复过程只读取 MA 已持久化事件，绝不发送 ``user.message``。因此它不会替
        用户执行 ``/continue``，只会把本地 SQLite 状态追到 MA 的最后可见状态。
        """
        results: dict[str, str] = {}
        for job in self._store.list_active_jobs():
            try:
                results[job.job_id] = await self.recover_job(job)
            except Exception as error:  # noqa: BLE001 - 单个 Session 失败不阻塞其它恢复
                log.exception(
                    "topic6 startup recovery failed job=%s session=%s: %s",
                    job.job_id,
                    job.ma_session_id,
                    error,
                )
                results[job.job_id] = "recovery_error"
        return results

    async def recover_job(self, job: PipelineJob) -> str:
        """根据历史事件恢复一个本地活跃任务，返回已采取的恢复动作。"""
        events = await self._ark.list_session_events(job.ma_session_id)
        if job.status == STATUS_STOPPING:
            return "stopping"
        messages = [
            body
            for event in events
            if event.get("type") == "agent.message"
            for body in [event_text(event)]
            if body
        ]
        for body in messages:
            self._maybe_update_project_dir(job.job_id, body)
            self._maybe_update_phase(job.job_id, body)

        terminal_types = {
            "user.interrupt",
            "session.error",
            "session.status_failed",
            "session.status_terminated",
            "session.status_idle",
        }
        last_terminal_index = max(
            (
                index
                for index, event in enumerate(events)
                if event.get("type") in terminal_types
            ),
            default=-1,
        )
        # 某个旧 idle/error 之后如果又有事件，说明用户或审核回调已让 Session
        # 进入下一轮；历史尾部尚无终态时仅恢复订阅，不能把旧 idle 当当前状态。
        if last_terminal_index == -1 or last_terminal_index < len(events) - 1:
            self._restore_stream(job)
            return "subscribed"

        terminal = events[last_terminal_index]
        terminal_type = terminal.get("type") or ""
        if terminal_type == "user.interrupt":
            reason = "用户在方舟后台中断了当前轮次，可发送 /continue 继续"
            self._store.mark_failed(job.job_id, reason)
            await self._render_and_patch(
                job.job_id, status=STATUS_FAILED, error=reason, force=True
            )
            return "failed"
        if terminal_type in {
            "session.error",
            "session.status_failed",
            "session.status_terminated",
        }:
            error = event_error(terminal) or (
                "session_terminated"
                if terminal_type == "session.status_terminated"
                else "session_failed"
            )
            self._store.mark_failed(
                job.job_id,
                error[:400],
                failure_scope=_session_failure_scope(terminal_type, terminal),
            )
            await self._render_and_patch(
                job.job_id, status=STATUS_FAILED, error=error[:400], force=True
            )
            return "failed"

        # idle：用完整消息历史重建 HC 或 Phase H 的最终结果。recovering=True
        # 会复用已有的未决 HC 事件，避免 Gateway 重启时重复生成审核卡。
        nudge_msg = await self._handle_idle(job.job_id, messages, recovering=True)
        if nudge_msg is not None:
            # Agent 偶发 idle + Gateway 恰好重启 → 注入催促消息并恢复 SSE 订阅。
            try:
                await self._send_message(job.ma_session_id, nudge_msg)
                log.info(
                    "topic6 recovery nudge sent job=%s session=%s",
                    job.job_id, job.ma_session_id,
                )
            except Exception as err:  # noqa: BLE001
                log.error(
                    "topic6 recovery nudge failed job=%s: %s", job.job_id, err,
                )
                self._store.mark_failed(
                    job.job_id, f"恢复续跑失败：{err!s}"[:400],
                )
                await self._render_and_patch(
                    job.job_id, status=STATUS_FAILED,
                    error=f"恢复续跑失败：{err!s}"[:400], force=True,
                )
                current = self._store.get_job(job.job_id)
                return current.status if current is not None else "failed"
            self._restore_stream(job)
            return "nudged"
        current = self._store.get_job(job.job_id)
        return current.status if current is not None else "missing"

    def _restore_stream(self, job: PipelineJob) -> None:
        """为仍未见终态的 Session 恢复 SSE，不注入任何新消息。"""
        existing = self._active_streams.get(job.ma_session_id)
        if existing is not None and not existing.done():
            return
        self._progress.setdefault(
            job.job_id, _ProgressState(started_at_wall=float(job.started_at))
        )
        self._card_locks.setdefault(job.job_id, asyncio.Lock())
        task = self._spawn_stream(job, first_user_message=None)
        self._active_streams[job.ma_session_id] = task
        log.info(
            "topic6 restored SSE subscription job=%s session=%s",
            job.job_id,
            job.ma_session_id,
        )

    # ---- SSE 消费 ----------------------------------------------------------

    def _spawn_stream(
        self, job: PipelineJob, first_user_message: Optional[str]
    ) -> asyncio.Task:
        loop = self._loop or asyncio.get_event_loop()
        return loop.create_task(self._drive_session(job, first_user_message))

    async def _drive_session(
        self, job: PipelineJob, first_user_message: Optional[str]
    ) -> None:
        """消费 MA 事件流；瞬时网络断线时重连但不重复发送 user.message。"""
        session_id = job.ma_session_id
        job_id = job.job_id
        sent_initial_message = first_user_message is None
        seen_event_ids: set[str] = set()
        try:
            for attempt in range(SSE_RECONNECT_MAX_ATTEMPTS + 1):
                try:
                    async with self._ark._open_event_stream(session_id) as stream:
                        if not sent_initial_message:
                            await self._send_message(session_id, first_user_message)
                            sent_initial_message = True
                        terminal = await self._consume_events(
                            job_id, stream, seen_event_ids=seen_event_ids
                        )
                        if terminal:
                            return
                        raise RuntimeError("方舟事件流意外结束，未观察到 Session 终态")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt >= SSE_RECONNECT_MAX_ATTEMPTS:
                        raise
                    delay = min(
                        SSE_RECONNECT_BASE_DELAY_SEC * (2**attempt),
                        SSE_RECONNECT_MAX_DELAY_SEC,
                    )
                    # 20% 抖动，既避免同步重连，也让最长等待受上限约束。
                    delay *= random.uniform(0.8, 1.2)
                    log.warning(
                        "topic6 SSE disconnected; reconnecting job=%s session=%s "
                        "attempt=%s/%s delay=%.1fs",
                        job_id,
                        session_id,
                        attempt + 1,
                        SSE_RECONNECT_MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            log.info("topic6 stream cancelled job=%s session=%s", job_id, session_id)
            raise
        except Exception as error:  # noqa: BLE001 - 网关层兜底
            log.exception("topic6 stream failed job=%s: %s", job_id, error)
            reason = f"sse_stream_error: {error!s}"[:400]
            self._store.mark_failed(job_id, reason)
            await self._render_and_patch(
                job_id, status=STATUS_FAILED, error=reason, force=True
            )
        finally:
            # 消费退出即清引用,避免 dict 里挂着 done Task。
            if self._active_streams.get(session_id) is asyncio.current_task():
                self._active_streams.pop(session_id, None)

    async def _consume_events(
        self,
        job_id: str,
        stream,
        *,
        seen_event_ids: Optional[set[str]] = None,
    ) -> bool:
        """SSE 事件循环:tool 事件 → 环形缓冲 + 节流 patch;terminal 走终态渲染。"""
        collected_messages: list[str] = []
        async for event in stream:
            event_id = event.get("id")
            if isinstance(event_id, str) and event_id:
                if seen_event_ids is not None:
                    if event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)
            etype = event.get("type") or ""

            # 阶段推进标记:coordinator 在切阶段时会调 `phase.mark` 之类的工具或直接发一句
            # `[phase] C1`,我们简单从 agent.message 里找 `[phase] X` 前缀更新 current_phase。
            phase_changed = False
            if etype == "agent.message":
                body = event_text(event)
                if body:
                    collected_messages.append(body)
                    self._maybe_update_project_dir(job_id, body)
                    phase_changed = self._maybe_update_phase(job_id, body)

            # 进度事件:塞进环形缓冲,由 patch 节流器决定何时刷卡片。
            progress = event_progress(event)
            if progress:
                self._append_progress_line(job_id, progress)

            if progress or phase_changed:
                # phase 切换是低频关键事件,强制立即刷,避免用户看到过时 phase。
                await self._render_and_patch(job_id, status=STATUS_RUNNING, force=phase_changed)

            # 模型请求成功一次即说明 provider 已恢复：重置 provider 重试额度。
            if etype == "span.model_request_end" and not event.get("is_error"):
                _ok_state = self._progress.get(job_id)
                if _ok_state is not None and _ok_state.provider_retry_count:
                    log.info(
                        "topic6 provider recovered job=%s (retries used %d), reset counter",
                        job_id, _ok_state.provider_retry_count,
                    )
                    _ok_state.provider_retry_count = 0

            if etype == "agent.tool_result":
                _sb_state = self._progress.get(job_id)
                if _sb_state is not None:
                    if _is_sandbox_tool_error(event):
                        _sb_state.sandbox_error_streak += 1
                    else:
                        _sb_state.sandbox_error_streak = 0

            if etype == "user.interrupt":
                current = self._store.get_job(job_id)
                if current and current.status == STATUS_STOPPING:
                    continue
                # 方舟控制台的 interrupt 只是暂停本轮，Session 仍可继续；不可逆停止只来自
                # 卡片「停止任务」(先置 stopping，上面已跳过)。记为可恢复失败，允许 /continue。
                reason = "用户在方舟后台中断了当前轮次，可发送 /continue 继续"
                self._store.mark_failed(job_id, reason)
                await self._render_and_patch(
                    job_id, status=STATUS_FAILED, error=reason, force=True
                )
                return True

            # 失败终态。
            if etype in (
                "session.error",
                "session.status_failed",
                "session.status_terminated",
            ):
                error = event_error(event) or (
                    "session_terminated"
                    if etype == "session.status_terminated"
                    else "session_failed"
                )
                # retry_status=retrying:服务端仍在自动重试，继续监听，不收口。
                # 必须先于下面的 provider 重试判断：平台自己在重试时 Gateway 不能再叠加
                # 一次「等待 + 催促」(双重重试会多吃一份额度并打乱 Agent 当前轮次)。
                if etype == "session.error" and _error_retry_status(event) == "retrying":
                    log.warning(
                        "topic6 session.error retrying job=%s: %s", job_id, error[:200],
                    )
                    continue

                # model_request_failed_error 是 provider 侧临时问题（速率限制/超载），
                # 不是 session 的永久终态。延迟后自动重试，避免频繁请求加剧速率限制。
                if etype == "session.error" and "model_request_failed" in error:
                    _state = self._progress.get(job_id)
                    _job = self._store.get_job(job_id)
                    if (
                        _state is not None
                        and _job is not None
                        and _state.provider_retry_count < _MAX_PROVIDER_RETRIES
                    ):
                        _state.provider_retry_count += 1
                        delay = min(30 * (2 ** (_state.provider_retry_count - 1)), 300)
                        log.warning(
                            "topic6 provider error (retry %d/%d in %ds) job=%s: %s",
                            _state.provider_retry_count, _MAX_PROVIDER_RETRIES,
                            delay, job_id, error[:200],
                        )
                        await self._render_and_patch(
                            job_id, status=STATUS_RUNNING,
                            error=f"模型服务暂时不可用，{delay}s 后自动重试（{_state.provider_retry_count}/{_MAX_PROVIDER_RETRIES}）",
                            force=True,
                        )
                        await asyncio.sleep(delay)
                        try:
                            await self._send_message(
                                _job.ma_session_id,
                                "[系统] 模型服务已恢复，请继续执行当前阶段。",
                            )
                            log.info(
                                "topic6 provider retry sent job=%s retry=%d",
                                job_id, _state.provider_retry_count,
                            )
                        except Exception as retry_err:  # noqa: BLE001
                            log.error(
                                "topic6 provider retry send_message failed job=%s: %s",
                                job_id, retry_err,
                            )
                            self._store.mark_failed(job_id, f"自动重试失败：{retry_err!s}"[:400])
                            await self._render_and_patch(
                                job_id, status=STATUS_FAILED,
                                error=f"自动重试失败：{retry_err!s}"[:400], force=True,
                            )
                            return True
                        _state.provider_retry_pending = True
                        # 重试成功——不 return，继续 SSE 消费循环
                        continue

                # 非 provider 错误，或重试次数耗尽——走终态；是否可恢复按方舟语义判定
                scope = _session_failure_scope(etype, event)
                log.error(
                    "topic6 session terminal job=%s etype=%s scope=%s error=%s",
                    job_id, etype, scope, error[:400],
                )
                self._store.mark_failed(
                    job_id,
                    error[:400],
                    failure_scope=scope,
                )
                await self._render_and_patch(
                    job_id, status=STATUS_FAILED, error=error[:400], force=True
                )
                return True

            # 空闲终态:HC 或最终完成——也可能是 Agent 偶发提前 idle。
            if etype == "session.status_idle" and not event_requires_action(event):
                current = self._store.get_job(job_id)
                if current and current.status == STATUS_STOPPING:
                    return True
                _idle_state = self._progress.get(job_id)
                if (
                    _idle_state is not None
                    and _idle_state.provider_retry_pending
                    and _idle_stop_reason(event) == "retries_exhausted"
                ):
                    # 与刚处理的 session.error 是同一次故障，provider 重试已唤醒 session。
                    _idle_state.provider_retry_pending = False
                    log.info(
                        "topic6 idle(retries_exhausted) absorbed by provider retry job=%s",
                        job_id,
                    )
                    continue
                if _idle_state is not None:
                    _idle_state.provider_retry_pending = False
                nudge_msg = await self._handle_idle(
                    job_id, collected_messages,
                )
                if nudge_msg is None:
                    # 真正的终态（HC / done / 超限 stopped），退出事件循环。
                    return True
                # L1 自动续跑：Agent 偶发中断，注入催促消息唤醒 session。
                # send_message 会让 idle session 切回 running，SSE 继续收到新事件。
                job = self._store.get_job(job_id)
                if not job:
                    return True
                try:
                    await self._send_message(job.ma_session_id, nudge_msg)
                except Exception as err:  # noqa: BLE001
                    log.error(
                        "topic6 idle nudge send_message failed job=%s session=%s: %s",
                        job_id, job.ma_session_id, err,
                    )
                    reason = f"自动续跑失败（send_message 异常）：{err!s}"[:400]
                    self._store.mark_failed(job_id, reason)
                    await self._render_and_patch(
                        job_id, status=STATUS_FAILED, error=reason, force=True,
                    )
                    return True
        return False

    async def _handle_idle(
        self, job_id: str, collected: list[str], *, recovering: bool = False
    ) -> Optional[str]:
        """Session 空闲了:看最后一条 agent.message 是否落了 HC JSON。

        返回值：
        - None：已处理终态（HC / done / stopped），调用方应退出事件循环。
        - str：Agent 偶发中断，返回催促消息文本，调用方注入后继续消费 SSE。
        """
        job = self._store.get_job(job_id)
        if not job:
            log.warning("topic6 handle_idle 找不到 job=%s", job_id)
            return None
        last = collected[-1] if collected else ""
        payload = extract_hc_payload(last)
        if payload:
            hc_kind = str(payload["hc"])
            # HC 正常识别说明之前的 nudge（如有）是成功的——Agent 已恢复并推进到
            # 审核卡点。重置 nudge 计数器，给 HC 通过后的后续阶段同等容错额度。
            state = self._progress.get(job_id)
            if state is not None and state.idle_nudge_count > 0:
                log.info(
                    "topic6 idle nudge counter reset job=%s hc=%s (was %d)",
                    job_id, hc_kind, state.idle_nudge_count,
                )
                state.idle_nudge_count = 0
            payload_project_dir = normalize_project_dir(payload.get("project_dir"))
            if payload_project_dir:
                self._maybe_update_project_dir(
                    job_id, f"[project_dir] {payload_project_dir}"
                )
                job = self._store.get_job(job_id) or job
            payload_error = validate_hc_payload(payload)
            # Recovery 路径已有 pending HC 时跳过校验——该 payload 创建时已通过验证，
            # 重新验证可能因为缺少运行时字段（如 wide_table_path）而误 reject。
            existing_hc = self._store.latest_hc_for_job(job_id, hc_kind)
            if recovering and existing_hc and existing_hc.resolved_at is None:
                payload_error = None
            if payload_error:
                log.error(
                    "topic6 invalid HC payload job=%s hc=%s: %s",
                    job_id,
                    hc_kind,
                    payload_error,
                )
                self._store.mark_failed(job_id, payload_error)
                await self._render_and_patch(
                    job_id,
                    status=STATUS_FAILED,
                    error=payload_error,
                    force=True,
                )
                await self._reply_async_by_job(job_id, f"❌ {payload_error}")
                return None
            if recovering and existing_hc and existing_hc.resolved_at is None:
                # Gateway 断线后本地已记下 HC，但卡片 patch 尚未送达或旧卡不可见。
                # 重建同一条审核事件，不能 append，否则同一个按钮会生成两段决策审计。
                self._store.mark_wait_hc(job_id, hc_kind)
                job = self._store.get_job(job_id) or job
                if self._card_sender is not None:
                    message_id = await self._card_sender.send_hc_card(
                        job, hc_kind, existing_hc.id, existing_hc.payload
                    )
                    if message_id and message_id != existing_hc.card_message_id:
                        self._store._conn.execute(  # noqa: SLF001 - runner 与 store 同包
                            "UPDATE pipeline_hc_events SET card_message_id = ? WHERE id = ?",
                            (message_id, existing_hc.id),
                        )
                else:
                    await self._render_and_patch(
                        job_id, status=STATUS_WAIT_HC, force=True
                    )
                log.info(
                    "topic6 restored pending hc card job=%s hc=%s event=%s",
                    job_id,
                    hc_kind,
                    existing_hc.id,
                )
                return None
            event_id = self._store.append_hc_event(job_id, hc_kind, payload)
            self._store.mark_wait_hc(job_id, hc_kind)
            job = self._store.get_job(job_id) or job
            if self._card_sender is None:
                # 兜底:没注入 HC 卡片发送器,退回一条纯文本让用户手工继续。
                await self._render_and_patch(job_id, status=STATUS_WAIT_HC, force=True)
                await self._reply_async_by_job(
                    job_id,
                    f"⏸️ 等你审 {hc_kind}(卡片发送器未注入,发送 /continue 注入「继续」推进该阶段)",
                )
                return None
            try:
                message_id = await self._card_sender.send_hc_card(job, hc_kind, event_id, payload)
                # HC 与运行进度复用同一个 message_id；写回事件便于回调审计与兜底反查。
                if message_id:
                    self._store._conn.execute(  # noqa: SLF001 - runner 与 store 同包
                        "UPDATE pipeline_hc_events SET card_message_id = ? WHERE id = ?",
                        (message_id, event_id),
                    )
            except Exception as error:  # noqa: BLE001 - 卡片失败仍算 wait_hc,人工兜底
                log.exception("topic6 send hc card failed job=%s hc=%s: %s", job_id, hc_kind, error)
                await self._render_and_patch(job_id, status=STATUS_WAIT_HC, force=True)
                await self._reply_async_by_job(
                    job_id, f"⚠️ {hc_kind} 卡片发送失败({error!s});可回复 pass/reject 手工继续"
                )
            return None

        # ---- L1 自动续跑：检测 Agent 偶发提前 idle ----
        # 条件：不是 HC payload、不是 Phase H 最终结果、pipeline 尚未完成。
        # 排除条件：current_phase 是 HC 卡点（HC idle 是等人审，不催）。
        online_url = extract_final_online_url(last)
        if not online_url and (job.current_phase or "") == "H" and job.ma_session_id:
            # 最后一条不含发布 JSON（如收尾说明/裁决文本），但本 Session 早先已交付过。
            online_url = await self._find_published_online_url(
                job.ma_session_id, self._history_floor(job_id)
            )
        if online_url:
            # Phase H 正常完成。
            self._store.mark_done(job_id, online_url=online_url)
            await self._render_and_patch(
                job_id, status=STATUS_DONE, online_url=online_url, force=True
            )
            self._progress.pop(job_id, None)
            return None

        # P0-4: Phase H idle 但无 online_url → 妙搭发布专项诊断，直接走终态。
        phase = job.current_phase or "A"
        if phase == "H" and not online_url:
            last_summary = (last[:300] + "…") if len(last) > 300 else (last or "(无输出)")
            reason = (
                f"Phase H 妙搭发布失败：Agent idle 但未输出有效 online_url。"
                f"\n最后输出：{last_summary}"
            )[:400]
            log.error(
                "topic6 phase_h publish failed job=%s last=%s",
                job_id, last_summary,
            )
            self._store.mark_failed(job_id, reason)
            await self._render_and_patch(
                job_id, status=STATUS_FAILED, error=reason, force=True
            )
            await self._reply_async_by_job(
                job_id,
                f"❌ 妙搭发布失败，Agent 未输出有效的发布链接。\n"
                f"可发送 /continue 重试，或联系管理员检查 skill 配置。",
            )
            return None

        # Agent 主动请求用户裁决（[need_decision]）：不是中断，不催促。
        # 标记可恢复 failed 并把报告原文发群里，用户用 `/continue <选择>` 续跑。
        # stopped 专指用户确认的不可逆停止，/continue 不会恢复。
        if NEED_DECISION_MARKER in last:
            reason = f"Phase {phase} {NEED_DECISION_REASON_MARK}（Agent 已停下给出选项）"
            log.warning("topic6 need_decision job=%s phase=%s", job_id, phase)
            self._store.mark_failed(job_id, reason)
            await self._render_and_patch(
                job_id, status=STATUS_FAILED, error=reason, force=True
            )
            report = last.replace(NEED_DECISION_MARKER, "").strip()
            if len(report) > NEED_DECISION_REPLY_LIMIT:
                report = report[:NEED_DECISION_REPLY_LIMIT] + "\n…（已截断，完整内容见 /log）"
            await self._reply_async_by_job(
                job_id,
                f"⏸️ 需要你裁决（job={job_id}）：\n{report}\n\n"
                f"请回复 `/continue <你的选择>` 继续，例如 `/continue A`。",
            )
            return None

        # 方舟沙箱故障(平台侧)：工具连续返回 task service unavailable / sandbox abnormal。
        # 催促只会让 Agent 反复探测、白耗上下文；直接收口为可恢复失败，平台恢复后 /continue。
        _sb = self._progress.get(job_id)
        if _sb is not None and _sb.sandbox_error_streak >= SANDBOX_ERROR_STREAK_LIMIT:
            reason = (
                f"方舟沙箱暂不可用（平台侧故障，连续 {_sb.sandbox_error_streak} 次工具调用返回"
                f" task service unavailable / sandbox abnormal），Pipeline 停在 Phase {phase}。"
                "已完成的产物与审核记录保持不变；平台恢复后发送 /continue 继续。"
            )[:400]
            log.error(
                "topic6 sandbox unavailable job=%s phase=%s streak=%d",
                job_id, phase, _sb.sandbox_error_streak,
            )
            _sb.sandbox_error_streak = 0
            self._store.mark_failed(job_id, reason)
            await self._render_and_patch(
                job_id, status=STATUS_FAILED, error=reason, force=True
            )
            await self._reply_async_by_job(job_id, f"⚠️ {reason}")
            return None

        # P0-1 第三优先级：结构化诊断（在 HC + Phase H 之后，nudge 之前）
        requires_human, diagnosis_summary = self._diagnose_interruption(
            job_id, phase, collected,
        )
        if requires_human:
            reason = diagnosis_summary[:400]
            log.error(
                "topic6 diagnosis requires_human job=%s phase=%s: %s",
                job_id, phase, reason,
            )
            self._store.mark_failed(job_id, reason)
            await self._render_and_patch(
                job_id, status=STATUS_FAILED, error=reason, force=True
            )
            await self._reply_async_by_job(job_id, f"⚠️ {reason}")
            return None

        state = self._progress.get(job_id)
        is_hc_phase = phase.startswith(_HC_PHASE_PREFIXES)
        can_nudge = (
            state is not None
            and state.idle_nudge_count < MAX_IDLE_NUDGES
            and not is_hc_phase
        )

        if can_nudge:
            assert state is not None  # guarded by can_nudge condition
            state.idle_nudge_count += 1
            nudge_n = state.idle_nudge_count
            log.warning(
                "topic6 idle nudge job=%s phase=%s nudge=%d/%d last_msg=%s",
                job_id, phase, nudge_n, MAX_IDLE_NUDGES,
                (last[:120] + "…") if len(last) > 120 else last,
            )
            # 卡片显示自动恢复状态
            state.push_tool_line(
                f"{time.strftime('%H:%M:%S')} · ⚡ Agent 暂停，正在自动恢复（{nudge_n}/{MAX_IDLE_NUDGES}）"
            )
            await self._render_and_patch(job_id, status=STATUS_RUNNING, force=True)
            # 返回催促消息——Coordinator 读到后应从当前 phase 继续执行。
            return (
                f"你在 Phase {phase} 中途停止了，请从当前阶段继续执行。"
                f"不要重复已完成的步骤，从上次输出之后的下一步继续。"
            )

        # ---- L2 结构化诊断：超限或不可催促，走终态 ----
        nudge_count = state.idle_nudge_count if state else 0
        last_summary = (last[:200] + "…") if len(last) > 200 else (last or "(无 agent 输出)")
        reason = (
            f"Pipeline 在 Phase {phase} 中断"
            f"（自动续跑 {nudge_count}/{MAX_IDLE_NUDGES} 次后仍未恢复）。"
            f"\n最后输出：{last_summary}"
        )[:400]
        log.error(
            "topic6 pipeline stalled job=%s phase=%s nudges=%d last=%s",
            job_id, phase, nudge_count, last_summary,
        )
        self._store.mark_failed(job_id, reason)
        await self._render_and_patch(
            job_id, status=STATUS_FAILED, error=reason, force=True
        )
        return None

    # ---- helper --------------------------------------------------------------

    def _maybe_update_phase(self, job_id: str, body: str) -> bool:
        """扫 body 里的 `[phase] X` 前缀更新 current_phase;有变化返回 True。"""
        match = re.search(r"\[phase\]\s+([A-Z0-9]+)", body)
        if not match:
            return False
        new_phase = match.group(1)
        job = self._store.get_job(job_id)
        if job and job.current_phase == new_phase:
            return False
        self._store.update_phase(job_id, new_phase)
        return True

    def _maybe_update_project_dir(self, job_id: str, body: str) -> bool:
        project_dir = extract_project_dir(body)
        if not project_dir:
            return False
        job = self._store.get_job(job_id)
        if not job or job.project_dir == project_dir:
            return False
        if job.project_dir:
            log.warning(
                "topic6 project_dir changed job=%s old=%s new=%s",
                job_id,
                job.project_dir,
                project_dir,
            )
        self._store.update_project_dir(job_id, project_dir)
        log.info("topic6 project_dir resolved job=%s path=%s", job_id, project_dir)
        return True

    def _append_progress_line(self, job_id: str, progress: str) -> None:
        """把一条 progress 事件塞进环形缓冲,带时间戳前缀。"""
        state = self._progress.get(job_id)
        if state is None:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        state.push_tool_line(f"{ts} · {progress}")

    def _diagnose_interruption(
        self, job_id: str, phase: str, collected: list[str],
    ) -> tuple[bool, str]:
        """P0-1 结构化诊断：分析 idle 中断原因，返回 (requires_human, summary)。

        requires_human=True 时调用方应标记可恢复 failed 而非 nudge。
        插入位置：HC 和 Phase H 检测之后、nudge 之前（第三优先级）。
        """
        state = self._progress.get(job_id)
        elapsed = time.time() - state.started_at_wall if state else 0
        last = collected[-1] if collected else ""
        nudge_count = state.idle_nudge_count if state else 0

        # 启动阶段失败：Session 创建 <60 秒就 idle，collected 为空
        if elapsed < 60 and not collected:
            return True, (
                f"Pipeline 在启动阶段（Phase {phase}）即中断，"
                f"Session 创建后 {int(elapsed)} 秒内无任何 Agent 输出。"
                f"请检查环境配置、Agent ID 和飞书授权状态。"
            )

        # 错误模式检测：Agent 最后输出含明确错误关键词
        error_patterns = ("error", "failed", "超时", "timeout", "quota", "rate limit", "异常")
        last_lower = last.lower()
        if any(p in last_lower for p in error_patterns):
            # 但如果 nudge 还没用完，先让 nudge 试一试（可能是临时性错误）
            if nudge_count >= MAX_IDLE_NUDGES:
                return True, (
                    f"Pipeline 在 Phase {phase} 中断，Agent 输出包含错误信息"
                    f"（自动续跑 {nudge_count} 次均未恢复）。"
                    f"\n最后输出：{last[:200]}"
                )

        # 默认不阻断，让 nudge 机制继续尝试
        return False, ""

    def _review_lines(self, job_id: str) -> list[str]:
        """格式化已落库的 HC 审核轨迹，供后续运行态和终态持续展示。"""
        decision_meta = {
            "pass": ("✅", "通过"),
            "reject": ("❌", "打回"),
            "remark": ("📝", "备注通过"),
        }
        lines: list[str] = []
        for event in self._store.list_resolved_hc_for_job(job_id):
            emoji, label = decision_meta.get(
                event.user_decision, ("•", event.user_decision or "已处理")
            )
            operator = event.operator_label or "审核人未记录"
            resolved_at = time.strftime(
                "%H:%M",
                time.localtime(event.resolved_at or event.created_at),
            )
            lines.append(
                f"{event.hc_kind} · {emoji} {label} · {operator} · {resolved_at}"
            )
        return lines

    def _report_doc_url(self, job_id: str) -> str:
        """最近一次 HC3 的飞书报告链接(HC3 打回 A 会生成新文档,取最新一条)。

        报告建在机器人云空间、HC3 卡通过后被进度卡覆盖,终态卡与通知是用户找到它的唯一入口。
        HC3 发卡前 validate_hc_payload 已保证 feishu_doc_url 非空;未到 HC3 返回空串。
        """
        try:
            event = self._store.latest_hc_for_job(job_id, "HC3")
        except Exception as err:  # noqa: BLE001 - 查不到不影响终态卡
            log.warning("topic6 report doc url lookup failed job=%s: %s", job_id, err)
            return ""
        if event is None:
            return ""
        return str((event.payload or {}).get("feishu_doc_url") or "").strip()

    async def _render_and_patch(
        self,
        job_id: str,
        *,
        status: str,
        error: str = "",
        online_url: str = "",
        force: bool = False,
    ) -> None:
        """按当前 state 渲染卡片并 patch;节流由 last_patch_at 决定,force=True 时无视节流。

        没有 message_id(初次 send 失败)时跳过 — 用户看不到卡片但 SSE 消费不受影响。
        """
        state = self._progress.get(job_id)
        job = self._store.get_job(job_id)
        if state is None or job is None or not job.progress_card_message_id:
            return
        if job.status in (STATUS_STOPPING, STATUS_STOPPED):
            status = job.status
            error = job.last_error or error
        now = time.monotonic()
        if not force and now - state.last_patch_at < PROGRESS_MIN_INTERVAL_SEC:
            return
        elapsed_sec = int(time.time() - state.started_at_wall)
        terminal = status in (STATUS_DONE, STATUS_FAILED, STATUS_STOPPED)
        if terminal and not state.process_files_scheduled:
            # 后台归档过程关键文件，不阻塞卡片/SSE，也不受卡片 patch 失败影响；每个终态只调度一次
            state.process_files_scheduled = True
            asyncio.get_running_loop().create_task(self._deliver_process_files(job_id))
        report_doc_url = self._report_doc_url(job_id) if terminal else ""
        card = build_progress_card(
            job=job,
            status=status,
            elapsed_sec=elapsed_sec,
            tool_lines=list(state.tool_lines),
            review_lines=self._review_lines(job_id),
            overflow=state.overflow,
            error=error,
            online_url=online_url,
            report_doc_url=report_doc_url,
        )
        signature = self._signature(card)
        if signature == state.last_signature and not force:
            # 内容没变(比如相邻两次 progress 完全同文),省一次 API 调用。
            return
        try:
            await self.patch_job_card(job_id, card)
        except Exception as error_patch:  # noqa: BLE001 - patch 失败不该拖垮 SSE
            log.warning(
                "topic6 progress card patch failed job=%s: %s", job_id, error_patch
            )
            return
        state.last_patch_at = now
        state.last_signature = signature

        # 终态统一通知：@ 任务发起人告知结果
        if terminal and self._notifier is not None:
            try:
                if status == STATUS_DONE:
                    self._notifier.notify_done(
                        job, online_url=online_url, report_doc_url=report_doc_url)
                elif status == STATUS_FAILED:
                    self._notifier.notify_failed(
                        job, error=error, report_doc_url=report_doc_url)
                elif status == STATUS_STOPPED:
                    self._notifier.notify_stopped(
                        job, reason=error, report_doc_url=report_doc_url)
            except Exception as notify_err:  # noqa: BLE001
                log.warning("topic6 terminal notification failed job=%s: %s", job_id, notify_err)

    async def patch_job_card(self, job_id: str, card: dict) -> Optional[str]:
        """串行 patch 当前任务唯一的交互卡片；没有 message_id 时返回 None。"""
        job = self._store.get_job(job_id)
        if job is None or not job.progress_card_message_id:
            return None
        lock = self._card_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            await self._loop_run(
                lambda: self._feishu.patch_interactive_card(
                    job.progress_card_message_id, card
                )
            )
        state = self._progress.get(job_id)
        if state is not None:
            state.last_signature = self._signature(card)
            state.last_patch_at = time.monotonic()
        return job.progress_card_message_id

    @staticmethod
    def _signature(card: dict) -> str:
        return json.dumps(card, ensure_ascii=False, sort_keys=True)

    async def _loop_run(self, fn):
        """在 executor 里跑同步的 feishu SDK 调用,避免阻塞事件循环。"""
        loop = self._loop or asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn)

    async def _reply_async_by_job(self, job_id: str, text: str) -> None:
        job = self._store.get_job(job_id)
        if job:
            await self._reply_async(
                job.chat_id, text, root_message_id=job.root_message_id
            )

    async def _reply_async(
        self, chat_id: str, text: str, root_message_id: str = ""
    ) -> None:
        loop = self._loop or asyncio.get_event_loop()
        try:
            if root_message_id:
                await loop.run_in_executor(
                    None,
                    lambda: self._feishu.reply_in_thread(root_message_id, text),
                )
            else:
                await loop.run_in_executor(
                    None, lambda: self._feishu.send_to_chat(chat_id, text)
                )
        except Exception as error:  # noqa: BLE001 - 回帖失败不该拖垮 SSE 消费
            log.warning("topic6 reply failed chat=%s: %s", chat_id, error)


class Topic6RunnerError(RuntimeError):
    """topic6 触发/续跑相关的业务错误。"""


class Topic6CardSenderProtocol:
    """topic6_hitl 会实现的审核态切换接口(避免 runner ↔ hitl 循环 import)。"""

    async def send_hc_card(
        self,
        _job: PipelineJob,
        _hc_kind: str,
        _event_id: int,
        _payload: dict,
    ) -> Optional[str]:
        """把任务主卡切到 HC 状态,返回复用的飞书 message_id(供反查)。"""
        raise NotImplementedError


FEISHU_HOST_SUFFIXES = ("feishu.cn", "larkoffice.com", "larksuite.com")
# 飞书云文档类路径：Phase F 文档链接常被误填为 online_url。妙搭 html 应用的
# /page/<meta_token> 不在此列，必须放行。
FEISHU_DOC_PATH_RE = re.compile(
    r"^/(docx|docs|doc|wiki|sheets|sheet|base|bitable|file|drive|mindnotes|slides)(/|$)"
)


def _is_feishu_doc_url(hostname: str, path: str) -> bool:
    on_feishu = any(
        hostname == suffix or hostname.endswith("." + suffix)
        for suffix in FEISHU_HOST_SUFFIXES
    )
    return on_feishu and FEISHU_DOC_PATH_RE.match(path) is not None


def extract_final_online_url(text: str) -> str:
    """读取 Phase H 结构化结果中的妙搭线上 URL。

    自由文本中的 URL 不可信：报告正文通常先出现飞书 docx 链接，而且 Markdown /
    JSON 标点会被宽松正则一并吞入。这里只接受显式 ``online_url`` 字段。

    不做域名白名单：妙搭按应用类型/租户返回不同格式（``*.aiforce.cloud``、
    ``*.feishuapp.cn/app/app_*``、``{租户}.feishu.cn/page/{meta_token}`` 等），
    以发布 API 返回值为准。只拦截确定无效的值：非 http(s)（本地路径等）、
    ``release_status`` 显式非 finished、飞书云文档链接。
    """
    if not text:
        return ""
    decoder = json.JSONDecoder()
    for start in (match.start() for match in re.finditer(r"\{", text)):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        candidate = str(value.get("online_url") or "").strip()
        if not candidate:
            continue
        release_status = str(value.get("release_status") or "").strip().lower()
        if release_status and release_status != "finished":
            continue
        # P0-4 URL 清洗：Agent 偶发把 JSON 结尾 "} 误拼进 URL（如 %22%7D）。
        # 先 URL 解码还原，再清洗明确的非 URL 尾部字符。
        decoded = unquote(candidate)
        cleaned = re.sub(r'["\'})\]\s]+$', "", decoded)
        if cleaned != candidate:
            cleaned_parsed = urlsplit(cleaned)
            if cleaned_parsed.scheme in ("http", "https") and cleaned_parsed.netloc:
                candidate = cleaned
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme in ("http", "https")
            and hostname
            and not _is_feishu_doc_url(hostname, parsed.path)
        ):
            return candidate
    return ""
