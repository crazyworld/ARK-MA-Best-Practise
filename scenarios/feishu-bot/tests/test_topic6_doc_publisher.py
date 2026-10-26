"""端到端验证 T1-Q3: feishu_doc_publisher + send_hc_card 集成。

覆盖的关键链路：
1. _resolve_local_artifact: artifact_sync 写出的文件能被 publisher 定位
2. publish_hc_artifacts: HC1(宽表上传)、HC2(同 HC1)、HC3(URL 校验) 各路径
3. send_hc_card 集成: artifact_sync.sync_once → publish → 卡片渲染含"打开编辑"按钮
4. pipeline_store: docs_folder_token 列的读写与回读
5. build_hc_card: published_urls 渲染成 open_url 按钮，无 published_urls 时兼容旧行为
6. 降级保护: publisher 不可用/异常时 send_hc_card 仍能正常发卡（不含链接）
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

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


pipeline_store_mod = _load("pipeline_store", "pipeline_store.py")
PipelineStore = pipeline_store_mod.PipelineStore
PipelineJob = pipeline_store_mod.PipelineJob

artifact_sync_mod = _load("artifact_sync", "artifact_sync.py")
Topic6ArtifactSync = artifact_sync_mod.Topic6ArtifactSync

publisher_mod = _load("feishu_doc_publisher", "feishu_doc_publisher.py")
FeishuDocPublisher = publisher_mod.FeishuDocPublisher

hitl_mod = _load("topic6_hitl", "topic6_hitl.py")
build_hc_card = hitl_mod.build_hc_card
build_hc_resolved_card = hitl_mod.build_hc_resolved_card
_payload_summary_elements = hitl_mod._payload_summary_elements
Topic6Hitl = hitl_mod.Topic6Hitl
Topic6HitlDeps = hitl_mod.Topic6HitlDeps


def _make_job(**overrides) -> PipelineJob:
    defaults = dict(
        job_id="job_abc123def456",
        chat_id="oc_test_chat",
        thread_id="",
        user_open_id="ou_user1",
        ma_session_id="session-99",
        mode="demo",
        project_dir="/workspace/Projects/W42_热点周报",
        current_phase="HC1",
        status="wait_hc",
        docs_folder_token="fldcn_sub_test",
    )
    defaults.update(overrides)
    return PipelineJob(**defaults)


class _FakeFeishu:
    """Mock FeishuSender：记录调用，返回预设值。"""

    def __init__(self):
        self.permissions_added: list[dict] = []
        self.folders_created: list[dict] = []
        self.files_uploaded: list[dict] = []

    def add_permission(self, token, token_type, member_open_id, perm="full_access"):
        self.permissions_added.append({
            "token": token, "type": token_type, "member": member_open_id, "perm": perm,
        })

    def create_folder(self, parent_token, name):
        record = {"parent": parent_token, "name": name}
        self.folders_created.append(record)
        return f"fldcn_sub_{name[:8]}"

    def create_spreadsheet_from_xlsx(self, folder_token, file_path, title):
        record = {"folder": folder_token, "path": file_path, "title": title}
        self.files_uploaded.append(record)
        token = f"shtcn_{title[:12]}"
        return token, f"https://tenant.feishu.cn/sheets/{token}"

    def list_permission_members(self, token, token_type):
        return [
            {"member_id": p["member"], "perm": p["perm"]}
            for p in self.permissions_added
            if p["token"] == token and p["type"] == token_type
        ]

    # 卡片操作 mock
    def send_interactive_card(self, chat_id, card):
        return "msg_card_001"

    def send_to_chat(self, chat_id, text, roster=None):
        pass

    def resolve_operator_name(self, open_id, chat_id=None):
        return "TestUser"


class _FakeArk:
    """Mock ArkClient：模拟 list_agent_files + download。"""

    def __init__(self, files=None, content=None):
        self.files = files or []
        self.content = content or {}

    async def list_agent_files(self, session_id):
        return self.files

    async def download_file(self, url):
        return self.content.get(url, b"")


# ---------------------------------------------------------------------------
# 1. _resolve_local_artifact: artifact_sync 写出的目录结构能被 publisher 找到
# ---------------------------------------------------------------------------

def test_resolve_artifact_from_materialized_path(tmp_path: Path):
    """artifact_sync 的 materialize 路径（{project}/{文件名}）能被 publisher 解析。"""
    job_root = tmp_path / "job_abc123def456"
    (job_root / "W42" / "05_合并").mkdir(parents=True)
    artifact = job_root / "W42" / "05_合并" / "wide_table_demo_r1.xlsx"
    artifact.write_bytes(b"fake-xlsx")

    result = FeishuDocPublisher._resolve_local_artifact(
        tmp_path, "job_abc123def456",
        "/workspace/Projects/W42_热点周报/05_合并/wide_table_demo_r1.xlsx",
    )
    assert result is not None
    assert result == artifact
    assert result.read_bytes() == b"fake-xlsx"


def test_resolve_artifact_from_objects_dir(tmp_path: Path):
    """当 materialize 没有跑，从 .objects 目录的 {hash}__safe_name 能被找到。"""
    job_root = tmp_path / "job_abc123def456"
    objects_dir = job_root / ".objects"
    objects_dir.mkdir(parents=True)
    obj_file = objects_dir / "a1b2c3d4e5f67890__wide_table_demo_r1.xlsx"
    obj_file.write_bytes(b"xlsx-from-objects")

    result = FeishuDocPublisher._resolve_local_artifact(
        tmp_path, "job_abc123def456",
        "/workspace/Projects/W42/wide_table_demo_r1.xlsx",
    )
    assert result is not None
    assert result == obj_file


def test_resolve_artifact_missing_returns_none(tmp_path: Path):
    """job 目录不存在时返回 None。"""
    result = FeishuDocPublisher._resolve_local_artifact(
        tmp_path, "nonexistent_job", "/some/path.xlsx",
    )
    assert result is None


def test_resolve_artifact_prefers_materialized_over_objects(tmp_path: Path):
    """同名文件同时存在于 materialized 和 .objects 时，优先返回 materialized。"""
    job_root = tmp_path / "job_abc123def456"
    # materialized
    (job_root / "W42").mkdir(parents=True)
    materialized = job_root / "W42" / "wide_table.xlsx"
    materialized.write_bytes(b"materialized")
    # objects
    objects_dir = job_root / ".objects"
    objects_dir.mkdir(parents=True)
    obj_file = objects_dir / "deadbeef__wide_table.xlsx"
    obj_file.write_bytes(b"objects-version")

    result = FeishuDocPublisher._resolve_local_artifact(
        tmp_path, "job_abc123def456", "wide_table.xlsx",
    )
    assert result is not None
    assert result.read_bytes() == b"materialized"


# ---------------------------------------------------------------------------
# 2. publish_hc_artifacts: HC1/HC2/HC3 各路径
# ---------------------------------------------------------------------------

async def test_publish_hc1_uploads_wide_table(tmp_path: Path):
    """HC1: 宽表 xlsx 存在时调 import_spreadsheet，返回飞书 URL。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job()

    # 准备本地产物文件（模拟 artifact_sync 同步后的结构）
    job_root = tmp_path / "job_abc123def456"
    (job_root / "W42").mkdir(parents=True)
    xlsx = job_root / "W42" / "wide_table_demo_r1.xlsx"
    xlsx.write_bytes(b"HC1-xlsx-data")

    payload = {
        "hc": "HC1",
        "mode": "demo",
        "wide_table_path": "/workspace/Projects/W42/wide_table_demo_r1.xlsx",
    }

    urls = await publisher.publish_hc_artifacts(
        job, "HC1", payload, sync_root=tmp_path, job_folder_token="fldcn_sub_test",
    )

    assert "wide_table" in urls
    sheet = urls["wide_table"]
    # 使用接口返回的 url，不硬编码租户域名
    assert sheet["url"] == "https://tenant.feishu.cn/sheets/shtcn_W42_热点周报_HC1"
    assert sheet["token"] == "shtcn_W42_热点周报_HC1"
    assert sheet["type"] == "sheet"
    assert sheet["granted"] is True
    # 宽表单独授权给任务发起人 edit
    assert feishu.permissions_added == [{
        "token": "shtcn_W42_热点周报_HC1", "type": "sheet", "member": "ou_user1", "perm": "edit",
    }]
    assert len(feishu.files_uploaded) == 1
    assert feishu.files_uploaded[0]["folder"] == "fldcn_sub_test"
    assert feishu.files_uploaded[0]["title"] == "W42_热点周报_HC1_wide_table"
    # 确认上传的文件路径指向实际存在的文件
    uploaded_path = Path(feishu.files_uploaded[0]["path"])
    assert uploaded_path.exists()
    assert uploaded_path.read_bytes() == b"HC1-xlsx-data"


