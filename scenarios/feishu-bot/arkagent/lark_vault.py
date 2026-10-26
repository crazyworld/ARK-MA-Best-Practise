"""lark-cli Bot 身份的 Vault 置备：飞书 tenant token 交换 + Ark Vault 凭据写入/原地刷新。

沙箱内 lark-cli 走 env-provider 模式，Bot 身份只需要环境变量
LARKSUITE_CLI_TENANT_ACCESS_TOKEN。App Secret 只留在 Gateway/Bot 主机，主机换得短期
tenant token 后写入 environment_variable 类型的 Vault 凭据；Ark 每次使用凭据时重新解析，
原地更新即对下一次请求生效，无需重启 Session。

本模块与具体 case 无关，数字员工与 topic6 共用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

FEISHU_TENANT_TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
)
LARK_CLI_CREDENTIAL_NAME = "lark-cli-bot-tenant-access-token"
LARK_CLI_SECRET_ENV_NAME = "LARKSUITE_CLI_TENANT_ACCESS_TOKEN"


@dataclass(frozen=True)
class FeishuTenantToken:
    value: str
    expires_in: int


async def fetch_feishu_tenant_access_token(
    app_id: str,
    app_secret: str,
    client: Optional[httpx.AsyncClient] = None,
) -> FeishuTenantToken:
    """在 Bot 主机交换短期 tenant token；App Secret 不进入方舟 Vault/沙箱。"""
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await http.post(
            FEISHU_TENANT_TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"飞书 tenant token 接口 HTTP {response.status_code}"
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise RuntimeError(
            f"获取飞书 tenant access token 失败：{type(error).__name__}"
        ) from error
    finally:
        if owns_client:
            await http.aclose()

    code = payload.get("code")
    token = str(payload.get("tenant_access_token") or "")
    try:
        expires_in = int(payload.get("expire") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if code != 0 or not token or expires_in <= 0:
        message = str(payload.get("msg") or "unknown error")[:200]
        raise RuntimeError(f"获取飞书 tenant access token 失败：code={code}, msg={message}")
    return FeishuTenantToken(token, expires_in)


def _sanitize_name(value: str) -> str:
    """把任意串清成方舟资源名允许的 [a-z0-9-]（对齐源仓库 sanitizeName）。空则回退 agent。"""
    cleaned = "".join(ch if (ch.isalnum() or ch == "-") else "-" for ch in value.lower())
    return cleaned.strip("-") or "agent"


async def ensure_lark_cli_vault(
    ark,
    feishu_app_id: str,
    tenant_access_token: str,
    name_hint: str = "group-bot",
) -> str:
    """建（或复用）一个只含短期 tenant token 的 Vault，返回 vault_id。

    Vault 的 environment_variable 只适合把值原样放进请求头，不能在沙箱内拿 opaque
    placeholder 做 JSON body token 交换。因此 App Secret 留在 Bot 主机，主机换得的短期 token
    存入 LARKSUITE_CLI_TENANT_ACCESS_TOKEN，并由调用方定期刷新。
    """
    vault_name = _sanitize_name(
        f"ark-{name_hint}-tenant-token-v3-{feishu_app_id}"
    )[:100]
    vault_id = ""
    for vault in await ark.list_vaults():
        if vault.get("display_name") == vault_name:
            vault_id = vault["id"]
            break
    if not vault_id:
        vault_id = await ark.create_vault(vault_name)

    for cred in await ark.list_credentials(vault_id):
        if cred.get("display_name") == LARK_CLI_CREDENTIAL_NAME and cred.get("auth_type") == "environment_variable":
            if cred.get("secret_name") == LARK_CLI_SECRET_ENV_NAME:
                await ark.update_environment_credential(
                    vault_id, cred["id"], tenant_access_token
                )
                return vault_id
            await ark.delete_credential(vault_id, cred["id"])
            break
    await ark.create_environment_variable_credential(
        vault_id,
        LARK_CLI_CREDENTIAL_NAME,
        LARK_CLI_SECRET_ENV_NAME,
        tenant_access_token,
    )
    return vault_id


async def update_lark_cli_vault_token(
    ark, vault_id: str, tenant_access_token: str
) -> None:
    """原地刷新现有 token 凭据，保持 Vault 和 Credential ID 稳定。"""
    for cred in await ark.list_credentials(vault_id):
        if (
            cred.get("display_name") == LARK_CLI_CREDENTIAL_NAME
            and cred.get("auth_type") == "environment_variable"
            and cred.get("secret_name") == LARK_CLI_SECRET_ENV_NAME
        ):
            await ark.update_environment_credential(
                vault_id, cred["id"], tenant_access_token
            )
            return
    raise RuntimeError(f"Vault {vault_id} 缺少 lark-cli tenant token 凭据")
