"""ada 可观测 P0:事件解析、脱敏、工具归类、adapter 与 service。

事件夹具按真实 session 的结构(字段名 / 类型 / 嵌套)构造,不含任何真实内容。
"""
import sqlite3

import pytest

from arkagent.observability.adapters.sqlite_sources import PipelineJobSource, TopicxSource
from arkagent.observability.events import parse_session_events, ts_ms
from arkagent.observability.model import (
    KIND_ERROR,
    KIND_HC,
    KIND_MODEL,
    KIND_PHASE,
    KIND_TOOL,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PENDING,
)
from arkagent.observability.redact import REDACTED, redact_text, redact_value
from arkagent.observability.service import build_trace
from arkagent.observability.tool_category import categorize_tool


def ev(etype, sec, eid=None, **fields):
    # 纳秒精度的时间戳,与方舟真实返回一致。
    return {
        "id": eid or f"{etype}-{sec}",
        "type": etype,
        "processed_at": f"2026-10-16T14:38:{sec:02d}.508799758+08:00",
        **fields,
    }


def text(body):
    return [{"type": "text", "text": body}]


def test_ts_ms_accepts_nanosecond_fraction():
    assert ts_ms("2026-10-16T14:38:39.508799758+08:00") - ts_ms("2026-10-16T14:38:38.508799758+08:00") == 1000


def test_model_and_tool_pairing_with_durations():
    events = [
        ev("user.message", 1, content=text("hello")),
        ev("session.thread_status_running", 1, session_thread_id="th1", agent_name="coord"),
        ev("span.model_request_start", 2, "ms1"),
        ev(
            "span.model_request_end", 5, "me1",
            model_request_start_id="ms1", is_error=False,
            model_usage={"input_tokens": 100, "output_tokens": 7, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0},
        ),
        ev("agent.tool_use", 6, "tu1", name="bash", input={"command": "lark-cli docs +fetch --token abc123secret"}, session_thread_id="th1"),
        ev("agent.tool_result", 9, "tr1", tool_use_id="tu1", is_error=False, content=text("ok")),
        ev("session.status_idle", 10, stop_reason={"type": "end_turn"}),
    ]
    node = parse_session_events(events, session_id="s1", role="business")
    assert len(node.turns) == 1
    turn = node.turns[0]
    assert (turn.stop_reason, turn.user_text_chars) == ("end_turn", 5)
    model = next(s for s in turn.spans if s.kind == KIND_MODEL)
    assert (model.duration_ms, model.attrs["input_tokens"], model.attrs["cache_read_input_tokens"]) == (3000, 100, 3)
    assert (model.thread, model.source) == ("coord", "inferred")
    tool = next(s for s in turn.spans if s.kind == KIND_TOOL)
    assert (tool.duration_ms, tool.status, tool.thread) == (3000, STATUS_OK, "coord")
    assert tool.attrs["category"] == "lark-cli docs +fetch"
    assert "abc123secret" not in tool.attrs["command_preview"]


def test_model_thread_left_empty_when_ambiguous():
    events = [
        ev("user.message", 1, content=text("x")),
        ev("session.thread_status_running", 1, session_thread_id="a", agent_name="coord"),
        ev("session.thread_status_running", 1, session_thread_id="b", agent_name="annotator"),
        ev("span.model_request_start", 2, "ms"),
        ev("span.model_request_end", 3, "me", model_request_start_id="ms", is_error=False, model_usage={}),
    ]
    model = next(s for s in parse_session_events(events, session_id="s", role="business").turns[0].spans if s.kind == KIND_MODEL)
    assert model.thread is None


def test_failed_session_matches_real_shape():
    events = [
        ev("user.message", 1, content=text("go")),
        ev("span.model_request_start", 2, "ms"),
        ev("span.model_request_end", 2, "me", model_request_start_id="ms", is_error=True,
           model_usage={"input_tokens": 0, "output_tokens": 0}),
        ev("session.error", 3, error={"type": "model_request_failed_error", "message": "provider issue", "retry_status": {"type": "exhausted"}}),
        ev("session.status_idle", 3, stop_reason={"type": "retries_exhausted"}),
    ]
    turn = parse_session_events(events, session_id="s", role="business").turns[0]
    assert turn.stop_reason == "retries_exhausted"
    assert next(s for s in turn.spans if s.kind == KIND_MODEL).status == STATUS_ERROR
    error = next(s for s in turn.spans if s.kind == KIND_ERROR)
    assert (error.name, error.attrs["retry_status"]) == ("model_request_failed_error", "exhausted")


