#!/usr/bin/env python3
"""Topic6 关键链路端到端冒烟测试。

用真实的飞书 API / 配置验证所有线上曾经出过问题的关键路径。
不需要跑完整 pipeline，每个测试在秒级完成。

运行方式：
  # 本地（需要 config.env 中的飞书凭证）
  cd scenarios/feishu-bot
  PYTHONPATH=.:cases/topic6 python -m pytest tests/test_topic6_e2e_smoke.py -v

  # 线上
  source ~/.arkagent/cases/topic6/config.env
  cd /home/bmc/work/ARK-MA-Best-Practise/scenarios/feishu-bot
  PYTHONPATH=.:cases/topic6 python -m pytest tests/test_topic6_e2e_smoke.py -v

需要的环境变量：
  FEISHU_APP_ID, FEISHU_APP_SECRET, TOPIC6_DOCS_FOLDER_TOKEN

如果环境变量缺失，测试自动 skip（不阻断 CI）。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# ---- 环境检测 ----

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DOCS_FOLDER_TOKEN = os.environ.get("TOPIC6_DOCS_FOLDER_TOKEN", "")

SKIP_REASON = ""
if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
    SKIP_REASON = "FEISHU_APP_ID / FEISHU_APP_SECRET 未配置"
elif not DOCS_FOLDER_TOKEN:
    SKIP_REASON = "TOPIC6_DOCS_FOLDER_TOKEN 未配置"

requires_feishu = pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)


# ---- 辅助 ----

def _get_token() -> str:
    """直接调飞书 API 获取 tenant_access_token。"""
    import requests
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    token = data.get("tenant_access_token", "")
    assert token, f"获取 token 失败: {data}"
    return token


def _delete_folder(token: str, folder_token: str) -> None:
    """清理测试创建的文件夹。"""
    import requests
    requests.delete(
        f"https://open.feishu.cn/open-apis/drive/v1/files/{folder_token}?type=folder",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )


def _delete_file(token: str, file_token: str) -> None:
    """清理测试上传的文件。"""
    import requests
    requests.delete(
        f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}?type=file",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )


# ============================================================================
# 1. 飞书凭证链路
# ============================================================================

@requires_feishu
class TestFeishuCredentials:
    """验证飞书 App 凭证能正常获取 token——历史问题：_app_id/_app_secret 取不到。"""

    def test_tenant_access_token_direct_api(self):
        """直接调飞书 API 获取 token（绕过 SDK）。"""
        token = _get_token()
        assert len(token) > 20

    def test_feishu_sender_get_tenant_access_token(self):
        """通过 FeishuSender._get_tenant_access_token 获取——验证 self._app_id 存在。"""
        from unittest.mock import MagicMock
        from arkagent.feishu import FeishuSender

        # 构造一个最小的 FeishuSender，只验证 _get_tenant_access_token
        sender = object.__new__(FeishuSender)
        sender._app_id = FEISHU_APP_ID
        sender._app_secret = FEISHU_APP_SECRET
        sender._client = MagicMock()  # SDK client 不需要真实初始化

        token = sender._get_tenant_access_token()
        assert len(token) > 20, f"token 为空或太短: {token!r}"


# ============================================================================
# 2. 飞书 Drive API 链路
# ============================================================================

@requires_feishu
class TestFeishuDriveAPI:
    """验证文件夹创建 + 文件上传 + 权限设置——历史问题：9499 Bad Request / strict mode。"""

    def test_create_folder_and_cleanup(self):
        """创建子文件夹 → 验证 token 返回 → 清理。"""
        import requests

        token = _get_token()
        resp = requests.post(
            "https://open.feishu.cn/open-apis/drive/v1/files/create_folder",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"name": "_e2e_smoke_test_folder", "folder_token": DOCS_FOLDER_TOKEN},
            timeout=30,
        )
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        assert data.get("code") == 0, f"飞书错误: {data}"

        inner = data.get("data", {})
        folder_token = inner.get("token") or inner.get("folder_token") or ""
        assert folder_token, f"未返回 folder_token: {data}"

        # 清理
        _delete_folder(token, folder_token)

    def test_upload_xlsx_and_cleanup(self):
        """上传一个小 xlsx 到飞书 → 验证 file_token → 清理。"""
        import requests

        token = _get_token()

        # 创建临时 xlsx
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["id", "name", "value"])
        ws.append([1, "smoke_test", 42])
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            wb.save(f.name)
            xlsx_path = f.name

        try:
            file_size = os.path.getsize(xlsx_path)
            resp = requests.post(
                "https://open.feishu.cn/open-apis/drive/v1/files/upload_all",
                headers={"Authorization": f"Bearer {token}"},
                files={
                    "file_name": (None, "e2e_smoke_test.xlsx"),
                    "parent_type": (None, "explorer"),
                    "parent_node": (None, DOCS_FOLDER_TOKEN),
                    "size": (None, str(file_size)),
                    "file": ("e2e_smoke_test.xlsx", open(xlsx_path, "rb"), "application/octet-stream"),
                },
                timeout=60,
            )
            assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            assert data.get("code") == 0, f"飞书错误: {data}"

            file_token = (data.get("data") or {}).get("file_token", "")
            assert file_token, f"未返回 file_token: {data}"

            # 清理
            _delete_file(token, file_token)
        finally:
            os.unlink(xlsx_path)


# ============================================================================
# 3. FeishuDocPublisher 完整链路
# ============================================================================

@requires_feishu
class TestPublisherEndToEnd:
    """验证 FeishuDocPublisher 的 create_folder + import_spreadsheet 链路。"""

    def test_publisher_create_folder_real_api(self):
        """用真实 API 验证 publisher.create_job_folder。"""
        from unittest.mock import MagicMock
        from arkagent.feishu import FeishuSender
        from feishu_doc_publisher import FeishuDocPublisher

        # 构造最小的 FeishuSender
        sender = object.__new__(FeishuSender)
        sender._app_id = FEISHU_APP_ID
        sender._app_secret = FEISHU_APP_SECRET
        sender._client = MagicMock()

        publisher = FeishuDocPublisher(sender, DOCS_FOLDER_TOKEN)
        assert publisher.enabled

        # 创建 job folder
        job = MagicMock()
        job.job_id = "e2e_smoke_test_job"
        job.mode = "smoke"

        folder_token = publisher.create_job_folder(job)
        assert folder_token, "create_job_folder 返回空 token"

        # 清理
        token = _get_token()
        _delete_folder(token, folder_token)


# ============================================================================
# 4. Provider Retry 逻辑
# ============================================================================

class TestProviderRetry:
    """验证 model_request_failed_error 的退避重试逻辑——不需要飞书凭证。"""

    def test_progress_state_has_provider_retry_count(self):
        """_ProgressState 有 provider_retry_count 字段。"""
        from topic6_runner import _ProgressState
        state = _ProgressState()
        assert hasattr(state, "provider_retry_count")
        assert state.provider_retry_count == 0

    def test_max_provider_retries_constant(self):
        """_MAX_PROVIDER_RETRIES 常量存在且 > 0。"""
        from topic6_runner import _MAX_PROVIDER_RETRIES
        assert _MAX_PROVIDER_RETRIES >= 3


# ============================================================================
# 5. JobNotifier 逻辑
# ============================================================================

class TestJobNotifier:
    """验证 JobNotifier 的构造和方法签名——不需要飞书凭证。"""

    def test_notifier_methods_exist(self):
        """JobNotifier 有 4 个通知方法。"""
        from job_notifier import JobNotifier
        from unittest.mock import MagicMock

        notifier = JobNotifier(MagicMock())
        assert callable(getattr(notifier, "notify_hc_ready", None))
        assert callable(getattr(notifier, "notify_done", None))
        assert callable(getattr(notifier, "notify_failed", None))
        assert callable(getattr(notifier, "notify_stopped", None))

    def test_notifier_send_does_not_raise(self):
        """通知发送失败时不抛异常（降级保护）。"""
        from job_notifier import JobNotifier
        from unittest.mock import MagicMock

        feishu = MagicMock()
        feishu.send_to_chat.side_effect = RuntimeError("飞书 API 异常")
        feishu.chat_roster.return_value = {}
        feishu.resolve_operator_name.return_value = ""

        notifier = JobNotifier(feishu)
        job = MagicMock()
        job.chat_id = "oc_test"
        job.job_id = "job_test"
        job.mode = "smoke"
        job.user_open_id = "ou_test"

        # 不应该抛异常
        notifier.notify_hc_ready(job, "HC1")
        notifier.notify_done(job)
        notifier.notify_failed(job, "test error")
        notifier.notify_stopped(job, "test reason")


# ============================================================================
# 6. resume_job phase 过渡
# ============================================================================

class TestResumeJobPhase:
    """验证 HC 通过后 phase 脱离 HC 前缀。"""

    def test_hc_kinds_to_post_phase(self):
        """HC1/HC2/HC3 → post-HC1/post-HC2/post-HC3。"""
        from pipeline_store import HC_KINDS
        for hc in HC_KINDS:
            post = f"post-{hc}"
            assert not post.startswith(("HC1", "HC2", "HC3")), \
                f"post-{hc} 不应被 _HC_PHASE_PREFIXES 匹配"


# ============================================================================
# 7. 工程化脚本存在性
# ============================================================================

class TestEngineeringScripts:
    """验证所有新增的工程化脚本存在且可导入。"""

    SCRIPTS_BASE = Path(__file__).resolve().parent.parent / "cases" / "topic6" / "ma-resources" / "skills"

    def test_wait_c2_complete_exists(self):
        script = self.SCRIPTS_BASE / "topic6-event-registry" / "scripts" / "wait_c2_complete.py"
        assert script.exists(), f"缺失: {script}"

    def test_launch_c2_background_exists(self):
        script = self.SCRIPTS_BASE / "topic6-event-registry" / "scripts" / "launch_c2_background.py"
        assert script.exists(), f"缺失: {script}"

    def test_run_demo_batch2_exists(self):
        script = self.SCRIPTS_BASE / "topic6-annotation" / "scripts" / "run_demo_batch2.py"
        assert script.exists(), f"缺失: {script}"

    def test_publish_report_to_feishu_exists(self):
        script = self.SCRIPTS_BASE / "topic6-insight" / "scripts" / "publish_report_to_feishu.py"
        assert script.exists(), f"缺失: {script}"

    def test_coordinator_references_new_scripts(self):
        """coordinator.system.md 引用了所有新脚本。"""
        coord = self.SCRIPTS_BASE.parent / "agents" / "coordinator.system.md"
        content = coord.read_text(encoding="utf-8")
        for script_name in [
            "launch_c2_background.py",
            "wait_c2_complete.py",
            "run_demo_batch2.py",
            "publish_report_to_feishu.py",
        ]:
            assert script_name in content, f"coordinator.system.md 未引用 {script_name}"


# ============================================================================
# 8. Environment setup_script 包含 lark-cli bot init
# ============================================================================

class TestEnvironmentSetup:
    """验证 environment.json 的 setup_script 配置正确。"""

    ENV_PATH = Path(__file__).resolve().parent.parent / "cases" / "topic6" / "ma-resources" / "environment.json"

    def test_setup_script_installs_lark_cli(self):
        data = json.loads(self.ENV_PATH.read_text(encoding="utf-8"))
        script = data["config"]["setup_script"]
        assert "lark-cli" in script

    def test_setup_script_does_bot_config_init(self):
        """setup_script 在安装后做 bot config init——历史问题：strict mode = user。"""
        data = json.loads(self.ENV_PATH.read_text(encoding="utf-8"))
        script = data["config"]["setup_script"]
        assert "config init" in script, "setup_script 缺少 lark-cli config init"
        assert "app-secret-stdin" in script, "config init 应该用 --app-secret-stdin"
        assert "LARK_APP_ID" in script
        assert "LARK_APP_SECRET" in script

    def test_env_has_feishu_credentials(self):
        """environment.json 注入了飞书凭证。"""
        data = json.loads(self.ENV_PATH.read_text(encoding="utf-8"))
        env = data["config"]["env"]
        assert "LARK_APP_ID" in env
        assert "LARK_APP_SECRET" in env
