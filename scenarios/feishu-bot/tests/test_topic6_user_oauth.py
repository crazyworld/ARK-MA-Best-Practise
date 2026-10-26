"""Topic6 妙搭用户 OAuth、Vault 与 Gateway 接入测试。"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from arkagent.feishu import IncomingMessage

CASE_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"


def _load(module_name: str, file_name: str):
    if str(CASE_DIR) not in sys.path:
        sys.path.insert(0, str(CASE_DIR))
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, CASE_DIR / file_name)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pipeline_store = _load("pipeline_store", "pipeline_store.py")
oauth_module = _load("topic6_user_oauth", "topic6_user_oauth.py")
gateway_module = _load("topic6_gateway_for_oauth_test", "gateway.py")
runner_module = sys.modules["topic6_runner"]

APPS_USER_SCOPES = oauth_module.APPS_USER_SCOPES
DeviceAuthorization = oauth_module.DeviceAuthorization
OAuthTokens = oauth_module.OAuthTokens
Topic6UserAuthorization = oauth_module.Topic6UserAuthorization
UserAuthorizationRequired = oauth_module.UserAuthorizationRequired
PipelineStore = pipeline_store.PipelineStore
Topic6Gateway = gateway_module.Topic6Gateway
Topic6Runner = runner_module.Topic6Runner
RunnerConfig = runner_module.Topic6Config


class FakeArk:
    def __init__(self):
        self.vaults = []
        self.credentials = []
        self.created_vaults = []
        self.created_credentials = []
        self.deleted_credentials = []
        self.updated = []
        self.session_vault_ids = None

    async def list_vaults(self):
        return list(self.vaults)

    async def create_vault(self, name):
        vault_id = f"vlt-{len(self.vaults) + 1}"
        self.vaults.append({"id": vault_id, "display_name": name})
        self.created_vaults.append(name)
        return vault_id

    async def list_credentials(self, vault_id):
        return [item for item in self.credentials if item["vault_id"] == vault_id]

    async def create_environment_variable_credential(
        self, vault_id, display_name, secret_name, secret_value
    ):
        credential_id = f"cred-{len(self.credentials) + 1}"
        item = {
            "id": credential_id,
            "vault_id": vault_id,
            "display_name": display_name,
            "secret_name": secret_name,
        }
        self.credentials.append(item)
        self.created_credentials.append(
            (vault_id, display_name, secret_name, secret_value)
        )
        return credential_id

    async def create_env_oauth_credential(
        self, vault_id, display_name, secret_name, access_token, expires_at_iso,
        *, token_endpoint, client_id, client_secret, refresh_token, scope="",
    ):
        credential_id = f"cred-{len(self.credentials) + 1}"
        item = {
            "id": credential_id,
            "vault_id": vault_id,
            "display_name": display_name,
            "secret_name": secret_name,
            "type": "env_oauth",
        }
        self.credentials.append(item)
        self.created_credentials.append(
            (vault_id, display_name, secret_name, "env_oauth")
        )
        return credential_id

    async def delete_credential(self, vault_id, credential_id):
        self.deleted_credentials.append((vault_id, credential_id))
        self.credentials = [
            c for c in self.credentials
            if not (c.get("vault_id") == vault_id and c.get("id") == credential_id)
        ]

    async def update_environment_credential(
        self, vault_id, credential_id, secret_value
    ):
        self.updated.append((vault_id, credential_id, secret_value))

    async def create_session(
        self,
        _agent_id,
        _environment_id,
        vault_ids=None,
        env_overrides=None,
        resources=None,
    ):
        _ = env_overrides, resources
        self.session_vault_ids = vault_ids
        return "sesn-topic6"


class FakeOAuth:
    def __init__(self, open_id="ou-scanned"):
        self.open_id = open_id
        self.poll_count = 0
        self.refresh_count = 0
        self.requested_scopes = []
        self.app_id = "fake-app-id"
        self.app_secret = "fake-app-secret"

    async def begin(self, scopes):
        self.requested_scopes.append(scopes)
        return DeviceAuthorization(
            "device-code",
            "https://accounts.example/device",
            int(time.time() * 1000) + 60_000,
            0,
        )

    async def poll(self, _device):
        self.poll_count += 1
        return OAuthTokens(
            f"access-{self.poll_count}",
            f"refresh-{self.poll_count}",
            int(time.time() * 1000) + 7_200_000,
        )

    async def get_user_open_id(self, _access_token):
        return self.open_id

    async def refresh(self, _refresh_token):
        self.refresh_count += 1
        return OAuthTokens(
            "access-refreshed",
            "refresh-rotated",
            int(time.time() * 1000) + 7_200_000,
        )


async def test_authorize_auto_detects_open_id_and_is_idempotent(tmp_path):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    ark = FakeArk()
    oauth = FakeOAuth()
    authorization = Topic6UserAuthorization(store, ark, oauth)
    shown = []

    assert await authorization.authorize(
        on_device_ready=lambda device: shown.append(device.verification_url)
    ) == "ou-scanned"
    assert await authorization.authorize() == "ou-scanned"

    saved = store.get_user_oauth("ou-scanned")
    assert oauth.requested_scopes == [APPS_USER_SCOPES, APPS_USER_SCOPES]
    assert shown == ["https://accounts.example/device"]
    assert saved["refresh_token"] == "refresh-2"
    assert saved["scopes"] == APPS_USER_SCOPES
    # 存真实过期时间（不再是平台托管的 0）
    assert saved["expires_at"] > int(time.time() * 1000)
    assert len(ark.created_vaults) == 1
    # 两次授权都建 environment_variable 凭据；第二次先删掉同名旧凭据
    assert [c[3] for c in ark.created_credentials] == ["access-1", "access-2"]
    assert len(ark.deleted_credentials) == 1
    assert ark.updated == []
    store.close()


async def test_expected_open_id_mismatch_writes_nothing(tmp_path):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    ark = FakeArk()
    authorization = Topic6UserAuthorization(store, ark, FakeOAuth("ou-other"))

    with pytest.raises(RuntimeError, match="expected-open-id"):
        await authorization.authorize(expected_open_id="ou-expected")

    assert store.get_user_oauth("ou-other") is None
    assert ark.created_vaults == []
    store.close()


def _seed(store, expires_at):
    store.save_user_oauth(
        "ou-scanned", "vlt-existing", "cred-existing", "refresh-old",
        expires_at, APPS_USER_SCOPES,
    )


async def test_near_expiry_token_is_refreshed_in_place(tmp_path):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    _seed(store, int(time.time() * 1000) + 60_000)  # 1 分钟后过期，进入刷新窗口
    ark = FakeArk()
    oauth = FakeOAuth()
    authorization = Topic6UserAuthorization(store, ark, oauth)

    assert await authorization.vault_id("ou-scanned") == "vlt-existing"
    assert oauth.refresh_count == 1
    assert ark.updated == [("vlt-existing", "cred-existing", "access-refreshed")]
    saved = store.get_user_oauth("ou-scanned")
    assert saved["refresh_token"] == "refresh-rotated"
    assert saved["credential_id"] == "cred-existing"
    assert saved["expires_at"] > int(time.time() * 1000) + 3_600_000
    store.close()


async def test_fresh_token_is_not_refreshed(tmp_path):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    _seed(store, int(time.time() * 1000) + 3_600_000)
    oauth = FakeOAuth()
    authorization = Topic6UserAuthorization(store, FakeArk(), oauth)

    assert await authorization.refresh_if_needed("ou-scanned") == "vlt-existing"
    assert oauth.refresh_count == 0
    store.close()


async def test_concurrent_refresh_updates_same_credential_once(tmp_path):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    _seed(store, int(time.time() * 1000) + 60_000)
    ark = FakeArk()
    oauth = FakeOAuth()
    authorization = Topic6UserAuthorization(store, ark, oauth)

    vault_ids = await asyncio.gather(
        authorization.vault_id("ou-scanned"),
        authorization.refresh_if_needed("ou-scanned"),
        authorization.vault_id("ou-scanned"),
    )

    assert vault_ids == ["vlt-existing"] * 3
    assert oauth.refresh_count == 1
    assert ark.updated == [("vlt-existing", "cred-existing", "access-refreshed")]
    store.close()


async def test_legacy_env_oauth_credential_is_migrated(tmp_path):
    """expires_at==0 的历史 env_oauth 凭据：刷新后删旧建 environment_variable，Vault 不变。"""
    store = PipelineStore(str(tmp_path / "topic6.db"))
    _seed(store, oauth_module.PLATFORM_MANAGED_EXPIRES_AT)
    ark = FakeArk()
    ark.credentials.append({
        "id": "cred-existing", "vault_id": "vlt-existing",
        "display_name": oauth_module.USER_CREDENTIAL_NAME,
        "secret_name": oauth_module.USER_ACCESS_TOKEN_ENV, "type": "env_oauth",
    })
    authorization = Topic6UserAuthorization(store, ark, FakeOAuth())

    assert await authorization.refresh_if_needed("ou-scanned") == "vlt-existing"
    assert ark.deleted_credentials == [("vlt-existing", "cred-existing")]
    assert ark.created_credentials[-1][3] == "access-refreshed"
    saved = store.get_user_oauth("ou-scanned")
    assert saved["vault_id"] == "vlt-existing"
    assert saved["credential_id"] != "cred-existing"
    assert saved["refresh_token"] == "refresh-rotated"
    assert saved["expires_at"] > oauth_module.EXPIRED_RETRY_AT
    store.close()


async def test_legacy_migration_refresh_failure_requires_rescan(tmp_path):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    _seed(store, oauth_module.PLATFORM_MANAGED_EXPIRES_AT)

    class DeadOAuth(FakeOAuth):
        async def refresh(self, _refresh_token):
            raise RuntimeError("20037 refresh token expired")

    ark = FakeArk()
    authorization = Topic6UserAuthorization(store, ark, DeadOAuth())
    with pytest.raises(UserAuthorizationRequired, match="重新扫码"):
        await authorization.refresh_if_needed("ou-scanned")
    assert ark.created_credentials == [] and ark.deleted_credentials == []
    assert store.get_user_oauth("ou-scanned")["refresh_token"] == "refresh-old"
    store.close()


async def test_rotated_refresh_token_saved_before_ark_write(tmp_path):
    """refresh_token 一次性：写 Ark 失败也必须保住新 refresh_token，下一轮删旧建新重试。"""
    store = PipelineStore(str(tmp_path / "topic6.db"))
    _seed(store, int(time.time() * 1000) + 60_000)

    class BrokenArk(FakeArk):
        async def update_environment_credential(self, *_args):
            raise RuntimeError("ark 503")

    authorization = Topic6UserAuthorization(store, BrokenArk(), FakeOAuth())
    with pytest.raises(RuntimeError, match="ark 503"):
        await authorization.refresh_if_needed("ou-scanned")
    saved = store.get_user_oauth("ou-scanned")
    assert saved["refresh_token"] == "refresh-rotated"
    assert saved["expires_at"] == oauth_module.EXPIRED_RETRY_AT

    # 下一轮：Ark 恢复，按"上次写失败"删旧建新
    ark = FakeArk()
    oauth = FakeOAuth()
    authorization = Topic6UserAuthorization(store, ark, oauth)
    await authorization.refresh_if_needed("ou-scanned")
    assert oauth.refresh_count == 1
    assert ark.created_credentials[-1][3] == "access-refreshed"
    assert store.get_user_oauth("ou-scanned")["expires_at"] > int(time.time() * 1000)
    store.close()


def _refresher_gateway(user_auth, *, users=(), ark=None, runner=None, tokens=None):
    class PipelineState:
        def list_user_oauth(self):
            return [{"open_id": u} for u in users]

    runner = runner or SimpleNamespace(_ark=ark, add_vault_id=lambda _v: None)
    gateway = Topic6Gateway(
        SimpleNamespace(
            authorized_open_ids=(), feishu_app_id="cli-app", feishu_app_secret="sec",
            lark_bot_vault_id="",
        ),
        SimpleNamespace(), runner, SimpleNamespace(), user_auth, None, None,
        pipeline_store=PipelineState(),
    )
    return gateway


async def test_refresher_tick_refreshes_bot_and_all_users(monkeypatch):
    calls = []
    added = []

    class UserAuth:
        async def refresh_if_needed(self, open_id):
            calls.append(open_id)
            if open_id == "ou-bad":
                raise UserAuthorizationRequired("expired")
            return "vlt"

    async def fake_fetch(app_id, app_secret):
        calls.append(("fetch", app_id))
        return SimpleNamespace(value="t-1", expires_in=7200)

    async def fake_ensure(_ark, app_id, token, name_hint):
        calls.append(("ensure", app_id, token, name_hint))
        return "vlt-bot"

    async def fake_update(_ark, vault_id, token):
        calls.append(("update", vault_id, token))

    monkeypatch.setattr(gateway_module, "fetch_feishu_tenant_access_token", fake_fetch)
    monkeypatch.setattr(gateway_module, "ensure_lark_cli_vault", fake_ensure)
    monkeypatch.setattr(gateway_module, "update_lark_cli_vault_token", fake_update)
    runner = SimpleNamespace(_ark=object(), add_vault_id=added.append)
    gateway = _refresher_gateway(UserAuth(), users=("ou-bad", "ou-ok"), runner=runner)

    await gateway.refresh_tokens_once()
    # 首轮：置备 bot Vault 并挂到 Runner；用户逐个刷新，单个失败不影响其他人
    assert ("ensure", "cli-app", "t-1", "topic6-bot") in calls
    assert added == ["vlt-bot"]
    assert calls[-2:] == ["ou-bad", "ou-ok"]

    # 第二轮：token 还很新，不重复申请
    calls.clear()
    await gateway.refresh_tokens_once()
    assert calls == ["ou-bad", "ou-ok"]

    # 剩余 <30min：原地更新同一个 Vault
    gateway._bot_token_expires_at = time.time() + 600
    calls.clear()
    await gateway.refresh_tokens_once()
    assert ("update", "vlt-bot", "t-1") in calls


async def test_refresher_loop_survives_failures(monkeypatch):
    ticks = 0

    async def flaky_tick():
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise RuntimeError("boom")
        if ticks == 3:
            raise asyncio.CancelledError

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(gateway_module.asyncio, "sleep", no_sleep)
    gateway = _refresher_gateway(SimpleNamespace())
    gateway.refresh_tokens_once = flaky_tick
    with pytest.raises(asyncio.CancelledError):
        await gateway._token_refresher_loop()
    assert ticks == 3


async def test_missing_authorization_triggers_in_chat_flow_and_rejects_on_failure():
    replies = []

    class Store:
        def complete_event(self, *_args):
            pass

    class Runner:
        async def start_job(self, **_kwargs):
            raise AssertionError("授权失败时不应创建任务")

    class Hitl:
        async def handle_remark_message(self, **_kwargs):
            return False

    class UserAuth:
        async def vault_id(self, _open_id):
            raise UserAuthorizationRequired("请先扫码授权")

        async def authorize(self, *, expected_open_id="", on_device_ready=None):
            raise RuntimeError("飞书 OAuth 网络错误")

    async def reply(_chat_id, text):
        replies.append(text)

    gateway = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=()),
        Store(),
        Runner(),
        Hitl(),
        UserAuth(),
        reply,
        None,
    )
    await gateway._process(_message())
    assert any("授权" in r for r in replies)
    assert any("失败" in r or "错误" in r for r in replies)


async def test_gateway_passes_scanned_users_vault_to_runner():
    calls = []
    keepalive_calls = []
    artifact_calls = []
    job = SimpleNamespace(job_id="job-1")

    class Store:
        def complete_event(self, *_args):
            pass

    class Runner:
        async def start_job(self, **kwargs):
            calls.append(kwargs)
            return job

    class Hitl:
        async def handle_remark_message(self, **_kwargs):
            return False

    class UserAuth:
        async def vault_id(self, open_id):
            assert open_id == "ou-scanned"
            return "vlt-user"

    async def reply(_chat_id, _text):
        pass

    class ArtifactSync:
        def start(self, started_job, loop):
            artifact_calls.append((started_job.job_id, loop))

    gateway = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=()),
        Store(),
        Runner(),
        Hitl(),
        UserAuth(),
        reply,
        None,
        artifact_sync=ArtifactSync(),
    )
    await gateway._process(_message())
    assert calls[0]["user_vault_id"] == "vlt-user"
    assert artifact_calls == [("job-1", None)]


async def test_gateway_bypasses_user_oauth_when_explicitly_configured():
    calls = []
    keepalive_calls = []
    job = SimpleNamespace(job_id="job-1")

    class Store:
        def complete_event(self, *_args):
            pass

    class Runner:
        async def start_job(self, **kwargs):
            calls.append(kwargs)
            return job

    class Hitl:
        async def handle_remark_message(self, **_kwargs):
            return False

    class UserAuth:
        async def vault_id(self, _open_id):
            raise AssertionError("bypass mode must not request user authorization")

    async def reply(_chat_id, _text):
        pass

    gateway = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=(), skip_miaoda_oauth=True),
        Store(),
        Runner(),
        Hitl(),
        UserAuth(),
        reply,
        None,
    )
    await gateway._process(_message())

    assert calls[0]["user_vault_id"] == ""


async def test_continue_command_passes_trailing_text_to_runner():
    calls = []
    replies = []

    class Store:
        def complete_event(self, *_args):
            pass

    class Runner:
        async def continue_job(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(job_id="job-1")

    async def reply(_chat_id, text):
        replies.append(text)

    gateway = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=()),
        Store(),
        Runner(),
        SimpleNamespace(),
        SimpleNamespace(),
        reply,
        None,
    )

    await gateway._process(
        replace(_message(), text="/continue 请优先检查异常热点")
    )

    assert calls == [{
        "chat_id": "chat-1",
        "thread_id": "",
        "user_open_id": "ou-scanned",
        "message": "请优先检查异常热点",
    }]
    assert "补充消息已注入" in replies[0]


async def test_new_command_stops_poller_and_runs_final_artifact_sync():
    calls = []
    replies = []
    job = SimpleNamespace(job_id="job-1", ma_session_id="session-1")

    class Store:
        def complete_event(self, *_args):
            pass

    class Runner:
        async def cancel_active_job(self, **_kwargs):
            return job

    class ArtifactSync:
        def stop(self, job_id):
            calls.append(("stop", job_id))

        async def sync_once(self, job_id, session_id):
            calls.append(("sync", job_id, session_id))

    async def reply(_chat_id, text):
        replies.append(text)

    gateway = Topic6Gateway(
        SimpleNamespace(authorized_open_ids=()),
        Store(),
        Runner(),
        SimpleNamespace(),
        SimpleNamespace(),
        reply,
        None,
        artifact_sync=ArtifactSync(),
    )

    await gateway._process(replace(_message(), text="/new"))

    assert calls == [
        ("stop", "job-1"),
        ("sync", "job-1", "session-1"),
    ]
    assert "已取消当前任务" in replies[0]


async def test_runner_mounts_static_and_user_vaults_on_new_session(tmp_path):
    class Sender:
        def send_interactive_card(self, _chat_id, _card):
            return ""

    store = PipelineStore(str(tmp_path / "topic6.db"))
    ark = FakeArk()
    runner = Topic6Runner(
        ark,
        Sender(),
        store,
        RunnerConfig(
            coordinator_agent_id="agent-1",
            environment_id="env-1",
            vault_ids=("vlt-static",),
        ),
    )
    runner._spawn_stream = lambda *_args, **_kwargs: object()

    await runner.start_job(
        chat_id="chat-1",
        thread_id="",
        user_open_id="ou-scanned",
        mode="demo",
        user_message="热点周报 demo",
        user_vault_id="vlt-user",
    )

    assert ark.session_vault_ids == ["vlt-static", "vlt-user"]
    store.close()


def _message() -> IncomingMessage:
    return IncomingMessage(
        event_id="event-1",
        message_id="message-1",
        chat_id="chat-1",
        chat_type="p2p",
        thread_id="",
        user_open_id="ou-scanned",
        user_name="扫码用户",
        tenant_key="tenant-1",
        text="热点周报 demo",
        mentioned_bot=False,
    )
