"""D2：报告留在机器人云空间根目录，终态卡与终态通知给出 HC3 定稿的飞书报告链接。

HC3 卡通过后会被进度卡覆盖，原终态卡只有妙搭按钮、通知只写妙搭链接，用户找不到飞书报告。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

CASE_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"


def _load(name: str, file: str):
    if str(CASE_DIR) not in sys.path:
        sys.path.insert(0, str(CASE_DIR))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, CASE_DIR / file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pipeline_store = _load("pipeline_store", "pipeline_store.py")
runner_mod = _load("topic6_runner", "topic6_runner.py")
progress_card_mod = _load("topic6_progress_card", "topic6_progress_card.py")
notifier_mod = _load("job_notifier", "job_notifier.py")

DOC = "https://bluefocus.feishu.cn/docx/AbCdEf123"
MIAODA = "https://miaoda.feishu.cn/page/xyz"


def _job(**kw):
    base = dict(job_id="job_d2test0001", chat_id="chat-1", thread_id="", user_open_id="ou_1",
                ma_session_id="sesn-1", mode="full", project_dir="/workspace/Projects/W44")
    base.update(kw)
    return pipeline_store.PipelineJob(**base)


def _buttons(card: dict) -> dict:
    out = {}
    for el in card.get("elements") or card.get("body", {}).get("elements", []):
        if el.get("tag") == "button":
            url = (el.get("behaviors") or [{}])[0].get("default_url", "")
            out[el["text"]["content"]] = url
    return out


def _card(status, **kw):
    return progress_card_mod.build_progress_card(
        job=_job(), status=status, elapsed_sec=10, tool_lines=[], **kw)


# ---- 卡片 ----------------------------------------------------------------

@pytest.mark.parametrize("status", ["done", "failed", "stopped"])
def test_terminal_card_shows_report_doc_button(status):
    btns = _buttons(_card(status, error="x", online_url=MIAODA, report_doc_url=DOC))
    assert btns.get("打开飞书报告文档") == DOC


def test_done_card_keeps_miaoda_primary_button():
    btns = _buttons(_card("done", online_url=MIAODA, report_doc_url=DOC))
    assert btns.get("打开报告") == MIAODA and btns.get("打开飞书报告文档") == DOC


@pytest.mark.parametrize("status", ["running", "wait_hc"])
def test_non_terminal_card_never_shows_report_doc(status):
    assert "打开飞书报告文档" not in _buttons(_card(status, report_doc_url=DOC))


def test_card_without_report_doc_unchanged():
    """新参数有默认值：不传时与改动前完全一致（现有调用点不受影响）。"""
    assert _card("done", online_url=MIAODA) == _card("done", online_url=MIAODA, report_doc_url="")
    assert "打开飞书报告文档" not in _buttons(_card("done", online_url=MIAODA))


# ---- 通知 ----------------------------------------------------------------

class _Feishu:
    def __init__(self):
        self.sent: list[str] = []

    def send_to_chat(self, _chat, text, roster=None):
        self.sent.append(text)

    def chat_roster(self, _chat):
        return {}


def _notifier():
    feishu = _Feishu()
    n = notifier_mod.JobNotifier(feishu)
    n._resolve_mention = lambda job: "@u"  # noqa: SLF001 - 不依赖 roster
    return n, feishu


@pytest.mark.parametrize("method,kw", [
    ("notify_done", {"online_url": MIAODA}),
    ("notify_failed", {"error": "Phase G 失败"}),
    ("notify_stopped", {"reason": "用户停止"}),
])
def test_terminal_notifications_include_report_line(method, kw):
    n, feishu = _notifier()
    getattr(n, method)(_job(), report_doc_url=DOC, **kw)
    assert f"飞书报告：{DOC}" in feishu.sent[-1]


@pytest.mark.parametrize("method", ["notify_done", "notify_failed", "notify_stopped"])
def test_notifications_without_report_doc_unchanged(method):
    n, feishu = _notifier()
    getattr(n, method)(_job())
    assert "飞书报告" not in feishu.sent[-1]


# ---- runner 取链接 ----------------------------------------------------------

class _Ark:
    async def send_message(self, *_a, **_k):
        pass


class _Sender:
    def __init__(self):
        self.patched: list[dict] = []

    def send_to_chat(self, *_a):
        pass

    def patch_interactive_card(self, _mid, card):
        self.patched.append(card)


class _Notifier:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def notify_done(self, job, **kw):
        self.calls.append(("done", kw))

    def notify_failed(self, job, **kw):
        self.calls.append(("failed", kw))

    def notify_stopped(self, job, **kw):
        self.calls.append(("stopped", kw))


@pytest.fixture()
def env(tmp_path):
    loop = asyncio.new_event_loop()
    store = pipeline_store.PipelineStore(str(tmp_path / "t.db"))
    cfg = runner_mod.Topic6Config(coordinator_agent_id="a", environment_id="e",
                                  memory_store_id="m", vault_ids=())
    sender, notifier = _Sender(), _Notifier()
    runner = runner_mod.Topic6Runner(_Ark(), sender, store, cfg, loop=loop, notifier=notifier)
    job = store.create_job(chat_id="chat-1", thread_id="", user_open_id="ou_1",
                           ma_session_id="sesn-1", mode="full", project_dir="/w/W44")
    store.set_progress_card_message_id(job.job_id, "om_card")
    runner._progress[job.job_id] = runner_mod._ProgressState()  # noqa: SLF001

    async def _run_sync(fn):
        return fn()

    runner._loop_run = _run_sync  # type: ignore[assignment]  # noqa: SLF001
    yield runner, store, sender, notifier, job, loop
    loop.close()


def test_report_doc_url_takes_latest_hc3(env):
    runner, store, *_rest, job, _loop = env
    assert runner._report_doc_url(job.job_id) == ""  # noqa: SLF001 - 未到 HC3
    store.append_hc_event(job.job_id, "HC1", {"wide_table_path": "x.xlsx"})
    old = store.append_hc_event(job.job_id, "HC3", {"feishu_doc_url": "https://f/docx/old"})
    # HC3 打回 A 后重新发布的新文档；created_at 是秒级，模拟真实间隔
    store._conn.execute("UPDATE pipeline_hc_events SET created_at = created_at - 600 WHERE id = ?",  # noqa: SLF001
                        (old,))
    store.append_hc_event(job.job_id, "HC3", {"feishu_doc_url": DOC})
    assert runner._report_doc_url(job.job_id) == DOC  # noqa: SLF001


def test_terminal_patch_renders_button_and_notifies(env):
    runner, store, sender, notifier, job, loop = env
    store.append_hc_event(job.job_id, "HC3", {"feishu_doc_url": DOC})
    loop.run_until_complete(runner._render_and_patch(  # noqa: SLF001
        job.job_id, status="failed", error="Phase G 失败", force=True))
    assert "打开飞书报告文档" in json.dumps(sender.patched[-1], ensure_ascii=False)
    assert notifier.calls[-1] == ("failed", {"error": "Phase G 失败", "report_doc_url": DOC})


def test_running_patch_skips_lookup(env, monkeypatch):
    runner, store, sender, notifier, job, loop = env
    store.append_hc_event(job.job_id, "HC3", {"feishu_doc_url": DOC})
    monkeypatch.setattr(runner, "_report_doc_url",
                        lambda _jid: pytest.fail("非终态不应查 HC3"))
    loop.run_until_complete(runner._render_and_patch(  # noqa: SLF001
        job.job_id, status="running", force=True))
    assert "打开飞书报告文档" not in json.dumps(sender.patched[-1], ensure_ascii=False)
    assert notifier.calls == []
