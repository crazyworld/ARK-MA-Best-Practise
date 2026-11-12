"""session 日志落飞书文档:渲染、幂等、失败不外溢、钩子不影响回复。lark-cli 用假的 api 代替。"""
import asyncio
import json
import re

import pytest

from arkagent.gateway.orchestrator import Gateway
from arkagent.observability.docsink import DocStore, SessionDocSink, render_events
from arkagent.observability.hook import make_after_turn


def ev(etype, sec, eid=None, **fields):
    return {"id": eid or f"{etype}-{sec}", "type": etype,
            "processed_at": f"2026-10-16T14:38:{sec:02d}.508799758+08:00", **fields}


def text(body):
    return [{"type": "text", "text": body}]


EVENTS = [
    ev("user.message", 1, "u1", content=text("帮我做周报")),
    ev("agent.tool_use", 2, "tu1", name="bash", input={"command": "curl -H 'Authorization: Bearer abcdef123456' x"}),
    ev("agent.tool_result", 3, "tr1", tool_use_id="tu1", is_error=False, content=text("活动名称:双十一\napi_key=sk-aaaaaaaaaaaaaaaaaaaa")),
    ev("span.model_request_end", 4, "me1", model_request_start_id="x", is_error=False, model_usage={}),
    ev("agent.thinking", 4, "th1", content=text("内心独白")),
    ev("agent.message", 5, "m1", content=text("好的,已完成")),
]


class FakeApi:
    """记录 (method, path, body)。create 返回文档 id,append 返回 ok。"""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail:
            raise RuntimeError("飞书接口失败 code=99991672")
        if method == "POST":
            return {"data": {"document": {"document_id": "doxcnX", "url": "https://x/docx/doxcnX"}}}
        return {"ok": True}


def make_sink(tmp_path, api):
    return SessionDocSink(DocStore(str(tmp_path / "d.db")), "fldcnPARENT", api)


def _all_ids(xml):
    import html
    return html.unescape(xml)


def test_render_keeps_every_event_including_thinking_and_successful_model_calls():
    body, last_id, _ = render_events(EVENTS)
    text = _all_ids(body)
    for e in EVENTS:                       # 不过滤:thinking、成功的 model_request_end 也在
        assert e["id"] in text, e["type"]
    assert "内心独白" in text and last_id == "m1"
    assert body.count("<pre lang=\"json\"") == len(EVENTS)   # 每个事件恰好一个原始 JSON 块


def test_render_redacts_secrets_but_keeps_everything_else():
    body, _, _ = render_events(EVENTS)
    text = _all_ids(body)
    assert "abcdef123456" not in text and "sk-aaaaaaaaaaaaaaaaaaaa" not in text
    assert "[REDACTED]" in text
    assert "帮我做周报" in text and "双十一" in text and "好的,已完成" in text


def test_render_never_truncates_large_bodies():
    big = "x" * 30000
    body, _, _ = render_events([ev("user.message", 1, "u", content=text("q")), ev("agent.message", 2, "m", content=text(big))])
    assert big in _all_ids(body) and "已截断" not in body


def test_render_groups_into_round_and_span_with_key_info():
    events = [
        ev("user.message", 1, "u1", content=text("问")),
        ev("span.model_request_start", 2, "ms1"),
        ev("span.model_request_end", 4, "me1", model_request_start_id="ms1", is_error=False,
           model_usage={"input_tokens": 100, "output_tokens": 7, "cache_read_input_tokens": 3}),
        ev("agent.tool_use", 5, "tu1", name="bash", input={"command": "ls"}),
        ev("agent.tool_result", 9, "tr1", tool_use_id="tu1", is_error=True, content=text("boom")),
        ev("session.status_idle", 10, "i1", stop_reason={"type": "end_turn"}),
    ]
    body, _, _ = render_events(events)
    plain = re.sub(r"<[^>]+>", "", body)
    assert "第 1 轮对话" in plain and "已完成" in plain and "模型推理 1 次" in plain and "工具调用 1 次" in plain
    assert "2.0 秒" in plain and "in 100" in plain and "out 7" in plain and "cache 3" in plain     # 模型用量不脱敏
    assert "bash" in plain and "4.0 秒" in plain
    assert 'emoji="❌"' in body and 'emoji="✅"' in body        # 工具报错的 Span 标红;本轮最终完成仍是绿


