"""session 日志文档的格式验收:顺序、脱敏口径、Turn 状态、并行批次、命名。

事件夹具取自 2026-11-12 对真实 session 的实测结构(并行批次、失败重试、字段名均为真实形态)。
"""
import html
import json
import re

import pytest

from arkagent.observability.docrender import build_turns, render_events_xml
from arkagent.observability.redact import redact_text, redact_value


def ev(sec, eid, etype, **f):
    return {"id": eid, "type": etype, "processed_at": f"2026-10-16T14:38:{sec:02d}.000000000+08:00", **f}


def text(s):
    return [{"type": "text", "text": s}]


def round_head(xml):
    """第一个 callout 里含「轮对话」的就是轮卡片。返回 (emoji, 正文, 附注)。"""
    for m in re.finditer(r'<callout emoji="([^"]+)"[^>]*>(.*?)</callout>', xml):
        paras = re.findall(r"<p>(.*?)</p>", m.group(2))
        if paras and "轮对话" in paras[0]:
            return m.group(1), plain(paras[0]), plain(paras[1]) if len(paras) > 1 else ""
    raise AssertionError("没有找到轮卡片")


def plain(xml):
    return re.sub(r"<[^>]+>", "", html.unescape(xml))


# ---- 一、脱敏:用量不是机密,凭据仍然是 ------------------------------------------------
USAGE = {"input_tokens": 23967, "output_tokens": 305, "cache_read_input_tokens": 8610,
         "cache_creation_input_tokens": 0, "total_tokens": 24272}


def test_token_usage_numbers_are_not_redacted():
    out = redact_value({"type": "span.model_request_end", "model_usage": USAGE})
    assert out["model_usage"] == USAGE


def test_session_usage_event_is_not_redacted():
    e = {"type": "session.usage", "usage": {**USAGE, "active_seconds": 60.4, "server_tool_use": {"web_fetch_requests": 0}}}
    assert redact_value(e) == e


@pytest.mark.parametrize("key", ["access_token", "refresh_token", "id_token", "token", "Authorization", "api_key",
                                 "apiKey", "client_secret", "password", "credential", "Cookie", "X-Api-Key"])
def test_credential_fields_are_still_redacted(key):
    assert redact_value({key: "abcdef1234567890"}) == {key: "[REDACTED]"}


def test_usage_numbers_inside_text_are_kept_but_credentials_in_text_are_not():
    assert redact_text("input_tokens: 23967, output_tokens=305") == "input_tokens: 23967, output_tokens=305"
    assert "abcdef1234567890" not in redact_text("lark-cli --token abcdef1234567890 docs")
    assert "abcdef1234567890" not in redact_text("export ACCESS_TOKEN=abcdef1234567890")
    assert "abcdef1234567890" not in redact_text("curl -H 'Authorization: Bearer abcdef1234567890'")
    assert "sk-aaaaaaaaaaaaaaaaaaaa" not in redact_text("key sk-aaaaaaaaaaaaaaaaaaaa")


def test_rendered_document_shows_real_token_numbers():
    events = [ev(1, "u", "user.message", content=text("q")),
              ev(2, "ms", "span.model_request_start"),
              ev(3, "me", "span.model_request_end", model_request_start_id="ms", is_error=False, model_usage=USAGE)]
    xml = render_events_xml(events)[0]
    p = plain(xml)
    assert "in 23967" in p and "out 305" in p and "cache 8610" in p        # 卡片关键信息
    assert '"input_tokens": 23967' in p and "[REDACTED]" not in p          # 原始 JSON 里也是真实数字


# ---- 二、Turn:叫「第 N 轮对话」,状态只看最终结果 ------------------------------------
def _turn(spans, idle):
    return [ev(1, "u", "user.message", content=text("q")), *spans, ev(20, "i", "session.status_idle", stop_reason={"type": idle})]


def _model(sec, ok=True):
    return [ev(sec, f"ms{sec}", "span.model_request_start"),
            ev(sec + 1, f"me{sec}", "span.model_request_end", model_request_start_id=f"ms{sec}", is_error=not ok, model_usage=USAGE)]


def test_turn_is_called_a_conversation_round_not_an_inference_round():
    p = plain(render_events_xml(_turn(_model(2), "end_turn"))[0])
    assert "第 1 轮对话" in p and "轮推理" not in p


