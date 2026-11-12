"""定时文档同步:增量拉取(倒序遇旧 id 即停)、调度(降频 / 终态 / 单周期上限)、失败自愈。

事件夹具按真实结构构造;fetch 用假实现,按 order / page 参数模拟方舟接口(含实测到的行为:
末页 next_page 为空、order=desc 可用)。
"""
import json
from urllib.parse import parse_qs, urlparse

from arkagent.observability.adapters.base import SessionRef, TraceRef
from arkagent.observability.docscheduler import DocScheduler, SyncConfig, due
from arkagent.observability.docsink import DocStore, SessionDocSink
from arkagent.observability.incremental import pull_new_events


def ev(sec, eid, etype="agent.message"):
    return {"id": eid, "type": etype, "processed_at": f"2026-10-16T14:{sec // 60:02d}:{sec % 60:02d}.5+08:00",
            "content": [{"type": "text", "text": eid}]}


def idle_ev(sec, eid):
    return ev(sec, eid, "session.status_idle") | {"stop_reason": {"type": "end_turn"}}


def done(events, sec=999):
    """给一批事件补上 idle 结尾,模拟「这一轮已经结束」——调度器只写完整的一轮(见 complete_prefix)。"""
    return [*events, idle_ev(sec, f"idle-{events[-1]['id']}")]


class FakeArk:
    """模拟 GET /sessions/{id}/events:支持 limit / order / page,末页 next_page 为空。"""

    def __init__(self, by_session, *, broken_desc=False):
        self.by_session, self.requests, self.broken_desc = by_session, [], broken_desc

    async def fetch(self, path):
        url = urlparse(path)
        sid = url.path.split("/")[2]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append((sid, q.get("order", "asc"), q.get("page", "")))
        events = self.by_session[sid]
        if isinstance(events, Exception):
            raise events
        ordered = sorted(events, key=lambda e: e["processed_at"], reverse=q.get("order") == "desc")
        if self.broken_desc and q.get("order") == "desc":
            ordered = sorted(events, key=lambda e: e["processed_at"])   # 假装忽略了 order 参数
        size, start = int(q["limit"]), int(q.get("page") or 0)
        batch = ordered[start:start + size]
        nxt = str(start + size) if start + size < len(ordered) else ""
        return {"data": batch, **({"next_page": nxt} if nxt else {})}


async def test_no_new_events_costs_exactly_one_request():
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 6)]})
    pull = await pull_new_events(ark.fetch, "s", last_event_id="e5")
    assert pull.events == [] and pull.complete and len(ark.requests) == 1


async def test_returns_only_events_after_last_synced_oldest_first():
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 11)]})
    pull = await pull_new_events(ark.fetch, "s", last_event_id="e7")
    assert [e["id"] for e in pull.events] == ["e8", "e9", "e10"]      # 升序,可直接渲染
    assert len(ark.requests) == 1                                      # 新事件都在第一页


async def test_first_sync_without_record_returns_everything_in_order():
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 8)]})
    pull = await pull_new_events(ark.fetch, "s")
    assert [e["id"] for e in pull.events] == [f"e{i}" for i in range(1, 8)] and pull.complete


async def test_large_backlog_pages_until_it_reaches_the_synced_event(monkeypatch):
    import arkagent.observability.incremental as inc
    monkeypatch.setattr(inc, "PAGE_SIZE", 3)
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 21)]})
    pull = await pull_new_events(ark.fetch, "s", last_event_id="e12")
    assert [e["id"] for e in pull.events] == [f"e{i}" for i in range(13, 21)]
    assert len(ark.requests) == 3                                      # 8 条新事件 / 3 = 3 页,没有读到 e12 之前更多


