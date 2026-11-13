"""bot 上线前的权限自检:启动时明确告知缺什么,而不是等用户用到才发现。

查的是**本应用的应用身份**(tenant)权限:``GET /open-apis/application/v6/scopes``。
注意 ``lark-cli auth check`` 查的是用户身份,不能代替本检查(2026-11 实测它不支持 ``--as bot``)。

检查项按"该 bot 实际启用了什么"决定,不强加:
- 租户级 user_id(私聊鉴权):contact:user.employee_id:readonly
- session 日志写飞书文档(仅配置了 SESSION_DOC_PARENT_TOKEN 时):docx:document:create、docx:document:write_only
缺失不阻断启动,但醒目告警并说明影响。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

from .feishu import USER_ID_SCOPE, ScopeProbe

log = logging.getLogger("arkagent.preflight")

DOC_SCOPES = ("docx:document:create", "docx:document:write_only")

# 查通讯录用户需要两层:入口权限(四选一,飞书报错为"One of the following") + 用户字段权限。
# 缺入口这一层是"信息卡显示不出真名"最常见的根因,飞书返回 99991672 且错误藏在 permission_violations。
CONTACT_ENTRY_SCOPES = (
    "contact:contact.base:readonly",
    "contact:contact:access_as_app",
    "contact:contact:readonly",
    "contact:contact:readonly_as_app",
)
CONTACT_USER_PROFILE_SCOPES = ("contact:user.base:readonly",)


@dataclass(frozen=True)
class Requirement:
    scopes: tuple[str, ...]
    why: str  # 缺失时对用户的影响,要具体到"会发生什么"
    any_of: bool = False  # True = 这些权限任一个即可(入口权限四选一);False = 都要


def requirements(env: Optional[Mapping[str, str]] = None) -> list[Requirement]:
    environ = os.environ if env is None else env
    reqs = [
        Requirement(
            (USER_ID_SCOPE,),
            "取不到租户级 user_id:排障 Bot 的私聊查询会回复\"无法确认你的身份\";会话记录里的 user_id 为空",
        )
    ]
    if (environ.get("SESSION_DOC_PARENT_TOKEN") or "").strip():
        reqs.append(
            Requirement(DOC_SCOPES, "session 日志写不进飞书文档(只记 warning 日志,不影响用户收到回复)")
        )
        reqs.append(
            Requirement(
                CONTACT_ENTRY_SCOPES,
                "飞书日志信息卡显示不出用户真名/邮箱(缺通讯录访问入口权限,任一即可)",
                any_of=True,
            )
        )
        reqs.append(
            Requirement(
                CONTACT_USER_PROFILE_SCOPES,
                "飞书日志信息卡显示不出用户真名/邮箱(缺用户字段权限)",
            )
        )
    return reqs


def evaluate(app_id: str, probe: ScopeProbe, reqs: list[Requirement]) -> list[str]:
    """返回告警文本列表;全部满足返回空列表。"""
    if probe.status != "ok":
        return [
            f"[权限未确认] 应用 {app_id} 无法查询自身已开通的权限:{probe.detail}\n"
            f"  这不代表缺权限。所需权限:{', '.join(s for r in reqs for s in r.scopes)}"
        ]
    reports = []
    for req in reqs:
        if req.any_of:
            missing = req.scopes if all(s not in probe.granted for s in req.scopes) else []
            label = "以下任一"
        else:
            missing = [s for s in req.scopes if s not in probe.granted]
            label = ""
        if not missing:
            continue
        display = f"{label}:{', '.join(missing)}" if label else ", ".join(missing)
        reports.append(
            f"[权限缺失] 应用 {app_id} 未开通(应用身份):{display}\n"
            f"  影响:{req.why}\n"
            f"  处理:开放平台 → 该应用 → 权限管理 → 应用身份权限,开通后需**发布新版本**才生效。\n"
            f"  直达:https://open.feishu.cn/page/scope-apply?clientID={app_id}&scopes="
            + "%2C".join(m.replace(":", "%3A") for m in missing)
        )
    return reports


def run_preflight(sender, config, app_id: str, env: Optional[Mapping[str, str]] = None) -> None:
    reqs = requirements(env)
    try:
        lister = getattr(sender, "list_app_scopes", None)
        probe = lister() if lister else ScopeProbe("unknown", "当前 sender 不支持权限查询")
        reports = evaluate(app_id, probe, reqs)
    except Exception as error:  # noqa: BLE001 - 自检本身不能阻断 bot 启动
        reports = [f"[权限未确认] 自检异常:{error}"]
    if reports:
        for report in reports:
            print(report)
            log.warning(report.replace("\n", " | "))
    else:
        print(f"- 权限自检:{'、'.join(s for r in reqs for s in r.scopes)} 均已开通 ✓")


class UserIdWatcher:
    """运行时兜底:第一次收到私聊却没有 user_id 时告警一次(启动自检可能是\"未确认\")。"""

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
