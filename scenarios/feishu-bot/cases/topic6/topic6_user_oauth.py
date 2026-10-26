"""Topic6 妙搭用户 OAuth、每用户 Vault 与短期 token 刷新。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

log = logging.getLogger("arkagent.case.topic6.oauth")

USER_ACCESS_TOKEN_ENV = "LARKSUITE_CLI_USER_ACCESS_TOKEN"
USER_CREDENTIAL_NAME = "topic6-miaoda-user-access-token"
APPS_USER_SCOPES = (
    "offline_access",
    "auth:user.id:read",
    "spark:app:read",
    "spark:app:write",
)
MAX_VAULT_SECRET_BYTES = 4096
# Gateway 常驻协程在过期前 15 分钟刷新；Ark 每次使用凭据都重新解析，更新即生效。
REFRESH_EARLY_MS = 15 * 60_000
# 任务触发时强制刷新：比任何 access_token 有效期都长的窗口，等价于"总是刷新"。
FORCE_REFRESH_MARGIN_MS = 365 * 24 * 3600_000
# refresh_token 剩余不足 2 天时告警，提醒即将需要重新扫码。
REFRESH_TOKEN_WARN_MS = 2 * 24 * 3600_000
# 飞书 OAuth token endpoint（v2 接口，返回标准 JSON）。
FEISHU_TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
# expires_at == 0：历史 env_oauth 凭据（平台按需刷新）。飞书 refresh_token 一次性且约 7 天
# 有效，周报每周只跑一次会断链，因此统一迁回 environment_variable + Gateway 常驻刷新。
PLATFORM_MANAGED_EXPIRES_AT = 0
# Feishu 已轮换 refresh_token 但写 Ark 失败时的占位：下一轮立即用新 refresh_token 重试。
EXPIRED_RETRY_AT = 1
# 常驻刷新对同一用户连续失败达到该次数，视为 refresh_token 已失效：清空本地 refresh_token
# （保留 Vault/Credential ID 供重新授权复用），常驻协程不再重试，下次触发走聊天内扫码。
MAX_REFRESH_FAILURES = 3


class UserAuthorizationRequired(RuntimeError):
    """当前消息发送者尚无可用的妙搭用户授权。"""


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int = 0


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    verification_url: str
    expires_at: int
    interval_s: float


class FeishuOAuth:
    """飞书 OAuth 2.0 Device Flow 的最小客户端。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = client

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def app_secret(self) -> str:
        return self._app_secret

    async def begin(
        self, scopes: tuple[str, ...] = APPS_USER_SCOPES
    ) -> DeviceAuthorization:
        payload = await self._request(
            "POST",
            "https://accounts.feishu.cn/oauth/v1/device_authorization",
            data={
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "scope": " ".join(scopes),
            },
        )
        device_code = _string(payload, "device_code")
        verification_url = (
            _string(payload, "verification_uri_complete")
            or _string(payload, "verification_uri")
        )
        if not device_code or not verification_url:
            raise RuntimeError("飞书 Device Flow 响应缺少授权地址或 device_code")
        return DeviceAuthorization(
            device_code=device_code,
            verification_url=verification_url,
            expires_at=_now_ms() + _positive_int(payload, "expires_in", 600) * 1000,
            interval_s=float(_positive_int(payload, "interval", 5)),
        )

    async def poll(self, device: DeviceAuthorization) -> OAuthTokens:
        interval_s = device.interval_s
        while _now_ms() < device.expires_at:
            response = await self._post_json(
                "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self._app_id,
                    "client_secret": self._app_secret,
                    "device_code": device.device_code,
                },
            )
            payload = _json(response)
            if response.is_success and payload.get("access_token"):
                return _parse_tokens(payload)
            error = str(payload.get("error") or payload.get("msg") or "unknown_error")
            if error == "authorization_pending":
                await asyncio.sleep(interval_s)
                continue
            if error == "slow_down":
                interval_s += 5
                await asyncio.sleep(interval_s)
                continue
            raise _oauth_error(response.status_code, payload)
        raise RuntimeError("飞书用户授权链接已过期，请重新运行授权脚本")

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        payload = await self._request(
            "POST",
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "refresh_token": refresh_token,
            },
        )
        return _parse_tokens(payload, refresh_token)

    async def get_user_open_id(self, access_token: str) -> str:
        payload = await self._request(
            "GET",
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        open_id = _string(payload, "open_id") or _string(data, "open_id")
        if not open_id:
            raise RuntimeError("飞书用户信息响应缺少 open_id")
        return open_id

    async def _post_json(self, url: str, body: dict) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, json=body)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, json=body)

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        if self._client is not None:
            response = await self._client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(method, url, **kwargs)
        payload = _json(response)
        if (
            not response.is_success
            or payload.get("error")
            or (isinstance(payload.get("code"), int) and payload["code"] != 0)
        ):
            raise _oauth_error(response.status_code, payload)
        return payload


DeviceReady = Callable[[DeviceAuthorization], Optional[Awaitable[None]]]


