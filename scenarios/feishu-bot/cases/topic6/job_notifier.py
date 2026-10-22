"""Topic6 任务通知器：统一处理所有场景的 @ 通知。

所有通知逻辑集中在此模块，由 gateway 装配时注入 FeishuSender 依赖。
调用方（runner / hitl）只需调一个方法，不需要自行拼 roster / mention。

通知场景：
  - HC 审核待处理（HC1/HC2/HC3 卡片发出后）
  - 任务完成（mark_done）
  - 任务失败（mark_failed）
  - 任务中断（mark_stopped）
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from arkagent.feishu import FeishuSender
    from topic6.pipeline_store import PipelineJob

log = logging.getLogger(__name__)

# HC 种类 → 中文标签
_HC_LABELS: dict[str, str] = {
    "HC1": "小样本人工审核",
    "HC2": "全量分布审核",
    "HC3": "报告终审",
}


class JobNotifier:
    """统一的任务通知器，封装 @ 人 + 发消息的完整逻辑。"""

    def __init__(self, feishu: FeishuSender) -> None:
        self._feishu = feishu

    # ---- 公共 API -----------------------------------------------------------

    def notify_hc_ready(self, job: PipelineJob, hc_kind: str) -> None:
        """HC 卡片已发出，@ 审核人通知有待审核任务。"""
        label = _HC_LABELS.get(hc_kind, hc_kind)
        mention = self._resolve_mention(job)
        text = (
            f"📋 **{hc_kind} · {label}** 已就绪，请审核\n"
            f"{mention} 请查看上方卡片，点击「通过」「打回」或「补充说明」完成审核。\n"
            f"任务：{job.job_id} · 模式：{job.mode}"
        )
        self._send(job.chat_id, text)

    def notify_done(self, job: PipelineJob, online_url: str = "") -> None:
        """任务完成。"""
        mention = self._resolve_mention(job)
        body = f"妙搭页面：{online_url}" if online_url else "Pipeline 已完成"
        text = f"{mention} ✅ **任务完成** · {job.mode}\n{body}\n任务：{job.job_id}"
        self._send(job.chat_id, text)

    def notify_failed(self, job: PipelineJob, error: str = "") -> None:
        """任务失败。"""
        mention = self._resolve_mention(job)
        body = error[:200] if error else "未知错误"
        text = f"{mention} ❌ **任务失败** · {job.mode}\n{body}\n任务：{job.job_id}"
        self._send(job.chat_id, text)

    def notify_stopped(self, job: PipelineJob, reason: str = "") -> None:
        """任务中断。"""
        mention = self._resolve_mention(job)
        body = reason[:200] if reason else "Pipeline 中断"
        text = f"{mention} ⚠️ **任务中断** · {job.mode}\n{body}\n任务：{job.job_id}"
        self._send(job.chat_id, text)

    # ---- 内部 ---------------------------------------------------------------

    def _resolve_mention(self, job: PipelineJob) -> str:
        """解析任务发起人的 @mention 文本。

        成功时返回 "@真名"（send_to_chat 会通过 roster 重写为可点击 <at>）。
        失败时返回空字符串，不阻塞通知。
        """
        roster = self._get_roster(job.chat_id)
        user_name = ""
        try:
            user_name = self._feishu.resolve_operator_name(
                job.user_open_id, chat_id=job.chat_id,
            )
        except Exception:  # noqa: BLE001
            pass
        if user_name and user_name in roster:
            return f"@{user_name}"
        return ""

    def _get_roster(self, chat_id: str) -> dict[str, str]:
        """获取群名册，失败返回空 dict。"""
        try:
            return self._feishu.chat_roster(chat_id)
        except Exception:  # noqa: BLE001
            return {}

    def _send(self, chat_id: str, text: str) -> None:
        """发送通知消息，失败只记日志不抛异常。"""
        roster = self._get_roster(chat_id)
        try:
            self._feishu.send_to_chat(chat_id, text, roster=roster or None)
        except Exception as err:  # noqa: BLE001
            log.warning("topic6 notification send failed chat=%s: %s", chat_id, err)
