"""N2：待裁决任务时只回复单个选项字母，Gateway 提示 `/continue <字母>`，不自动执行裁决。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arkagent.feishu import IncomingMessage
from cases.topic6.gateway import Topic6Gateway
from cases.topic6.pipeline_store import PipelineStore
from cases.topic6.topic6_runner import NEED_DECISION_REASON_MARK


def _msg(text: str, **kw) -> IncomingMessage:
    base = IncomingMessage(
        event_id="e1", message_id="m1", chat_id="c1", chat_type="group", thread_id="",
        user_open_id="u1", user_name="u", tenant_key="t", text=text, mentioned_bot=False,
    )
    return replace(base, **kw)


@pytest.fixture()
def env(tmp_path):
    store = PipelineStore(str(tmp_path / "t6.db"))
    replies: list[str] = []
    calls: list[dict] = []

    class Runner:
        async def continue_job(self, **kw):
            calls.append(kw)
            return None

        async def start_job(self, **kw):
            calls.append({"start": kw})
            raise AssertionError("不应启动任务")

    class Hitl:
        async def handle_remark_message(self, **_kw):
            return False

    async def reply(_chat, text):
        replies.append(text)

    gw = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=(), skip_miaoda_oauth=True),
        SimpleNamespace(complete_event=lambda *_a: None),
        Runner(), Hitl(), SimpleNamespace(), reply, None, pipeline_store=store,
    )
    job = store.create_job(chat_id="c1", thread_id="", user_open_id="u1", ma_session_id="s1",
                           mode="full", project_dir="/workspace/x")
    store.update_phase(job.job_id, "G")
    yield gw, store, job, replies, calls
    store.close()


def _run(gw, message):
    asyncio.new_event_loop().run_until_complete(gw._process(message))


def test_single_letter_with_pending_decision_hints_continue(env):
    gw, store, job, replies, calls = env
    store.mark_failed(job.job_id, f"Phase G {NEED_DECISION_REASON_MARK}（Agent 已停下给出选项）")
    _run(gw, _msg("a"))
    assert calls == []  # 不自动执行裁决
    assert len(replies) == 1 and "/continue A" in replies[0] and job.job_id in replies[0]


def test_single_letter_by_card_reply_from_other_user(env):
    gw, store, job, replies, calls = env
    store.set_progress_card_message_id(job.job_id, "om_card")
    store.mark_failed(job.job_id, f"Phase G {NEED_DECISION_REASON_MARK}（Agent 已停下给出选项）")
    _run(gw, _msg("B", user_open_id="u2", reply_to_message_id="om_card"))
    assert calls == [] and "/continue B" in replies[0]


@pytest.mark.parametrize("reason", ["Pipeline 在 Phase C0 中断", ""])
def test_single_letter_without_pending_decision_keeps_usage(env, reason):
    gw, store, job, replies, calls = env
    if reason:
        store.mark_failed(job.job_id, reason)
    _run(gw, _msg("A"))
    assert calls == [] and "/start" in replies[0] and "/continue A" not in replies[0]


def test_multi_letter_text_is_not_treated_as_choice(env):
    gw, store, job, replies, _calls = env
    store.mark_failed(job.job_id, f"Phase G {NEED_DECISION_REASON_MARK}（Agent 已停下给出选项）")
    _run(gw, _msg("AB"))
    assert "检测到待裁决任务" not in replies[0]
