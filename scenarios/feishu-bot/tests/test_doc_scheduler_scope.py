"""文档同步的三项调整:完整标题、去掉每周期个数上限(写飞书单独限额)、候选不再截断。

关键事实(2026-11-12 实测):
- 会话型 bot(分析师 / ada-pm)的 conversations.updated_at 只在新建会话时更新,与真实活动时间差好几天,
  所以"回看窗口"只能靠方舟事件流的最后一个事件时间判断,不能靠库里的时间;
- 一次空检查(拉最新 1~100 条)约 136 ms,检查本身很便宜,真正慢、会失败的是写飞书。
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_doc_scheduler import FakeApi, FakeArk, FakeSource, done, ev, idle_ev, ref  # noqa: E402

from arkagent.observability.adapters.base import SessionRef, TraceRef  # noqa: E402
from arkagent.observability.docscheduler import DocScheduler, SyncConfig  # noqa: E402
from arkagent.observability.docsink import DocStore, SessionDocSink  # noqa: E402
from arkagent.observability.events import ts_ms  # noqa: E402

NOW = 1_792_200_000.0   # 与 ev() 的 2026-10-16 事件时间对齐不重要:窗口判断用的是「事件时间 vs now」,各用例自行设定


def plain(x):
    return re.sub(r"<[^>]+>", "", x)


def title_of(api, index=0):
    posts = [c for c in api.calls if c[0] == "POST"]
    return re.search(r"<title>(.*?)</title>", posts[index][2]["content"]).group(1)


def mk(tmp_path, sources, sessions, *, cfg=None, now=None, api=None, resolver=None):
    now = now if now is not None else [NOW]
    store = DocStore(str(tmp_path / "d.db"), clock=lambda: now[0])
    api = api or FakeApi()
    ark = FakeArk(sessions)
    sched = DocScheduler(sources, ark.fetch, SessionDocSink(store, "p", api), store, cfg or SyncConfig(),
                         clock=lambda: now[0], name_resolver=resolver)
    return sched, store, api, ark, now


def ref_c(sid, *, scenario="analyst", chat="oc_chat1", chat_type="group", user_open="ou_u1", status="active", started=0):
    return TraceRef(trace_id=sid, root_kind="session", scenario=scenario, status=status, started_at_ms=started,
                    chat_id=chat, chat_type=chat_type, sessions=[SessionRef(sid, "business")], meta={"user_open_id": user_open})


# ============================== 一、标题与信息卡 ==============================
async def test_title_has_bot_full_session_id_and_full_date_taken_from_first_event(tmp_path):
    sid = "sesn-20261111134012-577ayclbbc"
    evs = done([ev(1, "e1"), ev(2, "e2")], sec=3)
    sched, store, api, ark, now = mk(tmp_path, {"ada-pm": FakeSource([ref_c(sid, scenario="ada-pm")])}, {sid: evs},
                                      cfg=SyncConfig(lookback_days=10**6))
    await sched.run_once()
    t = title_of(api)
    assert t == f"[ada-pm] {sid} · 2026-10-16 14:00"           # 完整 id(可直接拿去 trace show);年份齐全;时间=第一个事件
    assert "…" not in t and len(t) > 40


async def test_title_time_does_not_come_from_the_unreliable_db_timestamp(tmp_path):
    # 会话型 bot 的库时间只在新建时更新,与真实开始时间可能差几天:标题不能用它
    sid = "sesn-x"
    r = ref_c(sid, started=1_000_000)          # 库里一个离谱的时间(1970)
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([r])}, {sid: done([ev(5, "e1")], sec=6)}, cfg=SyncConfig(lookback_days=10**6))
    await sched.run_once()
    assert "1970" not in title_of(api) and "2026-10-16" in title_of(api)


async def test_title_escapes_markup_in_bot_name(tmp_path):
    sched, store, api, ark, now = mk(tmp_path, {"x": FakeSource([ref_c("s", scenario="a<b>&c")])}, {"s": done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6))
    await sched.run_once()
    assert "<b>" not in api.calls[0][2]["content"].split("</title>")[0]


async def test_first_batch_has_an_info_card_with_only_known_facts(tmp_path):
    sid = "sesn-1"
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid, chat="oc_abc", chat_type="p2p", user_open="ou_zzz")])},
                                      {sid: done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6))
    await sched.run_once()
    body = plain(api.calls[0][2]["content"])
    assert "会话信息" in body and "analyst" in body and sid in body
    assert "oc_abc" in body and "私聊" in body and "ou_zzz" in body
    assert "已归档" not in body and "user_id" not in body            # 没有的字段不写,不编


async def test_info_card_is_only_in_the_first_batch_not_repeated_on_append(tmp_path):
    sid = "sesn-1"
    sessions = {sid: done([ev(1, "e1")], sec=2)}
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid)])}, sessions, cfg=SyncConfig(lookback_days=10**6))
    await sched.run_once()
    sessions[sid] += done([ev(10, "e10")], sec=11)
    now[0] += 130
    await sched.run_once()
    assert "会话信息" in plain(api.calls[0][2]["content"]) and "会话信息" not in plain(api.calls[1][2]["content"])


async def test_info_card_marks_archived_sessions(tmp_path):
    sid = "sesn-old"
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid, status="archived")])}, {sid: done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6))
    await sched.run_once()
    assert "已归档" in plain(api.calls[0][2]["content"])


# ============================== 二、不再有「每周期 N 个」的硬上限 ==============================
async def test_all_due_sessions_are_checked_in_one_cycle_no_matter_how_many(tmp_path):
    n = 60                                                     # 远多于旧上限 20
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(n)}
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=10_000)
    sched, store, api, ark, now = mk(tmp_path, {"a": FakeSource([ref_c(f"s{i}") for i in range(n)])}, sessions, cfg=cfg)
    stats = await sched.run_once()
    assert stats["candidates"] == n and stats["written"] == n  # 一个周期全部处理


async def test_empty_checks_are_not_limited_and_use_exactly_one_request_each(tmp_path):
    n = 50
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(n)}
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=10_000)
    sched, store, api, ark, now = mk(tmp_path, {"a": FakeSource([ref_c(f"s{i}") for i in range(n)])}, sessions, cfg=cfg)
    await sched.run_once()
    ark.requests.clear()
    now[0] += 130
    stats = await sched.run_once()                             # 没有任何新事件:全是空检查
    assert stats["written"] == 0 and len(ark.requests) == n    # 每个 session 恰好 1 次请求,没有被上限截掉


async def test_writes_are_rate_limited_separately_and_new_sessions_go_first(tmp_path):
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(6)}
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=2)
    sched, store, api, ark, now = mk(tmp_path, {"a": FakeSource([ref_c(f"s{i}") for i in range(6)])}, sessions, cfg=cfg)
    assert (await sched.run_once())["written"] == 2           # 写飞书受限
    assert len([c for c in api.calls if c[0] == "POST"]) == 2
    now[0] += 130
    assert (await sched.run_once())["written"] == 2           # 下一周期继续,没处理的不会丢
    now[0] += 130
    assert (await sched.run_once())["written"] == 2
    created = {c[2]["content"].split("</title>")[0] for c in api.calls if c[0] == "POST"}
    assert len(created) == 6                                   # 每个 session 最终都轮到了,没有人被饿死


async def test_write_limit_never_blocks_checking_other_sessions(tmp_path):
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(5)}
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=1)
    sched, store, api, ark, now = mk(tmp_path, {"a": FakeSource([ref_c(f"s{i}") for i in range(5)])}, sessions, cfg=cfg)
    stats = await sched.run_once()
    assert stats["written"] == 1 and stats["deferred"] == 4 and len(ark.requests) == 5   # 5 个都检查了,4 个等写额度


async def test_a_session_waiting_for_write_quota_is_not_marked_idle_or_lost(tmp_path):
    sessions = {"a": done([ev(1, "a1")], sec=2), "b": done([ev(1, "b1")], sec=2)}
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=1)
    sched, store, api, ark, now = mk(tmp_path, {"x": FakeSource([ref_c("a"), ref_c("b")])}, sessions, cfg=cfg)
    await sched.run_once()
    now[0] += 130
    await sched.run_once()
    assert store.get("a") is not None and store.get("b") is not None     # 两个最终都写了


async def test_appends_to_existing_docs_are_prioritised_over_creating_new_ones_or_vice_versa_but_never_starve(tmp_path):
    sessions = {"old": done([ev(1, "o1")], sec=2)}
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=1)
    sched, store, api, ark, now = mk(tmp_path, {"x": FakeSource([ref_c("old")])}, sessions, cfg=cfg)
    await sched.run_once()                                     # old 已有文档
    sessions["new"] = done([ev(1, "n1")], sec=2)
    sessions["old"] += done([ev(10, "o2")], sec=11)
    sched._sources = {"x": FakeSource([ref_c("old"), ref_c("new")])}
    now[0] += 130
    await sched.run_once()
    now[0] += 130
    await sched.run_once()
    assert store.get("new") is not None                        # 新 session 第一时间出文档,不会被持续追加的老 session 挤掉


async def test_cycle_time_budget_stops_checking_and_resumes_next_cycle_from_the_longest_unchecked(tmp_path):
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(6)}
    ticks = iter(range(10**6))

    class Slow(FakeArk):
        async def fetch(self, path):
            clock[0] += 40                                      # 每次检查「耗时」40 秒
            return await super().fetch(path)

    clock = [NOW]
    store = DocStore(str(tmp_path / "d.db"), clock=lambda: clock[0])
    api = FakeApi()
    ark = Slow(sessions)
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=1000, cycle_budget_s=100, check_concurrency=1)
    sched = DocScheduler({"x": FakeSource([ref_c(f"s{i}") for i in range(6)])}, ark.fetch, SessionDocSink(store, "p", api), store, cfg, clock=lambda: clock[0])
    first = await sched.run_once()
    assert 0 < first["written"] < 6                              # 预算用完就停,没有强行跑完
    for _ in range(5):
        clock[0] += 130
        await sched.run_once()
    assert all(store.get(f"s{i}") is not None for i in range(6))  # 之后的周期接着做,最终全部处理到


async def test_check_concurrency_is_bounded(tmp_path):
    active, peak = [0], [0]
    sessions = {f"s{i}": done([ev(1, f"s{i}-e")], sec=2) for i in range(12)}

    class Counting(FakeArk):
        async def fetch(self, path):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.01)
            try:
                return await super().fetch(path)
            finally:
                active[0] -= 1

    now = [NOW]
    store = DocStore(str(tmp_path / "d.db"), clock=lambda: now[0])
    ark = Counting(sessions)
    cfg = SyncConfig(lookback_days=10**6, max_writes_per_cycle=1000, check_concurrency=3)
    sched = DocScheduler({"x": FakeSource([ref_c(f"s{i}") for i in range(12)])}, ark.fetch, SessionDocSink(store, "p", FakeApi()), store, cfg, clock=lambda: now[0])
    await sched.run_once()
    assert 1 < peak[0] <= 3                                      # 确实并发了,但不超过配置


# ============================== 三、候选不再截断,回看窗口只信事件流 ==============================
def ev_at(ts_iso, eid, etype="agent.message"):
    return {"id": eid, "type": etype, "processed_at": ts_iso, "content": [{"type": "text", "text": eid}]}


def idle_at(ts_iso, eid):
    return ev_at(ts_iso, eid, "session.status_idle") | {"stop_reason": {"type": "end_turn"}}


def ts_to_epoch(iso):
    return ts_ms(iso) / 1000


async def test_candidates_are_not_truncated_to_a_fixed_count(tmp_path):
    n = 450                                                    # 旧实现每个库只取 200
    base = "2026-11-12T10:00:00.000000000+08:00"
    sessions = {f"s{i}": [ev_at(base, f"s{i}-e"), idle_at(base, f"s{i}-i")] for i in range(n)}
    now = [ts_to_epoch(base) + 60]
    cfg = SyncConfig(max_writes_per_cycle=10_000, check_concurrency=8)
    src = FakeSource([ref_c(f"s{i}") for i in range(n)])
    sched, store, api, ark, _ = mk(tmp_path, {"a": src}, sessions, cfg=cfg, now=now)
    stats = await sched.run_once()
    assert stats["candidates"] == n and stats["written"] == n  # 450 个全部处理,第 201 个以后没有被丢掉


async def test_old_session_is_judged_by_its_last_event_not_by_unreliable_db_time(tmp_path):
    # 库里 started=1970(会话型 updated_at 常常如此陈旧),但事件流显示它昨天还在聊 → 必须被同步
    now = [ts_to_epoch("2026-11-12T20:00:00.000000000+08:00")]
    sessions = {"active-but-db-old": [ev_at("2026-11-12T18:00:00.000000000+08:00", "a1"), idle_at("2026-11-12T18:00:05.000000000+08:00", "a2")]}
    sched, store, api, ark, _ = mk(tmp_path, {"a": FakeSource([ref_c("active-but-db-old", started=1_000)])}, sessions, cfg=SyncConfig(lookback_days=7), now=now)
    assert (await sched.run_once())["written"] == 1


async def test_session_whose_last_event_is_outside_the_window_is_skipped(tmp_path):
    now = [ts_to_epoch("2026-11-12T20:00:00.000000000+08:00")]
    sessions = {"stale": [ev_at("2026-10-01T10:00:00.000000000+08:00", "x1"), idle_at("2026-10-01T10:00:05.000000000+08:00", "x2")]}
    sched, store, api, ark, _ = mk(tmp_path, {"a": FakeSource([ref_c("stale", started=int(now[0] * 1000))])}, sessions, cfg=SyncConfig(lookback_days=7), now=now)
    stats = await sched.run_once()
    assert stats["written"] == 0 and api.calls == []           # 库里说是"刚创建",但事件流里 6 周前就停了


async def test_window_check_costs_one_request_and_is_not_repeated_every_cycle(tmp_path):
    now = [ts_to_epoch("2026-11-12T20:00:00.000000000+08:00")]
    sessions = {f"old{i}": [ev_at("2026-09-01T10:00:00.000000000+08:00", f"o{i}"), idle_at("2026-09-01T10:00:05.000000000+08:00", f"oi{i}")] for i in range(10)}
    sched, store, api, ark, _ = mk(tmp_path, {"a": FakeSource([ref_c(f"old{i}") for i in range(10)])}, sessions, cfg=SyncConfig(lookback_days=7), now=now)
    await sched.run_once()
    assert len(ark.requests) == 10                              # 第一次:每个 1 次请求确认「在窗口外」
    ark.requests.clear()
    now[0] += 130
    await sched.run_once()
    assert len(ark.requests) == 0                               # 已确认在窗口外的,下一周期不再重复检查


async def test_outside_window_session_is_rechecked_later_in_case_it_wakes_up(tmp_path):
    now = [ts_to_epoch("2026-11-12T20:00:00.000000000+08:00")]
    sessions = {"sleepy": [ev_at("2026-09-01T10:00:00.000000000+08:00", "s1"), idle_at("2026-09-01T10:00:05.000000000+08:00", "s2")]}
    sched, store, api, ark, _ = mk(tmp_path, {"a": FakeSource([ref_c("sleepy")])}, sessions, cfg=SyncConfig(lookback_days=7, outside_window_recheck_s=3600), now=now)
    await sched.run_once()
    sessions["sleepy"] += [ev_at("2026-11-12T20:30:00.000000000+08:00", "s3"), idle_at("2026-11-12T20:30:05.000000000+08:00", "s4")]   # 用户又来聊了
    now[0] += 7200                                              # 过了复查间隔
    assert (await sched.run_once())["written"] == 1             # 被重新发现,而不是永远被当成老 session


async def test_session_with_no_events_at_all_is_not_a_candidate_and_does_not_crash(tmp_path):
    sched, store, api, ark, now = mk(tmp_path, {"a": FakeSource([ref_c("empty")])}, {"empty": []}, cfg=SyncConfig(lookback_days=7))
    stats = await sched.run_once()
    assert stats["written"] == 0 and api.calls == []


async def test_purged_session_404_is_not_retried_every_cycle(tmp_path):
    class Gone(Exception):
        status_code = 404

    sched, store, api, ark, now = mk(tmp_path, {"a": FakeSource([ref_c("gone")])}, {"gone": Gone("nf")}, cfg=SyncConfig(lookback_days=7))
    await sched.run_once()
    ark.requests.clear()
    now[0] += 130
    await sched.run_once()
    assert len(ark.requests) == 0                               # 方舟已清理:确认一次后不再每周期去撞


# ---- 信息卡:名字解析(群名 / 用户名),查不到降级 ----------------------------------
async def test_info_card_shows_resolved_chat_and_user_names(tmp_path):
    async def resolver(ref):
        return {"chat_name": "热点周报群", "user_name": "张三"}

    sid = "sesn-1"
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid, chat="oc_abc", chat_type="group", user_open="ou_zzz")])},
                                     {sid: done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6), resolver=resolver)
    await sched.run_once()
    body = plain(api.calls[0][2]["content"])
    assert "热点周报群" in body and "张三" in body          # 可读名
    assert "oc_abc" in body and "ou_zzz" in body          # 原 ID 保留,便于落库定位
    assert "群聊" in body and "群组" in body               # 类型标识


async def test_info_card_falls_back_to_ids_when_names_unresolvable(tmp_path):
    async def resolver(ref):
        return {"chat_name": "", "user_name": ""}         # 查不到

    sid = "sesn-1"
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid, chat="oc_abc", chat_type="p2p", user_open="ou_zzz")])},
                                     {sid: done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6), resolver=resolver)
    await sched.run_once()
    body = plain(api.calls[0][2]["content"])
    assert "oc_abc" in body and "ou_zzz" in body and "私聊" in body


async def test_name_resolver_failure_does_not_block_the_write(tmp_path):
    async def resolver(ref):
        raise RuntimeError("飞书查询挂了")

    sid = "sesn-1"
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid, chat="oc_abc", chat_type="group", user_open="ou_zzz")])},
                                     {sid: done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6), resolver=resolver)
    stats = await sched.run_once()
    assert stats["written"] == 1                          # 名字查不到不阻断写入
    assert "oc_abc" in plain(api.calls[0][2]["content"])


async def test_no_resolver_means_no_name_lookup_and_empty_card_still_written(tmp_path):
    sid = "sesn-1"
    sched, store, api, ark, now = mk(tmp_path, {"analyst": FakeSource([ref_c(sid, chat="oc_abc", chat_type="group", user_open="ou_zzz")])},
                                     {sid: done([ev(1, "e")], sec=2)}, cfg=SyncConfig(lookback_days=10**6))  # 不传 resolver
    stats = await sched.run_once()
    assert stats["written"] == 1 and "oc_abc" in plain(api.calls[0][2]["content"])
