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


def _check_response(resp: httpx.Response, step_name: str) -> dict:
    """统一校验飞书 API 响应."""
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
    """导入 markdown 为飞书 docx, 返回 (doc_token, doc_url).

    分两步: 1) 创建导入任务  2) 轮询任务完成
    """
    file_path = Path(report_path)
    if not file_path.exists():
        raise RuntimeError(f"[导入文档] 文件不存在: {report_path}")

    # Step 1: 上传文件到 import_tasks
    content = file_path.read_bytes()
    headers = {"Authorization": f"Bearer {_get_tenant_access_token()}"}
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/import_tasks",
        headers=headers,
        data={
            "file_extension": "md",
            "file_token": "",
            "type": "docx",
            "file_name": doc_name,
            "point": json.dumps({"mount_type": 1, "mount_key": folder_token}),
        },
        files={"file": (file_path.name, content, "text/markdown")},
        timeout=60,
    )
    data = _check_response(resp, "创建导入任务")
    ticket = data.get("data", {}).get("ticket", "")
    if not ticket:
        raise RuntimeError(f"[导入文档] 响应缺少 ticket: {json.dumps(data)[:200]}")

    # Step 2: 轮询导入任务
    for _ in range(30):  # 最多等 60s
        time.sleep(2)
        poll_resp = httpx.get(
            f"{FEISHU_BASE}/drive/v1/import_tasks/{ticket}",
            headers=_headers(),
            timeout=15,
        )
        poll_data = _check_response(poll_resp, "查询导入任务")
        result = poll_data.get("data", {}).get("result", {})
        if result.get("token"):
            doc_token = result["token"]
            doc_url = result.get("url", f"https://bytedance.feishu.cn/docx/{doc_token}")
            return doc_token, doc_url
        job_status = poll_data.get("data", {}).get("job_status", 0)
        if job_status in (3, 4):  # 3=failed, 4=cancelled
            error_msg = poll_data.get("data", {}).get("job_error_msg", "unknown")
            raise RuntimeError(f"[导入文档] 任务失败: status={job_status} msg={error_msg}")

    raise RuntimeError("[导入文档] 轮询超时（60s），任务仍未完成")


def step_grant_operator(doc_token: str, operator_open_id: str) -> None:
    """授予发起人 edit 权限."""
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members?type=docx",
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
        f"?type=docx",
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
            f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members?type=docx",
            headers=_headers(),
            json={
                "member_type": "email",
                "member_id": email,
                "perm": "full_access",
            },
            timeout=15,
        )
        _check_response(resp, f"授权 admin {email}")
