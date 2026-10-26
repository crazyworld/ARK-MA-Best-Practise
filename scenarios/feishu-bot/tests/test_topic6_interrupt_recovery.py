"""sesn-20261026150456-vmpfi 中断链路的回归测试。

1. C2 输入缺某平台时只为有数据的平台跑 00 阶段（原：00_clean_titles.py SystemExit）
2. provider 失败 → 紧随的 retries_exhausted idle 不再消耗 nudge 额度
3. 模型请求成功后 provider 重试额度重置
4. Agent 输出 [need_decision] 时不催促、stopped 并把报告发群里
5. 用户授权：触发时强制刷新；常驻刷新连续失败后停止重试
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = ROOT / "cases" / "topic6"
C2_RUNNER = CASE_DIR / "ma-resources" / "skills" / "topic6-event-registry" / "scripts" / "run_topic6_c2.py"


def _load(name: str, path: Path):
    if str(CASE_DIR) not in sys.path:
        sys.path.insert(0, str(CASE_DIR))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pipeline_store = _load("pipeline_store", CASE_DIR / "pipeline_store.py")
runner_mod = _load("topic6_runner", CASE_DIR / "topic6_runner.py")
oauth_mod = _load("topic6_user_oauth", CASE_DIR / "topic6_user_oauth.py")
c2 = _load("topic6_c2_runner_for_interrupt_test", C2_RUNNER)

PipelineStore = pipeline_store.PipelineStore


# ---------------------------------------------------------------------------
# 1. C2 空平台
# ---------------------------------------------------------------------------


def _write_input(path: Path, platforms: list[str]) -> Path:
    pd.DataFrame({
        "record_id": [f"r{i}" for i in range(len(platforms))],
        "platform": platforms,
        "title": [f"t{i}" for i in range(len(platforms))],
        "heat": [1] * len(platforms),
    }).to_csv(path, index=False, encoding="utf-8-sig")
    return path


class TestC2PlatformSubset:
    def test_input_platforms_skips_empty_platform(self, tmp_path):
        """vmpfi 第二轮：微博8/知乎5/抖音4/B站0。"""
        csv = _write_input(tmp_path / "input.csv", ["微博"] * 8 + ["知乎"] * 5 + ["抖音"] * 4)
        assert c2._input_platforms(csv) == ("weibo", "douyin", "zhihu")

    def test_plan_only_runs_present_platforms(self, tmp_path):
        plan = c2.build_stage_plan(
            tmp_path / "run", tmp_path / "input.csv", "chat", "emb",
            platforms=("weibo", "douyin", "zhihu"),
        )
        stage00 = dict(plan)["00_platforms"]
        assert len(stage00) == 3
        assert all("B站" not in cmd for cmd in stage00)
        x0 = dict(plan)["x0_merge"][0]
        assert not any(arg.endswith("/bilibili") for arg in x0)
        assert sum(arg.endswith(("/weibo", "/douyin", "/zhihu")) for arg in x0) == 3

    def test_log_names_follow_platform_not_position(self, tmp_path, monkeypatch):
        """跳过 douyin 后，zhihu 的日志不能被错标成 douyin。"""
        calls = []
        monkeypatch.setattr(c2, "_run_command", lambda cmd, log: calls.append(log.name))
        plan = c2.build_stage_plan(
            tmp_path / "run", tmp_path / "input.csv", "chat", "emb",
            platforms=("weibo", "zhihu"),
        )
        c2._run_stage("00_platforms", dict(plan)["00_platforms"], tmp_path)
        assert sorted(calls) == ["00_platforms_weibo.log", "00_platforms_zhihu.log"]

    def test_no_platform_raises(self, tmp_path):
        with pytest.raises(ValueError, match="没有任何已知平台"):
            c2.build_stage_plan(tmp_path, tmp_path / "i.csv", "c", "e", platforms=())

    # ---- 干净特性 ----
    def test_default_plan_still_runs_four_platforms(self, tmp_path):
        plan = c2.build_stage_plan(tmp_path / "run", tmp_path / "input.csv", "chat", "emb")
        assert len(dict(plan)["00_platforms"]) == 4

    def test_all_platforms_present(self, tmp_path):
        csv = _write_input(tmp_path / "input.csv", ["微博", "抖音", "B站", "知乎"])
        assert c2._input_platforms(csv) == ("weibo", "douyin", "bilibili", "zhihu")


# ---------------------------------------------------------------------------
# 2-4. Runner：provider/nudge 去重 + need_decision
# ---------------------------------------------------------------------------


class _Ark:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, _sid, msg, extra_headers=None):
        self.sent.append(msg)


class _Sender:
    def __init__(self):
        self.messages: list[str] = []

    def send_to_chat(self, _chat_id, text):
        self.messages.append(text)


class _Stream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for event in self._events:
            yield event


@pytest.fixture()
def env(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    store = PipelineStore(str(tmp_path / "topic6.db"))
    ark, sender = _Ark(), _Sender()
    runner = runner_mod.Topic6Runner(
        ark, sender, store,
        runner_mod.Topic6Config(coordinator_agent_id="a", environment_id="e"),
        loop=loop,
    )

    async def _noop(*_a, **_k):
        return None

    runner._render_and_patch = _noop  # type: ignore[assignment]

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(runner_mod.asyncio, "sleep", _no_sleep)
    job = store.create_job(
        chat_id="c1", thread_id="", user_open_id="u1",
        ma_session_id="s1", mode="smoke", project_dir="/workspace/Projects/W43",
    )
    store.update_phase(job.job_id, "C2")
    runner._progress[job.job_id] = runner_mod._ProgressState()
    try:
        yield loop, runner, store, ark, sender, job
    finally:
        store.close()
        loop.close()


def _msg(text):
    return {"type": "agent.message", "content": [{"type": "text", "text": text}]}


def _provider_failure():
    return [
        {"type": "span.model_request_end", "is_error": True},
        {"type": "session.error", "error": {
            "type": "model_request_failed_error",
            "message": "Model request failed due to a provider issue.",
        }},
        {"type": "session.status_idle", "stop_reason": {"type": "retries_exhausted"}},
    ]


class TestProviderNudgeDedup:
    def test_retries_exhausted_idle_after_provider_error_does_not_nudge(self, env):
        loop, runner, store, ark, _sender, job = env
        events = [_msg("C2 运行中"), *_provider_failure(), *_provider_failure()]
        loop.run_until_complete(runner._consume_events(job.job_id, _Stream(events)))
        state = runner._progress[job.job_id]
        assert state.idle_nudge_count == 0
        assert state.provider_retry_count == 2
        assert all("中途停止" not in m for m in ark.sent)
        assert ark.sent.count("[系统] 模型服务已恢复，请继续执行当前阶段。") == 2

    def test_successful_request_resets_provider_counter(self, env):
        loop, runner, _store, _ark, _sender, job = env
        events = [
            *_provider_failure(),
            {"type": "span.model_request_end", "is_error": False},
            _msg("继续执行"),
        ]
        loop.run_until_complete(runner._consume_events(job.job_id, _Stream(events)))
        assert runner._progress[job.job_id].provider_retry_count == 0

    # ---- 干净特性：普通 end_turn idle 仍然 nudge ----
    def test_plain_idle_still_nudges(self, env):
        loop, runner, _store, ark, _sender, job = env
        events = [
            _msg("C2 运行中"),
            {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
        ]
        loop.run_until_complete(runner._consume_events(job.job_id, _Stream(events)))
        assert runner._progress[job.job_id].idle_nudge_count == 1
        assert any("中途停止" in m for m in ark.sent)


REPORT = (
    "[need_decision]\n## 阻断报告 · 冒烟第二轮 C2 事件归档失败,Phase D 未启动,不产出 HC2\n"
    "请裁决(回复 A / B / C)"
)


class TestNeedDecision:
    def test_need_decision_stops_without_nudge(self, env):
        loop, runner, store, _ark, sender, job = env
        result = loop.run_until_complete(runner._handle_idle(job.job_id, [REPORT]))
        assert result is None
        reloaded = store.get_job(job.job_id)
        assert reloaded.status == pipeline_store.STATUS_STOPPED
        assert runner._progress[job.job_id].idle_nudge_count == 0
        assert sender.messages and "/continue" in sender.messages[-1]
        assert "[need_decision]" not in sender.messages[-1]
        # 报告里出现 HC2 也不能被意图兜底误判为 HC 卡
        assert store.latest_hc_for_job(job.job_id, "HC2") is None

    def test_report_without_marker_still_nudges(self, env):
        """干净特性：无标记的普通文本仍按原逻辑 nudge。"""
        loop, runner, _store, _ark, _sender, job = env
        result = loop.run_until_complete(runner._handle_idle(job.job_id, ["C2 仍在运行"]))
        assert isinstance(result, str) and "中途停止" in result

    def test_coordinator_documents_marker(self):
        text = (CASE_DIR / "ma-resources" / "agents" / "coordinator.system.md").read_text("utf-8")
        assert "[need_decision]" in text


# ---------------------------------------------------------------------------
# 5. 用户授权：触发时强制刷新 + 常驻刷新失败熔断
# ---------------------------------------------------------------------------


class _OAuth:
    def __init__(self, fail=False):
        self.fail = fail
        self.refresh_count = 0

    async def refresh(self, _token):
        self.refresh_count += 1
        if self.fail:
            raise RuntimeError("20064 refresh token revoked")
        return oauth_mod.OAuthTokens("access-new", "refresh-new", int(time.time() * 1000) + 7_200_000)


class _VaultArk:
    def __init__(self):
        self.updated = []

    async def update_environment_credential(self, vault_id, cred_id, value):
        self.updated.append((vault_id, cred_id, value))


def _seed(store, expires_at):
    store.save_user_oauth("ou-1", "vlt-1", "cred-1", "refresh-old", expires_at, oauth_mod.APPS_USER_SCOPES)


class TestTriggerForceRefresh:
    def test_force_refresh_even_when_fresh(self, tmp_path):
        store = PipelineStore(str(tmp_path / "t.db"))
        _seed(store, int(time.time() * 1000) + 3_600_000)
        oauth = _OAuth()
        auth = oauth_mod.Topic6UserAuthorization(store, _VaultArk(), oauth)
        assert asyncio.run(auth.vault_id("ou-1", force_refresh=True)) == "vlt-1"
        assert oauth.refresh_count == 1
        assert store.get_user_oauth("ou-1")["refresh_token"] == "refresh-new"
        store.close()

    def test_force_refresh_failure_requires_authorization(self, tmp_path):
        store = PipelineStore(str(tmp_path / "t.db"))
        _seed(store, int(time.time() * 1000) + 3_600_000)
        auth = oauth_mod.Topic6UserAuthorization(store, _VaultArk(), _OAuth(fail=True))
        with pytest.raises(oauth_mod.UserAuthorizationRequired):
            asyncio.run(auth.vault_id("ou-1", force_refresh=True))
        store.close()

    def test_default_vault_id_does_not_refresh_fresh_token(self, tmp_path):
        """干净特性：常驻刷新路径仍按过期窗口判断。"""
        store = PipelineStore(str(tmp_path / "t.db"))
        _seed(store, int(time.time() * 1000) + 3_600_000)
        oauth = _OAuth()
        auth = oauth_mod.Topic6UserAuthorization(store, _VaultArk(), oauth)
        asyncio.run(auth.vault_id("ou-1"))
        assert oauth.refresh_count == 0
        store.close()


class TestBackgroundRefreshCircuitBreaker:
    def test_stops_after_max_failures(self, tmp_path):
        store = PipelineStore(str(tmp_path / "t.db"))
        _seed(store, int(time.time() * 1000) + 60_000)
        oauth = _OAuth(fail=True)
        auth = oauth_mod.Topic6UserAuthorization(store, _VaultArk(), oauth)

        async def run():
            for _ in range(oauth_mod.MAX_REFRESH_FAILURES):
                with pytest.raises(oauth_mod.UserAuthorizationRequired):
                    await auth.refresh_in_background("ou-1")

        asyncio.run(run())
        saved = store.get_user_oauth("ou-1")
        assert saved["refresh_token"] == ""
        assert saved["vault_id"] == "vlt-1" and saved["credential_id"] == "cred-1"
        # 下次触发：没有 refresh_token → 直接要求扫码，不再调飞书
        with pytest.raises(oauth_mod.UserAuthorizationRequired):
            asyncio.run(auth.vault_id("ou-1", force_refresh=True))
        assert oauth.refresh_count == oauth_mod.MAX_REFRESH_FAILURES
        store.close()

    def test_success_resets_failure_count(self, tmp_path):
        store = PipelineStore(str(tmp_path / "t.db"))
        _seed(store, int(time.time() * 1000) + 60_000)
        oauth = _OAuth(fail=True)
        auth = oauth_mod.Topic6UserAuthorization(store, _VaultArk(), oauth)

        async def run():
            for _ in range(oauth_mod.MAX_REFRESH_FAILURES - 1):
                with pytest.raises(oauth_mod.UserAuthorizationRequired):
                    await auth.refresh_in_background("ou-1")
            oauth.fail = False
            await auth.refresh_in_background("ou-1")

        asyncio.run(run())
        assert auth._refresh_failures == {}
        assert store.get_user_oauth("ou-1")["refresh_token"] == "refresh-new"
        store.close()

    def test_refresher_skips_users_marked_for_reauth(self):
        """Gateway 常驻协程跳过已清空 refresh_token 的用户。"""
        from types import SimpleNamespace

        gateway_mod = _load("topic6_gateway_for_interrupt_test", CASE_DIR / "gateway.py")
        called = []

        class UserAuth:
            async def refresh_in_background(self, open_id):
                called.append(open_id)

        class PS:
            def list_user_oauth(self):
                return [
                    {"open_id": "ou-dead", "refresh_token": ""},
                    {"open_id": "ou-ok", "refresh_token": "r"},
                ]

        gateway = gateway_mod.Topic6Gateway(
            SimpleNamespace(authorized_open_ids=(), lark_bot_vault_id=""),
            SimpleNamespace(), SimpleNamespace(_ark=None), SimpleNamespace(),
            UserAuth(), None, None, pipeline_store=PS(),
        )
        asyncio.run(gateway.refresh_tokens_once())
        assert called == ["ou-ok"]
