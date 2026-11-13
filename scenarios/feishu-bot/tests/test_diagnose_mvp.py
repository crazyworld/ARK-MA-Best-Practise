"""排障 Bot MVP:权限边界、确定性诊断、步骤详情脱敏、网关入口。

权限是这个 bot 风险最高的部分,所以测试以\"越权拿不到\"为主。事件夹具按真实结构构造。
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cases"))

from arkagent.observability.adapters.sqlite_sources import PipelineJobSource, TopicxSource
from arkagent.observability.detail import MAX_DETAIL_CHARS, get_step_detail
from arkagent.observability.diagnose import STUCK_TOOL_MS, diagnose
from arkagent.observability.events import parse_session_events
from arkagent.observability.model import Trace
from diagnose.gateway import DiagnoseGateway, build_sources
from diagnose.tools import DiagnoseTools, Scope


def ev(etype, sec, eid=None, **fields):
    return {"id": eid or f"{etype}-{sec}", "type": etype,
            "processed_at": f"2026-10-16T14:38:{sec:02d}.508799758+08:00", **fields}


def text(body):
    return [{"type": "text", "text": body}]


def G(chat_id):
    """群聊范围。"""
    return Scope(chat_id=chat_id, chat_type="group")


FAILED_EVENTS = [
    ev("user.message", 1, content=text("go")),
    ev("span.model_request_start", 2, "ms"),
    ev("span.model_request_end", 2, "me", model_request_start_id="ms", is_error=True, model_usage={}),
    ev("session.error", 3, error={"type": "model_request_failed_error", "message": "provider issue",
                                  "retry_status": {"type": "exhausted"}}),
    ev("session.status_idle", 3, stop_reason={"type": "retries_exhausted"}),
]

RETRIED_EVENTS = [
    ev("user.message", 1, content=text("go")),
    ev("span.model_request_start", 2, "ms1"),
    ev("span.model_request_end", 2, "me1", model_request_start_id="ms1", is_error=True, model_usage={}),
    ev("span.model_request_start", 3, "ms2"),
    ev("span.model_request_end", 5, "me2", model_request_start_id="ms2", is_error=False,
       model_usage={"input_tokens": 9, "output_tokens": 1}),
    ev("session.status_idle", 6, stop_reason={"type": "end_turn"}),
]


class FakeArk:
    def __init__(self, by_session):
        self.by_session = by_session

    async def list_session_events(self, session_id, limit=200):
        return self.by_session[session_id]


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pipeline_jobs (job_id TEXT PRIMARY KEY, chat_id TEXT, ma_session_id TEXT, mode TEXT,
            current_phase TEXT, status TEXT, started_at INTEGER, finished_at INTEGER, last_error TEXT DEFAULT '');
        CREATE TABLE pipeline_hc_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, hc_kind TEXT,
            user_decision TEXT DEFAULT '', created_at INTEGER, resolved_at INTEGER);
        INSERT INTO pipeline_jobs (job_id, chat_id, ma_session_id, mode, current_phase, status, started_at, last_error)
          VALUES ('job_mine', 'oc_A', 'sesn-mine', 'demo', 'A', 'failed', 1000, 'model_request_failed_error'),
                 ('job_other', 'oc_B', 'sesn-other', 'demo', 'A', 'failed', 2000, 'secret failure of group B');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def tools(tmp_path):
    """线上新路径:topicx 的 task_runs(带 chat_type)+ topic6 的 pipeline_jobs。"""
    job_db, ctl_db = tmp_path / "t6.db", tmp_path / "c.db"
    make_db(str(job_db))
    ctl = sqlite3.connect(ctl_db)
    ctl.executescript(
        """
        CREATE TABLE task_runs (task_id TEXT PRIMARY KEY, chat_id TEXT, user_id TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '', router_session_id TEXT, business_session_id TEXT,
            scenario TEXT, mode TEXT, route_reason TEXT, business_job_id TEXT, status TEXT, last_error TEXT,
            created_at INTEGER, finished_at INTEGER);
        CREATE TABLE control_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, task_id TEXT, created_at INTEGER);
        INSERT INTO task_runs VALUES ('task_mine','oc_A','u1','group','sesn-r','sesn-mine','topic6','demo','r','job_mine','failed','model_request_failed_error',1000,NULL);
        INSERT INTO task_runs VALUES ('task_other','oc_B','u2','group','sesn-r','sesn-other','topic6','demo','r','job_other','failed','secret failure of group B',2000,NULL);
        """
    )
    ctl.commit()
    ctl.close()
    ark = FakeArk({"sesn-r": [], "sesn-mine": FAILED_EVENTS, "sesn-other": FAILED_EVENTS})
    return DiagnoseTools(build_sources({"topicx": str(ctl_db), "topic6": str(job_db)}), ark)


# ---- 权限:只能看当前群 ---------------------------------------------------
async def test_list_only_returns_current_chat(tools):
    result, err = await tools.call(G("oc_A"), "list_recent_tasks", {})
    assert not err
    assert [t["id"] for t in result["tasks"]] == ["task_mine"]
    assert "secret failure" not in json.dumps(result, ensure_ascii=False)


async def test_cannot_diagnose_task_from_another_chat(tools):
    result, err = await tools.call(G("oc_A"), "diagnose_task", {"task_id": "task_other"})
    assert not err and result["found"] is False
    # 通过 session id 也不行
    result, _ = await tools.call(G("oc_A"), "diagnose_task", {"task_id": "sesn-other"})
    assert result["found"] is False
    assert "group B" not in json.dumps(result, ensure_ascii=False)


async def test_not_found_message_does_not_reveal_existence(tools):
    other, _ = await tools.call(G("oc_A"), "diagnose_task", {"task_id": "task_other"})
    missing, _ = await tools.call(G("oc_A"), "diagnose_task", {"task_id": "job_nope"})
    assert other == missing


@pytest.mark.parametrize("field", ["chat_id", "open_id", "user_id", "tenant_key"])
async def test_identity_args_are_rejected(tools, field):
    result, err = await tools.call(G("oc_A"), "list_recent_tasks", {field: "oc_B"})
    assert err and "tasks" not in result


async def test_step_detail_session_must_belong_to_task_in_chat(tools):
    # 用本群的 task_id 去取别的群的 session,不能绕过范围
    result, _ = await tools.call(G("oc_A"), "get_step_detail",
        {"task_id": "task_mine", "session_id": "sesn-other", "event_id": "me"},
    )
    assert result["found"] is False
    result, _ = await tools.call(G("oc_A"), "get_step_detail",
        {"task_id": "task_other", "session_id": "sesn-other", "event_id": "me"},
    )
    assert result["found"] is False


async def test_unknown_tool_is_error(tools):
    _, err = await tools.call(G("oc_A"), "delete_everything", {})
    assert err


async def test_default_pick_is_flagged_so_agent_must_confirm(tools):
    result, _ = await tools.call(G("oc_A"), "diagnose_task", {})
    assert result["found"] and result["picked_by_default"] is True
    assert result["task"]["id"] == "task_mine"
    explicit, _ = await tools.call(G("oc_A"), "diagnose_task", {"task_id": "task_mine"})
    assert explicit["picked_by_default"] is False


async def test_empty_chat_has_nothing(tools):
    result, _ = await tools.call(G("oc_C"), "diagnose_task", {})
    assert result["found"] is False


async def test_topicx_task_and_its_job_are_not_double_counted(tmp_path):
    job_db, ctl_db = tmp_path / "t6.db", tmp_path / "c.db"
    make_db(str(job_db))
    ctl = sqlite3.connect(ctl_db)
    ctl.executescript(
        """
        CREATE TABLE task_runs (task_id TEXT PRIMARY KEY, chat_id TEXT, router_session_id TEXT, business_session_id TEXT,
            scenario TEXT, mode TEXT, route_reason TEXT, business_job_id TEXT, status TEXT, last_error TEXT,
            created_at INTEGER, finished_at INTEGER);
        CREATE TABLE control_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, task_id TEXT, created_at INTEGER);
        INSERT INTO task_runs VALUES ('task_1','oc_A','sesn-r','sesn-mine','topic6','demo','r','job_mine','failed','',1000,NULL);
        """
    )
    ctl.commit()
    ctl.close()
    tools = DiagnoseTools(
        build_sources({"topicx": str(ctl_db), "topic6": str(job_db)}),
        FakeArk({"sesn-r": [], "sesn-mine": FAILED_EVENTS}),
    )
    result, _ = await tools.call(G("oc_A"), "list_recent_tasks", {})
    assert [t["id"] for t in result["tasks"]] == ["task_1"]


# ---- 诊断逻辑 -------------------------------------------------------------
def trace_of(events, status="running"):
    node = parse_session_events(events, session_id="s", role="business")
    return Trace("t", "job", "topic6", status, 0, sessions=[node])


def test_exhausted_retries_is_error():
    findings = diagnose(trace_of(FAILED_EVENTS, "failed"))
    assert findings[0].level == "error" and findings[0].code == "model_retries_exhausted"


def test_retried_then_succeeded_is_only_warning():
    findings = diagnose(trace_of(RETRIED_EVENTS, "done"))
    assert [f.code for f in findings] == ["model_retried"]
    assert findings[0].level == "warning"


def test_healthy_trace_has_no_findings():
    ok = [e for e in RETRIED_EVENTS if e["id"] not in ("ms1", "me1")]
    assert diagnose(trace_of(ok, "done")) == []


def test_stuck_tool_only_after_threshold():
    events = [
        ev("user.message", 1, content=text("x")),
        ev("agent.tool_use", 2, "tu", name="bash_wait", input={}),
    ]
    trace = trace_of(events)
    started = trace.sessions[0].turns[0].spans[0].started_at_ms
    assert diagnose(trace, now_ms=started + STUCK_TOOL_MS - 1000) == []
    assert diagnose(trace, now_ms=started + STUCK_TOOL_MS + 1000)[0].code == "tool_stuck"


def test_unavailable_session_is_reported_not_hidden():
    from arkagent.observability.model import SessionNode

    trace = Trace("t", "job", "topic6", "failed", 0, last_error="boom",
                  sessions=[SessionNode("s", "business", available=False, unavailable_reason="已清理")])
    codes = [f.code for f in diagnose(trace)]
    assert "session_unavailable" in codes and "task_failed" in codes


# ---- 步骤详情:正文可见,密钥脱敏,过长截断 --------------------------------
async def test_step_detail_shows_body_but_redacts_secrets():
    events = [
        ev("agent.tool_use", 2, "tu", name="bash", input={"command": "curl -H 'Authorization: Bearer abcdef123456' x"}),
        ev("agent.tool_result", 3, "tr", tool_use_id="tu", is_error=False,
           content=text("活动名称:双十一复盘\napi_key=sk-aaaaaaaaaaaaaaaaaaaa")),
    ]
    detail = await get_step_detail(FakeArk({"s": events}), "s", "tu")
    blob = json.dumps(detail, ensure_ascii=False)
    assert "双十一复盘" in blob and "curl" in blob
    assert "abcdef123456" not in blob and "sk-aaaaaaaaaaaaaaaaaaaa" not in blob
    assert detail["finished"] is True and detail["is_error"] is False


async def test_step_detail_truncates_and_reports_length():
    big = "x" * (MAX_DETAIL_CHARS + 500)
    events = [ev("agent.message", 2, "m", content=text(big))]
    detail = await get_step_detail(FakeArk({"s": events}), "s", "m")
    assert "已截断" in detail["text"] and str(len(big)) in detail["text"]


async def test_step_detail_unknown_event_is_none():
    assert await get_step_detail(FakeArk({"s": []}), "s", "nope") is None


# ---- 网关入口 --------------------------------------------------------------
class _Msg:
    def __init__(self, **kw):
        self.text, self.chat_type, self.mentioned_bot = "为什么失败", "group", True
        self.event_id, self.chat_id, self.thread_id = "e1", "oc_A", ""
        self.user_open_id, self.tenant_key, self.user_id = "ou_1", "t", "u1"
        self.__dict__.update(kw)


class _Loop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, fn):
        self.calls.append(fn)


class _Store:
    def __init__(self):
        self.claimed = set()

    def claim_event(self, eid):
        if eid in self.claimed:
            return False
        self.claimed.add(eid)
        return True


def make_gateway():
    cfg = type("C", (), {"authorized_open_ids": (), "feishu_app_id": "cli_x"})()
    loop = _Loop()
    gw = DiagnoseGateway(cfg, _Store(), None, None, loop, None)
    return gw, loop


def test_group_without_mention_is_ignored():
    gw, loop = make_gateway()
    assert gw.accept(_Msg(mentioned_bot=False)) is False and not loop.calls


def test_private_chat_is_accepted_without_mention():
    gw, loop = make_gateway()
    assert gw.accept(_Msg(chat_type="p2p", mentioned_bot=False, user_id="u1")) is True
    assert len(loop.calls) == 1


def test_duplicate_event_is_dropped():
    gw, loop = make_gateway()
    assert gw.accept(_Msg()) is True
    assert gw.accept(_Msg()) is False


# ---- 私聊:只能看自己名下、发生在私聊里的任务(按租户级 user_id) -------------------
def make_topicx(tmp_path, rows, doc_lookup=None):
    ctl_db, job_db = tmp_path / "c.db", tmp_path / "t6.db"
    make_db(str(job_db))
    ctl = sqlite3.connect(ctl_db)
    ctl.executescript(
        """
        CREATE TABLE task_runs (task_id TEXT PRIMARY KEY, chat_id TEXT, user_id TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '', router_session_id TEXT, business_session_id TEXT,
            scenario TEXT, mode TEXT, route_reason TEXT, business_job_id TEXT, status TEXT, last_error TEXT,
            created_at INTEGER, finished_at INTEGER);
        CREATE TABLE control_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, task_id TEXT, created_at INTEGER);
        """
    )
    for r in rows:
        ctl.execute("INSERT INTO task_runs (task_id, chat_id, user_id, chat_type, router_session_id, business_session_id,"
                    " scenario, mode, route_reason, business_job_id, status, last_error, created_at)"
                    " VALUES (?,?,?,?,?,?, 'topic6','demo','r','',?, '', 1000)", r)
    ctl.commit()
    ctl.close()
    ark = FakeArk({"sesn-r": [], "sesn-b": FAILED_EVENTS})
    return DiagnoseTools(build_sources({"topicx": str(ctl_db), "topic6": str(job_db)}), ark, doc_lookup=doc_lookup)


def P(user_id, chat_id="oc_p_alice"):
    return Scope(chat_id=chat_id, chat_type="p2p", user_id=user_id)


async def test_private_chat_sees_only_own_private_tasks(tmp_path):
    tools = make_topicx(tmp_path, [
        ("task_alice_dm", "oc_p_alice", "u_alice", "p2p", "sesn-r", "sesn-b", "failed"),
        ("task_bob_dm", "oc_p_bob", "u_bob", "p2p", "sesn-r", "sesn-b", "failed"),
        ("task_alice_grp", "oc_group", "u_alice", "group", "sesn-r", "sesn-b", "failed"),
    ])
    listed, _ = await tools.call(P("u_alice"), "list_recent_tasks", {})
    # 只有 alice 本人、且发生在私聊里的那条;她在群里发起的不算"私聊名下"
    assert [t["id"] for t in listed["tasks"]] == ["task_alice_dm"]
    other, _ = await tools.call(P("u_alice"), "diagnose_task", {"task_id": "task_bob_dm"})
    assert other["found"] is False
    grp, _ = await tools.call(P("u_alice"), "diagnose_task", {"task_id": "task_alice_grp"})
    assert grp["found"] is False


async def test_private_chat_without_user_id_cannot_query_anything(tmp_path):
    tools = make_topicx(tmp_path, [("task_x", "oc_p_x", "", "p2p", "sesn-r", "sesn-b", "failed")])
    for uid in ("", "u_alice"):
        result, err = await tools.call(P(uid, "oc_p_x"), "diagnose_task", {"task_id": "task_x"})
        assert not err and result["found"] is False
    msg, _ = await tools.call(P(""), "list_recent_tasks", {})
    assert "user ID" in msg["message"]


async def test_group_scope_ignores_who_asked(tmp_path):
    tools = make_topicx(tmp_path, [
        ("task_1", "oc_group", "u_alice", "group", "sesn-r", "sesn-b", "failed"),
        ("task_2", "oc_group", "u_bob", "group", "sesn-r", "sesn-b", "failed"),
    ])
    listed, _ = await tools.call(G("oc_group"), "list_recent_tasks", {})
    assert {t["id"] for t in listed["tasks"]} == {"task_1", "task_2"}  # 群内共享,不区分发起人


async def test_group_cannot_see_private_task_even_in_same_chat_id_shape(tmp_path):
    tools = make_topicx(tmp_path, [("task_dm", "oc_p_alice", "u_alice", "p2p", "sesn-r", "sesn-b", "failed")])
    result, _ = await tools.call(G("oc_group"), "diagnose_task", {"task_id": "task_dm"})
    assert result["found"] is False


# ---- 通用 Gateway(分析师 / ada-pm)与数字员工的 adapter -----------------------
from arkagent.observability.adapters.sqlite_sources import ConversationSource, DigitalEmployeeSource  # noqa: E402


def make_conversations_db(path, *, with_new_cols=True):
    conn = sqlite3.connect(path)
    extra = ", user_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', chat_type TEXT NOT NULL DEFAULT ''" if with_new_cols else ""
    conn.executescript(
        f"""
        CREATE TABLE conversations (conversation_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            agent_version TEXT, updated_at TEXT NOT NULL{extra});
        CREATE TABLE session_history (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_key TEXT, chat_id TEXT,
            thread_id TEXT, user_open_id TEXT, user_id TEXT, session_id TEXT, agent_id TEXT, created_at TEXT,
            archived_at TEXT, reason TEXT, chat_type TEXT DEFAULT '');
        """
    )
    return conn


def seed_conversations(path):
    conn = make_conversations_db(path)
    conn.executescript(
        """
        INSERT INTO conversations VALUES ('oc_AAAA:-:ou_alice','sesn-alice-grp','ag',NULL,'2026-11-11T10:00:00','u_alice','2026-11-11T09:00:00','group');
        INSERT INTO conversations VALUES ('oc_p_alice:-:ou_alice','sesn-alice-dm','ag',NULL,'2026-11-11T11:00:00','u_alice','2026-11-11T09:30:00','p2p');
        INSERT INTO conversations VALUES ('oc_p_bob:-:ou_bob','sesn-bob-dm','ag',NULL,'2026-11-11T12:00:00','u_bob','2026-11-11T09:40:00','p2p');
        INSERT INTO conversations VALUES ('ocXAAAA:-:ou_eve','sesn-other-grp','ag',NULL,'2026-11-11T12:30:00','u_eve','2026-11-11T09:50:00','group');
        INSERT INTO session_history (conversation_key, chat_id, thread_id, user_open_id, user_id, session_id, agent_id,
            created_at, archived_at, reason, chat_type)
          VALUES ('oc_p_alice:-:ou_alice','oc_p_alice','','ou_alice','u_alice','sesn-alice-dm-OLD','ag',
            '2026-11-10T08:00:00','2026-11-11T08:00:00','reset','p2p');
        """
    )
    conn.commit()
    conn.close()


def make_conv_tools(tmp_path, events=None):
    db = tmp_path / "conv.db"
    seed_conversations(str(db))
    ark = FakeArk({sid: (events or FAILED_EVENTS) for sid in
                   ("sesn-alice-grp", "sesn-alice-dm", "sesn-bob-dm", "sesn-other-grp", "sesn-alice-dm-OLD")})
    return DiagnoseTools({"analyst": ConversationSource("analyst", str(db))}, ark)


async def test_conversation_group_scope_is_exact_prefix_not_like(tmp_path):
    tools = make_conv_tools(tmp_path)
    listed, _ = await tools.call(G("oc_AAAA"), "list_recent_tasks", {})
    # ocXAAAA 只差一个字符(_ 在 LIKE 里是通配符),绝不能被匹配进来
    assert [t["id"] for t in listed["tasks"]] == ["sesn-alice-grp"]
    other, _ = await tools.call(G("oc_AAAA"), "diagnose_task", {"task_id": "sesn-other-grp"})
    assert other["found"] is False


async def test_conversation_private_only_own_dm_including_archived(tmp_path):
    tools = make_conv_tools(tmp_path)
    listed, _ = await tools.call(P("u_alice"), "list_recent_tasks", {})
    # /new 之前那个会话(已归档)也能找回——这正是归档的目的;她的群聊会话和 bob 的私聊都不算
    assert {t["id"] for t in listed["tasks"]} == {"sesn-alice-dm", "sesn-alice-dm-OLD"}
    assert {t["status"] for t in listed["tasks"]} == {"active", "archived"}
    for sid in ("sesn-bob-dm", "sesn-alice-grp"):
        result, _ = await tools.call(P("u_alice"), "diagnose_task", {"task_id": sid})
        assert result["found"] is False


async def test_archived_session_is_diagnosable_and_marked(tmp_path):
    tools = make_conv_tools(tmp_path)
    result, _ = await tools.call(P("u_alice"), "diagnose_task", {"task_id": "sesn-alice-dm-OLD"})
    assert result["found"] and result["task"]["status"] == "archived"
    assert any(f["code"] == "model_retries_exhausted" for f in result["findings"])


async def test_conversation_legacy_rows_without_user_id_are_not_private_visible(tmp_path):
    db = tmp_path / "old.db"
    conn = make_conversations_db(str(db), with_new_cols=False)
    conn.execute("INSERT INTO conversations VALUES ('oc_p_alice:-:ou_alice','sesn-legacy','ag',NULL,'2026-11-11T11:00:00')")
    conn.commit()
    conn.close()
    tools = DiagnoseTools({"analyst": ConversationSource("analyst", str(db))}, FakeArk({"sesn-legacy": FAILED_EVENTS}))
    listed, _ = await tools.call(P("u_alice"), "list_recent_tasks", {})
    assert listed["tasks"] == []                      # 没有 user_id 的老数据:宁可查不到
    # 老数据没有 chat_type:按决策,群范围凭 chat_id 精确匹配放行(假设私聊 chat_id 不外泄)。
    grp, _ = await tools.call(G("oc_p_alice"), "diagnose_task", {"task_id": "sesn-legacy"})
    assert grp["found"] is True
    other, _ = await tools.call(G("oc_p_bob"), "diagnose_task", {"task_id": "sesn-legacy"})
    assert other["found"] is False                    # chat_id 不等仍然不行


def make_de_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (key TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE session_history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, chat_id TEXT, thread_id TEXT,
            session_id TEXT, created_at INTEGER, archived_at INTEGER, reason TEXT);
        CREATE TABLE session_memory_scopes (session_id TEXT PRIMARY KEY, tenant_key TEXT, scope_type TEXT,
            scope_id TEXT, store_id TEXT, created_at INTEGER);
        INSERT INTO sessions VALUES ('oc_G1:omt_topic1','sesn-de-grp',1000),('oc_DM1:-','sesn-de-dm',2000),
                                    ('oc_DM2:-','sesn-de-dm-bob',3000),('oc_DM3:-','sesn-de-dm-nomem',4000);
        INSERT INTO session_memory_scopes VALUES ('sesn-de-grp','t','group','oc_G1','s',1),
                                                 ('sesn-de-dm','t','user','u_alice','s',1),
                                                 ('sesn-de-dm-bob','t','user','u_bob','s',1);
        INSERT INTO session_history (key, chat_id, thread_id, session_id, created_at, archived_at, reason)
          VALUES ('oc_DM1:-','oc_DM1','','sesn-de-dm-OLD',500,1500,'reset');
        INSERT INTO session_memory_scopes VALUES ('sesn-de-dm-OLD','t','user','u_alice','s',1);
        """
    )
    conn.commit()
    conn.close()