class Topic6UserAuthorization:
    """把扫码用户的短期 token 放进独立 Ark Vault，并维护 refresh token。"""

    def __init__(self, store, ark, oauth: FeishuOAuth) -> None:
        self._store = store
        self._ark = ark
        self._oauth = oauth
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._refresh_failures: dict[str, int] = {}

    async def authorize(
        self,
        *,
        expected_open_id: str = "",
        on_device_ready: Optional[DeviceReady] = None,
    ) -> str:
        device = await self._oauth.begin(APPS_USER_SCOPES)
        if on_device_ready is not None:
            result = on_device_ready(device)
            if inspect.isawaitable(result):
                await result
        tokens = await self._oauth.poll(device)
        _validate_access_token_size(tokens.access_token)
        open_id = await self._oauth.get_user_open_id(tokens.access_token)
        if expected_open_id and open_id != expected_open_id:
            raise RuntimeError(
                "扫码账号与 --expected-open-id 不一致，未写入任何凭据"
            )
        credential = await self._ensure_credential(open_id, tokens.access_token)
        self._store.save_user_oauth(
            open_id,
            credential["vault_id"],
            credential["credential_id"],
            tokens.refresh_token,
            tokens.expires_at,
            APPS_USER_SCOPES,
        )
        self._refresh_failures.pop(open_id, None)
        return open_id

    async def vault_id(self, open_id: str, *, force_refresh: bool = False) -> str:
        """返回该用户可用的 Vault；临近过期时先刷新（与常驻协程共用同一把锁）。

        force_refresh=True：任务触发时用，无论本地记录的过期时间都真实刷新一次，
        确认 refresh_token 仍有效；失败抛 UserAuthorizationRequired，由 Gateway 当场
        发起聊天内扫码，而不是跑到 Phase H 才暴露授权失效。
        """
        margin = FORCE_REFRESH_MARGIN_MS if force_refresh else REFRESH_EARLY_MS
        return await self.refresh_if_needed(open_id, margin_ms=margin)

    async def refresh_in_background(self, open_id: str) -> None:
        """常驻协程入口：连续失败 MAX_REFRESH_FAILURES 次后标记为需重新授权、停止重试。"""
        try:
            await self.refresh_if_needed(open_id)
        except UserAuthorizationRequired:
            failures = self._refresh_failures.get(open_id, 0) + 1
            self._refresh_failures[open_id] = failures
            if failures >= MAX_REFRESH_FAILURES:
                self.mark_reauthorization_required(open_id)
            raise
        self._refresh_failures.pop(open_id, None)

    def mark_reauthorization_required(self, open_id: str) -> None:
        """清空本地 refresh_token；保留 Vault/Credential ID，重新扫码时原样复用。"""
        current = self._store.get_user_oauth(open_id)
        self._refresh_failures.pop(open_id, None)
        if not current or not current.get("refresh_token"):
            return
        self._store.save_user_oauth(
            open_id, current["vault_id"], current["credential_id"], "",
            current["expires_at"], tuple(current["scopes"]),
        )
        log.warning(
            "topic6 user oauth marked reauthorization_required user=%s "
            "(refresh failed %d times); 常驻刷新停止，下次触发将发起扫码授权",
            open_id, MAX_REFRESH_FAILURES,
        )

    async def refresh_if_needed(
        self, open_id: str, *, margin_ms: int = REFRESH_EARLY_MS
    ) -> str:
        """常驻刷新入口：未到刷新窗口直接返回；历史 env_oauth 凭据顺带迁移。

        refresh_token 只能用一次：飞书刷新成功后**先**落库新 refresh_token，再写 Ark。
        若写 Ark 失败，expires_at 记为 EXPIRED_RETRY_AT，下一轮用新 refresh_token 重试，
        不会丢失授权链（access_token 不落 SQLite）。
        """
        current = self._store.get_user_oauth(open_id)
        if not current or not current.get("refresh_token"):
            raise UserAuthorizationRequired(
                "当前飞书账号尚未授权妙搭发布。请先在 Gateway 主机运行 "
                "`python cases/topic6/authorize_miaoda_user.py` 完成扫码授权。"
            )
        if _is_fresh(current, margin_ms):
            return current["vault_id"]

        lock = self._refresh_locks.setdefault(open_id, asyncio.Lock())
        async with lock:
            current = self._store.get_user_oauth(open_id)
            if _is_fresh(current, margin_ms):
                return current["vault_id"]
            # 历史 env_oauth 或上次写 Ark 失败：删旧建新，保证凭据类型正确
            legacy = current["expires_at"] in (PLATFORM_MANAGED_EXPIRES_AT, EXPIRED_RETRY_AT)
            try:
                tokens = await self._oauth.refresh(current["refresh_token"])
                _validate_access_token_size(tokens.access_token)
            except Exception as error:
                raise UserAuthorizationRequired(
                    "当前飞书账号的妙搭授权已失效，需要重新扫码授权。"
                    "请重新发送触发词，按提示完成授权。"
                ) from error
            scopes = tuple(current["scopes"])
            # 先落库新 refresh_token（旧的已失效），expires_at 暂记为待重试。
            self._store.save_user_oauth(
                open_id, current["vault_id"], current["credential_id"],
                tokens.refresh_token, EXPIRED_RETRY_AT, scopes,
            )
            if legacy:
                credential = await self._ensure_credential(open_id, tokens.access_token)
                credential_id = credential["credential_id"]
                log.info(
                    "topic6 user credential migrated env_oauth -> environment_variable "
                    "user=%s vault=%s credential=%s",
                    open_id, current["vault_id"], credential_id,
                )
            else:
                await self._ark.update_environment_credential(
                    current["vault_id"], current["credential_id"], tokens.access_token,
                )
                credential_id = current["credential_id"]
            self._store.save_user_oauth(
                open_id, current["vault_id"], credential_id,
                tokens.refresh_token, tokens.expires_at, scopes,
            )
            if tokens.refresh_expires_at and (
                tokens.refresh_expires_at - _now_ms() < REFRESH_TOKEN_WARN_MS
            ):
                log.warning(
                    "topic6 user refresh_token 即将过期 user=%s expires_at=%s，届时需要重新扫码",
                    open_id, tokens.refresh_expires_at,
                )
            log.info(
                "topic6 user token refreshed user=%s vault=%s credential=%s expires_at=%s",
                open_id, current["vault_id"], credential_id, tokens.expires_at,
            )
            return current["vault_id"]

    async def _ensure_credential(self, open_id: str, access_token: str) -> dict[str, str]:
        """确保用户 Vault 中有 environment_variable 凭据（删旧建新，兼容历史 env_oauth）。

        凭据类型/secret_name 是结构性字段，不可原地改，所以统一删掉同名旧凭据后重建；
        Vault ID 保持不变（Session 只能在创建时挂 Vault）。
        """
        current = self._store.get_user_oauth(open_id)
        vault_id = current["vault_id"] if current else ""
        if not vault_id:
            safe_open_id = re.sub(r"[^a-z0-9-]+", "-", open_id.lower()).strip("-")
            vault_name = f"ark-topic6-miaoda-user-{safe_open_id or 'user'}"[:100]
            vault_id = next(
                (
                    str(vault["id"])
                    for vault in await self._ark.list_vaults()
                    if vault.get("display_name") == vault_name
                ),
                "",
            ) or await self._ark.create_vault(vault_name)

        for credential in await self._ark.list_credentials(vault_id):
            if credential.get("secret_name") != USER_ACCESS_TOKEN_ENV:
                continue
            try:
                await self._ark.delete_credential(vault_id, str(credential["id"]))
            except Exception:  # noqa: BLE001 - 旧凭据可能已不存在
                log.warning(
                    "topic6 delete stale credential failed vault=%s cred=%s",
                    vault_id, credential.get("id"),
                )
        credential_id = await self._ark.create_environment_variable_credential(
            vault_id, USER_CREDENTIAL_NAME, USER_ACCESS_TOKEN_ENV, access_token,
        )
        return {"vault_id": vault_id, "credential_id": credential_id}