def test_round_count_matches_user_messages_even_with_leading_state_events():
    events = [
        ev("session.status_running", 0, "s0"), ev("session.thread_status_running", 0, "t0"),
        ev("user.message", 1, "u1", content=text("a")), ev("session.status_idle", 2, "i1", stop_reason={"type": "end_turn"}),
        ev("user.message", 3, "u2", content=text("b")), ev("session.usage", 4, "us", usage={}),
    ]
    from arkagent.observability.docrender import analyse
    a = analyse(events)
    assert len(a.rounds) == 2                                 # 开头的状态事件不能自成一轮
    assert [e["id"] for e in a.ordered] == ["s0", "t0", "u1", "i1", "u2", "us"]   # 事件一个不少、顺序不变


def test_unfinished_tool_is_marked_pending_and_still_rendered():
    body, _, _ = render_events([ev("user.message", 1, "u", content=text("q")), ev("agent.tool_use", 2, "tu", name="bash_wait", input={})])
    assert 'emoji="⏳"' in body and "未完成" in body and "tu" in _all_ids(body)


def test_overlapping_tools_are_rendered_side_by_side():
    events = [
        ev("user.message", 1, "u", content=text("q")),
        ev("agent.tool_use", 2, "a", name="t1", input={}), ev("agent.tool_use", 2, "b", name="t2", input={}),
        ev("agent.tool_result", 6, "ra", tool_use_id="a", is_error=False, content=text("x")),
        ev("agent.tool_result", 7, "rb", tool_use_id="b", is_error=False, content=text("y")),
    ]
    body, _, _ = render_events(events)
    assert "<grid>" in body and "并行 2" in body


def test_xml_special_characters_are_escaped():
    body, _, _ = render_events([ev("user.message", 1, "u", content=text("a < b & <script>")), ])
    assert "<script>" not in body and "&lt;script&gt;" in body


async def test_first_publish_creates_doc_under_parent_then_appends_only_new(tmp_path):
    api = FakeApi()
    sink = make_sink(tmp_path, api)
    url = await sink.publish("sesn-1", EVENTS[:3], title="t")
    assert url == "https://x/docx/doxcnX"
    method, path, body = api.calls[0]
    assert (method, path) == ("POST", "/open-apis/docs_ai/v1/documents")
    assert body["parent_token"] == "fldcnPARENT" and body["format"] == "xml"
    assert body["content"].startswith("<title>t</title>") and "帮我做周报" in _all_ids(body["content"])

    await sink.publish("sesn-1", EVENTS, title="t")  # 第二轮:只追加新增部分
    method, path, body = api.calls[1]
    assert (method, path) == ("PUT", "/open-apis/docs_ai/v1/documents/doxcnX")
    assert body["command"] == "block_insert_after" and body["block_id"] == "-1"   # 文末追加
    assert "好的,已完成" in _all_ids(body["content"]) and "帮我做周报" not in _all_ids(body["content"])


async def test_title_is_escaped_so_it_cannot_inject_markup(tmp_path):
    api = FakeApi()
    await make_sink(tmp_path, api).publish("sesn-1", EVENTS[:1], title="a</title><b>x")
    assert "</title><b>" not in api.calls[0][2]["content"].split("\n")[0]


async def test_republishing_same_events_is_idempotent(tmp_path):
    api = FakeApi()
    sink = make_sink(tmp_path, api)
    await sink.publish("sesn-1", EVENTS, title="t")
    await sink.publish("sesn-1", EVENTS, title="t")
    await sink.publish("sesn-1", EVENTS, title="t")
    assert len(api.calls) == 1            # 重放/重启不重复追加