def make_de_tools(tmp_path):
    db = tmp_path / "de.db"
    make_de_db(str(db))
    ark = FakeArk({s: FAILED_EVENTS for s in ("sesn-de-grp", "sesn-de-dm", "sesn-de-dm-bob", "sesn-de-dm-nomem", "sesn-de-dm-OLD")})
    return DiagnoseTools({"digital-employee": DigitalEmployeeSource("digital-employee", str(db))}, ark)


async def test_digital_employee_group_is_shared_by_chat(tmp_path):
    tools = make_de_tools(tmp_path)
    listed, _ = await tools.call(G("oc_G1"), "list_recent_tasks", {})
    assert [t["id"] for t in listed["tasks"]] == ["sesn-de-grp"]


async def test_digital_employee_private_uses_memory_scope_owner(tmp_path):
    tools = make_de_tools(tmp_path)
    listed, _ = await tools.call(P("u_alice", "oc_DM1"), "list_recent_tasks", {})
    assert {t["id"] for t in listed["tasks"]} == {"sesn-de-dm", "sesn-de-dm-OLD"}   # 含 /new 归档的旧会话
    bob, _ = await tools.call(P("u_alice"), "diagnose_task", {"task_id": "sesn-de-dm-bob"})
    assert bob["found"] is False


async def test_digital_employee_private_without_owner_record_is_not_visible(tmp_path):
    tools = make_de_tools(tmp_path)
    result, _ = await tools.call(P("u_alice"), "diagnose_task", {"task_id": "sesn-de-dm-nomem"})
    assert result["found"] is False        # 私聊范围:反推不出归属,不放行


