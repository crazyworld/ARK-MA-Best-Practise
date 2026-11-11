"""N5：可恢复的 Session 不能被 Gateway 判死。

覆盖(均按方舟 Session 事件语义，见 common/docs/火山方舟_ManagedAgents_docs.md「Session 事件流」)：
- session.error：retry_status=retrying 继续监听；exhausted/缺省 → 可恢复；terminal → session 级
- session.status_failed / status_terminated → session 级(真终态不变)
- 沙箱故障(task service unavailable / sandbox abnormal)连续出现 → 不再 nudge，可恢复失败
- /continue 回复进度卡片 → 按卡片定位任务；他人任务仅发起人/管理员可续跑；卡片对应任务已完成/停止时直接提示不回落；不引用卡片时行为不变
- session.error retrying 先于 provider 重试判断，平台重试期间 Gateway 不叠加重试
- 卡片「停止任务」置 stopping 后的 interrupt 仍按原设计跳过(不可逆停止由 stop_handler 完成)
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

CASE_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
for p in (str(CASE_DIR), str(CASE_DIR.parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name: str, file: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, CASE_DIR / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pipeline_store = _load("pipeline_store", "pipeline_store.py")
runner_mod = _load("topic6_runner", "topic6_runner.py")
PipelineStore = pipeline_store.PipelineStore


class _Ark:
    session_status = "idle"

    def __init__(self):
        self.sent: list[str] = []

    async def get_session(self, session_id):
        return {"id": session_id, "status": self.session_status}

    async def send_message(self, _sid, text, extra_headers=None):
        self.sent.append(text)

    async def list_session_events(self, _sid):
        return []


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e


@pytest.fixture()
def env(tmp_path):
    loop = asyncio.new_event_loop()
    store = PipelineStore(str(tmp_path / "t6.db"))
    ark = _Ark()
    runner = runner_mod.Topic6Runner(
        ark, object(), store,
        runner_mod.Topic6Config(coordinator_agent_id="a", environment_id="e"), loop=loop,
    )

    async def _noop(*_a, **_k):
        return None

    runner._render_and_patch = _noop  # type: ignore[assignment]
    runner._reply_async_by_job = _noop  # type: ignore[assignment]
    spawned = []
    runner._spawn_stream = lambda job, first_user_message: spawned.append(  # type: ignore[assignment]
        (job.job_id, first_user_message)) or object()
    job = store.create_job(chat_id="c1", thread_id="", user_open_id="u_owner",
                           ma_session_id="s1", mode="full", project_dir="/workspace/x")
    store.update_phase(job.job_id, "C0")
    runner._progress[job.job_id] = runner_mod._ProgressState()
    try:
        yield loop, runner, store, ark, job, spawned
    finally:
        store.close()
        loop.close()


def _err(retry_status=None, etype="internal_error"):
    error = {"type": etype, "message": "boom"}
    if retry_status:
        error["retry_status"] = {"type": retry_status}
    return {"type": "session.error", "error": error}


def _idle(reason="end_turn"):
    return {"type": "session.status_idle", "stop_reason": {"type": reason}}


def _run(env, events):
    loop, runner, store, _ark, job, _ = env
    loop.run_until_complete(runner._consume_events(job.job_id, _Stream(events)))
    return store.get_job(job.job_id)


# ---------------------------------------------------------------- session.error 分流

def test_error_retrying_keeps_listening(env):
    got = _run(env, [_err("retrying"), {"type": "agent.message",
                                        "content": [{"type": "text", "text": "继续中"}]}])
    assert got.status == pipeline_store.STATUS_RUNNING


@pytest.mark.parametrize("retry_status", ["exhausted", None])
def test_error_exhausted_or_unknown_is_recoverable(env, retry_status):
    got = _run(env, [_err(retry_status)])
    assert got.status == pipeline_store.STATUS_FAILED
    assert got.failure_scope == "recoverable"


def test_error_terminal_is_session_scope(env):
    got = _run(env, [_err("terminal")])
    assert got.failure_scope == "session"


@pytest.mark.parametrize("etype", ["session.status_failed", "session.status_terminated"])
def test_real_terminal_status_still_session_scope(env, etype):
    got = _run(env, [{"type": etype}])
    assert got.status == pipeline_store.STATUS_FAILED
    assert got.failure_scope == "session"


def test_recoverable_error_can_continue_when_session_idle(env):
    loop, runner, store, ark, job, spawned = env
    _run(env, [_err("exhausted")])
    resumed = loop.run_until_complete(
        runner.continue_job(chat_id="c1", thread_id="", user_open_id="u_owner"))
    assert resumed is not None and spawned == [(job.job_id, "继续")]


def test_continue_rechecks_real_session_status(env):
    """可恢复只是本地判断；Session 实际已终止时 continue_job 仍按 GET /sessions 改判并拒绝。"""
    loop, runner, store, ark, job, spawned = env
    _run(env, [_err("exhausted")])
    ark.session_status = "terminated"
    with pytest.raises(runner_mod.Topic6RunnerError, match="terminated"):
        loop.run_until_complete(
            runner.continue_job(chat_id="c1", thread_id="", user_open_id="u_owner"))
    assert store.get_job(job.job_id).failure_scope == "session" and spawned == []


def test_stop_button_interrupt_is_still_skipped(env):
    loop, runner, store, _ark, job, _ = env
    store.mark_stopping(job.job_id, "u_owner")
    loop.run_until_complete(runner._consume_events(job.job_id, _Stream([{"type": "user.interrupt"}])))
    assert store.get_job(job.job_id).status == pipeline_store.STATUS_STOPPING


# ---------------------------------------------------------------- 沙箱故障不再无效 nudge

def _sandbox_err(i):
    text = ("task service unavailable: Tool execution failed due to a runtime issue."
            if i % 2 else '{"is_error":true,"error":{"message":"session s1 sandbox abnormal failed: '
            'dial tcp 10.0.21.136:3306: i/o timeout"}}')
    return {"type": "agent.tool_result", "is_error": True, "content": [{"type": "text", "text": text}]}


def test_sandbox_failure_stops_nudging_and_stays_recoverable(env):
    _loop, _runner, _store, ark, _job, _ = env
    events = [*(_sandbox_err(i) for i in range(3)),
              {"type": "agent.message", "content": [{"type": "text", "text": "沙箱不可用，未执行任何步骤"}]},
              _idle()]
    got = _run(env, events)
    assert got.status == pipeline_store.STATUS_FAILED
    assert got.failure_scope == "recoverable"
    assert "沙箱" in got.last_error
    assert not any("中途停止" in m for m in ark.sent)


def test_normal_tool_result_resets_sandbox_streak(env):
    _loop, _runner, _store, ark, _job, _ = env
    events = [_sandbox_err(0), _sandbox_err(1),
              {"type": "agent.tool_result", "is_error": False, "content": [{"type": "text", "text": "ok"}]},
              _sandbox_err(2),
              {"type": "agent.message", "content": [{"type": "text", "text": "C0 运行中"}]}, _idle()]
    _run(env, events)
    assert any("中途停止" in m for m in ark.sent)  # 未达阈值 → 仍按原逻辑 nudge


# ---------------------------------------------------------------- /continue 按进度卡片定位

def _failed_with_card(env, card="om_card_1"):
    _loop, _runner, store, _ark, job, _ = env
    store.set_progress_card_message_id(job.job_id, card)
    store.mark_failed(job.job_id, "Pipeline 在 Phase C0 中断")
    return job


def test_continue_by_card_reply_from_admin(env):
    loop, runner, _store, _ark, job, spawned = env
    _failed_with_card(env)
    resumed = loop.run_until_complete(runner.continue_job(
        chat_id="c1", thread_id="omt_other", user_open_id="u_admin",
        card_message_id="om_card_1", admin_open_ids=("u_admin",)))
    assert resumed.job_id == job.job_id and spawned == [(job.job_id, "继续")]


def test_continue_by_card_reply_from_other_user_is_refused(env):
    loop, runner, _store, _ark, job, spawned = env
    _failed_with_card(env)
    with pytest.raises(runner_mod.Topic6RunnerError, match="仅发起人或管理员"):
        loop.run_until_complete(runner.continue_job(
            chat_id="c1", thread_id="", user_open_id="u_stranger", card_message_id="om_card_1"))
    assert spawned == []


def test_continue_without_card_keeps_session_key_semantics(env):
    loop, runner, _store, _ark, _job, spawned = env
    _failed_with_card(env)
    assert loop.run_until_complete(runner.continue_job(
        chat_id="c1", thread_id="", user_open_id="u_stranger")) is None
    assert spawned == []


def test_continue_reply_to_unrelated_message_falls_back(env):
    loop, runner, _store, _ark, job, spawned = env
    _failed_with_card(env)
    resumed = loop.run_until_complete(runner.continue_job(
        chat_id="c1", thread_id="", user_open_id="u_owner", card_message_id="om_unrelated"))
    assert resumed.job_id == job.job_id


# ---------------------------------------------------------------- R4 回复不可续跑卡片不回落

@pytest.mark.parametrize("status,hint", [("done", "已完成"), ("stopped", "已停止"), ("stopping", "正在停止")])
def test_continue_reply_to_unresumable_card_does_not_fall_back(env, status, hint):
    """回复了已完成/已停止任务的卡片：直接提示，MUST NOT 续跑同一用户的另一个任务。"""
    loop, runner, store, _ark, job, spawned = env
    store.set_progress_card_message_id(job.job_id, "om_card_done")
    if status == "done":
        store.mark_done(job.job_id, online_url="https://x.example/app")
    elif status == "stopped":
        store.mark_stopped(job.job_id, "stopped_by_user")
    else:
        store.mark_stopping(job.job_id, "u_owner")
    other = store.create_job(chat_id="c1", thread_id="", user_open_id="u_owner",
                             ma_session_id="s2", mode="full", project_dir="/workspace/y")
    store.mark_failed(other.job_id, "Pipeline 在 Phase D 中断")
    with pytest.raises(runner_mod.Topic6RunnerError, match=hint):
        loop.run_until_complete(runner.continue_job(
            chat_id="c1", thread_id="", user_open_id="u_owner", card_message_id="om_card_done"))
    assert spawned == []
    assert store.get_job(other.job_id).status == pipeline_store.STATUS_FAILED


def test_continue_reply_to_unresumable_card_from_stranger_is_refused(env):
    loop, runner, store, _ark, job, spawned = env
    store.set_progress_card_message_id(job.job_id, "om_card_done")
    store.mark_done(job.job_id)
    with pytest.raises(runner_mod.Topic6RunnerError, match="仅发起人或管理员"):
        loop.run_until_complete(runner.continue_job(
            chat_id="c1", thread_id="", user_open_id="u_stranger", card_message_id="om_card_done"))
    assert spawned == []


# ---------------------------------------------------------------- R3 平台重试中不叠加 Gateway 重试

def test_model_request_failed_while_platform_retrying_is_not_double_retried(env, monkeypatch):
    loop, runner, _store, ark, job, _ = env
    slept = []

    async def _fast_sleep(delay, *_a, **_k):
        slept.append(delay)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", _fast_sleep)
    got = _run(env, [_err("retrying", etype="model_request_failed_error"),
                     {"type": "agent.message", "content": [{"type": "text", "text": "继续中"}]}])
    assert got.status == pipeline_store.STATUS_RUNNING
    assert not any("模型服务已恢复" in m for m in ark.sent)
    assert slept == []
    assert runner._progress[job.job_id].provider_retry_count == 0


def test_model_request_failed_without_platform_retry_still_gateway_retries(env, monkeypatch):
    """回归：平台不再重试(exhausted/缺省)时，Gateway 自身的 provider 重试保持原行为。"""
    loop, runner, _store, ark, job, _ = env

    async def _fast_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(runner_mod.asyncio, "sleep", _fast_sleep)
    _run(env, [_err("exhausted", etype="model_request_failed_error")])
    assert any("模型服务已恢复" in m for m in ark.sent)
    assert runner._progress[job.job_id].provider_retry_count == 1


# ---------------------------------------------------------------- 沙箱故障文案(topic6 入口无卡片按钮)

def test_sandbox_failure_text_only_mentions_continue_command(env):
    events = [*(_sandbox_err(i) for i in range(3)), _idle()]
    got = _run(env, events)
    assert "/continue" in got.last_error
    assert "继续当前任务」" not in got.last_error