def test_completed_turn_is_green_even_if_some_model_calls_were_retried():
    xml = render_events_xml(_turn([*_model(2, ok=False), *_model(5, ok=True)], "end_turn"))[0]
    emoji, body, note = round_head(xml)
    assert emoji == "✅"                                                   # 不能因为过程里失败过就标红
    assert "1 次模型调用失败后重试成功" in note
    assert "end_turn" not in body                                          # 不直接显示英文标识


def test_exhausted_retries_turn_is_red_with_plain_language():
    events = _turn([*_model(2, ok=False), *_model(4, ok=False),
                    ev(8, "err", "session.error", error={"type": "model_request_failed_error", "message": "x", "retry_status": {"type": "exhausted"}})],
                   "retries_exhausted")
    emoji, body, note = round_head(render_events_xml(events)[0])
    assert emoji == "❌" and "模型调用失败,重试耗尽" in body


def test_unfinished_turn_is_pending():
    events = [ev(1, "u", "user.message", content=text("q")), *_model(2)]
    emoji, body, note = round_head(render_events_xml(events)[0])
    assert emoji == "⏳" and "进行中" in body


def test_unknown_stop_reason_is_shown_verbatim_not_invented():
    emoji, body, note = round_head(render_events_xml(_turn(_model(2), "something_new"))[0])
    assert emoji == "⚠️" and "something_new" in body


def test_a_tool_error_alone_does_not_turn_a_completed_round_red():
    events = _turn([ev(2, "tu", "agent.tool_use", name="bash", input={}),
                    ev(3, "tr", "agent.tool_result", tool_use_id="tu", is_error=True, content=text("boom"))], "end_turn")
    emoji, body, note = round_head(render_events_xml(events)[0])
    assert emoji == "✅"                                                   # 工具报错由 Span 卡片标红,本轮最终仍完成了


# ---- 三、顺序:文档里的原始事件与 MA 流逐一相同 ------------------------------------------
def _doc_event_ids(xml):
    return re.findall(r'"id":\s*"([^"]+)"', html.unescape(xml))


REAL_SHAPE = [  # 取自真实 session 的开头:usage、状态事件夹在流中间
    ev(0, "us1", "session.usage", usage={}), ev(0, "us2", "session.usage", usage={}),
    ev(1, "st", "session.status_running"), ev(1, "th", "session.thread_status_running", session_thread_id="t1", agent_name="a"),
    ev(2, "u1", "user.message", content=text("q")),
    *_model(3), ev(5, "tu1", "agent.tool_use", name="bash", input={}), ev(6, "tr1", "agent.tool_result", tool_use_id="tu1", is_error=False, content=text("ok")),
    ev(7, "msg", "agent.message", content=text("done")),
    ev(8, "us3", "session.usage", usage={}), ev(8, "idle", "session.status_idle", stop_reason={"type": "end_turn"}),
]


def test_document_events_appear_in_exactly_the_original_stream_order():
    xml = render_events_xml(REAL_SHAPE)[0]
    expected = [e["id"] for e in sorted(REAL_SHAPE, key=lambda e: e["processed_at"])]
    # 原始事件块按文档顺序抽出事件 id(只取代码块里的 JSON)
    got = [json.loads(html.unescape(m))["id"] for m in re.findall(r'<pre lang="json"[^>]*><code>(.*?)</code></pre>', xml, re.S)]
    assert got == expected


def test_status_events_stay_where_they_happened():
    xml = render_events_xml(REAL_SHAPE)[0]
    ids = [json.loads(html.unescape(m))["id"] for m in re.findall(r'<pre lang="json"[^>]*><code>(.*?)</code></pre>', xml, re.S)]
    assert ids.index("us1") < ids.index("u1") and ids.index("st") < ids.index("u1")    # 开头的状态事件仍在用户消息之前
    assert ids.index("us3") > ids.index("msg")                                         # usage 在回复之后,没被搬到别处


def test_every_event_appears_exactly_once():
    xml = render_events_xml(REAL_SHAPE)[0]
    ids = [json.loads(html.unescape(m))["id"] for m in re.findall(r'<pre lang="json"[^>]*><code>(.*?)</code></pre>', xml, re.S)]
    assert sorted(ids) == sorted(e["id"] for e in REAL_SHAPE)


