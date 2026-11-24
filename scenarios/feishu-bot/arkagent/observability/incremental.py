"""session 事件的增量拉取:只取"上次已同步的事件之后"的新事件,避免每个周期都读全量。

做法:倒序(``order=desc``)从最新往旧翻,遇到上次记录的最后事件 id 就停。
- 没有新事件:1 次请求,第一条就是已记录的 id。
- 有新事件:通常 1 页。
- 积压很多:继续翻页到遇到旧 id 为止。

为什么不靠"记分页游标"做增量:实测(2026-11-12)游标内容是 ``{time, object_id}``,绑定某个事件;
读到末页时 ``next_page`` 为空,**没有"指向末尾之后"的游标可记**,所以游标解决不了"读完了、
等新事件"这个最常见的场景——倒序遇旧 id 即停才是增量的办法。

**也不做跨周期的游标续翻**。最初的设计是"首次回填太长时记下游标、下个周期接着翻",但倒序翻页时
先拿到的是**最新**的页,续翻拿到的是更旧的页;最新那几页上个周期已经翻过却没保存,下个周期拿不到了,
两段拼不起来,**会静默丢掉最新的内容**。所以回填必须在同一个周期内翻完;翻页上限(``max_pages``)只是
防死循环的兜底,到了上限就放弃本周期(不写、不推进),下个周期从头再来,宁可重做也不丢数据。

``order=desc`` 是实测可用、但**不在方舟文档里**的行为,可能变。所以拉到的数据要自检,
不满足预期就退回全量,不能静默出错。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import quote, urlencode

from .events import ts_ns

log = logging.getLogger("arkagent.observability.incremental")

PAGE_SIZE = 100
MAX_PAGES = 200  # 兜底,防止异常数据造成死循环;200 页 × 100 = 2 万事件

# (path) -> 响应 JSON。ArkClient._request 满足。
Fetch = Callable[[str], Awaitable[dict]]


@dataclass
class Pull:
    events: list[dict]  # 升序(旧→新),只含 last_event_id 之后的新事件
    complete: bool = True  # False = 翻到页数上限仍没追上已同步位置,本次结果不可用,下个周期重来
    fell_back: bool = False  # True = 倒序自检未通过,已退回全量拉取
    # 该 session **最新**一个事件的 processed_at(原样字符串);没有任何事件为空串。
    # 用于"回看窗口"判断:倒序第一页的第一条就是它,所以窗口判断不必再单独发请求。
    newest_at: str = ""


class DescOrderUnreliable(Exception):
    """倒序结果不满足预期(不是严格倒序,或翻页游标没有推进)。"""


async def pull_new_events(
    fetch: Fetch,
    session_id: str,
    *,
    last_event_id: str = "",
    max_pages: Optional[int] = None,
) -> Pull:
    # 默认值在调用时才取模块常量:写成 ``max_pages: int = MAX_PAGES`` 会在定义时绑定,之后改常量不生效。
    limit = MAX_PAGES if max_pages is None else max_pages
    try:
        return await _pull_desc(fetch, session_id, last_event_id, limit)
    except DescOrderUnreliable as error:
        log.warning("倒序拉取自检未通过,退回全量 session=%s: %s", session_id, error)
        full = await _pull_full(fetch, session_id)
        newest = max((str(e.get("processed_at") or "") for e in full), key=ts_ns, default="")
        return Pull(_after(full, last_event_id), fell_back=True, newest_at=newest)


async def _pull_desc(
    fetch: Fetch, session_id: str, last_event_id: str, max_pages: int
) -> Pull:
    collected: list[dict] = []  # 新→旧
    seen_cursors: set[str] = set()
    page = ""
    newest_at = ""
    for _ in range(max_pages):
        query = {"limit": str(PAGE_SIZE), "order": "desc", **({"page": page} if page else {})}
        payload = await fetch(f"/sessions/{quote(session_id, safe='')}/events?{urlencode(query)}")
        batch = [e for e in (payload.get("data") or []) if isinstance(e, dict)]
        _check_desc(batch)
        if not newest_at and batch:
            newest_at = str(batch[0].get("processed_at") or "")
        for event in batch:
            if last_event_id and event.get("id") == last_event_id:
                return Pull(_oldest_first(collected), newest_at=newest_at)  # 追上了已同步位置
            collected.append(event)
        next_page = str(payload.get("next_page") or "")
        if not next_page:
            # 翻到最旧一页都没遇到 last_event_id:首次(无记录)是正常的;有记录则说明那个事件已不存在
            # (被清理),此时 collected 是全部可见事件,调用方按时间去重,见 SessionSyncer。
            return Pull(_oldest_first(collected), newest_at=newest_at)
        if next_page in seen_cursors:
            raise DescOrderUnreliable("翻页游标没有推进")
        seen_cursors.add(next_page)
        page = next_page
    # 到上限还没追上:collected 只是最新的一段,缺中间部分,不能写(顺序会断)。放弃本周期,下次重来。
    return Pull([], complete=False, newest_at=newest_at)


def _check_desc(batch: list[dict]) -> None:
    times = [ts_ns(e.get("processed_at")) for e in batch]
    if any(a < b for a, b in zip(times, times[1:])):
        raise DescOrderUnreliable("返回的事件不是倒序")


def _oldest_first(newest_first: list[dict]) -> list[dict]:
    return list(reversed(newest_first))


async def _pull_full(fetch: Fetch, session_id: str) -> list[dict]:
    events: list[dict] = []
    page, seen = "", set()
    while True:
        query = {"limit": "200", **({"page": page} if page else {})}
        payload = await fetch(f"/sessions/{quote(session_id, safe='')}/events?{urlencode(query)}")
        events.extend(e for e in (payload.get("data") or []) if isinstance(e, dict))
        page = str(payload.get("next_page") or "")
        if not page:
            return events
        if page in seen:
            raise RuntimeError("全量拉取的翻页游标没有推进")
        seen.add(page)


def _after(events: list[dict], last_event_id: str) -> list[dict]:
    if not last_event_id:
        return events
    for index, event in enumerate(events):
        if event.get("id") == last_event_id:
            return events[index + 1 :]
    return events