def test_repeated_warnings_are_listed_individually_not_merged():
    # 长会话里每一轮都出现"重试后成功":按原始场景逐条列出,每条都带自己的事件 id
    events = []
    for turn in range(1, 6):
        base = turn * 10
        events += [
            ev("user.message", base, f"u{turn}", content=text("x")),
            ev("span.model_request_start", base + 1, f"ms{turn}"),
            ev("span.model_request_end", base + 1, f"me{turn}", model_request_start_id=f"ms{turn}", is_error=True, model_usage={}),
            ev("session.status_idle", base + 2, stop_reason={"type": "end_turn"}),
        ]
    findings = diagnose(trace_of(events, "done"))
    assert [f.code for f in findings] == ["model_retried"] * 5
    assert [f.span_id for f in findings] == [f"me{t}" for t in range(1, 6)]   # 每条可单独追问
    assert all(f.summary.startswith(f"第 {t} 轮") for t, f in enumerate(findings, 1))   # 各自带自己的轮次


def test_errors_are_each_kept():
    events = FAILED_EVENTS + [  # 同一会话里的真正异常必须单独保留
        ev("user.message", 20, "u2", content=text("again")),
        ev("session.error", 21, error={"type": "model_request_failed_error", "message": "x", "retry_status": {"type": "exhausted"}}),
    ]
    codes = [f.code for f in diagnose(trace_of(events, "failed"))]
    assert codes.count("model_retries_exhausted") == 2


