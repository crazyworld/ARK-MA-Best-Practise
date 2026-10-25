#!/usr/bin/env python3
"""Phase F: Publish merged report to Feishu as an online document.

Replaces the 60-line lark-cli shell sequence that Agents had to compose manually.
Encapsulates the full flow: create folder → import doc → grant operator edit →
transfer owner → grant admins full_access → validate every JSON response.

Requires lark-cli configured with bot identity (--as bot).

Exit codes:
  0 = published successfully
  1 = failed (details in stderr + JSON output)

Usage:
  python publish_report_to_feishu.py \
    --project-dir "{project_dir}" \
    --report-file "07_报告/热点报告_2026-W42_v1.md" \
    --folder-name "热点周报_2026-W42_full" \
    --doc-name "热点报告_2026-W42"

Output (JSON to stdout):
  {
    "status": "success",
    "doc_token": "...",
    "doc_url": "https://...",
    "folder_token": "..."
  }
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


# Default admin emails for full_access
DEFAULT_ADMIN_EMAILS = (
    "xiuyuan.zhao@bluefocus.com",
    "jiesong.yuan@bluefocus.com",
)


def _resolve_project_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path("/workspace") / path


def _run_lark_cli(args: list[str], step_name: str) -> dict:
    """Run a lark-cli command, parse JSON response, validate ok == true."""
    cmd = ["lark-cli"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"[{step_name}] lark-cli 未安装或不在 PATH 中。"
            f"请确认 Environment setup_script 已安装 lark-cli。"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"[{step_name}] lark-cli 超时（60s）")

    # lark-cli may return 0 even on business error, must check JSON
    output = result.stdout.strip()
    if result.returncode != 0 and not output:
        raise RuntimeError(
            f"[{step_name}] lark-cli 退出码 {result.returncode}: {result.stderr.strip()[:300]}"
        )

    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(
            f"[{step_name}] lark-cli 输出不是有效 JSON: {output[:300]}"
        )

    if data.get("ok") is not True:
        code = data.get("code", "?")
        msg = data.get("msg", data.get("message", output[:200]))
        raise RuntimeError(f"[{step_name}] 飞书 API 错误 code={code}: {msg}")

    return data


def _ensure_lark_cli_configured() -> None:
    """Ensure lark-cli is initialized with bot credentials."""
    app_id = os.environ.get("LARK_APP_ID") or os.environ.get("LARKSUITE_CLI_APP_ID") or ""
    app_secret = os.environ.get("LARK_APP_SECRET") or ""

    if not app_id or not app_secret:
        raise RuntimeError(
            "LARK_APP_ID / LARK_APP_SECRET 未注入，无法初始化 lark-cli。"
        )

    # Check if already configured by running a simple command
    try:
        result = subprocess.run(
            ["lark-cli", "config", "show"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and app_id in result.stdout:
            return  # Already configured
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Configure: pipe app_secret via stdin to avoid it appearing in args/logs
    try:
        proc = subprocess.run(
            [
                "lark-cli", "config", "init",
                "--app-id", app_id,
                "--app-secret-stdin",
                "--brand", "feishu",
            ],
            input=app_secret,
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            # 方舟沙箱环境中凭据由外部注入，config init 不可用——只要
            # LARKSUITE_CLI_STRICT_MODE 环境变量已设置就可以正常工作。
            if os.environ.get("LARKSUITE_CLI_STRICT_MODE"):
                return  # 外部注入凭据 + strict-mode 环境变量，无需 config init
            raise RuntimeError(
                f"lark-cli config init 失败: {proc.stderr.strip()[:200]}\n"
                "提示: 在 Environment env 中设置 LARKSUITE_CLI_STRICT_MODE=bot 可跳过 config init"
            )
    except FileNotFoundError:
        raise RuntimeError("lark-cli 未安装")


def step_create_folder(folder_name: str) -> str:
    """Step F-2a: Create a Feishu folder for the report. Returns folder_token."""
    data = _run_lark_cli(
        ["drive", "+create-folder", "--name", folder_name, "--as", "bot"],
        "创建文件夹",
    )
    token = (
        data.get("folder_token")
        or data.get("data", {}).get("folder_token")
        or ""
    )
    if not token:
        raise RuntimeError(f"[创建文件夹] 响应缺少 folder_token: {json.dumps(data)[:200]}")
    return token


def step_import_document(
    report_path: str, folder_token: str, doc_name: str,
) -> tuple[str, str]:
    """Step F-2b: Import markdown as Feishu docx. Returns (doc_token, doc_url)."""
    data = _run_lark_cli(
        [
            "drive", "+import",
            "--file", report_path,
            "--type", "docx",
            "--folder-token", folder_token,
            "--name", doc_name,
            "--as", "bot",
        ],
        "导入文档",
    )
    doc_token = data.get("token") or data.get("data", {}).get("token") or ""
    doc_url = data.get("url") or data.get("data", {}).get("url") or ""
    if not doc_token:
        raise RuntimeError(f"[导入文档] 响应缺少 doc token: {json.dumps(data)[:200]}")
    if not doc_url:
        raise RuntimeError(f"[导入文档] 响应缺少文档 URL: {json.dumps(data)[:200]}")
    return doc_token, doc_url


def step_grant_operator(doc_token: str, operator_open_id: str) -> None:
    """Step F-3a: Grant task operator edit permission."""
    data = _run_lark_cli(
        [
            "drive", "+member-add",
            "--token", doc_token, "--type", "docx",
            "--member-type", "openid", "--member-id", operator_open_id,
            "--perm", "edit", "--as", "bot", "--yes",
        ],
        "授权发起人",
    )
    # Validate succeeded_count
    inner = data.get("data") or {}
    succeeded = inner.get("succeeded_count", 1)
    partial = inner.get("partial", False)
    if succeeded < 1 or partial:
        raise RuntimeError(
            f"[授权发起人] 批量授权不完整: succeeded={succeeded}, partial={partial}"
        )


def step_transfer_owner(doc_token: str, operator_open_id: str) -> None:
    """Step F-3b: Transfer document ownership to operator."""
    transfer_data = json.dumps({
        "member_type": "openid",
        "member_id": operator_open_id,
    })
    _run_lark_cli(
        [
            "drive", "permission.members", "transfer_owner",
            "--token", doc_token, "--type", "docx",
            "--data", transfer_data,
            "--old-owner-perm", "full_access",
            "--as", "bot", "--yes",
        ],
        "转移所有权",
    )


def step_grant_admins(doc_token: str, admin_emails: tuple[str, ...]) -> None:
    """Step F-3c: Grant admin emails full_access."""
    for email in admin_emails:
        data = _run_lark_cli(
            [
                "drive", "+member-add",
                "--token", doc_token, "--type", "docx",
                "--member-type", "email", "--member-id", email,
                "--perm", "full_access", "--as", "bot", "--yes",
            ],
            f"授权 admin {email}",
        )
        inner = data.get("data") or {}
        succeeded = inner.get("succeeded_count", 1)
        partial = inner.get("partial", False)
        if succeeded < 1 or partial:
            raise RuntimeError(
                f"[授权 admin {email}] 不完整: succeeded={succeeded}, partial={partial}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--report-file", required=True,
                        help="项目内相对路径，如 07_报告/热点报告_2026-W42_v1.md")
    parser.add_argument("--folder-name", required=True,
                        help="飞书文件夹名，如 热点周报_2026-W42_full")
    parser.add_argument("--doc-name", required=True,
                        help="飞书文档标题，如 热点报告_2026-W42")
    parser.add_argument("--admin-emails", default=",".join(DEFAULT_ADMIN_EMAILS),
                        help="逗号分隔的 admin 邮箱")
    args = parser.parse_args()

    project = _resolve_project_dir(args.project_dir)
    report_path = project / args.report_file

    if not report_path.exists():
        result = {"status": "failed", "error": f"报告文件不存在: {report_path}"}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    operator_open_id = os.environ.get("FEISHU_USER_OPEN_ID", "").strip()
    if not operator_open_id:
        result = {"status": "failed", "error": "FEISHU_USER_OPEN_ID 未注入"}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    admin_emails = tuple(
        e.strip() for e in args.admin_emails.split(",") if e.strip()
    )

    # 尝试 lark-cli 主路径; 不可用时 fallback 到飞书 Open API 直连
    use_fallback = False
    try:
        _ensure_lark_cli_configured()
    except RuntimeError as cli_err:
        print(f"⚠️ lark-cli 不可用({cli_err}), fallback 到飞书 Open API 直连", file=sys.stderr)
        use_fallback = True

    if use_fallback:
        try:
            from feishu_api_fallback import (
                step_create_folder as fb_create_folder,
                step_import_document as fb_import_document,
                step_grant_operator as fb_grant_operator,
                step_transfer_owner as fb_transfer_owner,
                step_grant_admins as fb_grant_admins,
            )
        except ImportError as imp_err:
            result = {"status": "failed", "error": f"lark-cli 和 feishu_api_fallback 均不可用: {imp_err}"}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1

        try:
            folder_token = fb_create_folder(args.folder_name)
            print(f"✅ [fallback] 文件夹已创建: {folder_token}", file=sys.stderr)

            doc_token, doc_url = fb_import_document(
                str(report_path), folder_token, args.doc_name,
            )
            print(f"✅ [fallback] 文档已导入: {doc_url}", file=sys.stderr)

            fb_grant_operator(doc_token, operator_open_id)
            print(f"✅ [fallback] 发起人已授权 edit: {operator_open_id}", file=sys.stderr)

            fb_transfer_owner(doc_token, operator_open_id)
            print(f"✅ [fallback] 所有权已转移: {operator_open_id}", file=sys.stderr)

            fb_grant_admins(doc_token, admin_emails)
            print(f"✅ [fallback] Admin 已授权: {', '.join(admin_emails)}", file=sys.stderr)

            result = {
                "status": "success",
                "backend": "feishu_api_fallback",
                "doc_token": doc_token,
                "doc_url": doc_url,
                "folder_token": folder_token,
                "operator": operator_open_id,
                "admins": list(admin_emails),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        except Exception as err:
            result = {"status": "failed", "backend": "feishu_api_fallback", "error": str(err)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"❌ [fallback] {err}", file=sys.stderr)
            return 1

    try:
        # Step 0 already done above (lark-cli configured)

        # Step 1: Create folder
        folder_token = step_create_folder(args.folder_name)
        print(f"✅ 文件夹已创建: {folder_token}", file=sys.stderr)

        # Step 2: Import document
        doc_token, doc_url = step_import_document(
            str(report_path), folder_token, args.doc_name,
        )
        print(f"✅ 文档已导入: {doc_url}", file=sys.stderr)

        # Step 3: Grant operator edit
        step_grant_operator(doc_token, operator_open_id)
        print(f"✅ 发起人已授权 edit: {operator_open_id}", file=sys.stderr)

        # Step 4: Transfer ownership
        step_transfer_owner(doc_token, operator_open_id)
        print(f"✅ 所有权已转移: {operator_open_id}", file=sys.stderr)

        # Step 5: Grant admins
        step_grant_admins(doc_token, admin_emails)
        print(f"✅ Admin 已授权: {', '.join(admin_emails)}", file=sys.stderr)

        # Output result
        result = {
            "status": "success",
            "doc_token": doc_token,
            "doc_url": doc_url,
            "folder_token": folder_token,
            "operator": operator_open_id,
            "admins": list(admin_emails),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    except RuntimeError as err:
        result = {"status": "failed", "error": str(err)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"❌ {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
