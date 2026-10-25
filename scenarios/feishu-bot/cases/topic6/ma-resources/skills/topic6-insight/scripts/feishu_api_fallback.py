#!/usr/bin/env python3
"""飞书 Open API 直连 fallback —— 当 lark-cli 不可用时替代其 drive 操作。

仅在 publish_report_to_feishu.py 检测到 lark-cli 不可用时被 import，
提供与 lark-cli 等价的 5 个 step 函数。

依赖: httpx (沙箱 pip 已安装)、LARK_APP_ID + LARK_APP_SECRET 环境变量。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

FEISHU_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}


def _get_tenant_access_token() -> str:
    """获取 tenant_access_token (自动缓存, 提前 60s 刷新)."""
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > now + 60:
        return _TOKEN_CACHE["token"]

    app_id = os.environ.get("LARK_APP_ID") or os.environ.get("LARKSUITE_CLI_APP_ID") or ""
    app_secret = os.environ.get("LARK_APP_SECRET") or ""
    if not app_id or not app_secret:
        raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET 未注入，无法获取 tenant_access_token")

    resp = httpx.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", -1) != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")

    _TOKEN_CACHE["token"] = data["tenant_access_token"]
    _TOKEN_CACHE["expires_at"] = now + data.get("expire", 7200)
    return _TOKEN_CACHE["token"]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_tenant_access_token()}",
        "Content-Type": "application/json",
    }


def _check_response(resp: httpx.Response, step_name: str, *, raise_on_http: bool = True) -> dict:
    """统一校验飞书 API 响应."""
    if raise_on_http:
        resp.raise_for_status()
    data = resp.json()
    code = data.get("code", -1)
    if code != 0:
        msg = data.get("msg", json.dumps(data, ensure_ascii=False)[:300])
        raise RuntimeError(f"[{step_name}] 飞书 API 错误 code={code}: {msg}")
    return data


def step_create_folder(folder_name: str) -> str:
    """创建飞书文件夹, 返回 folder_token."""
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/files/create_folder",
        headers=_headers(),
        json={"name": folder_name, "folder_token": ""},
        timeout=30,
    )
    data = _check_response(resp, "创建文件夹")
    token = data.get("data", {}).get("token", "")
    if not token:
        raise RuntimeError(f"[创建文件夹] 响应缺少 token: {json.dumps(data)[:200]}")
    return token


def step_import_document(
    report_path: str, folder_token: str, doc_name: str,
) -> tuple[str, str]:
    """上传报告文件到飞书文件夹，返回 (file_token, file_url)。

    import_tasks API 需要额外权限，改为直接 upload_all 上传文件。
    审核人可在飞书中预览/下载。
    """
    file_path = Path(report_path)
    if not file_path.exists():
        raise RuntimeError(f"[导入文档] 文件不存在: {report_path}")

    content = file_path.read_bytes()
    file_size = len(content)
    file_name = f"{doc_name}.md" if not doc_name.endswith(".md") else doc_name
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/files/upload_all",
        headers=headers,
        data={
            "file_name": file_name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": str(file_size),
        },
        files={"file": (file_name, content, "text/markdown")},
        timeout=60,
    )
    data = _check_response(resp, "上传报告文件")
    file_token = (data.get("data") or {}).get("file_token", "")
    if not file_token:
        raise RuntimeError(f"[导入文档] 响应缺少 file_token: {json.dumps(data)[:200]}")
    doc_url = f"https://bluefocus.feishu.cn/file/{file_token}"
    return file_token, doc_url


def step_grant_operator(doc_token: str, operator_open_id: str) -> None:
    """授予发起人 edit 权限."""
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members?type=file",
        headers=_headers(),
        json={
            "member_type": "openid",
            "member_id": operator_open_id,
            "perm": "edit",
        },
        timeout=15,
    )
    _check_response(resp, "授权发起人")


def step_transfer_owner(doc_token: str, operator_open_id: str) -> None:
    """转移文档所有权给发起人."""
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members/transfer_owner"
        f"?type=file",
        headers=_headers(),
        json={
            "member_type": "openid",
            "member_id": operator_open_id,
            "remove_old_owner": False,
            "old_owner_perm": "full_access",
        },
        timeout=15,
    )
    _check_response(resp, "转移所有权")


def step_grant_admins(doc_token: str, admin_emails: tuple[str, ...]) -> None:
    """授予 admin 邮箱 full_access."""
    for email in admin_emails:
        resp = httpx.post(
            f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members?type=file",
            headers=_headers(),
            json={
                "member_type": "email",
                "member_id": email,
                "perm": "full_access",
            },
            timeout=15,
        )
        _check_response(resp, f"授权 admin {email}")
