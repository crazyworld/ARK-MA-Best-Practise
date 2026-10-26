#!/usr/bin/env python3
"""Phase F: 把合并报告发布为飞书云文档（docx）。

单一入口，Agent 不需要自行拼 lark-cli 命令。流程：
  创建文件夹 → 导入 docx → 给发起人授 edit → 转移 owner → 给 admin 授 full_access

身份与后端：
  - 主路径：lark-cli ``--as bot``。身份来自 Gateway 通过 Ark Vault 注入的
    ``LARKSUITE_CLI_APP_ID`` + ``LARKSUITE_CLI_TENANT_ACCESS_TOKEN``（env-provider 模式），
    不执行 ``config init``，不打印任何 token。
  - 先用只读接口 ``GET /open-apis/drive/explorer/v2/root_folder/meta`` 做健康探测
    （``--dry-run`` 无论 token 是否有效都返回 ok，不能作为探测）。
  - lark-cli 不可用 / 探测失败 / 在文档创建前的步骤失败 → 自动切换到
    ``feishu_api_fallback``（飞书 Open API 直连，需要 LARK_APP_ID / LARK_APP_SECRET）。
  - 文档已创建后的失败不再切 fallback（避免产生第二份文档），直接返回真实错误。

权限失败策略：
  - 给发起人授 edit 失败 → 阻断（exit 1，status=failed）
  - 转移 owner / 给 admin 授权失败 → 仅告警（写入 warnings），仍视为成功

Exit codes:
  0 = 发布成功
  1 = 失败（详情见 stdout JSON 的 error）

Usage:
  python publish_report_to_feishu.py \\
    --project-dir "{project_dir}" \\
    --report-file "07_报告/热点报告_2026-W42_v1.md" \\
    --folder-name "热点周报_2026-W42_full" \\
    --doc-name "热点报告_2026-W42"

Output (JSON to stdout):
  {"status": "success", "backend": "lark-cli" | "feishu_api_fallback",
   "doc_token": "...", "doc_url": "https://.../docx/...", "folder_token": "...",
   "operator": "ou_...", "admins": [...], "warnings": [...]}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# 默认授 full_access 的 admin 邮箱
DEFAULT_ADMIN_EMAILS = (
    "xiuyuan.zhao@bluefocus.com",
    "jiesong.yuan@bluefocus.com",
)

BACKEND_CLI = "lark-cli"
BACKEND_FALLBACK = "feishu_api_fallback"
PROBE_PATH = "/open-apis/drive/explorer/v2/root_folder/meta"


class PublishError(RuntimeError):
    """发布失败。doc_created=True 表示飞书侧已经生成了文档，不能再走 fallback。"""

    def __init__(self, message: str, *, doc_created: bool = False,
                 partial: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.doc_created = doc_created
        self.partial = partial or {}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _resolve_project_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path("/workspace") / path


def _is_docx_url(url: str) -> bool:
    return "/docx/" in (url or "")


# ---------------------------------------------------------------------------
# lark-cli 主路径
# ---------------------------------------------------------------------------

def _error_detail(data: dict, raw: str) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        parts = [f"{k}={err[k]}" for k in ("type", "subtype", "code", "message") if err.get(k)]
        if parts:
            return ", ".join(parts)
    if data.get("code") is not None or data.get("msg"):
        return f"code={data.get('code', '?')}, msg={data.get('msg') or data.get('message') or ''}"
    return raw[:300]


def _run_lark_cli(args: list[str], step_name: str, *, cwd: str | None = None,
                  timeout: int = 120) -> dict:
    """执行 lark-cli，解析 stdout JSON，要求 ok == true。

    lark-cli 的业务错误可能以退出码 0 返回，必须看 JSON；stderr 中的
    auto-grant Warning 属正常噪声，忽略。
    """
    cmd = ["lark-cli", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[{step_name}] lark-cli 未安装或不在 PATH 中") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"[{step_name}] lark-cli 超时（{timeout}s）") from exc

    output = (proc.stdout or "").strip()
    try:
        data = json.loads(output) if output else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        stderr = (proc.stderr or "").strip()[-300:]
        raise RuntimeError(
            f"[{step_name}] lark-cli 输出不是有效 JSON（exit={proc.returncode}）: "
            f"{output[:300] or stderr}"
        )
    if data.get("ok") is not True:
        raise RuntimeError(f"[{step_name}] lark-cli 失败: {_error_detail(data, output)}")
    return data


def _payload(data: dict) -> dict:
    inner = data.get("data")
    return inner if isinstance(inner, dict) else {}


def _check_member_add(data: dict, step_name: str) -> None:
    inner = _payload(data)
    succeeded = inner.get("succeeded_count", 1)
    partial = inner.get("partial", False)
    if (isinstance(succeeded, int) and succeeded < 1) or partial:
        raise RuntimeError(f"[{step_name}] 授权不完整: succeeded={succeeded}, partial={partial}")


def probe_lark_cli() -> str:
    """返回空串表示 bot 身份可用，否则返回不可用原因。"""
    if not shutil.which("lark-cli"):
        return "lark-cli 未安装或不在 PATH 中"
    if not os.environ.get("LARKSUITE_CLI_APP_ID"):
        return "LARKSUITE_CLI_APP_ID 未注入"
    try:
        _run_lark_cli(["api", "GET", PROBE_PATH, "--as", "bot"], "身份探测", timeout=30)
    except RuntimeError as exc:
        return str(exc)
    return ""


def cli_create_folder(folder_name: str) -> str:
    data = _run_lark_cli(["drive", "+create-folder", "--name", folder_name, "--as", "bot"], "创建文件夹")
    token = data.get("folder_token") or _payload(data).get("folder_token") or ""
    if not token:
        raise RuntimeError(f"[创建文件夹] 响应缺少 folder_token: {json.dumps(data, ensure_ascii=False)[:200]}")
    return token


def cli_import_document(report_path: str, folder_token: str, doc_name: str) -> tuple[str, str]:
    path = Path(report_path)
    # 在报告所在目录执行，--file 只传文件名，规避 lark-cli 对绝对路径/越界路径的限制
    data = _run_lark_cli(
        ["drive", "+import", "--file", path.name, "--type", "docx",
         "--folder-token", folder_token, "--name", doc_name, "--as", "bot"],
        "导入文档", cwd=str(path.parent), timeout=300,
    )
    inner = _payload(data)
    doc_token = data.get("token") or inner.get("token") or ""
    doc_url = data.get("url") or inner.get("url") or ""
    if not doc_token or not doc_url:
        raise RuntimeError(f"[导入文档] 响应缺少 token/url: {json.dumps(data, ensure_ascii=False)[:200]}")
    return doc_token, doc_url


def cli_grant_operator(doc_token: str, operator_open_id: str) -> None:
    data = _run_lark_cli(
        ["drive", "+member-add", "--token", doc_token, "--type", "docx",
         "--member-type", "openid", "--member-id", operator_open_id,
         "--perm", "edit", "--as", "bot", "--yes"],
        "授权发起人",
    )
    _check_member_add(data, "授权发起人")


def cli_transfer_owner(doc_token: str, operator_open_id: str) -> None:
    body = json.dumps({"member_type": "openid", "member_id": operator_open_id})
    _run_lark_cli(
        ["drive", "permission.members", "transfer_owner", "--token", doc_token,
         "--type", "docx", "--data", body, "--old-owner-perm", "full_access",
         "--as", "bot", "--yes"],
        "转移所有权",
    )


def cli_grant_admin(doc_token: str, email: str) -> None:
    data = _run_lark_cli(
        ["drive", "+member-add", "--token", doc_token, "--type", "docx",
         "--member-type", "email", "--member-id", email,
         "--perm", "full_access", "--as", "bot", "--yes"],
        f"授权 admin {email}",
    )
    _check_member_add(data, f"授权 admin {email}")


CLI_BACKEND = {
    "create_folder": cli_create_folder,
    "import_document": cli_import_document,
    "grant_operator": cli_grant_operator,
    "transfer_owner": cli_transfer_owner,
    "grant_admin": cli_grant_admin,
}


def _load_fallback_backend() -> dict:
    """延迟导入 Open API fallback（依赖 httpx）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import feishu_api_fallback as fb  # noqa: PLC0415

    return {
        "create_folder": fb.step_create_folder,
        "import_document": fb.step_import_document,
        "grant_operator": fb.step_grant_operator,
        "transfer_owner": fb.step_transfer_owner,
        "grant_admin": fb.step_grant_admin,
    }


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def run_publish(backend: dict, *, report_path: str, folder_name: str, doc_name: str,
                operator_open_id: str, admin_emails: tuple[str, ...],
                folder_token: str = "") -> dict:
    """执行一次完整发布。文档创建前失败抛 PublishError(doc_created=False)。"""
    warnings: list[str] = []
    partial: dict[str, Any] = {}
    try:
        if not folder_token:
            folder_token = backend["create_folder"](folder_name)
            _log(f"✅ 文件夹已创建: {folder_token}")
        partial["folder_token"] = folder_token
        doc_token, doc_url = backend["import_document"](report_path, folder_token, doc_name)
    except Exception as exc:  # noqa: BLE001
        raise PublishError(str(exc), doc_created=False, partial=partial) from exc

    partial.update(doc_token=doc_token, doc_url=doc_url)
    if not _is_docx_url(doc_url):
        raise PublishError(f"[导入文档] 返回的 URL 不是 /docx/ 文档: {doc_url}",
                           doc_created=True, partial=partial)
    _log(f"✅ 文档已导入: {doc_url}")

    try:
        backend["grant_operator"](doc_token, operator_open_id)
    except Exception as exc:  # noqa: BLE001
        raise PublishError(str(exc), doc_created=True, partial=partial) from exc
    _log(f"✅ 发起人已授权 edit: {operator_open_id}")

    try:
        backend["transfer_owner"](doc_token, operator_open_id)
        _log(f"✅ 所有权已转移: {operator_open_id}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"转移所有权失败: {exc}")
        _log(f"⚠️ 转移所有权失败（不阻断）: {exc}")

    for email in admin_emails:
        try:
            backend["grant_admin"](doc_token, email)
            _log(f"✅ admin 已授权 full_access: {email}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"授权 admin {email} 失败: {exc}")
            _log(f"⚠️ 授权 admin {email} 失败（不阻断）: {exc}")

    return {
        "doc_token": doc_token,
        "doc_url": doc_url,
        "folder_token": folder_token,
        "warnings": warnings,
    }


def publish(*, report_path: str, folder_name: str, doc_name: str,
            operator_open_id: str, admin_emails: tuple[str, ...]) -> tuple[int, dict]:
    base = {"operator": operator_open_id, "admins": list(admin_emails)}
    pre_warnings: list[str] = []
    reuse_folder = ""

    reason = probe_lark_cli()
    if not reason:
        try:
            out = run_publish(CLI_BACKEND, report_path=report_path, folder_name=folder_name,
                              doc_name=doc_name, operator_open_id=operator_open_id,
                              admin_emails=admin_emails)
            return 0, {"status": "success", "backend": BACKEND_CLI, **out, **base}
        except PublishError as exc:
            if exc.doc_created:
                return 1, {"status": "failed", "backend": BACKEND_CLI, "error": str(exc),
                           **exc.partial, **base}
            reason = str(exc)
            reuse_folder = exc.partial.get("folder_token", "")
    _log(f"⚠️ lark-cli 主路径不可用，切换 Open API fallback: {reason}")
    pre_warnings.append(f"lark-cli 主路径不可用，已使用 fallback: {reason}")

    try:
        backend = _load_fallback_backend()
    except Exception as exc:  # noqa: BLE001
        return 1, {"status": "failed", "backend": BACKEND_FALLBACK,
                   "error": f"lark-cli 不可用（{reason}）；fallback 加载失败: {exc}", **base}

    try:
        out = run_publish(backend, report_path=report_path, folder_name=folder_name,
                          doc_name=doc_name, operator_open_id=operator_open_id,
                          admin_emails=admin_emails, folder_token=reuse_folder)
    except PublishError as exc:
        return 1, {"status": "failed", "backend": BACKEND_FALLBACK,
                   "error": f"lark-cli 不可用（{reason}）；fallback 失败: {exc}",
                   **exc.partial, **base}
    out["warnings"] = pre_warnings + out["warnings"]
    return 0, {"status": "success", "backend": BACKEND_FALLBACK, **out, **base}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--report-file", required=True,
                        help="项目内相对路径，如 07_报告/热点报告_2026-W42_v1.md")
    parser.add_argument("--folder-name", required=True, help="飞书文件夹名，如 热点周报_2026-W42_full")
    parser.add_argument("--doc-name", required=True, help="飞书文档标题，如 热点报告_2026-W42")
    parser.add_argument("--admin-emails", default=",".join(DEFAULT_ADMIN_EMAILS),
                        help="逗号分隔的 admin 邮箱；传空串表示不授权 admin")
    args = parser.parse_args(argv)

    report_path = _resolve_project_dir(args.project_dir) / args.report_file
    if not report_path.is_file():
        print(json.dumps({"status": "failed", "error": f"报告文件不存在: {report_path}"},
                         ensure_ascii=False))
        return 1

    operator_open_id = os.environ.get("FEISHU_USER_OPEN_ID", "").strip()
    if not operator_open_id:
        print(json.dumps({"status": "failed", "error": "FEISHU_USER_OPEN_ID 未注入"}, ensure_ascii=False))
        return 1

    admin_emails = tuple(e.strip() for e in args.admin_emails.split(",") if e.strip())
    code, result = publish(report_path=str(report_path), folder_name=args.folder_name,
                           doc_name=args.doc_name, operator_open_id=operator_open_id,
                           admin_emails=admin_emails)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if code != 0:
        _log(f"❌ {result.get('error')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