# ---- 配置:两个 bot 不能指向同一个库 ------------------------------------------
def _cfg_env(tmp_path, **paths):
    env = {"ARK_API_KEY": "k", "FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s",
           "DIAGNOSE_AGENT_ID": "ag", "DIAGNOSE_ENVIRONMENT_ID": "en"}
    env.update(paths)
    return env


def test_config_rejects_two_bots_pointing_at_the_same_db(tmp_path):
    from diagnose.config import load_case_config

    shared = tmp_path / "gateway.db"
    shared.write_text("")
    link = tmp_path / "link.db"
    link.symlink_to(shared)
    for second in (shared, link):   # 同一路径、软链接,都要识别
        env = _cfg_env(tmp_path, ANALYST_GATEWAY_DB_PATH=str(shared), ADA_PM_GATEWAY_DB_PATH=str(second))
        with pytest.raises(RuntimeError, match="同一个库文件"):
            load_case_config(env)


def test_config_accepts_same_filename_in_different_dirs(tmp_path):
    from diagnose.config import load_case_config

    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "gateway.db").write_text("")
    cfg = load_case_config(_cfg_env(
        tmp_path,
        ANALYST_GATEWAY_DB_PATH=str(tmp_path / "a" / "gateway.db"),
        ADA_PM_GATEWAY_DB_PATH=str(tmp_path / "b" / "gateway.db"),
    ))
    assert sorted(cfg.db_paths) == ["ada-pm", "analyst"]   # 文件名相同但不是同一个库,放行