# ---- 四、并行:连续的 tool_use(中间没有别的事件)= 同一次模型回复发出的并行批次 ----------
# 真实形态(qwhmzbji0g):USE#3 USE#1 USE#4 USE#2 RES#4 RES#1 RES#3 RES#2,时间戳各不相同
def _parallel_events():
    return [ev(1, "u", "user.message", content=text("q")), *_model(2),
            ev(5, "a", "agent.tool_use", name="fetch_region", input={"command": "r"}),
            ev(5, "b", "agent.tool_use", name="fetch_channel", input={"command": "c"}),
            ev(6, "c", "agent.tool_use", name="fetch_kpi", input={"command": "k"}),
            ev(9, "rb", "agent.tool_result", tool_use_id="b", is_error=False, content=text("b")),
            ev(10, "ra", "agent.tool_result", tool_use_id="a", is_error=False, content=text("a")),
            ev(12, "rc", "agent.tool_result", tool_use_id="c", is_error=True, content=text("c")),
            ev(14, "i", "session.status_idle", stop_reason={"type": "end_turn"})]


def test_consecutive_tool_uses_form_one_parallel_group_rendered_as_grid():
    xml = render_events_xml(_parallel_events())[0]
    assert xml.count("<grid>") == 1 and xml.count("<column") == 3 and "并行 3" in plain(xml)


def test_parallel_cards_show_each_tools_own_duration_and_status():
    xml = render_events_xml(_parallel_events())[0]
    grid = re.search(r"<grid>.*?</grid>", xml, re.S).group(0)
    p = plain(grid)
    assert "fetch_region" in p and "5.0 秒" in p and "fetch_channel" in p and "4.0 秒" in p and "fetch_kpi" in p and "6.0 秒" in p
    assert grid.count('emoji="❌"') == 1 and grid.count('emoji="✅"') == 2          # 失败的那个标红


def test_grid_holds_cards_only_and_raw_json_stays_in_stream_order():
    xml = render_events_xml(_parallel_events())[0]
    grid = re.search(r"<grid>.*?</grid>", xml, re.S).group(0)
    assert "<pre" not in grid                                                       # 原始 JSON 不进列,否则流顺序被拆散
    ids = [json.loads(html.unescape(m))["id"] for m in re.findall(r'<pre lang="json"[^>]*><code>(.*?)</code></pre>', xml, re.S)]
    assert ids[ids.index("a"):ids.index("rc") + 1] == ["a", "b", "c", "rb", "ra", "rc"]   # 严格原始顺序,use 与 result 交错


def test_json_blocks_of_a_parallel_group_are_labelled_with_their_column():
    xml = html.unescape(render_events_xml(_parallel_events())[0])
    assert 'caption="并行 #1' in xml and 'caption="并行 #3' in xml


def test_single_tool_is_not_a_parallel_group():
    events = [ev(1, "u", "user.message", content=text("q")), ev(2, "a", "agent.tool_use", name="bash", input={}),
              ev(3, "ra", "agent.tool_result", tool_use_id="a", is_error=False, content=text("x"))]
    assert "<grid>" not in render_events_xml(events)[0]


def test_tools_separated_by_another_event_are_not_grouped_even_if_times_overlap():
    # 中间夹了一个模型调用:不是同一次回复发出的,不能因为时间重叠就当并行
    events = [ev(1, "u", "user.message", content=text("q")),
              ev(2, "a", "agent.tool_use", name="t1", input={}),
              *_model(3),
              ev(6, "b", "agent.tool_use", name="t2", input={}),
              ev(7, "ra", "agent.tool_result", tool_use_id="a", is_error=False, content=text("x")),
              ev(8, "rb", "agent.tool_result", tool_use_id="b", is_error=False, content=text("y"))]
    assert "<grid>" not in render_events_xml(events)[0]


def test_unfinished_parallel_member_is_marked_pending_inside_grid():
    events = [ev(1, "u", "user.message", content=text("q")),
              ev(2, "a", "agent.tool_use", name="t1", input={}), ev(2, "b", "agent.tool_use", name="t2", input={}),
              ev(5, "ra", "agent.tool_result", tool_use_id="a", is_error=False, content=text("x"))]
    grid = re.search(r"<grid>.*?</grid>", render_events_xml(events)[0], re.S).group(0)
    assert grid.count('emoji="⏳"') == 1 and "未完成" in plain(grid)


# ---- 五、同一毫秒内的事件:排序与去重都必须用纳秒,否则错位或永久丢事件 -----------------------
def ns_ev(frac, eid, etype="agent.message", **f):
    """同一秒、不同纳秒的事件(真实 session 里约 1% 的事件与前一个事件毫秒相同)。"""
    return {"id": eid, "type": etype, "processed_at": f"2026-10-16T14:38:39.{frac}+08:00", **f}


