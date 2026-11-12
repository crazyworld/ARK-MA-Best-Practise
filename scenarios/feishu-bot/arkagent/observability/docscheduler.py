"""定时把各 bot 的 session 日志同步到飞书文档(取代"每轮结束钩子")。

为什么用定时任务:
- 不用改任何业务 bot 的代码,只读它们的库(与排障 adapter 同一套),五类 bot 全部覆盖;
- 运行中的长 session 也能写到当前进度,不必等一轮结束;
- 写文档的权限收敛到**一个应用**(排障应用),而不是每个业务应用各配一遍;
- 失败自然在下个周期补上。

每个周期:
1. 从各 adapter 取最近活跃的 session(按 ``started_at``/活动时间倒序,限天数)。
2. 按"静止降频"决定本周期要不要检查它——长期没新事件的 session 检查间隔逐步拉长,已进入终态的只做最后一次。
3. 增量拉取(见 incremental.py)→ 有新事件才写文档。
4. 单周期最多处理 ``max_per_cycle`` 个,按"最久没检查"优先,防止积压把方舟接口打满。

**不做隔离(MVP 决策,2026-11-12)**:所有 session 的日志——包括私聊——都写进同一个父节点下。wiki 文档权限继承
父节点,有该空间访问权的人能看到所有日志。这是已知风险,不是疏漏。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .adapters.base import TraceRef, TraceSource
from .docsink import DocStore, SessionDocSink, newer_than
from .events import stream_order, ts_ms
from .incremental import Fetch, pull_new_events

log = logging.getLogger("arkagent.observability.docscheduler")

# 进入终态后不再需要轮询的状态(任务型);会话型没有终态,靠静止降频。
TERMINAL_STATUSES = frozenset({"done", "failed", "stopped", "unsupported", "busy", "archived"})


@dataclass(frozen=True)
class SyncConfig:
    interval_s: int = 120  # 周期,2 分钟
    lookback_days: int = 7  # 只看最近这些天内开始的 session
    max_per_cycle: int = 20  # 单周期最多检查几个
    candidates_per_source: int = 200  # 每个库取多少候选
    # 静止降频:连续 N 次没有新事件后,检查间隔乘以对应倍数
    idle_backoff: tuple[tuple[int, int], ...] = ((3, 3), (10, 15), (30, 60))
    # 一轮对话没有 idle 结尾、且最后一个事件已经这么久没动静 → 视为异常终止,强制写入(否则永远写不出来)。
    # 实测一次工具调用最长约 10.5 分钟、一轮对话最长约 18.6 分钟,取 30 分钟留余量。
    stale_s: int = 1800


def due(
    ref: TraceRef,
    checked_at: int,
    idle_rounds: int,
    now: float,
    cfg: SyncConfig,
    *,
    synced: bool,
) -> bool:
    """这个 session 本周期要不要检查。纯函数,便于测试。"""
    if checked_at == 0:
        return True  # 从未检查过:一定要来一次(首次回填)
    if ref.status in TERMINAL_STATUSES and synced and idle_rounds >= 1:
        return False  # 终态且已经同步过、之后又确认没有新事件:不再轮询
    multiplier = 1
    for threshold, factor in cfg.idle_backoff:
        if idle_rounds >= threshold:
            multiplier = factor
    return now - checked_at >= cfg.interval_s * multiplier


class DocScheduler:
    def __init__(
        self,
        sources: dict[str, TraceSource],
        fetch: Fetch,
        sink: SessionDocSink,
        store: DocStore,
        cfg: SyncConfig = SyncConfig(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sources = sources
        self._fetch = fetch
        self._sink = sink
        self._store = store
        self._cfg = cfg
        self._clock = clock

    # ---- 一个周期 -----------------------------------------------------------
    async def run_once(self) -> dict:
        now = self._clock()
        picked = self._pick(now)
        stats = {"candidates": len(picked), "written": 0, "idle": 0, "failed": 0}
        for ref in picked:
            outcome = await self._sync_one(ref)
            stats[outcome] += 1
        return stats

    def _candidates(self, now: float) -> Iterable[TraceRef]:
        """候选 = 各库最近的 session,去重。

        回看窗口(``lookback_days``)的规则只有一条:**窗口外且从未同步过的不要**。
        已经开始同步的 session 不受窗口限制——它可能是个跑了很久的长任务,
        仍要把后续内容写完;它什么时候停止轮询由 ``due`` 的终态 / 静止降频决定。
        """
        floor_ms = (now - self._cfg.lookback_days * 86400) * 1000
        seen: set[str] = set()
        for name, source in self._sources.items():
            try:
                refs = source.list_refs(limit=self._cfg.candidates_per_source)
            except Exception as error:  # noqa: BLE001 - 一个库坏了不影响其他库
                log.warning("读取候选失败 source=%s: %s", name, error)
                continue
            for ref in refs:
                sid = _primary_session(ref)
                if not sid or sid in seen:
                    continue
                if ref.started_at_ms < floor_ms and self._store.get(sid) is None:
                    continue
                seen.add(sid)
                yield ref

    def _pick(self, now: float) -> list[TraceRef]:
        due_refs: list[tuple[int, TraceRef]] = []
        for ref in self._candidates(now):
            sid = _primary_session(ref)
            checked_at, idle = self._store.schedule_info(sid)
            if due(ref, checked_at, idle, now, self._cfg, synced=self._store.get(sid) is not None):
                due_refs.append((checked_at, ref))
        due_refs.sort(key=lambda item: item[0])  # 最久没检查的优先
        return [ref for _, ref in due_refs[: self._cfg.max_per_cycle]]

    def _next_round(self, sid: str) -> int:
        record = self._store.get(sid)
        return (record.rounds_written if record else 0) + 1

    async def _sync_one(self, ref: TraceRef) -> str:
        sid = _primary_session(ref)
        record = self._store.get(sid)
        try:
            pull = await pull_new_events(self._fetch, sid, last_event_id=record.last_event_id if record else "")
        except Exception as error:  # noqa: BLE001 - 单个 session 失败不影响其他
            status = getattr(error, "status_code", None)
            # session 被方舟清理(404)是预期内的:记一次检查,别每个周期都重试
            log.log(logging.INFO if status == 404 else logging.WARNING, "拉取事件失败 session=%s: %s", sid, error)
            if record:
                self._store.mark_checked(sid, idle=True)
            return "failed"
        if not pull.complete:
            log.warning("session 事件过多,本周期没能追上已同步位置,放弃重试 session=%s", sid)
            return "failed"
        events = _drop_already_written(pull.events, record)
        # 只写完整的一轮对话;正在进行的这一轮留到下个周期(见 complete_prefix)
        events, held = complete_prefix(events, now_ms=int(self._clock() * 1000), stale_ms=self._cfg.stale_s * 1000)
        if not events:
            if record:
                self._store.mark_checked(sid, idle=not held)   # 有暂缓的事件说明 session 在动,不算"静止"
            return "idle"
        url = await self._sink.publish_new(sid, events, title=_title(ref), first_round=self._next_round(sid))
        if url is None:
            return "failed"  # 写失败:进度没推进,下个周期自然补上
        return "written"


def complete_prefix(events: list[dict], *, now_ms: int, stale_ms: int) -> tuple[list[dict], list[dict]]:
    """把事件切成 (可以写入的, 本周期暂缓的)。**只在一轮对话的边界落笔**。

    文档只能追加,写出去就改不了,所以不能在一个 Span / 并行批次中间写:实测在"3 个并行工具只拿到前 2 个
    tool_use"时写入,会留下永远停在"未完成"的卡片、被切成两半的 grid、下一批里找不到调用的 tool_result,
    以及重新从 1 开始的轮号。四个后果都无法事后修复。

    规则(边界 = ``session.status_idle``,即一轮对话结束):
    - 最后一个事件就是 idle → 全部可写;
    - 否则只写到**最近一个** idle 为止,其后(正在进行的这一轮)暂缓;
    - 从未出现过 idle → 什么都不写;
    - 兜底:暂缓部分的最后一个事件已经 ``stale_ms`` 没有动静 → 视为异常终止,全部写入,否则它永远写不出来。
    """
    ordered = stream_order(events)
    if not ordered:
        return [], []
    last_idle = max((i for i, e in enumerate(ordered) if e.get("type") == "session.status_idle"), default=-1)
    if last_idle == len(ordered) - 1:
        return ordered, []
    held = ordered[last_idle + 1 :]
    if now_ms - ts_ms(held[-1].get("processed_at")) >= stale_ms:
        return ordered, []
    return ordered[: last_idle + 1], held


def _primary_session(ref: TraceRef) -> str:
    """一个任务可能有 router + business 两个 session;日志按业务 session 写,没有业务 session 才退回第一个。"""
    business = [s for s in ref.sessions if s.role == "business"]
    chosen = business or ref.sessions
    return chosen[0].session_id if chosen else ""


def _drop_already_written(events: list[dict], record) -> list[dict]:
    """last_event_id 被清理(没追上)时 incremental 返回的是全部可见事件,这里按**纳秒**去掉已写过的。"""
    if record is None or not record.last_event_id:
        return events
    if events and events[0].get("id") == record.last_event_id:
        return events[1:]
    return newer_than(events, record)


def _title(ref: TraceRef) -> str:
    when = time.strftime("%m-%d %H:%M", time.localtime(ref.started_at_ms / 1000))
    return f"[{ref.scenario}] {_primary_session(ref)[-12:]} {when}"


async def run_forever(scheduler: DocScheduler, interval_s: int) -> None:
    """后台循环。单个周期的任何异常只记日志,不终止循环。"""
    while True:
        try:
            stats = await scheduler.run_once()
            if stats["written"] or stats["failed"]:
                log.info("文档同步 %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("文档同步周期异常")
        await asyncio.sleep(interval_s)
