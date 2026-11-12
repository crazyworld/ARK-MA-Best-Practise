"""排障 Agent 的 3 个只读工具。

权限在这里强制,不在提示词里:chat_id 来自飞书消息(由网关绑定),工具参数里没有
chat_id 这个字段,模型无法指定去查别的群。越权的任务一律当作\"找不到\",不暴露其存在。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from arkagent.observability.adapters.base import TraceRef, TraceSource
from arkagent.observability.detail import get_step_detail
from arkagent.observability.diagnose import diagnose
from arkagent.observability.service import EventSource, build_trace

# 参数里不允许出现的身份字段:出现就拒绝,避免模型试图自己指定范围。
FORBIDDEN_ARGS = {"chat_id", "open_id", "user_id", "tenant_key"}
LIST_LIMIT = 10
MAX_SPANS_PER_TURN = 40


@dataclass(frozen=True)
class Scope:
    """一次提问的查询范围,全部取自飞书消息,由网关绑定,模型无法修改。

    - 群聊:本群全部任务,不区分发起人。
    - 私聊:只能看**自己发起的、发生在私聊里**的任务,用租户级 user_id 判断
      (open_id 按应用隔离,排障 Bot 与业务 bot 是不同应用,不能用)。
    """

    chat_id: str
    chat_type: str  # p2p | group
    user_id: str = ""

    def allows(self, ref: TraceRef) -> bool:
        if self.chat_type != "p2p":
            # 群范围:chat_id 精确相等,且不是**明确标成私聊**的记录。
            # chat_type 为空(升级前的旧数据,无法确认群还是私聊)按用户决策放行——它依赖\"私聊的
            # chat_id 只出现在该私聊自己的会话里\"这一假设,不是强校验。新数据都带 chat_type,
            # 不受此影响;私聊范围对旧数据则一律不放行(见下)。
            return ref.chat_id == self.chat_id and ref.chat_type != "p2p"
        # 私聊:缺 user_id 或任务没记 user_id(旧数据 / 未开通权限)一律不放行,宁可查不到。
        return bool(
            self.user_id
            and ref.user_id
            and ref.user_id == self.user_id
            and ref.chat_type == "p2p"
        )

    def unavailable_reason(self) -> str:
        if self.chat_type == "p2p" and not self.user_id:
            return "无法确认你的身份(应用未开通\"获取用户 user ID\"权限),私聊暂时无法查询;请在群聊里使用"
        return ""


class DiagnoseTools:
    def __init__(self, sources: dict[str, TraceSource], events: EventSource) -> None:
        self._sources = sources
        self._events = events

    async def call(self, scope: Scope, name: str, arguments: dict) -> tuple[dict, bool]:
        """返回 (结果, 是否出错)。"""
        reason = scope.unavailable_reason()
        if reason:
            return {"found": False, "message": reason}, False
        bad = FORBIDDEN_ARGS & set(arguments)
        if bad:
            return {"error": f"参数不允许包含 {sorted(bad)}:查询范围固定为当前会话"}, True
        handler: Optional[Callable[[Scope, dict], Awaitable[dict]]] = {
            "list_recent_tasks": self._list_recent,
            "diagnose_task": self._diagnose,
            "get_step_detail": self._step_detail,
        }.get(name)
        if handler is None:
            return {"error": f"未知工具 {name}"}, True
        try:
            return await handler(scope, arguments), False
        except Exception as error:  # noqa: BLE001 - 工具失败要让 Agent 看到,而不是让会话崩溃
            return {"error": f"{type(error).__name__}: {error}"[:300]}, True

    # ---- 范围控制 -----------------------------------------------------------
    def _refs_in_scope(self, scope: Scope, status: Optional[str] = None) -> list[TraceRef]:
        refs: list[TraceRef] = []
        for source in self._sources.values():
            # 私聊没有 chat_id 过滤的意义(每个人私聊的 chat_id 不同,但同一人可能有多个 bot),
            # 取全量后由 scope.allows 逐条判断,保证权限只有一处实现。
            pool = source.list_refs(
                limit=LIST_LIMIT * 5,
                status=status,
                chat_id=scope.chat_id if scope.chat_type != "p2p" else None,
            )
            refs += [r for r in pool if scope.allows(r)]
        return _dedupe(refs)

    def _resolve_in_scope(self, scope: Scope, ident: str) -> Optional[TraceRef]:
        for source in self._sources.values():
            ref = source.resolve(ident)
            if ref and scope.allows(ref):
                return ref
        return None

    # ---- 工具 ---------------------------------------------------------------
    async def _list_recent(self, scope: Scope, args: dict) -> dict:
        status = args.get("status") or None
        refs = self._refs_in_scope(scope, status)[:LIST_LIMIT]
        return {
            "count": len(refs),
            "tasks": [
                {
                    "id": r.trace_id,
                    "scenario": r.scenario,
                    "status": r.status,
                    "started_at": time.strftime("%m-%d %H:%M:%S", time.localtime(r.started_at_ms / 1000)),
                    "last_error": r.last_error[:120],
                }
                for r in refs
            ],
            "note": ("只包含你在私聊里发起的任务" if scope.chat_type == "p2p" else "只包含当前群聊内的任务") if refs else "没有找到任务",
        }

    async def _diagnose(self, scope: Scope, args: dict) -> dict:
        ident = str(args.get("task_id") or "").strip()
        ref = self._resolve_in_scope(scope, ident) if ident else None
        if ref is None:
            # 没给 id 时取本群最近一个,但必须告诉模型选了谁,由它向用户复述确认。
            candidates = self._refs_in_scope(scope)
            if ident or not candidates:
                return {"found": False, "message": "没有找到该任务"}
            ref = candidates[0]
            picked_by_default = True
        else:
            picked_by_default = False
        trace = await build_trace(ref, self._events, redact=True)
        findings = diagnose(trace, now_ms=int(time.time() * 1000))
        return {
            "found": True,
            "picked_by_default": picked_by_default,
            "task": {
                "id": trace.trace_id,
                "scenario": trace.scenario,
                "status": trace.status,
                "started_at": time.strftime("%m-%d %H:%M:%S", time.localtime(trace.started_at_ms / 1000)),
                "last_error": trace.last_error[:300],
                "current_phase": trace.meta.get("current_phase"),
            },
            "summary": trace.summary(),
            "findings": [f.to_dict() for f in findings[:8]],
            "sessions": [
                {
                    "session_id": n.session_id,
                    "role": n.role,
                    "available": n.available,
                    "unavailable_reason": n.unavailable_reason,
                    "turns": len(n.turns),
                }
                for n in trace.sessions
            ],
            "steps": _step_index(trace),
        }

    async def _step_detail(self, scope: Scope, args: dict) -> dict:
        ident = str(args.get("task_id") or "").strip()
        session_id = str(args.get("session_id") or "").strip()
        event_id = str(args.get("event_id") or "").strip()
        ref = self._resolve_in_scope(scope, ident) if ident else None
        # session_id 必须属于该任务,否则等于绕过范围限制。
        if ref is None or session_id not in {s.session_id for s in ref.sessions}:
            return {"found": False, "message": "没有找到该任务或会话"}
        detail = await get_step_detail(self._events, session_id, event_id)
        return detail or {"found": False, "message": "没有找到该事件"}


def _dedupe(refs: list[TraceRef]) -> list[TraceRef]:
    """topicx 的 task 与它下面的 topic5 / topic6 job 是同一个任务,只留一个(按时间倒序)。"""
    seen: set[str] = set()
    unique: list[TraceRef] = []
    for ref in sorted(refs, key=lambda r: r.started_at_ms, reverse=True):
        job = ref.meta.get("business_job_id") or ref.meta.get("job_id")
        if job and job in seen:
            continue
        if job:
            seen.add(job)
        unique.append(ref)
    return unique


def _step_index(trace) -> list[dict]:
    """给 Agent 一份\"哪一步对应哪个事件 id\"的索引,只列失败 / 未完成 / 最慢的,控制体量。"""
    from arkagent.observability.model import KIND_ERROR, KIND_MODEL, KIND_TOOL, STATUS_ERROR, STATUS_PENDING

    rows = []
    for node in trace.sessions:
        for turn in node.turns:
            tools = [s for s in turn.spans if s.kind == KIND_TOOL]
            slow = sorted((s for s in tools if s.duration_ms), key=lambda s: s.duration_ms, reverse=True)[:2]
            for s in turn.spans:
                interesting = s.status in (STATUS_ERROR, STATUS_PENDING) or s in slow or s.kind == KIND_ERROR
                if not interesting:
                    continue
                rows.append(
                    {
                        "session_id": node.session_id,
                        "turn": turn.index,
                        "event_id": s.span_id,
                        "kind": s.kind,
                        "name": s.attrs.get("category") or s.name,
                        "status": s.status,
                        "duration_ms": s.duration_ms,
                    }
                )
    return rows[:MAX_SPANS_PER_TURN]
