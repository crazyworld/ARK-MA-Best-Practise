#!/usr/bin/env python3
"""飞书 Open API 直连 fallback —— lark-cli 主路径不可用时由 publish_report_to_feishu.py 调用。

提供与 lark-cli 主路径等价的 step 函数，产物同样是 /docx/ 在线文档：
  upload_all 上传 md 源文件 → import_tasks 转 docx（轮询完成）→ 删除 md 源文件
  （必须在任何所有权转移之前删除，否则会留下 bot 无法删除的残留文件）。

依赖: httpx（沙箱 pip 已安装）、LARK_APP_ID + LARK_APP_SECRET 环境变量。
不打印任何 token / secret。
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
IMPORT_POLL_INTERVAL_S = 1.5
IMPORT_POLL_TIMEOUT_S = 120
_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}


def _get_tenant_access_token() -> str:
    """获取 tenant_access_token（自动缓存，提前 60s 刷新）。"""
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
    data = _check_response(resp, "获取 tenant_access_token")
    _TOKEN_CACHE["token"] = data["tenant_access_token"]
    _TOKEN_CACHE["expires_at"] = now + data.get("expire", 7200)
    return _TOKEN_CACHE["token"]


def _headers(json_body: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_get_tenant_access_token()}"}
    if json_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    return headers


def _check_response(resp: httpx.Response, step_name: str) -> dict:
    """统一校验飞书 API 响应；HTTP 非 2xx 时也尽量带出飞书 code/msg。"""
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"[{step_name}] HTTP {resp.status_code}: {resp.text[:300]}") from None
    code = data.get("code", -1)
    if resp.status_code >= 400 or code != 0:
        msg = data.get("msg") or json.dumps(data, ensure_ascii=False)[:300]
        raise RuntimeError(f"[{step_name}] 飞书 API 错误 HTTP {resp.status_code} code={code}: {msg}")
    return data


def step_create_folder(folder_name: str) -> str:
    """在应用云空间根目录创建文件夹，返回 folder_token。"""
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/files/create_folder",
        headers=_headers(),
        json={"name": folder_name, "folder_token": ""},
        timeout=30,
    )
    data = _check_response(resp, "创建文件夹")
    token = (data.get("data") or {}).get("token", "")
    if not token:
        raise RuntimeError(f"[创建文件夹] 响应缺少 token: {json.dumps(data, ensure_ascii=False)[:200]}")
    return token


def _upload_source(file_path: Path, folder_token: str) -> str:
    content = file_path.read_bytes()
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/files/upload_all",
        headers=_headers(json_body=False),
        data={
            "file_name": file_path.name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": str(len(content)),
        },
        files={"file": (file_path.name, content, "text/markdown")},
        timeout=60,
    )
    data = _check_response(resp, "上传导入源文件")
    file_token = (data.get("data") or {}).get("file_token", "")
    if not file_token:
        raise RuntimeError(f"[上传导入源文件] 响应缺少 file_token: {json.dumps(data, ensure_ascii=False)[:200]}")
    return file_token


def _delete_source(file_token: str) -> None:
    resp = httpx.request(
        "DELETE",
        f"{FEISHU_BASE}/drive/v1/files/{file_token}",
        params={"type": "file"},
        headers=_headers(json_body=False),
        timeout=15,
    )
    _check_response(resp, "删除导入源文件")


def _wait_import(ticket: str) -> tuple[str, str]:
    deadline = time.monotonic() + IMPORT_POLL_TIMEOUT_S
    last: dict = {}
    while True:
        resp = httpx.get(
            f"{FEISHU_BASE}/drive/v1/import_tasks/{ticket}",
            headers=_headers(json_body=False),
            timeout=15,
        )
        last = (_check_response(resp, "查询导入任务").get("data") or {}).get("result") or {}
        status = last.get("job_status")
        if status == 0:
            token, url = last.get("token", ""), last.get("url", "")
            if not token or not url:
                raise RuntimeError(f"[导入文档] 导入完成但缺少 token/url: {json.dumps(last, ensure_ascii=False)[:200]}")
            return token, url
        if status not in (1, 2, None):
            raise RuntimeError(
                f"[导入文档] 导入任务失败 job_status={status}: {last.get('job_error_msg', '')}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(f"[导入文档] 导入任务超时（{IMPORT_POLL_TIMEOUT_S}s），最后状态: {status}")
        time.sleep(IMPORT_POLL_INTERVAL_S)


def step_import_document(report_path: str, folder_token: str, doc_name: str) -> tuple[str, str]:
    """把 md 导入为 docx 在线文档，返回 (doc_token, doc_url)。

    无论导入成功与否都会删除上传的 md 源文件，文件夹内不留残留。
    """
    file_path = Path(report_path)
    if not file_path.is_file():
        raise RuntimeError(f"[导入文档] 文件不存在: {report_path}")

    file_token = _upload_source(file_path, folder_token)
    try:
        resp = httpx.post(
            f"{FEISHU_BASE}/drive/v1/import_tasks",
            headers=_headers(),
            json={
                "file_extension": "md",
                "file_token": file_token,
                "type": "docx",
                "file_name": doc_name,
                "point": {"mount_type": 1, "mount_key": folder_token},
            },
            timeout=30,
        )
        ticket = (_check_response(resp, "创建导入任务").get("data") or {}).get("ticket", "")
        if not ticket:
            raise RuntimeError("[创建导入任务] 响应缺少 ticket")
        doc_token, doc_url = _wait_import(ticket)
    finally:
        try:
            _delete_source(file_token)
        except Exception as exc:  # noqa: BLE001
            # 删除失败不影响导入结果，但要带出真实原因
            print(f"⚠️ 删除导入源文件 {file_token} 失败: {exc}", file=sys.stderr)
    return doc_token, doc_url


def _add_member(doc_token: str, member_type: str, member_id: str, perm: str, step_name: str) -> None:
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members",
        params={"type": "docx", "need_notification": "false"},
        headers=_headers(),
        json={"member_type": member_type, "member_id": member_id, "perm": perm},
        timeout=15,
    )
    _check_response(resp, step_name)


def step_grant_operator(doc_token: str, operator_open_id: str) -> None:
    """授予发起人 edit 权限。"""
    _add_member(doc_token, "openid", operator_open_id, "edit", "授权发起人")


def step_transfer_owner(doc_token: str, operator_open_id: str) -> None:
    """转移文档所有权给发起人，bot 保留 full_access。"""
    resp = httpx.post(
        f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members/transfer_owner",
        params={"type": "docx", "need_notification": "false",
                "remove_old_owner": "false", "old_owner_perm": "full_access"},
        headers=_headers(),
        json={"member_type": "openid", "member_id": operator_open_id},
        timeout=15,
    )
    _check_response(resp, "转移所有权")


def step_grant_admin(doc_token: str, email: str) -> None:
    """授予单个 admin 邮箱 full_access。"""
    _add_member(doc_token, "email", email, "full_access", f"授权 admin {email}")


def step_grant_admins(doc_token: str, admin_emails: tuple[str, ...]) -> None:
    """兼容旧调用：逐个授予 admin full_access。"""
    for email in admin_emails:
        step_grant_admin(doc_token, email)
