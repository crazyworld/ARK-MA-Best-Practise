"""Topic6 临时能力：调度 Bot（Ada）代发 /start、富文本入站、话题内文字确认 HC。

- TOPIC6_PROXY_OPEN_IDS：调度方 open_id → 固定真人 open_id，入口处替换发起人；
- 纯文本富文本(post)消息不再在入站映射时被丢弃；
- 待审 HC 时 @bot 回复「通过 / 打回 原因 / 补充 内容」等价于点卡片按钮。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from arkagent.feishu import IncomingMessage, _inbound_to_incoming
from cases.topic6.gateway import Topic6Gateway, parse_text_hc_decision
from cases.topic6.pipeline_store import PipelineStore

CASE_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
_spec = importlib.util.spec_from_file_location("topic6_case_config_dispatch", CASE_DIR / "config.py")
config_module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["topic6_case_config_dispatch"] = config_module
_spec.loader.exec_module(config_module)

ADA = "ou_ada"
HUMAN = "ou_human"


def _msg(text: str, **kw) -> IncomingMessage:
    base = IncomingMessage(
        event_id="e1", message_id="m1", chat_id="c1", chat_type="group", thread_id="",
        user_open_id=HUMAN, user_name="u", tenant_key="t", text=text, mentioned_bot=True,
    )
    return replace(base, **kw)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- 富文本入站 -----------------------------------------------------------------


def test_inbound_keeps_text_only_post():
    msg = SimpleNamespace(
        id="om-post", raw_content_type="post", content_text="@社媒领域分析师-热点分析 /start demo",
        create_time=1, mentioned_bot=True,
        conversation=SimpleNamespace(chat_id="oc-1", chat_type="group", thread_id=None),
        sender=SimpleNamespace(open_id=ADA), resources=[],
    )
    result = _inbound_to_incoming(msg)
    assert result is not None
    assert result.content_type == "post"
    assert result.text == "@社媒领域分析师-热点分析 /start demo"
    assert result.mentioned_bot is True


# ---- 代发映射配置 ---------------------------------------------------------------


def _env(**overrides):
    env = {
        "ARK_API_KEY": "k", "FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s",
        "TOPIC6_COORDINATOR_AGENT_ID": "agent", "TOPIC6_ENVIRONMENT_ID": "env",
    }
    env.update(overrides)
    return env


def test_proxy_open_ids_parsed_from_env():
    cfg = config_module.load_case_config(_env(TOPIC6_PROXY_OPEN_IDS=f"{ADA}:{HUMAN}, ou_b:ou_c"))
    assert dict(cfg.proxy_open_ids) == {ADA: HUMAN, "ou_b": "ou_c"}
    assert config_module.load_case_config(_env()).proxy_open_ids == ()


def test_proxy_open_ids_rejects_malformed_entry():
    with pytest.raises(RuntimeError):
        config_module.load_case_config(_env(TOPIC6_PROXY_OPEN_IDS="ou_only"))


# ---- 代发 /start ----------------------------------------------------------------


def _start_gateway(config, calls, replies, **kw):
    class Runner:
        async def start_job(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(job_id="job-1")

    class Hitl:
        async def handle_remark_message(self, **_kw):
            return False

    async def reply(_chat, text):
        replies.append(text)

    return Topic6Gateway(
        config, SimpleNamespace(complete_event=lambda *_a: None),
        Runner(), Hitl(), SimpleNamespace(), reply, None, **kw,
    )


def test_proxied_sender_starts_job_as_fixed_human():
    calls, replies = [], []
    gw = _start_gateway(
        SimpleNamespace(authorized_open_ids=(HUMAN,), skip_miaoda_oauth=True,
                        proxy_open_ids=((ADA, HUMAN),)),
        calls, replies,
    )
    _run(gw._process(_msg("@社媒领域分析师-热点分析 /start demo", user_open_id=ADA)))
    assert replies == []  # 白名单按映射后的真人校验
    assert calls[0]["user_open_id"] == HUMAN
    assert calls[0]["mode"] == "demo"


def test_post_title_line_is_ignored_for_start():
    calls, replies = [], []
    gw = _start_gateway(
        SimpleNamespace(authorized_open_ids=(), skip_miaoda_oauth=True, proxy_open_ids=()),
        calls, replies,
    )
    _run(gw._process(_msg("# 启动任务\n\n@热点分析 /start demo", content_type="post")))
    assert calls[0]["mode"] == "demo"


def test_unmapped_sender_unchanged():
    calls, replies = [], []
    gw = _start_gateway(
        SimpleNamespace(authorized_open_ids=(HUMAN,), skip_miaoda_oauth=True,
                        proxy_open_ids=((ADA, HUMAN),)),
        calls, replies,
    )
    _run(gw._process(_msg("/start demo", user_open_id="ou_other")))
    assert calls == [] and "未授权" in replies[0]


# ---- 话题内文字确认 HC -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("通过", ("pass", "")),
        ("确认", ("pass", "")),
        ("PASS", ("pass", "")),
        ("打回 数据口径不对", ("reject", "数据口径不对")),
        ("驳回：重新分层", ("reject", "重新分层")),
        ("打回", ("reject", "")),
        ("补充 关注美妆赛道", ("remark", "关注美妆赛道")),
        ("备注：只看抖音", ("remark", "只看抖音")),
        ("补充", None),  # 补充必须带内容
        ("通过了吗", None),
        ("看起来不错", None),
        ("/start demo", None),
    ],
)
def test_parse_text_hc_decision(text, expected):
    assert parse_text_hc_decision(text) == expected


@pytest.fixture()
def hc_env(tmp_path):
    store = PipelineStore(str(tmp_path / "t6.db"))
    actions: list = []
    replies: list[str] = []

    class Hitl:
        async def handle_remark_message(self, **_kw):
            return False

        async def handle_card_action(self, action):
            actions.append(action)

    class Runner:
        async def start_job(self, **_kw):
            raise AssertionError("不应启动任务")

    async def reply(_chat, text):
        replies.append(text)

    gw = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=(), skip_miaoda_oauth=True,
                        proxy_open_ids=((ADA, HUMAN),)),
        SimpleNamespace(complete_event=lambda *_a: None),
        Runner(), Hitl(), SimpleNamespace(), reply, None, pipeline_store=store,
    )
    job = store.create_job(chat_id="c1", thread_id="", user_open_id=HUMAN, ma_session_id="s1",
                           mode="full", project_dir="/workspace/x")
    event_id = store.append_hc_event(job.job_id, "HC2", {"k": 1}, card_message_id="om_card")
    store.mark_wait_hc(job.job_id, "HC2")
    yield gw, store, job, event_id, actions, replies
    store.close()


def test_thread_text_pass_resolves_like_card_click(hc_env):
    gw, _store, job, event_id, actions, replies = hc_env
    _run(gw._process(_msg("@热点分析 通过", thread_id="omt_1", root_id="om_card")))
    assert replies == []
    value = actions[0]["action"]["value"]
    assert value == {"decision": "pass", "event_id": event_id, "job_id": job.job_id, "hc": "HC2"}
    assert actions[0]["operator"] == {"open_id": HUMAN}
    assert actions[0]["chat_id"] == "c1"


def test_thread_text_reject_carries_note_and_proxy_operator(hc_env):
    gw, _store, _job, _event_id, actions, _replies = hc_env
    _run(gw._process(_msg("打回 换成近两周数据", user_open_id=ADA, thread_id="omt_1")))
    assert actions[0]["action"]["value"]["decision"] == "reject"
    assert actions[0]["action"]["form_value"] == {"note": "换成近两周数据"}
    assert actions[0]["operator"] == {"open_id": HUMAN}


def test_non_keyword_text_falls_through_to_usage(hc_env):
    gw, _store, _job, _event_id, actions, replies = hc_env
    _run(gw._process(_msg("这个结果怎么样")))
    assert actions == [] and "/start" in replies[0]


def test_no_pending_hc_keyword_is_not_consumed(hc_env):
    gw, store, _job, event_id, actions, replies = hc_env
    store.resolve_hc_event(event_id, "pass")
    _run(gw._process(_msg("通过")))
    assert actions == [] and "/start" in replies[0]


def test_other_chat_does_not_resolve(hc_env):
    gw, _store, _job, _event_id, actions, _replies = hc_env
    _run(gw._process(_msg("通过", chat_id="c2")))
    assert actions == []


# ---- 与真实 Topic6Hitl 联调：构造的 dict 走完 落库 → patch 卡片 → resume ----------


def test_text_reject_drives_real_hitl(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from cases.topic6.topic6_hitl import Topic6Hitl, Topic6HitlDeps

    store = PipelineStore(str(tmp_path / "t6.db"))
    job = store.create_job(chat_id="c1", thread_id="", user_open_id=HUMAN, ma_session_id="s1",
                           mode="full", project_dir="/workspace/x")
    event_id = store.append_hc_event(job.job_id, "HC3", {"k": 1}, card_message_id="om_card")
    store.mark_wait_hc(job.job_id, "HC3")
    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="om_card")
    runner.resume_job = AsyncMock()
    runner._feishu = SimpleNamespace(
        resolve_operator_name=lambda open_id, chat_id=None: "赵修源" if open_id == HUMAN else "",
        send_to_chat=lambda *_a, **_k: None,
    )
    hitl = Topic6Hitl(Topic6HitlDeps(store=store, runner=runner))

    async def reply(_chat, _text):
        pass

    gw = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=(), skip_miaoda_oauth=True, proxy_open_ids=((ADA, HUMAN),)),
        SimpleNamespace(complete_event=lambda *_a: None),
        SimpleNamespace(), hitl, SimpleNamespace(), reply, None, pipeline_store=store,
    )
    _run(gw._process(_msg("驳回：标题换成周报", user_open_id=ADA, content_type="post",
                          thread_id="omt_1", root_id="om_card")))

    hc = store.get_hc_event(event_id)
    assert hc.user_decision == "reject" and hc.user_note == "标题换成周报"
    assert hc.operator_label == "赵修源" and hc.resolved_at is not None
    assert runner.resume_job.await_args[0][1] == "HC3 reject: 标题换成周报"
    assert runner.patch_job_card.await_count == 1
    store.close()