# ---- 文档同步的接入 --------------------------------------------------------------
def test_doc_sync_is_off_unless_a_parent_node_is_configured(tmp_path):
    from diagnose.config import load_case_config

    db = tmp_path / "t.db"
    db.write_text("")
    base = _cfg_env(tmp_path, TOPIC6_PIPELINE_DB_PATH=str(db))
    assert load_case_config(base).doc_parent_token == ""
    on = load_case_config({**base, "SESSION_DOC_PARENT_TOKEN": " wikcnX ", "SESSION_DOC_INTERVAL_S": "60"})
    assert (on.doc_parent_token, on.doc_sync_interval_s, on.doc_lookback_days, on.doc_max_writes_per_cycle, on.doc_check_concurrency, on.doc_cycle_budget_s) == ("wikcnX", 60, 7, 10, 4, 90)


@pytest.mark.parametrize("raw", ["0", "-5", "abc"])
def test_doc_sync_rejects_nonsense_intervals(tmp_path, raw):
    from diagnose.config import load_case_config

    db = tmp_path / "t.db"
    db.write_text("")
    with pytest.raises(RuntimeError, match="SESSION_DOC_INTERVAL_S"):
        load_case_config({**_cfg_env(tmp_path, TOPIC6_PIPELINE_DB_PATH=str(db)), "SESSION_DOC_INTERVAL_S": raw})


