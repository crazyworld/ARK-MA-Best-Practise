"""HC1/HC2 飞书在线表格往返：创建+授权 → 落库 artifacts → 审核通过后回读并挂载回 Session。

全部使用 fake，不访问网络。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = ROOT / "cases" / "topic6"


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
publisher_mod = _load("feishu_doc_publisher", "feishu_doc_publisher.py")
FeishuDocPublisher = publisher_mod.FeishuDocPublisher
hitl_mod = _load("topic6_hitl", "topic6_hitl.py")

WIDE_PATH = "/mnt/session/outputs/W42/05_合并/wide_table_demo_r1.xlsx"
BOT = "ou_bot"
USER = "ou_user"


class _FakeFeishu:
    def __init__(self, *, meta=None, grant_error=None, members=None, export=b"edited-xlsx"):
        self.meta = meta or {}
        self.grant_error = grant_error
        self.members = members
        self.export = export
        self.grants: list[tuple] = []
        self.exports: list[str] = []
        self.created: list[dict] = []

    def create_folder(self, parent_token, name):
        return "fld_job"

    def create_spreadsheet_from_xlsx(self, folder_token, file_path, title):
        self.created.append({"folder": folder_token, "title": title})
        return "sht_1", "https://x.feishu.cn/sheets/sht_1"

    def add_permission(self, token, token_type, member_open_id, perm="full_access"):
        if self.grant_error:
            raise self.grant_error
        self.grants.append((token, token_type, member_open_id, perm))

    def list_permission_members(self, token, token_type):
        if self.members is not None:
            return self.members
        return [{"member_id": g[2], "perm": g[3]} for g in self.grants]

    def get_doc_meta(self, token, doc_type):
        return self.meta

    def export_sheet_xlsx(self, token):
        self.exports.append(token)
        return self.export

    def send_interactive_card(self, chat_id, card):
        return "msg_card"

    def send_to_chat(self, chat_id, text, roster=None):
        pass


def _job(**kw):
    defaults = dict(
        job_id="job_rt_1", chat_id="c1", thread_id="", user_open_id=USER,
        ma_session_id="sess-1", mode="demo", project_dir="/workspace/Projects/W42",
    )
    defaults.update(kw)
    return pipeline_store_mod.PipelineJob(**defaults)


# ---- publisher：创建 + 授权 + 回读判定 ---------------------------------------


def _publish(tmp_path, feishu):
    job_root = tmp_path / "job_rt_1" / "W42" / "05_合并"
    job_root.mkdir(parents=True)
    (job_root / "wide_table_demo_r1.xlsx").write_bytes(b"x")
    publisher = FeishuDocPublisher(feishu, "")
    import asyncio
    return asyncio.new_event_loop().run_until_complete(publisher.publish_hc_artifacts(
        _job(), "HC1", {"wide_table_path": WIDE_PATH}, sync_root=tmp_path, job_folder_token="fld_job",
    ))


def test_publish_grants_user_edit_and_verifies(tmp_path):
    feishu = _FakeFeishu()
    result = _publish(tmp_path, feishu)
    assert result["wide_table"] == {
        "url": "https://x.feishu.cn/sheets/sht_1", "token": "sht_1", "type": "sheet", "granted": True,
    }
    assert feishu.grants == [("sht_1", "sheet", USER, "edit")]


def test_publish_grant_failure_still_returns_url(tmp_path):
    feishu = _FakeFeishu(grant_error=RuntimeError("no perm"))
    result = _publish(tmp_path, feishu)
    assert result["wide_table"]["url"] == "https://x.feishu.cn/sheets/sht_1"
    assert result["wide_table"]["granted"] is False


def test_publish_grant_not_verified_marks_false(tmp_path):
    feishu = _FakeFeishu(members=[{"member_id": "ou_other", "perm": "edit"}])
    result = _publish(tmp_path, feishu)
    assert result["wide_table"]["granted"] is False


def test_publish_url_fallback_when_api_omits_url(tmp_path):
    feishu = _FakeFeishu()
    feishu.create_spreadsheet_from_xlsx = lambda **kw: ("sht_2", "")
    result = _publish(tmp_path, feishu)
    assert result["wide_table"]["url"] == f"{publisher_mod.FEISHU_SHEETS_URL_PREFIX}sht_2"


@pytest.mark.parametrize("meta", [
    {"owner_id": BOT, "latest_modify_user": BOT, "latest_modify_time": "2000"},  # 机器人自己写的
    {"owner_id": BOT, "latest_modify_user": USER, "latest_modify_time": "999"},  # 发卡前修改
    {"owner_id": BOT, "latest_modify_user": "", "latest_modify_time": "2000"},
])
def test_fetch_user_edited_sheet_unchanged(meta):
    feishu = _FakeFeishu(meta=meta)
    assert FeishuDocPublisher(feishu, "").fetch_user_edited_sheet("sht_1", 1000) is None
    assert feishu.exports == []


def test_fetch_user_edited_sheet_exports_when_user_edited():
    feishu = _FakeFeishu(meta={"owner_id": BOT, "latest_modify_user": USER, "latest_modify_time": "2000"})
    data = FeishuDocPublisher(feishu, "").fetch_user_edited_sheet("sht_1", 1000)
    assert data == b"edited-xlsx"
    assert feishu.exports == ["sht_1"]


def test_artifact_urls_converts_and_tolerates_legacy():
    assert publisher_mod.artifact_urls({"wide_table": {"url": "u1", "token": "t"}, "old": "u2"}) == {
        "wide_table": "u1", "old": "u2",
    }
    assert publisher_mod.artifact_urls(None) == {}


# ---- FeishuSender：写入失败必须抛错 -----------------------------------------


def test_create_spreadsheet_raises_on_write_failure(tmp_path, monkeypatch):
    import openpyxl
    import requests

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from arkagent.feishu import FeishuSender

    xlsx = tmp_path / "w.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["a", "b"])
    wb.active.append([1, 2])
    wb.save(xlsx)

    def _resp(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body, text=str(body))

    monkeypatch.setattr(requests, "post", lambda *a, **k: _resp(200, {
        "code": 0, "data": {"spreadsheet": {"spreadsheet_token": "sht_w", "url": "https://x/sheets/sht_w"}},
    }))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _resp(200, {"data": {"sheets": [{"sheet_id": "s1"}]}}))
    monkeypatch.setattr(requests, "put", lambda *a, **k: _resp(200, {"code": 90001, "msg": "bad"}))

    sender = object.__new__(FeishuSender)
    sender._get_tenant_access_token = lambda: "t"
    with pytest.raises(RuntimeError, match="写入失败"):
        sender.create_spreadsheet_from_xlsx("", str(xlsx), "demo")

    monkeypatch.setattr(requests, "put", lambda *a, **k: _resp(200, {"code": 0}))
    assert sender.create_spreadsheet_from_xlsx("", str(xlsx), "demo") == ("sht_w", "https://x/sheets/sht_w")


# ---- B1：宽表写入按请求体大小切批 + 失败删表 + 不重试建表 ---------------------------


def test_sheet_write_batches_split_by_rows_and_chars():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from arkagent.feishu import _sheet_write_batches

    rows = [["x" * 100] for _ in range(10)]  # 每行 JSON 约 106 字符
    batches = _sheet_write_batches(rows, max_rows=4, max_chars=10_000)
    assert [(s, len(b)) for s, b in batches] == [(0, 4), (4, 4), (8, 2)]
    batches = _sheet_write_batches(rows, max_rows=5000, max_chars=330)
    assert [(s, len(b)) for s, b in batches] == [(0, 3), (3, 3), (6, 3), (9, 1)]
    # 拼回去与原表逐行一致：分批不改变单元格值
    assert [r for _s, b in batches for r in b] == rows
    # 单行超过上限独占一批，不丢行
    assert [len(b) for _s, b in _sheet_write_batches([["y" * 500], ["z"]], 5000, 100)] == [1, 1]
    # 按真实请求体(requests json= 默认 ensure_ascii=True)计：中文每字 6 字节
    cn = [["中" * 10]] * 4  # 每行 json.dumps 为 64 字符(ensure_ascii=False 时仅 14)
    assert [len(b) for _s, b in _sheet_write_batches(cn, 5000, 130)] == [2, 2]


def _sender_with_requests(monkeypatch, tmp_path, *, put_codes, delete_code=0):
    import openpyxl
    import requests

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import arkagent.feishu as feishu_mod

    xlsx = tmp_path / "w.xlsx"
    wb = openpyxl.Workbook()
    for i in range(6):
        wb.active.append([f"r{i}", "v" * 50])
    wb.save(xlsx)

    calls = {"put": [], "delete": []}
    codes = iter(put_codes)

    def _resp(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body, text=str(body))

    def _put(url, headers=None, json=None, timeout=None):
        calls["put"].append(json["valueRange"]["range"])
        return _resp(200, {"code": next(codes)})

    def _delete(url, headers=None, params=None, timeout=None):
        calls["delete"].append((url.rsplit("/", 1)[-1], params))
        return _resp(200, {"code": delete_code})

    monkeypatch.setattr(requests, "post", lambda *a, **k: _resp(200, {
        "code": 0, "data": {"spreadsheet": {"spreadsheet_token": "sht_w", "url": "https://x/sheets/sht_w"}},
    }))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _resp(200, {"data": {"sheets": [{"sheet_id": "s1"}]}}))
    monkeypatch.setattr(requests, "put", _put)
    monkeypatch.setattr(requests, "delete", _delete)
    sender = object.__new__(feishu_mod.FeishuSender)
    sender._get_tenant_access_token = lambda: "t"
    return feishu_mod, sender, xlsx, calls


def test_create_spreadsheet_splits_large_body(tmp_path, monkeypatch):
    feishu_mod, sender, xlsx, calls = _sender_with_requests(monkeypatch, tmp_path, put_codes=[0] * 10)
    monkeypatch.setattr(feishu_mod, "SHEET_WRITE_MAX_CHARS", 150)  # 每行约 62 字符 → 每批 2 行
    assert sender.create_spreadsheet_from_xlsx("", str(xlsx), "demo") == ("sht_w", "https://x/sheets/sht_w")
    assert calls["put"] == ["s1!A1:B2", "s1!A3:B4", "s1!A5:B6"]
    assert calls["delete"] == []


def test_create_spreadsheet_deletes_empty_sheet_on_write_failure(tmp_path, monkeypatch):
    feishu_mod, sender, xlsx, calls = _sender_with_requests(
        monkeypatch, tmp_path, put_codes=[0, 90227])
    monkeypatch.setattr(feishu_mod, "SHEET_WRITE_MAX_CHARS", 150)
    with pytest.raises(feishu_mod.SpreadsheetWriteError, match="已删除该空表"):
        sender.create_spreadsheet_from_xlsx("", str(xlsx), "demo")
    assert calls["delete"] == [("sht_w", {"type": "sheet"})]


def test_create_spreadsheet_delete_failure_keeps_original_error(tmp_path, monkeypatch):
    feishu_mod, sender, xlsx, calls = _sender_with_requests(
        monkeypatch, tmp_path, put_codes=[90227], delete_code=1061004)
    with pytest.raises(feishu_mod.SpreadsheetWriteError, match="90227.*删除空表失败"):
        sender.create_spreadsheet_from_xlsx("", str(xlsx), "demo")


async def test_send_hc_card_write_failure_no_retry_and_card_warns(tmp_path):
    """本地文件已同步、飞书写入失败：只建表一次(不再 3 次重试建 3 张空表)，卡片显示失败提示，不落库。"""
    store = PipelineStore(str(tmp_path / "t.db"))
    job = store.create_job(
        chat_id="c1", thread_id="", user_open_id=USER, ma_session_id="sess-1",
        mode="full", project_dir="/workspace/Projects/W42",
    )
    payload = {"hc": "HC2", "wide_table_path": WIDE_PATH}
    event_id = store.append_hc_event(job.job_id, "HC2", payload)
    safe_job = "".join(c if c.isalnum() or c in "._-" else "_" for c in job.job_id)
    d = tmp_path / "sync" / safe_job / "W42" / "05_合并"
    d.mkdir(parents=True)
    (d / "wide_table_demo_r1.xlsx").write_bytes(b"x")

    feishu = _FakeFeishu()

    def _fail(folder_token, file_path, title):
        feishu.created.append({"folder": folder_token, "title": title})
        raise RuntimeError("飞书表格写入失败 code=90227 request too large")

    feishu.create_spreadsheet_from_xlsx = _fail
    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="msg_card")
    runner._feishu = feishu
    artifact_sync = MagicMock()
    artifact_sync.sync_once = AsyncMock(return_value=0)
    hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(
        store=store, runner=runner, publisher=FeishuDocPublisher(feishu, ""),
        artifact_sync=artifact_sync, sync_root=tmp_path / "sync",
    ))
    await hitl.send_hc_card(store.get_job(job.job_id), "HC2", event_id, payload)

    assert len(feishu.created) == 1
    assert artifact_sync.sync_once.await_count == 1
    assert store.get_hc_event(event_id).artifacts == {}
    text = str(runner.patch_job_card.call_args[0][1])
    assert "宽表推送飞书失败" in text and "90227" in text
    store.close()


# ---- send_hc_card 落库 artifacts ----------------------------------------------


async def test_send_hc_card_persists_artifacts(tmp_path):
    store = PipelineStore(str(tmp_path / "t.db"))
    job = store.create_job(
        chat_id="c1", thread_id="", user_open_id=USER, ma_session_id="sess-1",
        mode="demo", project_dir="/workspace/Projects/W42",
    )
    payload = {"hc": "HC1", "wide_table_path": WIDE_PATH}
    event_id = store.append_hc_event(job.job_id, "HC1", payload)
    safe_job = "".join(c if c.isalnum() or c in "._-" else "_" for c in job.job_id)
    d = tmp_path / "sync" / safe_job / "W42" / "05_合并"
    d.mkdir(parents=True)
    (d / "wide_table_demo_r1.xlsx").write_bytes(b"x")

    feishu = _FakeFeishu()
    runner = MagicMock()
    runner.patch_job_card = AsyncMock(return_value="msg_card")
    runner._feishu = feishu
    artifact_sync = MagicMock()
    artifact_sync.sync_once = AsyncMock(return_value=0)
    hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(
        store=store, runner=runner, publisher=FeishuDocPublisher(feishu, ""),
        artifact_sync=artifact_sync, sync_root=tmp_path / "sync",
    ))
    await hitl.send_hc_card(store.get_job(job.job_id), "HC1", event_id, payload)

    assert store.get_job(job.job_id).docs_folder_token == "fld_job"  # 无根 token 也建了 job 文件夹
    assert store.get_hc_event(event_id).artifacts == {
        "wide_table": {"token": "sht_1", "type": "sheet", "url": "https://x.feishu.cn/sheets/sht_1"},
    }
    card = runner.patch_job_card.call_args[0][1]
    urls = [
        b["default_url"] for el in card["body"]["elements"] if el.get("tag") == "button"
        for b in (el.get("behaviors") or []) if b.get("type") == "open_url"
    ]
    assert urls == ["https://x.feishu.cn/sheets/sht_1"]
    store.close()


# ---- handle_card_action：通过后回读 ---------------------------------------------


class _FakeArk:
    def __init__(self, fail=False):
        self.fail = fail
        self.uploads: list[tuple] = []
        self.mounts: list[tuple] = []

    async def upload_file(self, name, mime, data):
        if self.fail:
            raise RuntimeError("files api down")
        self.uploads.append((name, mime, data))
        return "file_1"

    async def add_session_file(self, session_id, file_id, mount_path):
        self.mounts.append((session_id, file_id, mount_path))


class _Publisher:
    enabled = True

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls: list[tuple] = []

    def fetch_user_edited_sheet(self, token, since_ts):
        self.calls.append((token, since_ts))
        if self.error:
            raise self.error
        return self.data


async def _pass_hc(tmp_path, publisher, ark, hc="HC1", artifacts=True):
    store = PipelineStore(str(tmp_path / "t.db"))
    job = store.create_job(
        chat_id="c1", thread_id="", user_open_id=USER, ma_session_id="sess-1",
        mode="demo", project_dir="/workspace/Projects/W42",
    )
    event_id = store.append_hc_event(job.job_id, hc, {"hc": hc, "wide_table_path": WIDE_PATH})
    if artifacts:
        store.set_hc_artifacts(event_id, {"wide_table": {"token": "sht_1", "type": "sheet", "url": "u"}})
    store.mark_wait_hc(job.job_id, hc)

    captured: list[str] = []
    runner = MagicMock()
    runner._ark = ark
    runner._feishu = _FakeFeishu()

    async def _resume(_job, msg):
        captured.append(msg)

    runner.resume_job = _resume
    runner.patch_job_card = AsyncMock(return_value="msg_card")
    hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner, publisher=publisher))
    hitl._reply_sync = lambda *a, **k: None
    await hitl.handle_card_action({
        "action": {"value": {"job_id": job.job_id, "hc": hc, "event_id": event_id, "decision": "pass"}},
        "open_message_id": "msg_card", "chat_id": "c1", "operator": {"open_id": USER},
    })
    created_at = store.get_hc_event(event_id).created_at
    store.close()
    return captured, created_at


async def test_pass_edited_sheet_mounts_and_hints(tmp_path):
    publisher, ark = _Publisher(data=b"new-xlsx"), _FakeArk()
    captured, created_at = await _pass_hc(tmp_path, publisher, ark, hc="HC2")
    assert publisher.calls == [("sht_1", created_at)]
    assert ark.uploads == [("wide_table_demo_r1.xlsx", hitl_mod.XLSX_MIME, b"new-xlsx")]
    assert ark.mounts == [("sess-1", "file_1", "hc_edits/HC2/wide_table_demo_r1.xlsx")]
    assert len(captured) == 1
    msg = captured[0]
    assert msg.startswith("HC2 pass")
    assert "/mnt/session/uploads/hc_edits/HC2/wide_table_demo_r1.xlsx" in msg
    assert WIDE_PATH in msg
    assert "05_合并/" in msg
    assert "行数" in msg and "列" in msg


async def test_pass_unchanged_sheet_no_hint(tmp_path):
    ark = _FakeArk()
    captured, _ = await _pass_hc(tmp_path, _Publisher(data=None), ark)
    assert captured == ["HC1 pass"]
    assert ark.uploads == []


async def test_pass_without_artifacts_skips_reread(tmp_path):
    publisher = _Publisher(data=b"x")
    captured, _ = await _pass_hc(tmp_path, publisher, _FakeArk(), artifacts=False)
    assert captured == ["HC1 pass"]
    assert publisher.calls == []


async def test_pass_fetch_error_appends_failure_note_and_resumes(tmp_path):
    captured, _ = await _pass_hc(tmp_path, _Publisher(error=RuntimeError("meta 403")), _FakeArk())
    assert len(captured) == 1
    assert "飞书表格回读失败，使用沙箱版本继续" in captured[0]


async def test_pass_upload_error_appends_failure_note(tmp_path):
    captured, _ = await _pass_hc(tmp_path, _Publisher(data=b"x"), _FakeArk(fail=True))
    assert len(captured) == 1
    assert "飞书表格回读失败，使用沙箱版本继续" in captured[0]
