"""排障 Bot MVP:权限边界、确定性诊断、步骤详情脱敏、网关入口。

权限是这个 bot 风险最高的部分,所以测试以\"越权拿不到\"为主。事件夹具按真实结构构造。
"""
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
from diagnose.gateway import DiagnoseGateway, PRIVATE_CHAT_REPLY, build_sources
from diagnose.tools import DiagnoseTools


def ev(etype, sec, eid=None, **fields):
    return {"id": eid or f"{etype}-{sec}", "type": etype,
            "processed_at": f"2026-10-16T14:38:{sec:02d}.508799758+08:00", **fields}


def text(body):
    return [{"type": "text", "text": body}]


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
    db = tmp_path / "t6.db"
    make_db(str(db))
    ark = FakeArk({"sesn-mine": FAILED_EVENTS, "sesn-other": FAILED_EVENTS})
    return DiagnoseTools(build_sources({"topic6": str(db)}), ark)


# ---- 权限:只能看当前群 ---------------------------------------------------
async def test_list_only_returns_current_chat(tools):
    result, err = await tools.call("oc_A", "list_recent_tasks", {})
    assert not err
    assert [t["id"] for t in result["tasks"]] == ["job_mine"]
    assert "secret failure" not in json.dumps(result, ensure_ascii=False)


async def test_cannot_diagnose_task_from_another_chat(tools):
    result, err = await tools.call("oc_A", "diagnose_task", {"task_id": "job_other"})
    assert not err and result["found"] is False
    # 通过 session id 也不行
    result, _ = await tools.call("oc_A", "diagnose_task", {"task_id": "sesn-other"})
    assert result["found"] is False
    assert "group B" not in json.dumps(result, ensure_ascii=False)


async def test_not_found_message_does_not_reveal_existence(tools):
    other, _ = await tools.call("oc_A", "diagnose_task", {"task_id": "job_other"})
    missing, _ = await tools.call("oc_A", "diagnose_task", {"task_id": "job_nope"})
    assert other == missing


@pytest.mark.parametrize("field", ["chat_id", "open_id", "user_id", "tenant_key"])
async def test_identity_args_are_rejected(tools, field):
    result, err = await tools.call("oc_A", "list_recent_tasks", {field: "oc_B"})
    assert err and "tasks" not in result


async def test_step_detail_session_must_belong_to_task_in_chat(tools):
    # 用本群的 task_id 去取别的群的 session,不能绕过范围
    result, _ = await tools.call(
        "oc_A", "get_step_detail",
        {"task_id": "job_mine", "session_id": "sesn-other", "event_id": "me"},
    )
    assert result["found"] is False
    result, _ = await tools.call(
        "oc_A", "get_step_detail",
        {"task_id": "job_other", "session_id": "sesn-other", "event_id": "me"},
    )
    assert result["found"] is False


async def test_unknown_tool_is_error(tools):
    _, err = await tools.call("oc_A", "delete_everything", {})
    assert err


async def test_default_pick_is_flagged_so_agent_must_confirm(tools):
    result, _ = await tools.call("oc_A", "diagnose_task", {})
    assert result["found"] and result["picked_by_default"] is True
    assert result["task"]["id"] == "job_mine"
    explicit, _ = await tools.call("oc_A", "diagnose_task", {"task_id": "job_mine"})
    assert explicit["picked_by_default"] is False


async def test_empty_chat_has_nothing(tools):
    result, _ = await tools.call("oc_C", "diagnose_task", {})
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
    result, _ = await tools.call("oc_A", "list_recent_tasks", {})
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
        self.user_open_id, self.tenant_key = "ou_1", "t"
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
    cfg = type("C", (), {"authorized_open_ids": ()})()
    loop = _Loop()
    gw = DiagnoseGateway(cfg, _Store(), None, None, loop, None)
    return gw, loop


def test_group_without_mention_is_ignored():
    gw, loop = make_gateway()
    assert gw.accept(_Msg(mentioned_bot=False)) is False and not loop.calls


def test_private_chat_gets_guidance_and_nothing_else():
    gw, loop = make_gateway()
    assert gw.accept(_Msg(chat_type="p2p")) is True and len(loop.calls) == 1
    assert "群聊" in PRIVATE_CHAT_REPLY


def test_duplicate_event_is_dropped():
    gw, loop = make_gateway()
    assert gw.accept(_Msg()) is True
    assert gw.accept(_Msg()) is False