def test_start_background_is_a_noop_without_scheduler():
    gw, loop = make_gateway()
    gw.start_background()          # 未启用:不报错、不调度


def test_start_background_schedules_the_loop_when_enabled():
    import asyncio

    class Sched:
        async def run_once(self):
            return {"written": 0, "failed": 0}

    loop = asyncio.new_event_loop()
    cfg = type("C", (), {"authorized_open_ids": (), "feishu_app_id": "x", "doc_sync_interval_s": 1, "doc_lookback_days": 7})()
    gw = DiagnoseGateway(cfg, _Store(), None, None, loop, None, doc_scheduler=Sched())
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        gw.start_background()
        import time
        time.sleep(0.3)
        assert [task for task in asyncio.all_tasks(loop)]        # 后台循环确实在事件循环上跑着
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(1)


# ---- 诊断结论附加飞书文档链接 -----------------------------------------------------
async def test_diagnose_attaches_doc_url_for_sessions_with_a_doc(tmp_path):
    lookup = {"sesn-b": "https://doc/abc"}
    tools = make_topicx(tmp_path, [("task_1", "oc_g", "u1", "group", "sesn-r", "sesn-b", "failed")], doc_lookup=lookup.get)
    result, err = await tools.call(G("oc_g"), "diagnose_task", {"task_id": "task_1"})
    assert not err and result["found"]
    by_sid = {s["session_id"]: s.get("doc_url") for s in result["sessions"]}
    assert by_sid["sesn-b"] == "https://doc/abc"          # 有文档的 session 带链接
    assert by_sid["sesn-r"] is None                        # 无文档的 session 不带