def test_unpaired_calls_stay_pending_and_phase_dedupes():
    events = [
        ev("user.message", 1, content=text("x")),
        ev("agent.message", 2, content=text("[phase] A 开始")),
        ev("agent.message", 3, content=text("[phase] A 继续")),
        ev("agent.message", 4, content=text("[phase] C1")),
        ev("agent.tool_use", 5, "tu", name="bash_wait", input={}),
        ev("span.model_request_start", 6, "ms"),
    ]
    spans = parse_session_events(events, session_id="s", role="business").turns[0].spans
    assert [s.name for s in spans if s.kind == KIND_PHASE] == ["A", "C1"]
    assert {s.kind for s in spans if s.status == STATUS_PENDING} == {KIND_TOOL, KIND_MODEL}


def test_events_before_first_user_message_get_implicit_turn():
    node = parse_session_events([ev("session.error", 2, error={"type": "x"})], session_id="s", role="router")
    assert len(node.turns) == 1 and node.turns[0].user_event_id is None


def test_redaction():
    assert "secretvalue" not in redact_text("curl -H 'Authorization: Bearer secretvalue123' x")
    assert "tok999" not in redact_text("lark-cli --token tok999 docs")
    assert "pw1" not in redact_text("DB_PASSWORD=pw1 run")
    assert redact_text("ls -la /workspace") == "ls -la /workspace"
    assert redact_value({"api_key": "k", "nested": [{"x": "--token abc12345"}]}) == {
        "api_key": REDACTED,
        "nested": [{"x": f"--token {REDACTED}"}],
    }


@pytest.mark.parametrize(
    "name,tool_input,expected",
    [
        ("bash", {"command": "cd /w && python3 /mnt/s/run.py --x 1"}, "python:run.py"),
        ("bash", {"command": "python3 -c \"print(1)\""}, "python:-c"),
        ("bash", {"command": "python3 <<'EOF'\nprint(1)\nEOF"}, "python:inline"),
        ("bash", {"command": "for f in a b; do ls $f; done"}, "ls"),
        ("bash", {"command": "FOO=1 nohup lark-cli drive +upload f"}, "lark-cli drive +upload"),
        ("bash", {"command": "curl -s https://open.feishu.cn/x"}, "curl:open.feishu.cn"),
        ("bash", {"command": "timeout 30 ls -la"}, "ls"),
        ("bash", {}, "bash"),
        ("memory_read", {"path": "x"}, "memory_read"),
    ],
)
def test_tool_category(name, tool_input, expected):
    assert categorize_tool(name, tool_input) == expected


def _make_job_db(path, with_task_id):
    conn = sqlite3.connect(path)
    task_col = ", task_id TEXT NOT NULL DEFAULT ''" if with_task_id else ""
    conn.executescript(
        f"""
        CREATE TABLE pipeline_jobs (job_id TEXT PRIMARY KEY, ma_session_id TEXT, mode TEXT,
            current_phase TEXT, status TEXT, started_at INTEGER, finished_at INTEGER,
            last_error TEXT DEFAULT ''{task_col});
        CREATE TABLE pipeline_hc_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
            hc_kind TEXT, user_decision TEXT DEFAULT '', created_at INTEGER, resolved_at INTEGER);
        INSERT INTO pipeline_jobs (job_id, ma_session_id, mode, current_phase, status, started_at)
            VALUES ('job_1', 'sesn-1', 'demo', 'HC1', 'wait_hc', 1000);
        INSERT INTO pipeline_hc_events (job_id, hc_kind, user_decision, created_at, resolved_at)
            VALUES ('job_1', 'HC1', 'pass', 1100, 1160), ('job_1', 'HC1', '', 1200, NULL);
        """
    )
    conn.commit()
    conn.close()