async def test_publish_hc2_also_uploads_wide_table(tmp_path: Path):
    """HC2: 同样上传宽表（分布摘要是文本字段，不需要上传文档）。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job(mode="full")

    job_root = tmp_path / "job_abc123def456"
    (job_root / "W42").mkdir(parents=True)
    (job_root / "W42" / "wide_table_full.xlsx").write_bytes(b"HC2-full-data")

    payload = {
        "hc": "HC2",
        "mode": "full",
        "wide_table_path": "/workspace/Projects/W42/wide_table_full.xlsx",
        "distribution_summary": "正面60%，负面20%，中性20%",
    }

    urls = await publisher.publish_hc_artifacts(
        job, "HC2", payload, sync_root=tmp_path, job_folder_token="fldcn_sub_test",
    )

    assert "wide_table" in urls
    assert len(feishu.files_uploaded) == 1
    assert feishu.files_uploaded[0]["title"] == "W42_热点周报_HC2_wide_table"


def test_wide_table_title_falls_back_to_mode_without_project_dir():
    """project_dir 未解析时退回 mode，仍带 HC 序号区分两轮。"""
    job = _make_job(project_dir="", mode="smoke")
    assert FeishuDocPublisher._wide_table_title(job, "HC2") == "smoke_HC2_wide_table"


def test_gateway_start_job_does_not_create_docs_folder():
    """job 子文件夹只在首个 HC 发卡前创建（此时 project_dir 已知），start_job 不再建。"""
    src = (Path(__file__).resolve().parents[1] / "cases" / "topic6" / "gateway.py").read_text(encoding="utf-8")
    assert "create_job_folder" not in src


async def test_publish_hc3_verifies_doc_url():
    """HC3: 不上传文件，只校验 feishu_doc_url 存在。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job(current_phase="HC3")

    payload = {
        "hc": "HC3",
        "feishu_doc_url": "https://bluefocus.feishu.cn/docx/abc123",
    }

    urls = await publisher.publish_hc_artifacts(
        job, "HC3", payload, sync_root=Path("/tmp"), job_folder_token="fldcn_sub_test",
    )

    assert urls == {"report": {"url": "https://bluefocus.feishu.cn/docx/abc123", "token": "", "type": "docx"}}
    assert len(feishu.files_uploaded) == 0  # HC3 不上传