async def test_diagnose_doc_url_is_none_without_lookup(tmp_path):
    tools = make_topicx(tmp_path, [("task_1", "oc_g", "u1", "group", "sesn-r", "sesn-b", "failed")])
    result, _ = await tools.call(G("oc_g"), "diagnose_task", {"task_id": "task_1"})
    assert all(s.get("doc_url") is None for s in result["sessions"])


async def test_diagnose_doc_url_is_none_when_lookup_returns_none(tmp_path):
    tools = make_topicx(tmp_path, [("task_1", "oc_g", "u1", "group", "sesn-r", "sesn-b", "failed")], doc_lookup=lambda sid: None)
    result, _ = await tools.call(G("oc_g"), "diagnose_task", {"task_id": "task_1"})
    assert all(s.get("doc_url") is None for s in result["sessions"])


async def test_doc_lookup_failure_is_swallowed_and_diagnosis_still_works(tmp_path):
    def broken(sid):
        raise RuntimeError("查文档链接失败")

    tools = make_topicx(tmp_path, [("task_1", "oc_g", "u1", "group", "sesn-r", "sesn-b", "failed")], doc_lookup=broken)
    result, err = await tools.call(G("oc_g"), "diagnose_task", {"task_id": "task_1"})
    assert not err and result["found"]                     # 诊断本身不受影响
    assert all(s["doc_url"] is None for s in result["sessions"])