def test_events_in_the_same_millisecond_keep_their_true_order():
    # 实测形态:message 与紧随其后的 model_request_start 毫秒相同,纳秒相差 2.8 万
    events = [ns_ev("554183484", "second", "span.model_request_start"), ns_ev("554155192", "first", content=text("x"))]
    from arkagent.observability.events import stream_order
    assert [e["id"] for e in stream_order(events)] == ["first", "second"]       # 旧代码按毫秒排,会保持输入顺序


def test_document_order_is_the_nanosecond_order_not_input_order():
    events = [ev(1, "u", "user.message", content=text("q")),
              ns_ev("554183484", "b", "span.model_request_start"), ns_ev("554155192", "a", content=text("x"))]
    xml = render_events_xml(events)[0]
    ids = [json.loads(html.unescape(m))["id"] for m in re.findall(r'<pre lang="json"[^>]*><code>(.*?)</code></pre>', xml, re.S)]
    assert ids.index("a") < ids.index("b")


def test_identical_timestamps_keep_their_input_order():
    events = [ns_ev("554155192", "x"), ns_ev("554155192", "y"), ns_ev("554155192", "z")]
    from arkagent.observability.events import stream_order
    assert [e["id"] for e in stream_order(events)] == ["x", "y", "z"]            # 稳定,不乱序


def test_a_new_event_in_the_same_millisecond_as_the_last_written_one_is_not_lost():
    from arkagent.observability.docsink import DocRecord, newer_than
    from arkagent.observability.events import ts_ms, ts_ns
    last = ns_ev("554155192", "last")
    nxt = ns_ev("554183484", "next", "span.model_request_start")                  # 与 last 同一毫秒
    assert ts_ms(last["processed_at"]) == ts_ms(nxt["processed_at"])              # 前提:毫秒确实相同
    rec = DocRecord("s", "d", "u", "last", ts_ms(last["processed_at"]), ts_ns(last["processed_at"]))
    assert [e["id"] for e in newer_than([last, nxt], rec)] == ["next"]            # 旧代码按毫秒 > 判断,会把 next 丢掉


def test_legacy_record_without_nanoseconds_errs_on_the_side_of_duplicating_not_losing():
    from arkagent.observability.docsink import DocRecord, newer_than
    from arkagent.observability.events import ts_ms
    last = ns_ev("554155192", "last")
    nxt = ns_ev("554183484", "next", "span.model_request_start")
    legacy = DocRecord("s", "d", "u", "last", ts_ms(last["processed_at"]), 0)      # 升级前写入的记录,没有纳秒
    got = [e["id"] for e in newer_than([last, nxt], legacy)]
    assert "next" in got                                                          # 绝不丢;可能多出一个 last,可以接受


def test_unparseable_timestamp_does_not_crash_ordering():
    from arkagent.observability.events import stream_order, ts_ns
    assert ts_ns("garbage") == 0 and ts_ns(None) == 0
    events = [{"id": "bad", "type": "x", "processed_at": "garbage"}, ns_ev("554155192", "ok")]
    assert [e["id"] for e in stream_order(events)] == ["bad", "ok"]


def test_already_ordered_input_is_returned_untouched_even_with_equal_timestamps():
    # 真实形态:session.usage 与紧随其后的 session.status_idle 纳秒完全相同,只有接口顺序能区分它们
    from arkagent.observability.events import stream_order
    usage = ns_ev("026000000", "usage", "session.usage", usage={})
    idle = ns_ev("026000000", "idle", "session.status_idle", stop_reason={"type": "end_turn"})
    assert [e["id"] for e in stream_order([usage, idle])] == ["usage", "idle"]
    assert [e["id"] for e in stream_order([idle, usage])] == ["idle", "usage"]      # 输入已"有序"(相等),不能擅自调换


def test_out_of_order_input_is_sorted_with_input_position_as_tiebreaker():
    from arkagent.observability.events import stream_order
    a, b, c = ns_ev("500000000", "a"), ns_ev("100000000", "b"), ns_ev("500000000", "c")
    assert [e["id"] for e in stream_order([a, b, c])] == ["b", "a", "c"]


# ---- 六、只写完整的一轮对话:不在 Span / 并行批次中间落笔(文档只能追加,写错无法回头改)-------------
from arkagent.observability.docrender import analyse as _analyse  # noqa: E402
from arkagent.observability.docscheduler import complete_prefix  # noqa: E402


def _idle(sec, eid="i", reason="end_turn"):
    return ev(sec, eid, "session.status_idle", stop_reason={"type": reason})