def test_job_source_falls_back_to_job_id_when_no_task_id_column(tmp_path):
    db = tmp_path / "p.db"
    _make_job_db(str(db), with_task_id=False)
    source = PipelineJobSource("topic6", str(db))
    for ident in ("job_1", "sesn-1"):
        ref = source.resolve(ident)
        assert (ref.trace_id, ref.root_kind) == ("job_1", "job")
    assert source.resolve("nope") is None
    hc = [s for s in ref.db_spans if s.kind == KIND_HC]
    assert [(s.duration_ms, s.status) for s in hc] == [(60000, STATUS_OK), (None, STATUS_PENDING)]


def test_job_source_prefers_task_id_and_is_read_only(tmp_path):
    db = tmp_path / "p.db"
    _make_job_db(str(db), with_task_id=True)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE pipeline_jobs SET task_id = 'task_9'")
    conn.commit()
    conn.close()
    source = PipelineJobSource("topic6", str(db))
    assert source.resolve("task_9").trace_id == "task_9"
    assert source.list_refs(status="wait_hc")[0].root_kind == "task"
    before = db.read_bytes()
    source.resolve("job_1")
    assert db.read_bytes() == before


def test_topicx_source_joins_router_business_and_job_hc(tmp_path):
    job_db = tmp_path / "p.db"
    _make_job_db(str(job_db), with_task_id=False)
    ctl = sqlite3.connect(tmp_path / "c.db")
    ctl.executescript(
        """
        CREATE TABLE task_runs (task_id TEXT PRIMARY KEY, router_session_id TEXT, business_session_id TEXT,
            scenario TEXT, mode TEXT, route_reason TEXT, business_job_id TEXT, status TEXT,
            last_error TEXT, created_at INTEGER, finished_at INTEGER);
        CREATE TABLE control_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, task_id TEXT, created_at INTEGER);
        INSERT INTO task_runs VALUES ('task_1', 'sesn-r', 'sesn-1', 'topic6', 'demo', 'r', 'job_1', 'running', '', 900, NULL);
        INSERT INTO control_events (event_type, task_id, created_at) VALUES ('router.routed', 'task_1', 910);
        """
    )
    ctl.commit()
    ctl.close()
    source = TopicxSource(str(tmp_path / "c.db"), {"topic6": PipelineJobSource("topic6", str(job_db))})
    ref = source.resolve("job_1")
    assert ref.trace_id == "task_1"
    assert [(s.session_id, s.role) for s in ref.sessions] == [("sesn-r", "router"), ("sesn-1", "business")]
    assert {s.kind for s in ref.db_spans} == {"control_event", KIND_HC}


class _FakeArk:
    def __init__(self, by_session):
        self._by_session = by_session

    async def list_session_events(self, session_id, limit=200):
        result = self._by_session[session_id]
        if isinstance(result, Exception):
            raise result
        return result


class _NotFound(Exception):
    status_code = 404


async def test_build_trace_degrades_when_session_purged(tmp_path):
    db = tmp_path / "p.db"
    _make_job_db(str(db), with_task_id=False)
    ref = PipelineJobSource("topic6", str(db)).resolve("job_1")
    trace = await build_trace(ref, _FakeArk({"sesn-1": _NotFound("gone")}))
    node = trace.sessions[0]
    assert not node.available and "清理" in node.unavailable_reason
    assert len([s for s in trace.db_spans if s.kind == KIND_HC]) == 2
    assert trace.to_dict()["summary"]["model_calls"] == 0


async def test_build_trace_without_event_source_and_summary(tmp_path):
    db = tmp_path / "p.db"
    _make_job_db(str(db), with_task_id=False)
    ref = PipelineJobSource("topic6", str(db)).resolve("job_1")
    assert (await build_trace(ref, None)).sessions[0].unavailable_reason.startswith("未拉取")

    events = [
        ev("user.message", 1, content=text("x")),
        ev("span.model_request_start", 2, "ms"),
        ev("span.model_request_end", 4, "me", model_request_start_id="ms", is_error=False,
           model_usage={"input_tokens": 10, "output_tokens": 2}),
    ]
    summary = (await build_trace(ref, _FakeArk({"sesn-1": events}))).summary()
    assert (summary["model_calls"], summary["input_tokens"], summary["output_tokens"]) == (1, 10, 2)