# ---- 收到消息贴「稍等」表情,处理完撤回 ---------------------------------------------
class _FakeSender:
    def __init__(self, fail_react=False):
        self.reacted, self.deleted, self.fail_react = [], [], fail_react

    def react(self, message_id, emoji_type):
        if self.fail_react:
            raise RuntimeError("表情接口挂了")
        self.reacted.append((message_id, emoji_type))
        return "rid-1"

    def delete_reaction(self, message_id, reaction_id):
        self.deleted.append((message_id, reaction_id))


def _ack_gateway(sender):
    loop = asyncio.new_event_loop()
    cfg = type("C", (), {"authorized_open_ids": (), "feishu_app_id": "cli_x"})()
    return DiagnoseGateway(cfg, None, None, None, loop, None, sender), loop


def test_ack_then_unack_uses_one_second_emoji():
    sender = _FakeSender()
    gw, loop = _ack_gateway(sender)
    try:
        async def go():
            msg = _Msg(message_id="m1")
            rid = await gw._ack(msg)
            await gw._unack(msg, rid)

        loop.run_until_complete(go())
        assert sender.reacted == [("m1", "OneSecond")]
        assert sender.deleted == [("m1", "rid-1")]
    finally:
        loop.close()


def test_no_sender_or_no_message_id_means_no_reaction():
    gw, loop = _ack_gateway(None)                      # 没有 sender
    try:
        async def go():
            assert await gw._ack(_Msg(message_id="m1")) is None
            await gw._unack(_Msg(message_id="m1"), "rid-x")   # 无 sender,不该崩

        loop.run_until_complete(go())
    finally:
        loop.close()


def test_react_failure_is_degraded_to_no_reaction():
    sender = _FakeSender(fail_react=True)
    gw, loop = _ack_gateway(sender)
    try:
        async def go():
            assert await gw._ack(_Msg(message_id="m1")) is None   # 贴失败降级,返回 None

        loop.run_until_complete(go())
        assert sender.reacted == []
    finally:
        loop.close()
