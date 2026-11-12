"""MA session 事件 → turn / span。纯函数,不做 IO。

事件字段以 2026-10 对真实 session 的实测为准(见 docs/OBSERVABILITY_PLAN.md §3):
span.model_request_* 无模型名、无线程 id;tool_use / tool_result 靠 id 配对且无耗时字段;
仅部分事件带 session_thread_id。因此线程归属有两种来源,通过 ``Span.source`` 区分。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from .model import (
    KIND_ERROR,
    KIND_MODEL,
    KIND_PHASE,
    KIND_THREAD_MESSAGE,
    KIND_TOOL,
    SOURCE_EVENT,
    SOURCE_INFERRED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PENDING,
    SessionNode,
    Span,
    Turn,
)
from .redact import redact_text
from .tool_category import categorize_tool

# 与各 runner 的 _maybe_update_phase 保持同一口径。
_PHASE_RE = re.compile(r"\[phase\]\s+([A-Z0-9]+)")
_FRACTION = re.compile(r"(\.\d{6})\d+")
COMMAND_PREVIEW_CHARS = 200


def ts_ms(value: Any) -> int:
    """解析方舟事件的 processed_at 为毫秒。**只用于展示和算耗时,不要用来给事件排序**——毫秒会把
    同一毫秒内的多个事件并成一个键(实测同一 session 里有 17 组、38 个事件毫秒相同),排序会错位。"""
    text = _FRACTION.sub(r"\1", str(value or "")).replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp() * 1000)


_NS_PARTS = re.compile(r"^(.*?T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$")


def ts_ns(value: Any) -> int:
    """processed_at 的**纳秒**整数,事件排序专用。

    实测 6 个真实 session 按它排序后与方舟接口原始返回顺序**逐个一致**;纳秒仍相同的(约占 1%)
    由调用方用输入位置作次级键保持稳定。解析不了的时间戳返回 0(排到最前),不抛异常。
    """
    m = _NS_PARTS.match(str(value or ""))
    if not m:
        return 0
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3)
    try:
        seconds = int(datetime.fromisoformat(base + tz.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0
    return seconds * 10**9 + int((frac + "000000000")[:9])


def stream_order(events: list) -> list:
    """返回按事件发生顺序排列的事件。

    **接口给的顺序是权威的**:输入的纳秒时间已经非降序(正常情况——方舟返回的就是这样,实测 6 个真实 session
    逐个一致,其中纳秒完全相同的 ``session.usage`` → ``session.status_idle`` 也靠接口顺序区分)时,
    原样保留,不重排。只有检测到乱序才排序:纳秒为主键,输入位置为次级键,时间戳相同的保持原相对顺序。
    """
    items = [e for e in events if isinstance(e, dict)]
    keys = [ts_ns(e.get("processed_at")) for e in items]
    if all(a <= b for a, b in zip(keys, keys[1:])):
        return items
    return [e for _, _, e in sorted(((k, i, e) for i, (k, e) in enumerate(zip(keys, items))), key=lambda t: (t[0], t[1]))]


def event_text(event: dict) -> str:
    content = event.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def parse_session_events(
    events: list[dict],
    *,
    session_id: str,
    role: str,
    redact: bool = True,
) -> SessionNode:
    return _Parser(session_id, role, redact).run(events)


class _Parser:
    def __init__(self, session_id: str, role: str, redact: bool) -> None:
        self._node = SessionNode(session_id=session_id, role=role)
        self._redact = redact
        self._turn: Optional[Turn] = None
        self._thread_names: dict[str, str] = {}
        self._active_threads: set[str] = set()
        self._model_starts: dict[str, tuple[Turn, int, Optional[str], str]] = {}
        self._tool_uses: dict[str, tuple[Turn, dict, int]] = {}
        self._last_phase = ""

    def run(self, events: list[dict]) -> SessionNode:
        ordered = stream_order(events)
        self._node.event_count = len(ordered)
        for event in ordered:
            handler = getattr(self, "_on_" + str(event.get("type", "")).replace(".", "_"), None)
            if handler:
                handler(event, ts_ms(event.get("processed_at")))
        self._flush_pending()
        for turn in self._node.turns:
            turn.spans.sort(key=lambda s: s.started_at_ms)
        return self._node

    # ---- turn ---------------------------------------------------------------
    def _current_turn(self, at: int) -> Turn:
        if self._turn is None:
            # 分页截断或旧事件缺失时,没有 user.message 也要有地方挂 span。
            self._turn = self._new_turn(None, at, 0)
        return self._turn

    def _new_turn(self, event_id: Optional[str], at: int, chars: int) -> Turn:
        turn = Turn(
            index=len(self._node.turns) + 1,
            user_event_id=event_id,
            started_at_ms=at,
            user_text_chars=chars,
        )
        self._node.turns.append(turn)
        self._turn = turn
        return turn

    def _on_user_message(self, event: dict, at: int) -> None:
        self._new_turn(event.get("id"), at, len(event_text(event)))

    def _on_session_status_idle(self, event: dict, at: int) -> None:
        turn = self._current_turn(at)
        turn.ended_at_ms = at
        reason = event.get("stop_reason")
        turn.stop_reason = reason.get("type") if isinstance(reason, dict) else None

    def _end_turn_terminal(self, event: dict, at: int) -> None:
        turn = self._current_turn(at)
        turn.ended_at_ms = at
        turn.stop_reason = str(event.get("type"))

    _on_session_status_failed = _end_turn_terminal
    _on_session_status_terminated = _end_turn_terminal

    # ---- 线程 ---------------------------------------------------------------
    def _remember_thread(self, event: dict) -> Optional[str]:
        thread_id = event.get("session_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return None
        name = event.get("agent_name")
        if isinstance(name, str) and name:
            self._thread_names[thread_id] = name
        return thread_id

    def _thread_label(self, thread_id: Optional[str]) -> Optional[str]:
        if not thread_id:
            return None
        return self._thread_names.get(thread_id, thread_id)

    def _on_session_thread_created(self, event: dict, at: int) -> None:
        self._remember_thread(event)

    def _on_session_thread_status_running(self, event: dict, at: int) -> None:
        thread_id = self._remember_thread(event)
        if thread_id:
            self._active_threads.add(thread_id)

    def _on_session_thread_status_idle(self, event: dict, at: int) -> None:
        thread_id = self._remember_thread(event)
        if thread_id:
            self._active_threads.discard(thread_id)

    def _infer_thread(self) -> tuple[Optional[str], str]:
        """span.model_request_* 不带线程 id:只有恰好一个活跃线程时才敢归属,否则置空。"""
        if len(self._active_threads) == 1:
            return self._thread_label(next(iter(self._active_threads))), SOURCE_INFERRED
        return None, SOURCE_INFERRED

    def _on_agent_thread_message_sent(self, event: dict, at: int) -> None:
        self._thread_message(event, at, "→", event.get("to_agent_name"))

    def _on_agent_thread_message_received(self, event: dict, at: int) -> None:
        self._thread_message(event, at, "←", event.get("from_agent_name"))

    def _thread_message(self, event: dict, at: int, arrow: str, peer: Any) -> None:
        self._current_turn(at).spans.append(
            Span(
                span_id=str(event.get("id")),
                kind=KIND_THREAD_MESSAGE,
                name=f"{arrow} {peer or '未知'}",
                started_at_ms=at,
                source=SOURCE_EVENT,
                attrs={"chars": len(event_text(event))},
            )
        )

    # ---- 模型调用 -----------------------------------------------------------
    def _on_span_model_request_start(self, event: dict, at: int) -> None:
        thread, source = self._infer_thread()
        self._model_starts[str(event.get("id"))] = (self._current_turn(at), at, thread, source)

    def _on_span_model_request_end(self, event: dict, at: int) -> None:
        start = self._model_starts.pop(str(event.get("model_request_start_id")), None)
        turn, started, thread, source = start or (self._current_turn(at), at, None, SOURCE_INFERRED)
        usage = event.get("model_usage") if isinstance(event.get("model_usage"), dict) else {}
        turn.spans.append(
            Span(
                span_id=str(event.get("id")),
                kind=KIND_MODEL,
                name="model_request",
                started_at_ms=started,
                duration_ms=at - started if start else None,
                status=STATUS_ERROR if event.get("is_error") else STATUS_OK,
                thread=thread,
                source=source,
                attrs={
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                },
            )
        )

    # ---- 工具调用 -----------------------------------------------------------
    def _on_agent_tool_use(self, event: dict, at: int) -> None:
        self._remember_thread(event)
        self._tool_uses[str(event.get("id"))] = (self._current_turn(at), event, at)

    def _on_agent_tool_result(self, event: dict, at: int) -> None:
        use = self._tool_uses.pop(str(event.get("tool_use_id")), None)
        if use is None:
            return
        turn, use_event, started = use
        turn.spans.append(
            self._tool_span(
                use_event,
                started,
                duration=at - started,
                status=STATUS_ERROR if event.get("is_error") else STATUS_OK,
                result_chars=len(event_text(event)),
            )
        )

    def _tool_span(
        self,
        use_event: dict,
        started: int,
        *,
        duration: Optional[int],
        status: str,
        result_chars: Optional[int] = None,
    ) -> Span:
        name = str(use_event.get("name") or "未知工具")
        tool_input = use_event.get("input")
        thread_id = use_event.get("session_thread_id")
        attrs: dict[str, Any] = {"category": categorize_tool(name, tool_input)}
        if result_chars is not None:
            attrs["result_chars"] = result_chars
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str):
            preview = command[:COMMAND_PREVIEW_CHARS]
            attrs["command_preview"] = redact_text(preview) if self._redact else preview
        return Span(
            span_id=str(use_event.get("id")),
            kind=KIND_TOOL,
            name=name,
            started_at_ms=started,
            duration_ms=duration,
            status=status,
            thread=self._thread_label(thread_id if isinstance(thread_id, str) else None),
            source=SOURCE_EVENT,
            attrs=attrs,
        )

    # ---- 错误 / 阶段 --------------------------------------------------------
    def _on_session_error(self, event: dict, at: int) -> None:
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        retry = error.get("retry_status")
        message = str(error.get("message") or "")
        self._current_turn(at).spans.append(
            Span(
                span_id=str(event.get("id")),
                kind=KIND_ERROR,
                name=str(error.get("type") or "session.error"),
                started_at_ms=at,
                status=STATUS_ERROR,
                source=SOURCE_EVENT,
                attrs={
                    "message": redact_text(message) if self._redact else message,
                    "retry_status": retry.get("type") if isinstance(retry, dict) else None,
                },
            )
        )

    def _on_agent_message(self, event: dict, at: int) -> None:
        match = _PHASE_RE.search(event_text(event))
        if not match or match.group(1) == self._last_phase:
            return
        self._last_phase = match.group(1)
        self._current_turn(at).spans.append(
            Span(
                span_id=str(event.get("id")),
                kind=KIND_PHASE,
                name=self._last_phase,
                started_at_ms=at,
                source=SOURCE_INFERRED,
            )
        )

    # ---- 收尾 ---------------------------------------------------------------
    def _flush_pending(self) -> None:
        """没有配对 end / result 的调用:保留为 pending,正在跑还是卡住由调用方判断。"""
        for start_id, (turn, started, thread, source) in self._model_starts.items():
            turn.spans.append(
                Span(
                    span_id=start_id,
                    kind=KIND_MODEL,
                    name="model_request",
                    started_at_ms=started,
                    status=STATUS_PENDING,
                    thread=thread,
                    source=source,
                )
            )
        for turn, use_event, started in self._tool_uses.values():
            turn.spans.append(self._tool_span(use_event, started, duration=None, status=STATUS_PENDING))
        self._model_starts.clear()
        self._tool_uses.clear()