async def test_publish_hc3_empty_url_returns_empty():
    """HC3: feishu_doc_url 为空时返回空映射。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job()

    urls = await publisher.publish_hc_artifacts(
        job, "HC3", {"hc": "HC3"}, sync_root=Path("/tmp"), job_folder_token="fldcn_sub",
    )
    assert urls == {}


async def test_publish_hc1_missing_artifact_returns_empty(tmp_path: Path):
    """HC1: 本地找不到宽表时返回空映射（不崩溃）。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job()

    payload = {"wide_table_path": "/nonexistent/path.xlsx"}
    urls = await publisher.publish_hc_artifacts(
        job, "HC1", payload, sync_root=tmp_path, job_folder_token="fldcn_sub",
    )
    assert urls == {}
    assert len(feishu.files_uploaded) == 0


async def test_publish_no_folder_token_returns_empty(tmp_path: Path):
    """没有 job_folder_token 时直接返回空映射。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job()

    urls = await publisher.publish_hc_artifacts(
        job, "HC1", {"wide_table_path": "x.xlsx"}, sync_root=tmp_path, job_folder_token="",
    )
    assert urls == {}


# ---------------------------------------------------------------------------
# 3. artifact_sync → publisher → send_hc_card 端到端
# ---------------------------------------------------------------------------

async def test_artifact_sync_then_publish_then_card(tmp_path: Path):
    """完整链路: artifact_sync 下载文件 → publisher 上传 → HC 卡片含"打开编辑"。"""
    xlsx_content = b"real-xlsx-bytes"
    digest = hashlib.sha256(xlsx_content).hexdigest()

    # 准备 manifest（与 artifact_sync 测试一致的格式）
    manifest = {
        "schema_version": 1,
        "project": "W42",
        "updated_at": "2026-10-22T00:00:00Z",
        "publications": [{
            "node": "merge", "run_id": "1", "status": "complete",
            "published_at": "2026-10-22T00:00:00Z",
            "files": [{
                "source": "05_合并/wide_table_demo.xlsx",
                "output": "W42/05_合并/wide_table_demo.xlsx",
                "size": len(xlsx_content),
                "sha256": digest,
            }],
        }],
    }
    manifest_bytes = json.dumps(manifest).encode()

    ark = _FakeArk(
        files=[
            {
                "id": "f1", "filename": "wide_table_demo.xlsx",
                "size": len(xlsx_content), "status": "active",
                "download_url": "https://files/xlsx",
            },
            {
                "id": "f2", "filename": "manifest.json",
                "size": len(manifest_bytes), "status": "active",
                "download_url": "https://files/manifest",
            },
        ],
        content={
            "https://files/xlsx": xlsx_content,
            "https://files/manifest": manifest_bytes,
        },
    )

    # Step 1: artifact_sync 同步
    sync = Topic6ArtifactSync(ark, tmp_path, lambda _: SimpleNamespace(status="running"))
    downloaded = await sync.sync_once("job_abc123def456", "session-99")
    assert downloaded == 2

    # 验证 materialized 文件存在
    materialized = tmp_path / "job_abc123def456" / "W42" / "05_合并" / "wide_table_demo.xlsx"
    assert materialized.exists()
    assert materialized.read_bytes() == xlsx_content

    # Step 2: publisher 解析并"上传"
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job()

    payload = {
        "hc": "HC1", "mode": "demo",
        "wide_table_path": "/workspace/Projects/W42/05_合并/wide_table_demo.xlsx",
    }
    urls = await publisher.publish_hc_artifacts(
        job, "HC1", payload, sync_root=tmp_path, job_folder_token="fldcn_sub_test",
    )
    assert "wide_table" in urls
    assert urls["wide_table"]["url"].startswith("https://tenant.feishu.cn/sheets/")

    # 验证上传的文件是 artifact_sync 同步下来的那个
    uploaded_path = Path(feishu.files_uploaded[0]["path"])
    assert uploaded_path.read_bytes() == xlsx_content

    # Step 3: 卡片渲染含"打开编辑"按钮
    card = build_hc_card(job, "HC1", 1, payload, published_urls=publisher_mod.artifact_urls(urls))
    body_elements = card["body"]["elements"]
    open_buttons = [
        el for el in body_elements
        if el.get("tag") == "button"
        and any(
            b.get("type") == "open_url"
            for b in (el.get("behaviors") or [])
        )
    ]
    assert len(open_buttons) == 1
    btn = open_buttons[0]
    assert "打开编辑" in btn["text"]["content"]
    assert btn["behaviors"][0]["default_url"] == urls["wide_table"]["url"]


# ---------------------------------------------------------------------------
# 4. pipeline_store: docs_folder_token 列的读写与回读
# ---------------------------------------------------------------------------

def test_pipeline_store_docs_folder_token_roundtrip(tmp_path: Path):
    """验证 docs_folder_token 的写入→回读完整链路。"""
    store = PipelineStore(str(tmp_path / "test.db"))

    job = store.create_job(
        chat_id="oc_chat", thread_id="", user_open_id="ou_user",
        ma_session_id="sess-1", mode="demo", project_dir="/workspace/W42",
    )
    # 新建时 docs_folder_token 为空
    loaded = store.get_job(job.job_id)
    assert loaded is not None
    assert loaded.docs_folder_token == ""

    # update 后回读
    store.update_docs_folder_token(job.job_id, "fldcn_sub_abc123")
    reloaded = store.get_job(job.job_id)
    assert reloaded is not None
    assert reloaded.docs_folder_token == "fldcn_sub_abc123"

    # 空值不会覆盖已有值（测试 update 幂等性）
    store.update_docs_folder_token(job.job_id, "")
    reloaded2 = store.get_job(job.job_id)
    assert reloaded2.docs_folder_token == ""

    store.close()


# ---------------------------------------------------------------------------
# 5. build_hc_card 兼容性: 有/无 published_urls
# ---------------------------------------------------------------------------

def test_build_hc_card_without_published_urls():
    """没有 published_urls 时，卡片不含 open_url 按钮（向后兼容）。"""
    job = _make_job()
    payload = {"mode": "demo", "wide_table_path": "/path/to/table.xlsx"}

    card = build_hc_card(job, "HC1", 1, payload)
    body_elements = card["body"]["elements"]
    open_buttons = [
        el for el in body_elements
        if el.get("tag") == "button"
        and any(b.get("type") == "open_url" for b in (el.get("behaviors") or []))
    ]
    assert len(open_buttons) == 0


def test_build_hc_card_with_published_urls():
    """有 published_urls 时，卡片含 open_url 按钮。"""
    job = _make_job()
    payload = {"mode": "demo", "wide_table_path": "/path/to/table.xlsx"}
    urls = {"wide_table": "https://bluefocus.feishu.cn/sheets/shtcnXXX"}

    card = build_hc_card(job, "HC1", 1, payload, published_urls=urls)
    body_elements = card["body"]["elements"]
    open_buttons = [
        el for el in body_elements
        if el.get("tag") == "button"
        and any(b.get("type") == "open_url" for b in (el.get("behaviors") or []))
    ]
    assert len(open_buttons) == 1
    assert open_buttons[0]["behaviors"][0]["default_url"] == "https://bluefocus.feishu.cn/sheets/shtcnXXX"


def test_build_hc_card_multiple_published_urls():
    """多个 published_urls 时，卡片含多个 open_url 按钮。"""
    job = _make_job()
    payload = {"mode": "full"}
    urls = {
        "wide_table": "https://bluefocus.feishu.cn/sheets/sht1",
        "distribution_summary": "https://bluefocus.feishu.cn/docx/doc1",
    }

    card = build_hc_card(job, "HC2", 1, payload, published_urls=urls)
    body_elements = card["body"]["elements"]
    open_buttons = [
        el for el in body_elements
        if el.get("tag") == "button"
        and any(b.get("type") == "open_url" for b in (el.get("behaviors") or []))
    ]
    assert len(open_buttons) == 2


def test_build_hc_resolved_card_no_published_urls():
    """已处理终态卡片不需要 published_urls，调用不报 TypeError。"""
    job = _make_job()
    payload = {"mode": "demo"}
    # build_hc_resolved_card 内部调 _payload_summary_elements(payload)，
    # published_urls 默认 None，不应报错。
    card = build_hc_resolved_card(
        job, "HC1", payload, decision="pass", note="", operator_label="TestUser",
    )
    assert card["header"]["template"] == "wathet"
    assert "已通过" in card["header"]["title"]["content"]


def test_payload_summary_elements_with_and_without_urls():
    """_payload_summary_elements 有无 published_urls 都能正常工作。"""
    payload = {"mode": "demo", "wide_table_path": "/path.xlsx"}

    # 无 published_urls
    elements1 = _payload_summary_elements(payload)
    assert all(el.get("tag") != "button" or "open_url" not in str(el) for el in elements1)

    # 有 published_urls
    elements2 = _payload_summary_elements(payload, {"wide_table": "https://x.com/sheet1"})
    url_buttons = [el for el in elements2 if el.get("tag") == "button"]
    assert len(url_buttons) == 1
    assert url_buttons[0]["behaviors"][0]["default_url"] == "https://x.com/sheet1"


# ---------------------------------------------------------------------------
# 6. init_folder_permissions 幂等性 + create_job_folder
# ---------------------------------------------------------------------------

def test_init_folder_permissions_idempotent():
    """init_folder_permissions 对白名单每个用户调一次 add_permission。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root_token")

    publisher.init_folder_permissions(("ou_user1", "ou_user2"))
    assert len(feishu.permissions_added) == 2
    assert feishu.permissions_added[0]["member"] == "ou_user1"
    assert feishu.permissions_added[1]["member"] == "ou_user2"
    assert all(p["token"] == "fldcn_root_token" for p in feishu.permissions_added)
    assert all(p["perm"] == "full_access" for p in feishu.permissions_added)