def _is_fresh(current: Optional[dict], margin_ms: int) -> bool:
    if not current:
        return False
    expires_at = current["expires_at"]
    return expires_at != PLATFORM_MANAGED_EXPIRES_AT and expires_at - _now_ms() > margin_ms


def _validate_access_token_size(access_token: str) -> None:
    if len(access_token.encode("utf-8")) > MAX_VAULT_SECRET_BYTES:
        raise RuntimeError(
            "飞书用户令牌超过 Ark Vault 4096 字节限制，请缩减应用用户权限"
        )


def _parse_tokens(payload: dict, fallback_refresh: str = "") -> OAuthTokens:
    access_token = _string(payload, "access_token")
    refresh_token = _string(payload, "refresh_token") or fallback_refresh
    if not access_token or not refresh_token:
        raise RuntimeError(
            "飞书 OAuth 响应缺少 access_token 或 refresh_token；"
            "请确认应用已开通 offline_access"
        )
    refresh_expires_in = _positive_int(payload, "refresh_token_expires_in", 0)
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_now_ms() + _positive_int(payload, "expires_in", 7200) * 1000,
        refresh_expires_at=(_now_ms() + refresh_expires_in * 1000) if refresh_expires_in else 0,
    )


def _json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"飞书 OAuth 请求失败 HTTP {response.status_code}") from error
    return payload if isinstance(payload, dict) else {}


def _oauth_error(status: int, payload: dict) -> RuntimeError:
    detail = (
        payload.get("error_description")
        or payload.get("msg")
        or payload.get("error")
        or payload.get("code")
        or "未知错误"
    )
    return RuntimeError(f"飞书 OAuth 请求失败 {status}: {str(detail)[:180]}")


def _string(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _positive_int(payload: dict, key: str, fallback: int) -> int:
    try:
        value = int(payload.get(key))
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _now_ms() -> int:
    return int(time.time() * 1000)
