"""定时把各 bot 的 session 日志同步到飞书文档(取代"每轮结束钩子")。

为什么用定时任务:
- 不用改任何业务 bot 的代码,只读它们的库(与排障 adapter 同一套),五类 bot 全部覆盖;
- 运行中的长 session 也能写到当前进度,不必等一轮结束;
- 写文档的权限收敛到**一个应用**(排障应用),而不是每个业务应用各配一遍;
- 失败自然在下个周期补上。

每个周期:
1. 候选 = 各库**全部** session(不截断;此前每个库只取最近 200 个,超出的老 session 永远进不了候选)。
2. **检查**阶段:并发拉增量(见 incremental.py),有时间预算。空检查很便宜(实测约 136 ms 一次),
   所以检查**不限个数**;真正慢、会失败的是写飞书。
3. **写飞书**阶段:单独限额(``max_writes_per_cycle``),新 session 优先;没轮到的不推进进度,下个周期自然补上。

**回看窗口只信事件流,不信库里的时间**:会话型 bot(分析师 / ada-pm)的 ``conversations.updated_at`` 只在新建会话时更新,
实测与真实最后活动差好几天——一个 11-09 创建、昨天还在聊的会话,按库里的时间会被当成"老会话"。所以从未同步过的
session 用事件流的**最后一个事件时间**判断是否在窗口内(一次倒序取 1 条);已确认在窗口外的记入进度库,
按 ``outside_window_recheck_s`` 低频复查,免得每个周期对所有老 session 重复请求,又不会让睡着的会话醒来后永远被漏掉。

**不做隔离(MVP 决策,2026-11-12)**:所有 session 的日志——包括私聊——都写进同一个父节点下。wiki 文档权限继承
父节点,有该空间访问权的人能看到所有日志。这是已知风险,不是疏漏。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import quote

from .adapters.base import TraceRef, TraceSource
from .docsink import DocStore, SessionDocSink, newer_than
from .events import stream_order, ts_ms
from .incremental import Fetch, pull_new_events

log = logging.getLogger("arkagent.observability.docscheduler")

# 进入终态后不再需要轮询的状态(任务型);会话型没有终态,靠静止降频。
ALL_LIMIT = 1_000_000  # "不截断":远大于任何现实规模,adapter 的 LIMIT 因此不再是候选的隐性上限

TERMINAL_STATUSES = frozenset({"done", "failed", "stopped", "unsupported", "busy", "archived"})


@dataclass(frozen=True)
class SyncConfig:
    interval_s: int = 120  # 周期,2 分钟
    lookback_days: int = 7  # 回看窗口:事件流最后一个事件在这之内的才同步;已经开始同步的不受限
    # 写飞书单独限额。检查(读方舟)不限个数——实测一次空检查约 136 ms;写飞书才慢、才会失败、才有频率限制。
    max_writes_per_cycle: int = 10
    check_concurrency: int = 4  # 并发检查数。实测并发 8 方舟无 429,但没查到限流规则,保守取 4
    # 一个周期里"检查阶段"最多跑多久,跑不完就停,没处理的靠"最久没检查优先"在下个周期接着做
    cycle_budget_s: int = 90
    outside_window_recheck_s: int = 6 * 3600  # 已确认在窗口外的 session 隔多久再看一眼(睡着的会话可能被用户重新唤醒)
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


@dataclass
class _Checked:
    """检查阶段的产物:一个 session 本周期要写什么(还没写)。"""

    ref: TraceRef
    sid: str
    events: list[dict]
    is_new: bool  # 还没有文档
    last_checked: int


class DocScheduler:
    def __init__(
        self,
        sources: dict[str, TraceSource],
        fetch: Fetch,
        sink: SessionDocSink,
        store: DocStore,
        cfg: SyncConfig = SyncConfig(),
        clock: Callable[[], float] = time.time,
        name_resolver: Optional[Callable[[TraceRef], Awaitable[dict]]] = None,
    ) -> None:
        self._sources = sources
        self._fetch = fetch
        self._sink = sink
        self._store = store
        self._cfg = cfg
        self._clock = clock
        self._name_resolver = name_resolver

    # ---- 一个周期 -----------------------------------------------------------
    async def run_once(self) -> dict:
        start = self._clock()
        stats = {"candidates": 0, "written": 0, "idle": 0, "failed": 0, "deferred": 0, "skipped": 0}
        due_refs = self._due(start, stats)
        stats["candidates"] = len(due_refs)

        # 阶段一:检查(读方舟)。并发、不限个数,只受时间预算约束。
        pending: list[_Checked] = []
        sem = asyncio.Semaphore(max(1, self._cfg.check_concurrency))

        async def check(ref: TraceRef) -> None:
            async with sem:
                if self._clock() - start > self._cfg.cycle_budget_s:
                    stats["skipped"] += 1  # 预算用完:不检查,留给下个周期(靠"最久没检查优先")
                    return
                outcome = await self._check_one(ref, start)
            if isinstance(outcome, _Checked):
                pending.append(outcome)
            else:
                stats[outcome] += 1

        await asyncio.gather(*(check(r) for r in due_refs))

        # 阶段二:写飞书(限额)。新 session 优先——用户最想第一时间看到新会话的日志;其次最久没写的。
        pending.sort(key=lambda c: (not c.is_new, c.last_checked))
        for index, item in enumerate(pending):
            if index >= self._cfg.max_writes_per_cycle:
                stats["deferred"] += len(pending) - index
                break
            names: dict = {}
            if item.is_new and self._name_resolver:
                try:
                    names = await self._name_resolver(item.ref)
                except Exception:  # noqa: BLE001 - 名字可读性只是锦上添花,查不到不影响写入
                    names = {}
            url = await self._sink.publish_new(
                item.sid, item.events, title=_title(item.ref, item.events),
                first_round=self._next_round(item.sid),
                header=_info_card(item.ref, names) if item.is_new else "",
            )
            stats["written" if url else "failed"] += 1  # 写失败:进度没推进,下个周期自然补上
        if stats["deferred"]:
            log.info("写飞书限额已用完,%s 个 session 留到下个周期", stats["deferred"])
        return stats

    def _candidates(self) -> Iterable[TraceRef]:
        """各库全部 session,按 session id 去重。**不截断**。"""
        seen: set[str] = set()
        for name, source in self._sources.items():
            try:
                refs = _all_refs(source)
            except Exception as error:  # noqa: BLE001 - 一个库坏了不影响其他库
                log.warning("读取候选失败 source=%s: %s", name, error)
                continue
            for ref in refs:
                sid = _primary_session(ref)
                if sid and sid not in seen:
                    seen.add(sid)
                    yield ref

    def _due(self, now: float, stats: dict) -> list[TraceRef]:
        """本周期该检查哪些。已确认在窗口外 / 已被方舟清理、还没到复查时间的,不产生任何请求。"""
        out: list[tuple[int, bool, TraceRef]] = []
        for ref in self._candidates():
            sid = _primary_session(ref)
            record = self._store.get(sid)
            if record is None:
                sched = self._store.sched_state(sid)
                if sched and (sched[1] == 0 or now < sched[1]):
                    stats["skipped"] += 1
                    continue
                out.append((0, True, ref))  # 还没有文档:一定检查
                continue
            checked_at, idle = self._store.schedule_info(sid)
            if due(ref, checked_at, idle, now, self._cfg, synced=True):
                out.append((checked_at, False, ref))
        out.sort(key=lambda t: (not t[1], t[0]))  # 新的在前,其次最久没检查
        return [r for _, _, r in out]

    def _next_round(self, sid: str) -> int:
        record = self._store.get(sid)
        return (record.rounds_written if record else 0) + 1

    async def _check_one(self, ref: TraceRef, now: float):
        """返回 _Checked(有内容要写),或一个统计键('idle' / 'failed')。

        一个新 session 只发**一次**请求:倒序第一页既是增量拉取的起点,第一条又正好是"最新事件",
        窗口判断复用它(实测 limit=1 与 limit=100 耗时几乎一样,约 173 vs 193 ms,单独发一次 limit=1 纯属浪费)。
        """
        sid = _primary_session(ref)
        record = self._store.get(sid)
        try:
            pull = await pull_new_events(self._fetch, sid, last_event_id=record.last_event_id if record else "")
        except Exception as error:  # noqa: BLE001 - 单个 session 失败不影响其他
            status = getattr(error, "status_code", None)
            log.log(logging.INFO if status == 404 else logging.WARNING, "拉取事件失败 session=%s: %s", sid, error)
            if status == 404:
                # 方舟已清理:确认一次就够,别每个周期都去撞(有文档的保留文档,只是不再检查)
                self._store.set_sched(sid, "purged", 0)
            elif record:
                self._store.mark_checked(sid, idle=True)
            return "failed"
        if not pull.complete:
            log.warning("session 事件过多,本周期没能追上已同步位置,放弃重试 session=%s", sid)
            return "failed"
        if record is None and not self._in_window(sid, pull.newest_at, now):
            return "idle"  # 窗口外或无事件:已记入进度库,低频复查
        events = _drop_already_written(pull.events, record)
        # 只写完整的一轮对话;正在进行的这一轮留到下个周期(见 complete_prefix)
        events, held = complete_prefix(events, now_ms=int(self._clock() * 1000), stale_ms=self._cfg.stale_s * 1000)
        if not events:
            if record:
                self._store.mark_checked(sid, idle=not held)   # 有暂缓的事件说明 session 在动,不算"静止"
            return "idle"
        checked_at, _ = self._store.schedule_info(sid)
        return _Checked(ref, sid, events, record is None, checked_at)

    def _in_window(self, sid: str, newest_at: str, now: float) -> bool:
        """从未同步过的 session:事件流最后一个事件在回看窗口内吗?

        **只信事件流,不信库里的时间**(会话型 bot 的库时间只在新建时更新)。窗口外的、以及没有任何事件的,
        记入进度库并按 ``outside_window_recheck_s`` 低频复查:既不会每个周期对所有老 session 重复请求,
        也不会让睡着的会话被用户重新唤醒后永远被漏掉。
        """
        if not newest_at or ts_ms(newest_at) < (now - self._cfg.lookback_days * 86400) * 1000:
            self._store.set_sched(sid, "outside_window", int(now + self._cfg.outside_window_recheck_s))
            return False
        self._store.clear_sched(sid)
        return True


def _all_refs(source: TraceSource) -> list[TraceRef]:
    """取一个库的**全部** session。adapter 的 list_refs 必须有 limit,这里取一个远大于现实规模的值而不是真的"无限"。"""
    return source.list_refs(limit=ALL_LIMIT)


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


def _title(ref: TraceRef, events: list[dict]) -> str:
    """``[{bot}] {完整 session id} · {YYYY-MM-DD HH:MM}``。

    session id 保持完整,可直接拿去 ``arkagent trace show``;时间取自**第一个事件**——会话型 bot 库里的时间
    只在新建时更新,不可信(实测与真实活动差好几天)。第一个事件不在本批里(增量)时只会发生在已有文档上,
    标题在创建时就定了,不会再改。
    """
    first = stream_order(events)[0] if events else None
    when = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms(first.get("processed_at")) / 1000))
        if first
        else time.strftime("%Y-%m-%d %H:%M", time.localtime(ref.started_at_ms / 1000))
    )
    return f"[{ref.scenario}] {_primary_session(ref)} · {when}"


def _info_card(ref: TraceRef, names: Optional[dict] = None) -> str:
    """首批写入时放在文档最前面的会话信息。**只放库里有、且确定的字段,没有的不写、不编。**

    ``names`` 是 name_resolver 的结果(``{"chat_name": ..., "user_name": ...}``),用于把冷冰冰的
    chat_id / open_id 换成可读的群名 / 用户名;查不到的值是空串,此时回退显示原 ID。名字只是可读性
    增强,原 ID 始终保留,便于排障时落库定位。
    """
    from .docrender import esc

    names = names or {}
    rows = [("来源", ref.scenario), ("session", _primary_session(ref))]
    if ref.chat_id:
        kind = {"p2p": "私聊", "group": "群聊"}.get(ref.chat_type, "")
        chat_name = str(names.get("chat_name") or "").strip()
        label = chat_name if chat_name else ref.chat_id
        if chat_name:
            label = f"{chat_name}（{ref.chat_id}）"
        rows.append(("会话" if kind != "群聊" else "群组", f"{label}" + (f" · {kind}" if kind else "")))
    if ref.meta.get("user_open_id"):
        open_id = str(ref.meta["user_open_id"])
        user_name = str(names.get("user_name") or "").strip()
        rows.append(("用户", f"{user_name}（{open_id}）" if user_name else open_id))
    if ref.status == "archived":
        rows.append(("状态", "已归档(用户 /new 重置后被替换的旧会话)"))
    body = "".join(f"<p><b>{esc(k)}</b>:{esc(v)}</p>" for k, v in rows)
    return f'<callout emoji="ℹ️" background-color="light-blue" border-color="blue"><p><b>会话信息</b></p>{body}</callout>'


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