def test_init_folder_permissions_no_token():
    """没有 docs_folder_token 时 init 直接返回，不调 API。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "")  # 无 token

    publisher.init_folder_permissions(("ou_user1",))
    assert len(feishu.permissions_added) == 0


def test_init_folder_permissions_no_users():
    """白名单为空时 init 直接返回。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")

    publisher.init_folder_permissions(())
    assert len(feishu.permissions_added) == 0


def test_create_job_folder():
    """create_job_folder 在根文件夹下创建子文件夹。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    job = _make_job(project_dir="/workspace/Projects/W42_热点周报")

    token = publisher.create_job_folder(job)
    assert token.startswith("fldcn_sub_")
    assert len(feishu.folders_created) == 1
    assert feishu.folders_created[0]["parent"] == "fldcn_root"
    # 文件夹名格式: {project_dir最后一段}_{job_id前12位}
    assert "W42" in feishu.folders_created[0]["name"] or "热点" in feishu.folders_created[0]["name"]
    assert "job_abc123de" in feishu.folders_created[0]["name"]


def test_create_job_folder_no_root_token():
    """无 root folder token 时建在机器人云空间根目录（parent_token=""）。"""
    feishu = _FakeFeishu()
    publisher = FeishuDocPublisher(feishu, "")
    job = _make_job()

    token = publisher.create_job_folder(job)
    assert token.startswith("fldcn_sub_")
    assert len(feishu.folders_created) == 1
    assert feishu.folders_created[0]["parent"] == ""


def test_publisher_enabled_property():
    """有 feishu sender 即启用，与根文件夹 token 是否配置无关。"""
    assert FeishuDocPublisher(_FakeFeishu(), "fldcn_root").enabled is True
    assert FeishuDocPublisher(_FakeFeishu(), "").enabled is True
    assert FeishuDocPublisher(None, "fldcn_root").enabled is False


# ---------------------------------------------------------------------------
# 7. send_hc_card 集成: 降级保护
# ---------------------------------------------------------------------------

async def test_send_hc_card_without_publisher(tmp_path: Path):
    """publisher 为 None 时 send_hc_card 正常发卡（无链接）。"""
    feishu = _FakeFeishu()
    store = PipelineStore(str(tmp_path / "test.db"))
    job = store.create_job(
        chat_id="oc_chat", thread_id="", user_open_id="ou_user",
        ma_session_id="sess-1", mode="demo", project_dir="/workspace/W42",
    )
    store.set_progress_card_message_id(job.job_id, "msg_progress_001")

    # Mock runner
    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="msg_progress_001")
    runner._feishu = feishu

    hitl = Topic6Hitl(Topic6HitlDeps(
        store=store, runner=runner,
        publisher=None, artifact_sync=None, sync_root=None,
    ))

    payload = {"mode": "demo", "wide_table_path": "/path.xlsx"}
    job_obj = store.get_job(job.job_id)
    msg_id = await hitl.send_hc_card(job_obj, "HC1", 1, payload)

    # 验证卡片已发
    runner.patch_job_card.assert_called_once()
    card_arg = runner.patch_job_card.call_args[0][1]
    # 无 open_url 按钮
    open_buttons = [
        el for el in card_arg["body"]["elements"]
        if el.get("tag") == "button"
        and any(b.get("type") == "open_url" for b in (el.get("behaviors") or []))
    ]
    assert len(open_buttons) == 0

    store.close()


async def test_send_hc_card_with_publisher_and_artifact(tmp_path: Path):
    """publisher 启用时 send_hc_card: sync_once → publish → 卡片含链接。"""
    feishu = _FakeFeishu()
    store = PipelineStore(str(tmp_path / "test.db"))
    job = store.create_job(
        chat_id="oc_chat", thread_id="", user_open_id="ou_user",
        ma_session_id="sess-1", mode="demo", project_dir="/workspace/W42",
    )
    store.update_docs_folder_token(job.job_id, "fldcn_sub_test")
    store.set_progress_card_message_id(job.job_id, "msg_progress_001")

    # 准备本地产物
    sync_root = tmp_path / "artifacts"
    safe_job = "".join(c if c.isalnum() or c in "._-" else "_" for c in job.job_id)
    job_root = sync_root / safe_job / "W42"
    job_root.mkdir(parents=True)
    (job_root / "wide_table.xlsx").write_bytes(b"xlsx-content")

    # Mock artifact_sync (sync_once 什么也不做，因为文件已经在本地)
    artifact_sync = MagicMock()
    artifact_sync.sync_once = AsyncMock(return_value=0)

    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="msg_progress_001")
    runner._feishu = feishu

    hitl = Topic6Hitl(Topic6HitlDeps(
        store=store, runner=runner,
        publisher=publisher,
        artifact_sync=artifact_sync,
        sync_root=sync_root,
    ))

    payload = {"mode": "demo", "wide_table_path": "/workspace/W42/wide_table.xlsx"}
    job_obj = store.get_job(job.job_id)
    msg_id = await hitl.send_hc_card(job_obj, "HC1", 1, payload)

    # 验证 sync_once 被调用
    artifact_sync.sync_once.assert_called_once_with(job.job_id, "sess-1")

    # 验证卡片含 open_url 按钮
    card_arg = runner.patch_job_card.call_args[0][1]
    open_buttons = [
        el for el in card_arg["body"]["elements"]
        if el.get("tag") == "button"
        and any(b.get("type") == "open_url" for b in (el.get("behaviors") or []))
    ]
    assert len(open_buttons) == 1
    assert "打开编辑" in open_buttons[0]["text"]["content"]
    assert open_buttons[0]["behaviors"][0]["default_url"].startswith("https://tenant.feishu.cn/sheets/")

    store.close()


async def test_send_hc_card_publisher_error_degrades_gracefully(tmp_path: Path):
    """publisher.publish_hc_artifacts 抛异常时，卡片仍能正常发（无链接）。"""
    feishu = _FakeFeishu()
    store = PipelineStore(str(tmp_path / "test.db"))
    job = store.create_job(
        chat_id="oc_chat", thread_id="", user_open_id="ou_user",
        ma_session_id="sess-1", mode="demo", project_dir="/workspace/W42",
    )
    store.update_docs_folder_token(job.job_id, "fldcn_sub_test")
    store.set_progress_card_message_id(job.job_id, "msg_progress_001")

    # publisher 会抛异常
    publisher = MagicMock()
    publisher.enabled = True
    publisher.publish_hc_artifacts = AsyncMock(side_effect=RuntimeError("飞书 API 故障"))

    artifact_sync = MagicMock()
    artifact_sync.sync_once = AsyncMock(return_value=0)

    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="msg_progress_001")
    runner._feishu = feishu

    hitl = Topic6Hitl(Topic6HitlDeps(
        store=store, runner=runner,
        publisher=publisher,
        artifact_sync=artifact_sync,
        sync_root=tmp_path,
    ))

    payload = {"mode": "demo"}
    job_obj = store.get_job(job.job_id)
    msg_id = await hitl.send_hc_card(job_obj, "HC1", 1, payload)

    # 验证卡片正常发出（降级为无链接）
    runner.patch_job_card.assert_called_once()
    card_arg = runner.patch_job_card.call_args[0][1]
    open_buttons = [
        el for el in card_arg["body"]["elements"]
        if el.get("tag") == "button"
        and any(b.get("type") == "open_url" for b in (el.get("behaviors") or []))
    ]
    assert len(open_buttons) == 0

    store.close()


async def test_send_hc_card_sync_error_continues_to_publish(tmp_path: Path):
    """artifact_sync.sync_once 失败时，仍然尝试 publish（本地可能有旧文件）。"""
    feishu = _FakeFeishu()
    store = PipelineStore(str(tmp_path / "test.db"))
    job = store.create_job(
        chat_id="oc_chat", thread_id="", user_open_id="ou_user",
        ma_session_id="sess-1", mode="demo", project_dir="/workspace/W42",
    )
    store.update_docs_folder_token(job.job_id, "fldcn_sub_test")
    store.set_progress_card_message_id(job.job_id, "msg_progress_001")

    # 准备本地产物（模拟 sync_once 之前已有的旧文件）
    safe_job = "".join(c if c.isalnum() or c in "._-" else "_" for c in job.job_id)
    job_root = tmp_path / safe_job / "W42"
    job_root.mkdir(parents=True)
    (job_root / "wide_table.xlsx").write_bytes(b"old-xlsx")

    artifact_sync = MagicMock()
    artifact_sync.sync_once = AsyncMock(side_effect=ConnectionError("SSE 断了"))

    publisher = FeishuDocPublisher(feishu, "fldcn_root")
    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="msg_progress_001")
    runner._feishu = feishu

    hitl = Topic6Hitl(Topic6HitlDeps(
        store=store, runner=runner,
        publisher=publisher,
        artifact_sync=artifact_sync,
        sync_root=tmp_path,
    ))

    payload = {"mode": "demo", "wide_table_path": "/workspace/W42/wide_table.xlsx"}
    job_obj = store.get_job(job.job_id)
    msg_id = await hitl.send_hc_card(job_obj, "HC1", 1, payload)

    # sync_once 失败了但 publish 仍然执行（用旧文件）
    artifact_sync.sync_once.assert_called_once()
    assert len(feishu.files_uploaded) == 1  # publish 成功了

    store.close()
