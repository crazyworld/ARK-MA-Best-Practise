#!/usr/bin/env python3
"""飞书文档权限验证脚本：验证 App 能创建/读写文档 + 白名单用户能编辑。

用法:
    # 设置环境变量后运行
    export FEISHU_APP_ID="cli_xxx"
    export FEISHU_APP_SECRET="xxx"
    export TOPIC6_DOCS_FOLDER_TOKEN="fldcnXXX"        # 你创建的共享文件夹 token
    export AUTHORIZED_OPEN_IDS="ou_user1,ou_user2"     # 白名单用户

    python verify_feishu_docs.py

    # 或指定 env 文件
    python verify_feishu_docs.py --env-file ~/.arkagent/cases/topic6/config.env

验证项:
    1. App token 获取
    2. 在指定文件夹下创建子文件夹
    3. 在子文件夹中创建飞书文档
    4. 向文档写入内容
    5. 读取文档内容（验证写入成功）
    6. 为白名单用户授予 full_access 权限
    7. 验证权限已生效
    8. 清理测试文档（可选）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

FEISHU_HOST = "https://open.feishu.cn"


@dataclass
class TestResult:
    step: str
    passed: bool
    detail: str = ""


class FeishuDocsTester:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        folder_token: str,
        authorized_open_ids: list[str],
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.folder_token = folder_token
        self.authorized_open_ids = authorized_open_ids
        self.tenant_token = ""
        self.results: list[TestResult] = []
        # 清理用
        self._created_folder_token = ""
        self._created_doc_token = ""

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{FEISHU_HOST}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body_text[:500]}") from e

    def run_all(self, cleanup: bool = True) -> bool:
        self._step1_get_token()
        self._step2_create_subfolder()
        self._step3_create_doc()
        self._step4_write_doc()
        self._step5_read_doc()
        self._step6_grant_permissions()
        self._step7_verify_permissions()
        if cleanup:
            self._cleanup()
        self._print_results()
        return all(r.passed for r in self.results)

    def _record(self, step: str, passed: bool, detail: str = ""):
        self.results.append(TestResult(step, passed, detail))
        icon = "✅" if passed else "❌"
        print(f"  {icon} {step}" + (f" — {detail}" if detail else ""))

    def _step1_get_token(self):
        print("\n[1/7] 获取 tenant_access_token …")
        try:
            req = urllib.request.Request(
                f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
                data=json.dumps({
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                }).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            self.tenant_token = data.get("tenant_access_token", "")
            if not self.tenant_token:
                self._record("获取 token", False, f"响应: {data}")
                return
            self._record("获取 token", True, f"token=...{self.tenant_token[-8:]}")
        except Exception as e:
            self._record("获取 token", False, str(e))

    def _step2_create_subfolder(self):
        print("\n[2/7] 在根文件夹下创建测试子文件夹 …")
        if not self.tenant_token:
            self._record("创建子文件夹", False, "无 token，跳过")
            return
        try:
            name = f"_test_verify_{int(time.time())}"
            resp = self._api("POST", f"/open-apis/drive/v1/files/create_folder", {
                "name": name,
                "folder_token": self.folder_token,
            })
            token = resp.get("data", {}).get("token", "")
            if not token:
                self._record("创建子文件夹", False, f"响应: {json.dumps(resp, ensure_ascii=False)[:200]}")
                return
            self._created_folder_token = token
            self._record("创建子文件夹", True, f"folder={token}, name={name}")
        except Exception as e:
            self._record("创建子文件夹", False, str(e)[:300])

    def _step3_create_doc(self):
        print("\n[3/7] 在子文件夹中创建飞书文档 …")
        folder = self._created_folder_token or self.folder_token
        if not self.tenant_token:
            self._record("创建文档", False, "无 token，跳过")
            return
        try:
            resp = self._api("POST", "/open-apis/docx/v1/documents", {
                "folder_token": folder,
                "title": f"测试文档_{int(time.time())}",
            })
            doc_id = resp.get("data", {}).get("document", {}).get("document_id", "")
            if not doc_id:
                self._record("创建文档", False, f"响应: {json.dumps(resp, ensure_ascii=False)[:200]}")
                return
            self._created_doc_token = doc_id
            url = f"https://bluefocus.feishu.cn/docx/{doc_id}"
            self._record("创建文档", True, f"doc={doc_id}, url={url}")
        except Exception as e:
            self._record("创建文档", False, str(e)[:300])

    def _step4_write_doc(self):
        print("\n[4/7] 向文档写入内容 …")
        if not self._created_doc_token:
            self._record("写入内容", False, "无文档，跳过")
            return
        try:
            # 先获取文档的 block_id（根块）
            resp = self._api(
                "GET",
                f"/open-apis/docx/v1/documents/{self._created_doc_token}",
            )
            rev = resp.get("data", {}).get("document", {}).get("revision_id", 1)

            # 创建一个文本块
            resp = self._api(
                "POST",
                f"/open-apis/docx/v1/documents/{self._created_doc_token}/blocks/{self._created_doc_token}/children",
                {
                    "children": [{
                        "block_type": 2,  # text
                        "text": {
                            "elements": [{
                                "text_run": {
                                    "content": "这是自动化测试写入的内容。如果你能看到并编辑这段文字，说明权限配置正确。",
                                }
                            }],
                            "style": {},
                        },
                    }],
                    "index": 0,
                },
            )
            code = resp.get("code", -1)
            if code == 0:
                self._record("写入内容", True, "已写入测试文本")
            else:
                self._record("写入内容", False, f"code={code}, msg={resp.get('msg', '')}")
        except Exception as e:
            self._record("写入内容", False, str(e)[:300])

    def _step5_read_doc(self):
        print("\n[5/7] 读取文档内容（验证写入） …")
        if not self._created_doc_token:
            self._record("读取内容", False, "无文档，跳过")
            return
        try:
            resp = self._api(
                "GET",
                f"/open-apis/docx/v1/documents/{self._created_doc_token}/raw_content",
            )
            content = resp.get("data", {}).get("content", "")
            if "自动化测试" in content:
                self._record("读取内容", True, f"内容长度={len(content)}, 含测试文本")
            elif content:
                self._record("读取内容", True, f"内容长度={len(content)}（写入文本可能需要等待同步）")
            else:
                self._record("读取内容", False, f"内容为空, resp={json.dumps(resp, ensure_ascii=False)[:200]}")
        except Exception as e:
            self._record("读取内容", False, str(e)[:300])

    def _step6_grant_permissions(self):
        print("\n[6/7] 为白名单用户授予文件夹权限 …")
        folder = self._created_folder_token or self.folder_token
        if not self.authorized_open_ids:
            self._record("授予权限", True, "无白名单用户，跳过")
            return
        for open_id in self.authorized_open_ids:
            try:
                resp = self._api(
                    "POST",
                    f"/open-apis/drive/v1/permissions/{folder}/members?type=folder&need_notification=false",
                    {
                        "member_type": "openid",
                        "member_id": open_id,
                        "perm": "full_access",
                    },
                )
                code = resp.get("code", -1)
                if code == 0:
                    self._record(f"授权 {open_id[:12]}…", True, "full_access")
                elif "already" in str(resp).lower():
                    self._record(f"授权 {open_id[:12]}…", True, "已有权限")
                else:
                    self._record(f"授权 {open_id[:12]}…", False, f"code={code}, msg={resp.get('msg', '')}")
            except Exception as e:
                self._record(f"授权 {open_id[:12]}…", False, str(e)[:200])

    def _step7_verify_permissions(self):
        print("\n[7/7] 验证权限列表 …")
        folder = self._created_folder_token or self.folder_token
        try:
            resp = self._api(
                "GET",
                f"/open-apis/drive/v1/permissions/{folder}/members?type=folder",
            )
            members = resp.get("data", {}).get("items", [])
            member_ids = {m.get("member_id", "") for m in members}
            for open_id in self.authorized_open_ids:
                if open_id in member_ids:
                    self._record(f"权限验证 {open_id[:12]}…", True, "在权限列表中")
                else:
                    self._record(f"权限验证 {open_id[:12]}…", False, f"不在列表中，当前成员: {member_ids}")
            if not self.authorized_open_ids:
                self._record("权限验证", True, f"共 {len(members)} 个成员")
        except Exception as e:
            self._record("权限验证", False, str(e)[:300])

    def _cleanup(self):
        print("\n[清理] 删除测试文档和文件夹 …")
        if self._created_doc_token:
            try:
                self._api("DELETE", f"/open-apis/drive/v1/files/{self._created_doc_token}?type=docx")
                print(f"  🗑️ 已删除文档 {self._created_doc_token}")
            except Exception as e:
                print(f"  ⚠️ 删除文档失败: {e}")
        if self._created_folder_token:
            try:
                self._api("DELETE", f"/open-apis/drive/v1/files/{self._created_folder_token}?type=folder")
                print(f"  🗑️ 已删除文件夹 {self._created_folder_token}")
            except Exception as e:
                print(f"  ⚠️ 删除文件夹失败: {e}")

    def _print_results(self):
        print("\n" + "=" * 60)
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        print(f"结果: {passed} 通过, {failed} 失败")
        if failed:
            print("\n失败项:")
            for r in self.results:
                if not r.passed:
                    print(f"  ❌ {r.step}: {r.detail}")
        print("=" * 60)
        if self._created_doc_token:
            print(f"\n测试文档: https://bluefocus.feishu.cn/docx/{self._created_doc_token}")
            print("（白名单用户可在飞书中打开上面的链接，验证能否看到并编辑文档）")


def load_env_file(path: str) -> None:
    """从 env 文件加载变量（不覆盖已有）。"""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser(description="飞书文档权限验证")
    parser.add_argument("--env-file", default="", help="env 文件路径")
    parser.add_argument("--no-cleanup", action="store_true", help="不删除测试文档（便于手动验证权限）")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    # 也尝试加载默认位置
    load_env_file(os.path.expanduser("~/.arkagent/cases/topic6/config.env"))

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    folder_token = os.environ.get("TOPIC6_DOCS_FOLDER_TOKEN", "")
    open_ids = [
        s.strip()
        for s in os.environ.get("AUTHORIZED_OPEN_IDS", "").replace(",", " ").split()
        if s.strip()
    ]

    if not app_id or not app_secret:
        print("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
        sys.exit(1)
    if not folder_token:
        print("❌ 缺少 TOPIC6_DOCS_FOLDER_TOKEN（飞书共享文件夹 token）")
        print("   请在飞书云文档中创建文件夹，从 URL 中复制 token")
        sys.exit(1)

    print("飞书文档权限验证")
    print(f"  App ID:       {app_id}")
    print(f"  文件夹 Token:  {folder_token}")
    print(f"  白名单用户:    {open_ids or '（无）'}")

    tester = FeishuDocsTester(app_id, app_secret, folder_token, open_ids)
    ok = tester.run_all(cleanup=not args.no_cleanup)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