async def test_page_limit_gives_up_the_cycle_rather_than_returning_a_partial_tail(monkeypatch):
    import arkagent.observability.incremental as inc
    monkeypatch.setattr(inc, "PAGE_SIZE", 3)
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 31)]})
    pull = await pull_new_events(ark.fetch, "s", max_pages=2)
    # 只翻到最新的 6 条,缺中间部分:返回它们会让文档顺序断掉,所以宁可什么都不给
    assert not pull.complete and pull.events == []
    full = await pull_new_events(ark.fetch, "s", max_pages=50)           # 下个周期从头再来,不丢
    assert full.complete and [e["id"] for e in full.events] == [f"e{i}" for i in range(1, 31)]


async def test_unreliable_desc_falls_back_to_full_pull():
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 8)]}, broken_desc=True)
    pull = await pull_new_events(ark.fetch, "s", last_event_id="e5")
    assert pull.fell_back and [e["id"] for e in pull.events] == ["e6", "e7"]   # 结果仍然正确


async def test_purged_last_event_returns_all_visible_events():
    ark = FakeArk({"s": [ev(i, f"e{i}") for i in range(1, 5)]})
    pull = await pull_new_events(ark.fetch, "s", last_event_id="e-gone")
    assert [e["id"] for e in pull.events] == ["e1", "e2", "e3", "e4"]   # 交给调度层按时间去重


# ---- 调度 ------------------------------------------------------------------
CFG = SyncConfig(interval_s=120, max_per_cycle=2)
NOW = 10_000_000.0   # 测试时钟(秒);session 的 started_at_ms 要相对它,否则会被 7 天回看窗口正确地丢掉


def ref(sid, status="active", started=int((NOW - 3600) * 1000)):
    return TraceRef(trace_id=sid, root_kind="session", scenario="analyst", status=status,
                    started_at_ms=started, sessions=[SessionRef(sid, "business")])


def test_due_first_time_always():
    assert due(ref("s"), 0, 0, 1000.0, CFG, synced=False)


def test_due_respects_interval_and_idle_backoff():
    assert not due(ref("s"), 1000, 0, 1000 + 119, CFG, synced=True)
    assert due(ref("s"), 1000, 0, 1000 + 120, CFG, synced=True)
    assert not due(ref("s"), 1000, 3, 1000 + 120 * 3 - 1, CFG, synced=True)   # 连续 3 次没动静:间隔 ×3
    assert due(ref("s"), 1000, 3, 1000 + 120 * 3, CFG, synced=True)
    assert not due(ref("s"), 1000, 10, 1000 + 120 * 15 - 1, CFG, synced=True)


def test_terminal_and_synced_and_confirmed_idle_stops_polling():
    assert not due(ref("s", "done"), 1000, 1, 10**9, CFG, synced=True)
    assert due(ref("s", "done"), 1000, 0, 10**9, CFG, synced=True)             # 终态但刚有新内容:还要再确认一次
    assert due(ref("s", "active"), 1000, 50, 10**9, CFG, synced=True)          # 会话型没有终态:只降频,永不停


class FakeSource:
    def __init__(self, refs):
        self.refs = refs

    def list_refs(self, *, limit=20, status=None, chat_id=None):
        return self.refs

    def resolve(self, ident):
        return None


class FakeApi:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail:
            raise RuntimeError("飞书接口失败")
        return {"data": {"document": {"document_id": "doxcnX", "url": "https://x/docx/doxcnX"}}} if method == "POST" else {"ok": True}


def make(tmp_path, refs, sessions, *, api=None, cfg=CFG, now=None):
    now = now if now is not None else [NOW]
    store = DocStore(str(tmp_path / "d.db"), clock=lambda: now[0])    # 与调度器同一个时钟
    api = api or FakeApi()
    ark = FakeArk(sessions)
    sched = DocScheduler({"analyst": FakeSource(refs)}, ark.fetch, SessionDocSink(store, "p", api), store, cfg, clock=lambda: now[0])
    return sched, store, api, ark, now


