"""session 日志落飞书文档:渲染、幂等、失败不外溢、钩子不影响回复。lark-cli 用假的 api 代替。"""
import asyncio
import json

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


def test_render_shows_body_redacts_secrets_and_skips_noise():
    body, last_id, _ = render_events(EVENTS)
    assert "帮我做周报" in body and "双十一" in body and "好的,已完成" in body
    assert "abcdef123456" not in body and "sk-aaaaaaaaaaaaaaaaaaaa" not in body
    assert "内心独白" not in body            # thinking 不进文档
    assert last_id == "m1"
    assert "event_id: tu1" in body           # 保留事件 id,便于和排障 Bot 的结论对上


def test_render_fence_survives_backticks_in_body():
    body, _, _ = render_events([ev("agent.tool_result", 2, "tr", tool_use_id="t", is_error=False,
                                   content=text("```python\nprint(1)\n```"))])
    assert "````" in body


def test_render_truncates_huge_body():
    body, _, _ = render_events([ev("agent.message", 2, "m", content=text("x" * 9000))])
    assert "已截断" in body and "9000" in body and len(body) < 4000


async def test_first_publish_creates_doc_under_parent_then_appends_only_new(tmp_path):
    api = FakeApi()
    sink = make_sink(tmp_path, api)
    url = await sink.publish("sesn-1", EVENTS[:3], title="t")
    assert url == "https://x/docx/doxcnX"
    method, path, body = api.calls[0]
    assert (method, path) == ("POST", "/open-apis/docs_ai/v1/documents")
    assert body["parent_token"] == "fldcnPARENT" and body["format"] == "markdown"
    assert body["content"].startswith("<title>t</title>") and "帮我做周报" in body["content"]

    await sink.publish("sesn-1", EVENTS, title="t")  # 第二轮:只追加新增部分
    method, path, body = api.calls[1]
    assert (method, path) == ("PUT", "/open-apis/docs_ai/v1/documents/doxcnX")
    assert body["command"] == "block_insert_after" and body["block_id"] == "-1"   # 文末追加
    assert "好的,已完成" in body["content"] and "帮我做周报" not in body["content"]


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
    assert "好的,已完成" in api.calls[1][2]["content"] and "帮我做周报" not in api.calls[1][2]["content"]


async def test_nothing_new_means_no_call(tmp_path):
    api = FakeApi()
    sink = make_sink(tmp_path, api)
    assert await sink.publish("sesn-1", [ev("agent.thinking", 1, content=text("x"))], title="t") is None
    assert api.calls == []


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