async def test_progress_survives_restart(tmp_path):
    api = FakeApi()
    await make_sink(tmp_path, api).publish("sesn-1", EVENTS, title="t")
    await make_sink(tmp_path, api).publish("sesn-1", EVENTS, title="t")  # 新进程,同一个库
    assert len(api.calls) == 1


async def test_failure_never_raises_and_progress_not_advanced(tmp_path):
    bad = FakeApi(fail=True)
    sink = make_sink(tmp_path, bad)
    assert await sink.publish("sesn-1", EVENTS, title="t") is None   # 不抛
    good = FakeApi()
    sink2 = make_sink(tmp_path, good)
    await sink2.publish("sesn-1", EVENTS, title="t")                 # 失败的那次不算数,可补写
    assert len(good.calls) == 1


async def test_missing_document_id_is_a_failure_not_a_crash(tmp_path):
    class NoId(FakeApi):
        async def __call__(self, method, path, body):
            return {"data": {}}

    assert await make_sink(tmp_path, NoId()).publish("sesn-1", EVENTS, title="t") is None


async def test_event_id_purged_falls_back_to_time(tmp_path):
    api = FakeApi()
    sink = make_sink(tmp_path, api)
    await sink.publish("sesn-1", EVENTS[:3], title="t")
    # 事件流被截断,上次写到的 id 不在了:按时间只追加更晚的
    await sink.publish("sesn-1", [EVENTS[0], EVENTS[5]], title="t")
    appended = _all_ids(api.calls[1][2]["content"])
    assert "好的,已完成" in appended and "帮我做周报" not in appended


async def test_nothing_new_means_no_call(tmp_path):
    api = FakeApi()
    sink = make_sink(tmp_path, api)
    assert await sink.publish("sesn-1", [], title="t") is None          # 没有任何事件:不调用飞书
    await sink.publish("sesn-1", EVENTS, title="t")
    calls = len(api.calls)
    await sink.publish("sesn-1", EVENTS, title="t")                     # 已全部写过:不再调用
    assert len(api.calls) == calls


async def test_thinking_only_batch_is_written_not_filtered(tmp_path):
    api = FakeApi()
    await make_sink(tmp_path, api).publish("sesn-1", [ev("agent.thinking", 1, "th", content=text("独白"))], title="t")
    assert len(api.calls) == 1 and "独白" in _all_ids(api.calls[0][2]["content"])   # 不过滤


# ---- 钩子 ------------------------------------------------------------------
def test_hook_is_off_without_folder_token(tmp_path):
    assert make_after_turn(object(), case_name="c", db_path=str(tmp_path / "d.db"), app_id="a", app_secret="s", env={}) is None


class _Ark:
    def __init__(self):
        self.ran = 0

    async def list_session_events(self, session_id, limit=200):
        return EVENTS

    async def run(self, *a, **k):
        self.ran += 1
        return _result()

    async def create_session(self, *a, **k):
        return "sesn-new"


def _result():
    from types import SimpleNamespace
    return SimpleNamespace(messages=["回复内容"], terminal="idle", error=None)


async def test_gateway_hook_failure_does_not_break_reply(tmp_path):
    from arkagent.feishu import IncomingMessage
    from arkagent.store import GatewayStore

    replies = []

    async def reply(chat_id, text_):
        replies.append(text_)

    async def boom(session_id, open_id):
        raise RuntimeError("文档服务挂了")

    gw = Gateway(GatewayStore(":memory:"), _Ark(), reply, agent_id="a", environment_id="e",
                 vault_id="", timeout_ms=1000, after_turn=boom)
    msg = IncomingMessage(event_id="e1", message_id="m", chat_id="oc_1", thread_id="", chat_type="p2p",
                          user_open_id="ou_1", user_name="张三", tenant_key="t", text="你好", mentioned_bot=False)
    from arkagent.store import ConversationKey
    await gw._process(msg, ConversationKey("t", "oc_1", "", "ou_1"))
    assert replies[-1] == "回复内容"           # 用户照常收到回复