async def test_cycle_writes_new_session_then_only_appends_new_events(tmp_path):
    sessions = {"s1": done([ev(i, f"e{i}") for i in range(1, 4)], sec=5)}
    sched, store, api, ark, now = make(tmp_path, [ref("s1")], sessions)
    assert (await sched.run_once())["written"] == 1 and api.calls[0][0] == "POST"
    now[0] += 130
    sessions["s1"] += done([ev(10, "e10")], sec=11)
    assert (await sched.run_once())["written"] == 1
    method, path, body = api.calls[1]
    assert method == "PUT" and "e10" in body["content"] and "e1\"" not in body["content"]   # 只追加新的
    now[0] += 130
    assert (await sched.run_once())["written"] == 0 and len(api.calls) == 2                  # 没动静:不写


async def test_cycle_caps_work_and_prefers_longest_unchecked(tmp_path):
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(5)}
    sched, store, api, ark, now = make(tmp_path, [ref(f"s{i}") for i in range(5)], sessions)
    assert (await sched.run_once())["written"] == 2                    # max_per_cycle=2
    now[0] += 5
    assert (await sched.run_once())["written"] == 2                    # 下一周期轮到另外的,不重复处理已检查的
    written = {c[2]["content"].split("<title>")[1].split("</title>")[0] for c in api.calls if c[0] == "POST"}
    assert len(written) == 4


async def test_write_failure_does_not_advance_and_is_retried_next_cycle(tmp_path):
    sessions = {"s1": done([ev(1, "e1")], sec=2)}
    sched, store, api, ark, now = make(tmp_path, [ref("s1")], sessions, api=FakeApi(fail=True))
    assert (await sched.run_once())["failed"] == 1 and store.get("s1") is None
    api.fail = False
    now[0] += 130
    assert (await sched.run_once())["written"] == 1                    # 自愈:补上


async def test_over_limit_backlog_is_not_written_partially_and_not_marked_done(tmp_path, monkeypatch):
    import arkagent.observability.incremental as inc
    monkeypatch.setattr(inc, "PAGE_SIZE", 3)
    monkeypatch.setattr(inc, "MAX_PAGES", 2)
    sessions = {"s": [ev(i, f"e{i}") for i in range(1, 31)]}
    sched, store, api, ark, now = make(tmp_path, [ref("s")], sessions)
    assert (await sched.run_once())["failed"] == 1
    assert api.calls == [] and store.get("s") is None                    # 没写任何半截内容,也没有记录

async def test_one_broken_session_does_not_block_others(tmp_path):
    sessions = {"bad": RuntimeError("500"), "good": done([ev(1, "g1")], sec=2)}
    sched, store, api, ark, now = make(tmp_path, [ref("bad"), ref("good")], sessions)
    stats = await sched.run_once()
    assert stats["failed"] == 1 and stats["written"] == 1


async def test_session_outside_lookback_window_is_skipped_until_it_has_a_record(tmp_path):
    old = ref("old", started=1)                                         # 远早于回看窗口(1970 年)
    sched, *_ = make(tmp_path, [old], {"old": [ev(1, "o1")]})
    assert (await sched.run_once())["candidates"] == 0


async def test_already_synced_long_running_session_is_followed_beyond_the_window(tmp_path):
    sessions = {"s": [ev(1, "e1")]}
    r = ref("s", started=int((NOW - 8 * 86400) * 1000))                # 8 天前开始
    sched, store, api, ark, now = make(tmp_path, [r], sessions)
    store.create("s", "doxcnX", "u")                                   # 已经开始同步过
    sessions["s"].append(ev(5, "e2"))
    assert (await sched.run_once())["candidates"] == 1                 # 长任务不能因为超过 7 天就被丢下