def _full_round(base, n):
    """一个完整的一轮对话:user → 模型 → 工具 → idle。"""
    return [ev(base, f"u{n}", "user.message", content=text(f"q{n}")),
            ev(base + 1, f"ms{n}", "span.model_request_start"),
            ev(base + 2, f"me{n}", "span.model_request_end", model_request_start_id=f"ms{n}", is_error=False, model_usage=USAGE),
            ev(base + 3, f"tu{n}", "agent.tool_use", name="bash", input={}),
            ev(base + 4, f"tr{n}", "agent.tool_result", tool_use_id=f"tu{n}", is_error=False, content=text("ok")),
            _idle(base + 5, f"i{n}")]


def test_session_ending_in_idle_is_written_entirely():
    evs = _full_round(1, 1) + _full_round(10, 2)
    safe, held = complete_prefix(evs, now_ms=10**13, stale_ms=1800_000)
    assert [e["id"] for e in safe] == [e["id"] for e in evs] and held == []


def test_running_round_is_held_back_and_earlier_complete_rounds_are_written():
    running = [ev(20, "u3", "user.message", content=text("q3")), ev(21, "tu3", "agent.tool_use", name="bash", input={})]
    evs = _full_round(1, 1) + _full_round(10, 2) + running
    safe, held = complete_prefix(evs, now_ms=ts_of(evs[-1]) + 1000, stale_ms=1800_000)
    assert [e["id"] for e in safe][-1] == "i2"                       # 写到最近一个 idle 为止
    assert [e["id"] for e in held] == ["u3", "tu3"]                  # 正在进行的这一轮,本周期不写


def test_never_idle_means_write_nothing():
    evs = [ev(1, "u1", "user.message", content=text("q")), ev(2, "ms", "span.model_request_start")]
    safe, held = complete_prefix(evs, now_ms=ts_of(evs[-1]) + 1000, stale_ms=1800_000)
    assert safe == [] and len(held) == 2


def test_parallel_batch_cut_in_the_middle_is_never_written():
    # 复现场景:3 个并行工具只拿到前 2 个 tool_use
    evs = _full_round(1, 1) + [ev(10, "u2", "user.message", content=text("q")),
                               ev(11, "a", "agent.tool_use", name="t", input={}), ev(11, "b", "agent.tool_use", name="t", input={})]
    safe, held = complete_prefix(evs, now_ms=ts_of(evs[-1]) + 1000, stale_ms=1800_000)
    rendered = render_events_xml(safe)[0]
    assert "⏳" not in rendered and "grid" not in rendered           # 已写入部分不含"未完成"卡片或被切开的 grid
    assert [e["id"] for e in held] == ["u2", "a", "b"]


def test_stale_unfinished_round_is_flushed_instead_of_blocking_forever():
    running = [ev(20, "u3", "user.message", content=text("q3")), ev(21, "tu3", "agent.tool_use", name="bash", input={})]
    evs = _full_round(1, 1) + running
    last = ts_of(evs[-1])
    safe, held = complete_prefix(evs, now_ms=last + 31 * 60_000, stale_ms=1800_000)     # 31 分钟没动静
    assert [e["id"] for e in safe] == [e["id"] for e in evs] and held == []            # 兜底:全部写入,不能永远不写


def test_not_yet_stale_unfinished_round_is_still_held():
    running = [ev(20, "u3", "user.message", content=text("q3")), ev(21, "tu3", "agent.tool_use", name="bash", input={})]
    evs = _full_round(1, 1) + running
    safe, held = complete_prefix(evs, now_ms=ts_of(evs[-1]) + 29 * 60_000, stale_ms=1800_000)
    assert [e["id"] for e in held] == ["u3", "tu3"]


def test_empty_batch():
    assert complete_prefix([], now_ms=0, stale_ms=1) == ([], [])


def ts_of(e):
    from arkagent.observability.events import ts_ms
    return ts_ms(e["processed_at"])


# ---- 七、轮号接续:增量写入不能让第二批的轮次编号从 1 重新开始 -----------------------------
def test_round_numbering_continues_from_a_start_index():
    a = _analyse(_full_round(1, 1) + _full_round(10, 2), first_round=5)
    assert [r.index for r in a.rounds] == [5, 6]
    xml = render_events_xml(_full_round(1, 1), first_round=5)[0]
    assert "第 5 轮对话" in plain(xml) and "第 1 轮对话" not in plain(xml)


def test_default_numbering_starts_at_one():
    assert [r.index for r in _analyse(_full_round(1, 1) + _full_round(10, 2)).rounds] == [1, 2]
