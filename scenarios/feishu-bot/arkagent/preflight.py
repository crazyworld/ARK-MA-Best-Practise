"""bot 上线前的权限自检:启动时明确告知缺什么,而不是等用户用到才发现。

目前检查一项:能否读到租户级 user_id。排障 Bot 判断私聊"是不是本人"依赖它(open_id 按应用隔离),
读取它需要 ``contact:user.employee_id:readonly``。缺失不阻断启动,但会醒目告警,并说明影响。
"""
from __future__ import annotations

import logging
from typing import Optional

from .feishu import USER_ID_SCOPE, ScopeProbe

log = logging.getLogger("arkagent.preflight")


def check_user_id_scope(sender, config) -> ScopeProbe:
    """用已授权用户中的第一个去探测;没有可用的探测对象时返回 unknown。"""
    open_ids = tuple(getattr(config, "authorized_open_ids", ()) or ())
    if not open_ids:
        return ScopeProbe(
            "unknown",
            "没有配置 AUTHORIZED_OPEN_IDS,缺少可用于探测的用户,无法在启动时检查该权限",
        )
    probe = getattr(sender, "probe_user_id_scope", None)
    if probe is None:
        return ScopeProbe("unknown", "当前 sender 不支持权限探测")
    return probe(open_ids[0])


def format_report(app_id: str, result: ScopeProbe) -> Optional[str]:
    """返回要打印的多行告警;一切正常返回 None。"""
    if result.status == "ok":
        return None
    if result.status == "missing_scope":
        need = ", ".join(result.missing or (USER_ID_SCOPE,))
        return (
            f"[权限缺失] 应用 {app_id} 未开通:{need}\n"
            f"  影响:取不到租户级 user_id。排障 Bot 的私聊查询会回复\"无法确认你的身份\";"
            f"通用 Gateway 记录会话时 user_id 为空。\n"
            f"  处理:到飞书开放平台 → 该应用 → 权限管理,开通后需发布新版本才生效。\n"
            f"  直达:https://open.feishu.cn/page/scope-apply?clientID={app_id}&scopes="
            f"{need.replace(':', '%3A').replace(', ', '%2C')}"
        )
    return (
        f"[权限未确认] 应用 {app_id} 无法在启动时确认是否已开通 {USER_ID_SCOPE}:{result.detail}\n"
        f"  这不代表缺权限。收到第一条消息时若 user_id 为空,会再次告警。"
    )


def run_preflight(sender, config, app_id: str) -> None:
    try:
        report = format_report(app_id, check_user_id_scope(sender, config))
    except Exception as error:  # noqa: BLE001 - 自检本身不能阻断 bot 启动
        report = f"[权限未确认] 自检异常:{error}"
    if report:
        print(report)
        log.warning(report.replace("\n", " | "))
    else:
        print(f"- 权限自检:{USER_ID_SCOPE} 已开通 ✓")


class UserIdWatcher:
    """运行时兜底:第一次收到私聊却没有 user_id 时告警一次(启动探测可能是 unknown)。"""

    def __init__(self, app_id: str) -> None:
        self._app_id = app_id
        self._warned = False

    def observe(self, user_id: str, chat_type: str) -> None:
        if user_id or chat_type != "p2p" or self._warned:
            return
        self._warned = True
        log.warning(
            "[权限缺失] 应用 %s 收到私聊但事件里没有 user_id,请确认已开通并发布 %s",
            self._app_id,
            USER_ID_SCOPE,
        )