async def test_purged_last_event_does_not_duplicate_already_written_content(tmp_path):
    sessions = {"s": done([ev(i, f"e{i}") for i in range(1, 5)], sec=6)}
    sched, store, api, ark, now = make(tmp_path, [ref("s")], sessions)
    await sched.run_once()
    last_idle = "idle-e4"
    sessions["s"] = [ev(i, f"e{i}") for i in range(3, 5)] + [idle_ev(6, last_idle)] + done([ev(9, "e9")], sec=10)   # 前面被方舟清理,上次末尾 idle 还在
    now[0] += 130
    await sched.run_once()
    appended = api.calls[1][2]["content"]
    assert "e9" in appended and "e3\"" not in appended and "e4\"" not in appended


# ---- 只写完整的一轮对话(调度层):运行中的 session 不落笔 -----------------------------------
def ref_at(sid, events):
    """开始时间取自事件本身:这组用例的时钟跟着真实事件走,ref 若仍用固定的假时间会被回看窗口(正确地)过滤掉。"""
    return ref(sid, started=int(ts_of_event(events[0]) * 1000))


def running_tail(sec0, n=1):
    """一轮正在进行:用户消息 + 一个还没返回的工具调用,没有 idle。"""
    return [ev(sec0, f"u{n}", "user.message"), ev(sec0 + 1, f"tu{n}", "agent.tool_use") | {"name": "bash", "input": {}}]


async def test_session_still_running_writes_nothing_and_is_not_counted_as_idle(tmp_path):
    sessions = {"s": running_tail(1)}
    sched, store, api, ark, now = make(tmp_path, [ref_at("s", sessions["s"])], sessions)
    now[0] = ts_of_event(sessions["s"][-1]) + 5          # 刚有动静
    assert (await sched.run_once())["written"] == 0 and api.calls == [] and store.get("s") is None


async def test_earlier_complete_rounds_are_written_while_current_round_waits(tmp_path):
    sessions = {"s": done([ev(1, "a1")], sec=2) + running_tail(10, n=2)}
    sched, store, api, ark, now = make(tmp_path, [ref_at("s", sessions["s"])], sessions)
    now[0] = ts_of_event(sessions["s"][-1]) + 5
    assert (await sched.run_once())["written"] == 1
    written = api.calls[0][2]["content"]
    assert "a1" in written and "tu2" not in written and "⏳" not in written     # 第 1 轮写了,进行中的第 2 轮没写
    sessions["s"] = sessions["s"] + [ev(14, "tr2", "agent.tool_result") | {"tool_use_id": "tu2", "is_error": False, "content": []}, idle_ev(15, "i2")]
    now[0] += 130
    assert (await sched.run_once())["written"] == 1
    appended = api.calls[1][2]["content"]
    assert "tu2" in appended and "tr2" in appended and "缺调用" not in appended   # 第 2 轮结束后整轮一起写,配对完整
    assert "第 2 轮对话" in re.sub(r"<[^>]+>", "", appended) and "第 1 轮对话" not in re.sub(r"<[^>]+>", "", appended)   # 轮号接续


async def test_stuck_session_is_flushed_after_the_stale_window(tmp_path):
    sessions = {"s": running_tail(1)}
    sched, store, api, ark, now = make(tmp_path, [ref_at("s", sessions["s"])], sessions)
    now[0] = ts_of_event(sessions["s"][-1]) + 31 * 60                            # 31 分钟没有任何动静
    assert (await sched.run_once())["written"] == 1 and api.calls[0][0] == "POST"


async def test_waiting_on_a_running_round_does_not_trigger_idle_backoff(tmp_path):
    sessions = {"s": done([ev(1, "a1")], sec=2) + running_tail(10, n=2)}
    sched, store, api, ark, now = make(tmp_path, [ref_at("s", sessions["s"])], sessions)
    now[0] = ts_of_event(sessions["s"][-1]) + 5
    await sched.run_once()
    now[0] += 130
    await sched.run_once()                                                       # 第 2 轮仍在跑:暂缓,但不能当"静止"降频
    assert store.schedule_info("s")[1] == 0


def ts_of_event(e):
    from arkagent.observability.events import ts_ms
    return ts_ms(e["processed_at"]) / 1000


import re  # noqa: E402
