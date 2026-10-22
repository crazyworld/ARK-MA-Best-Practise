"""针对本次修复（P0-1/P0-4/P1-1/P1-2/P1-4）的回归测试。

覆盖每个改动点的正确性 + 不破坏原有逻辑（干净特性）。
测试策略：纯函数 + 桩集成，不依赖外部服务，pytest 秒级完成。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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
hitl_mod = _load("topic6_hitl", "topic6_hitl.py")

PipelineStore = pipeline_store.PipelineStore
PipelineJob = pipeline_store.PipelineJob
Topic6Runner = runner_mod.Topic6Runner
RunnerConfig = runner_mod.Topic6Config
extract_final_online_url = runner_mod.extract_final_online_url
validate_hc_payload = runner_mod.validate_hc_payload
build_hc_card = hitl_mod.build_hc_card
build_error_diagnosis_card = progress_card_mod.build_error_diagnosis_card
build_progress_card = progress_card_mod.build_progress_card


# ---- 桩 & fixture ----


class _StubArk:
    async def send_message(self, _sid, _msg, extra_headers=None):
        pass


class _StubSender:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send_to_chat(self, chat_id: str, text: str):
        self.messages.append((chat_id, text))


def _make_runner(
    tmp_path: Path, loop: asyncio.AbstractEventLoop
) -> tuple[Topic6Runner, PipelineStore, _StubSender]:
    store = PipelineStore(str(tmp_path / "topic6.db"))
    cfg = RunnerConfig(
        coordinator_agent_id="agent-x",
        environment_id="env-x",
        memory_store_id="mem-x",
        vault_ids=(),
    )
    sender = _StubSender()
    runner = Topic6Runner(_StubArk(), sender, store, cfg, loop=loop)

    async def _noop(*_args, **_kwargs):
        return None

    runner._render_and_patch = _noop  # type: ignore[assignment]
    return runner, store, sender


def _make_job(**overrides) -> PipelineJob:
    defaults = {
        "job_id": "job_test123",
        "chat_id": "chat-1",
        "thread_id": "",
        "user_open_id": "user-1",
        "ma_session_id": "session_test456",
        "mode": "demo",
        "project_dir": "/workspace/Projects/W42_2026-10-12",
    }
    defaults.update(overrides)
    return PipelineJob(**defaults)


@pytest.fixture()
def loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


# ======================================================================
# P0-4: extract_final_online_url URL 清洗
# ======================================================================


class TestP04UrlCleaning:
    """URL 尾部粘连 %22%7D 清洗 + 正常 URL 不误截。"""

    def test_cleans_trailing_url_encoded_json(self):
        """Agent 把 JSON 结尾 "} 误拼进 URL（%22%7D），应被清洗。"""
        text = json.dumps({
            "online_url": "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html%22%7D"
        })
        result = extract_final_online_url(text)
        assert result == "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html"

    def test_clean_url_passes_through(self):
        """干净 URL 不应被截断。"""
        text = json.dumps({
            "online_url": "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html"
        })
        assert extract_final_online_url(text) == (
            "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html"
        )

    def test_url_ending_with_digit_not_truncated(self):
        """URL 尾部是数字 2/7/d 时不应被截掉（旧正则的陷阱）。"""
        text = json.dumps({
            "online_url": "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/page2"
        })
        assert extract_final_online_url(text) == (
            "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/page2"
        )

    def test_url_ending_with_d_not_truncated(self):
        text = json.dumps({
            "online_url": "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abcd"
        })
        assert extract_final_online_url(text) == (
            "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abcd"
        )

    def test_rejects_non_https(self):
        text = json.dumps({
            "online_url": "http://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html"
        })
        assert extract_final_online_url(text) == ""

    def test_rejects_unknown_domain(self):
        text = json.dumps({
            "online_url": "https://evil.com/html/abc.html"
        })
        assert extract_final_online_url(text) == ""

    def test_cleans_double_quote_suffix(self):
        """URL 尾部粘连了引号和花括号（未编码形式）。"""
        text = json.dumps({
            "online_url": 'https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html"}'
        })
        result = extract_final_online_url(text)
        assert result == "https://bmc-aigc-hb.bluemediacdn.com/html/2026-10/abc.html"


# ======================================================================
# P0-4: Phase H 诊断
# ======================================================================


class TestP04PhaseHDiagnosis:
    """Phase H idle 但无 online_url 时，走专项诊断而非通用 nudge。"""

    def test_phase_h_idle_marks_failed_not_nudge(self, loop, tmp_path):
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        store.update_phase(job.job_id, "H")
        runner._progress[job.job_id] = runner_mod._ProgressState()

        result = loop.run_until_complete(
            runner._handle_idle(job.job_id, ["报告已生成，正在发布..."])
        )

        assert result is None  # 终态，不是 nudge
        reloaded = store.get_job(job.job_id)
        assert reloaded.status == pipeline_store.STATUS_FAILED
        assert "Phase H" in reloaded.last_error or "妙搭" in reloaded.last_error

    def test_phase_a_idle_still_nudges(self, loop, tmp_path):
        """非 Phase H 的 idle 应走正常 nudge 路径，不受 Phase H 诊断影响。"""
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        store.update_phase(job.job_id, "A")
        runner._progress[job.job_id] = runner_mod._ProgressState()

        result = loop.run_until_complete(
            runner._handle_idle(job.job_id, ["正在取数..."])
        )

        # 应返回 nudge 消息（字符串），而非 None（终态）
        assert result is not None
        assert isinstance(result, str)
        assert "Phase A" in result


# ======================================================================
# P0-1: _diagnose_interruption
# ======================================================================


class TestP01Diagnosis:
    """结构化诊断方法正确识别启动失败 vs 可恢复中断。"""

    def test_early_idle_no_output_requires_human(self, tmp_path, loop):
        runner, store, _ = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="",
        )
        state = runner_mod._ProgressState()
        state.started_at_wall = time.time() - 10  # 10 秒前创建
        runner._progress[job.job_id] = state

        requires_human, summary = runner._diagnose_interruption(
            job.job_id, "A", [],  # 无输出
        )

        assert requires_human is True
        assert "启动" in summary

    def test_normal_idle_does_not_require_human(self, tmp_path, loop):
        runner, store, _ = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        state = runner_mod._ProgressState()
        state.started_at_wall = time.time() - 3600  # 1 小时前创建
        runner._progress[job.job_id] = state

        requires_human, summary = runner._diagnose_interruption(
            job.job_id, "C1", ["正在标注..."],
        )

        assert requires_human is False

    def test_error_pattern_after_max_nudges_requires_human(self, tmp_path, loop):
        runner, store, _ = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        state = runner_mod._ProgressState()
        state.started_at_wall = time.time() - 7200
        state.idle_nudge_count = 3  # 已用完 nudge
        runner._progress[job.job_id] = state

        requires_human, summary = runner._diagnose_interruption(
            job.job_id, "C2", ["执行 merge 时发生 error: disk full"],
        )

        assert requires_human is True
        assert "error" in summary.lower() or "错误" in summary

    def test_error_pattern_with_nudges_remaining_does_not_block(self, tmp_path, loop):
        """有 error 关键词但 nudge 还没用完，应让 nudge 继续尝试。"""
        runner, store, _ = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        state = runner_mod._ProgressState()
        state.started_at_wall = time.time() - 3600
        state.idle_nudge_count = 1  # 还有 nudge 额度
        runner._progress[job.job_id] = state

        requires_human, _ = runner._diagnose_interruption(
            job.job_id, "D", ["error: temporary network issue"],
        )

        assert requires_human is False


# ======================================================================
# P0-1: build_error_diagnosis_card
# ======================================================================


class TestP01ErrorCard:

    def test_card_has_schema_and_error(self):
        card = build_error_diagnosis_card(
            error="SSE connection lost",
            phase="C1",
            job_id="job_abc",
            suggestion="发送 /continue 恢复",
        )
        assert card["schema"] == "2.0"
        assert card["header"]["template"] == "red"
        body_text = json.dumps(card, ensure_ascii=False)
        assert "SSE connection lost" in body_text
        assert "job_abc" in body_text
        assert "C1" in body_text
        assert "/continue" in body_text

    def test_card_without_job_id(self):
        card = build_error_diagnosis_card(error="boom")
        assert card["schema"] == "2.0"
        # 不应有空的 job_id 占位
        body_text = json.dumps(card, ensure_ascii=False)
        assert "boom" in body_text


# ======================================================================
# P1-1: HC 卡片 remark 表单
# ======================================================================


class TestP11RemarkForm:

    def _build_card(self):
        job = _make_job()
        return build_hc_card(job, "HC1", event_id=1, payload={"mode": "demo"})

    def test_hc_card_has_three_action_zones(self):
        """卡片应有：通过按钮 + 打回表单 + 补充说明表单。"""
        card = self._build_card()
        elements = card["body"]["elements"]
        buttons = [e for e in elements if e.get("tag") == "button"]
        forms = [e for e in elements if e.get("tag") == "form"]
        assert len(buttons) >= 1, "应有通过按钮"
        assert len(forms) == 2, "应有打回表单 + 补充说明表单"

    def test_remark_form_input_name_is_remark_note(self):
        """input name 必须是 remark_note（与 _extract_action_note 一致）。"""
        card = self._build_card()
        elements = card["body"]["elements"]
        remark_form = next(
            e for e in elements
            if e.get("tag") == "form" and "remark" in e.get("name", "")
        )
        input_elem = next(
            e for e in remark_form["elements"] if e.get("tag") == "input"
        )
        assert input_elem["name"] == "remark_note"

    def test_remark_button_carries_remark_decision(self):
        card = self._build_card()
        elements = card["body"]["elements"]
        remark_form = next(
            e for e in elements
            if e.get("tag") == "form" and "remark" in e.get("name", "")
        )
        submit = next(
            e for e in remark_form["elements"] if e.get("tag") == "button"
        )
        value = submit["behaviors"][0]["value"]
        assert value["decision"] == "remark"

    def test_footer_mentions_three_actions(self):
        card = self._build_card()
        elements = card["body"]["elements"]
        footers = [
            e for e in elements
            if e.get("tag") == "markdown" and "补充说明" in e.get("content", "")
        ]
        assert len(footers) >= 1

    def test_remark_note_is_extracted_by_existing_backend(self):
        """确认 _extract_action_note 能从 remark_note 字段提取值。"""
        _extract_action_note = hitl_mod._extract_action_note
        action = {
            "action": {
                "name": "remark_submit",
                "form_value": {"remark_note": "请补充 R3 数据来源"},
            }
        }
        note = _extract_action_note(action)
        assert note == "请补充 R3 数据来源"


# ======================================================================
# P1-2: HC 审核后 @ 通知
# ======================================================================


class TestP12Notification:
    """@ 通知在 try 内、resume 后发出；resume 失败时不发。"""

    def test_resume_success_sends_notification(self, loop, tmp_path):
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        event_id = store.append_hc_event(job.job_id, "HC1", {"hc": "HC1", "wide_table_path": "/x"})
        store.mark_wait_hc(job.job_id, "HC1")

        hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner))
        action = {
            "action": {"value": {"job_id": job.job_id, "hc": "HC1", "event_id": event_id, "decision": "pass"}},
            "open_message_id": "msg-1",
            "chat_id": "c1",
            "operator": {"open_id": "u1"},
        }

        async def _fake_resume(job, msg):
            pass  # 成功
        runner.resume_job = _fake_resume

        async def _noop_patch(*a, **kw):
            pass
        runner.patch_job_card = _noop_patch

        loop.run_until_complete(hitl.handle_card_action(action))

        # 应有通知消息
        notifications = [
            (cid, txt) for cid, txt in sender.messages
            if "审核结果" in txt and "通过" in txt
        ]
        assert len(notifications) >= 1

    def test_resume_failure_does_not_send_notification(self, loop, tmp_path):
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        event_id = store.append_hc_event(job.job_id, "HC1", {"hc": "HC1", "wide_table_path": "/x"})
        store.mark_wait_hc(job.job_id, "HC1")

        hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner))
        action = {
            "action": {"value": {"job_id": job.job_id, "hc": "HC1", "event_id": event_id, "decision": "pass"}},
            "open_message_id": "msg-1",
            "chat_id": "c1",
            "operator": {"open_id": "u1"},
        }

        async def _failing_resume(job, msg):
            raise RuntimeError("MA Session terminated")
        runner.resume_job = _failing_resume

        async def _noop_patch(*a, **kw):
            pass
        runner.patch_job_card = _noop_patch

        loop.run_until_complete(hitl.handle_card_action(action))

        # 不应有"通过"通知（应只有"续跑失败"）
        pass_notifications = [
            txt for _, txt in sender.messages if "审核结果" in txt and "通过" in txt
        ]
        assert len(pass_notifications) == 0


# ======================================================================
# P1-4: validate_hc_payload 扩展
# ======================================================================


class TestP14ValidatePayload:

    def test_hc3_missing_url_rejected(self):
        assert validate_hc_payload({"hc": "HC3"}) is not None
        assert "feishu_doc_url" in validate_hc_payload({"hc": "HC3"})

    def test_hc3_with_url_accepted(self):
        assert validate_hc_payload({
            "hc": "HC3", "feishu_doc_url": "https://feishu.cn/docx/abc"
        }) is None

    def test_hc3_block_verdict_not_rejected(self):
        """BLOCK 不应在 validate_hc_payload 中 reject——已有卡片红色警示处理。"""
        result = validate_hc_payload({
            "hc": "HC3",
            "feishu_doc_url": "https://feishu.cn/docx/abc",
            "validator_verdict": {"conclusion": "BLOCK", "issues": [{"description": "缺少板块"}]},
        })
        assert result is None  # 不 reject

    def test_hc1_normal_payload_missing_wide_table_rejected(self):
        result = validate_hc_payload({"hc": "HC1", "mode": "full"})
        assert result is not None
        assert "wide_table" in result

    def test_hc1_with_wide_table_accepted(self):
        result = validate_hc_payload({
            "hc": "HC1", "wide_table_path": "/mnt/session/outputs/W42/wide_table.xlsx"
        })
        assert result is None

    def test_hc1_fallback_skips_wide_table_check(self):
        """__fallback__=True 的兜底 payload 跳过 wide_table_path 校验。"""
        result = validate_hc_payload({
            "hc": "HC1", "__fallback__": True, "mode": "full"
        })
        assert result is None  # 不 reject

    def test_hc2_fallback_also_skips(self):
        result = validate_hc_payload({
            "hc": "HC2", "__fallback__": True
        })
        assert result is None

    def test_hc2_normal_missing_wide_table_rejected(self):
        result = validate_hc_payload({"hc": "HC2"})
        assert result is not None


# ======================================================================
# 进度卡片 session_id 展示
# ======================================================================


class TestSessionIdDisplay:

    def test_progress_card_shows_session_id(self):
        job = _make_job()
        card = build_progress_card(
            job=job, status="running", elapsed_sec=60,
            tool_lines=[], overflow=0,
        )
        body_text = json.dumps(card, ensure_ascii=False)
        assert "session_test456" in body_text

    def test_hc_card_shows_session_id(self):
        job = _make_job()
        card = build_hc_card(job, "HC1", event_id=1, payload={})
        body_text = json.dumps(card, ensure_ascii=False)
        assert "session_test456" in body_text

    def test_error_diagnosis_card_shows_job_id(self):
        card = build_error_diagnosis_card(
            error="test", job_id="job_xyz", phase="C1"
        )
        body_text = json.dumps(card, ensure_ascii=False)
        assert "job_xyz" in body_text


# ======================================================================
# 干净特性：原有逻辑不被破坏
# ======================================================================


class TestCleanness:
    """确保改动没有破坏原有行为。"""

    def test_hc_payload_json_extraction_still_works(self):
        """原有的 HC JSON 提取不受 URL 清洗等改动影响。"""
        extract_hc_payload = runner_mod.extract_hc_payload
        text = '{"hc": "HC1", "mode": "full", "project_dir": "/workspace/W42"}'
        result = extract_hc_payload(text)
        assert result is not None
        assert result["hc"] == "HC1"

    def test_fallback_intent_detection_still_works(self):
        detect_hc_intent = runner_mod.detect_hc_intent
        assert detect_hc_intent("HC1 等用户确认") == "HC1"
        assert detect_hc_intent("无关文本") is None

    def test_original_hc3_validation_preserved(self):
        """HC3 缺 URL 的原有校验不受新增逻辑影响。"""
        assert validate_hc_payload({"hc": "HC3"}) == (
            "HC3 缺少 feishu_doc_url：Phase F 飞书发布未完成"
        )

    def test_online_url_rejects_feishu_doc_url(self):
        """飞书 docx URL 不应被当作 Phase H 的 online_url（原有行为）。"""
        text = json.dumps({
            "online_url": "https://bluefocus.feishu.cn/docx/ABC123"
        })
        assert extract_final_online_url(text) == ""

    def test_hc_card_still_has_pass_and_reject(self):
        """remark 表单不应影响原有的通过按钮和打回表单。"""
        job = _make_job()
        card = build_hc_card(job, "HC2", event_id=1, payload={})
        elements = card["body"]["elements"]
        # 通过按钮
        pass_buttons = [
            e for e in elements
            if e.get("tag") == "button" and "通过" in e.get("text", {}).get("content", "")
        ]
        assert len(pass_buttons) >= 1
        # 打回表单
        reject_forms = [
            e for e in elements
            if e.get("tag") == "form" and "reject" in e.get("name", "")
        ]
        assert len(reject_forms) == 1

    def test_hc1_pass_decision_message_includes_reread_hint(self, loop, tmp_path):
        """HC1 通过后 decision_message 应包含飞书表格回读提示。"""
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        payload = {"hc": "HC1", "wide_table_path": "/mnt/session/outputs/W42/05_合并/wide_table_demo_r1.xlsx"}
        event_id = store.append_hc_event(job.job_id, "HC1", payload)
        store.mark_wait_hc(job.job_id, "HC1")

        hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner))
        action = {
            "action": {"value": {"job_id": job.job_id, "hc": "HC1", "event_id": event_id, "decision": "pass"}},
            "open_message_id": "msg-1",
            "chat_id": "c1",
            "operator": {"open_id": "u1"},
        }

        captured_messages = []
        async def _spy_resume(job, msg):
            captured_messages.append(msg)
        runner.resume_job = _spy_resume

        async def _noop_patch(*a, **kw):
            pass
        runner.patch_job_card = _noop_patch

        loop.run_until_complete(hitl.handle_card_action(action))

        assert len(captured_messages) == 1
        msg = captured_messages[0]
        assert "HC1 pass" in msg
        assert "飞书在线表格" in msg
        assert "wide_table" in msg

    def test_hc2_pass_decision_message_includes_reread_hint(self, loop, tmp_path):
        """HC2 通过后同样包含回读提示。"""
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="full", project_dir="/workspace/Projects/W42",
        )
        payload = {"hc": "HC2", "wide_table_path": "/mnt/session/outputs/W42/05_合并/wide_table_skip_sampling_r1.xlsx"}
        event_id = store.append_hc_event(job.job_id, "HC2", payload)
        store.mark_wait_hc(job.job_id, "HC2")

        hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner))
        action = {
            "action": {"value": {"job_id": job.job_id, "hc": "HC2", "event_id": event_id, "decision": "pass"}},
            "open_message_id": "msg-1",
            "chat_id": "c1",
            "operator": {"open_id": "u1"},
        }

        captured_messages = []
        async def _spy_resume(job, msg):
            captured_messages.append(msg)
        runner.resume_job = _spy_resume

        async def _noop_patch(*a, **kw):
            pass
        runner.patch_job_card = _noop_patch

        loop.run_until_complete(hitl.handle_card_action(action))

        assert len(captured_messages) == 1
        assert "飞书在线表格" in captured_messages[0]

    def test_hc3_pass_decision_message_no_spreadsheet_hint(self, loop, tmp_path):
        """HC3 通过后不应包含宽表回读提示（HC3 是报告文档，不是表格）。"""
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        payload = {"hc": "HC3", "feishu_doc_url": "https://feishu.cn/docx/abc"}
        event_id = store.append_hc_event(job.job_id, "HC3", payload)
        store.mark_wait_hc(job.job_id, "HC3")

        hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner))
        action = {
            "action": {"value": {"job_id": job.job_id, "hc": "HC3", "event_id": event_id, "decision": "pass"}},
            "open_message_id": "msg-1",
            "chat_id": "c1",
            "operator": {"open_id": "u1"},
        }

        captured_messages = []
        async def _spy_resume(job, msg):
            captured_messages.append(msg)
        runner.resume_job = _spy_resume

        async def _noop_patch(*a, **kw):
            pass
        runner.patch_job_card = _noop_patch

        loop.run_until_complete(hitl.handle_card_action(action))

        assert len(captured_messages) == 1
        assert "HC3 pass" in captured_messages[0]
        assert "飞书在线表格" not in captured_messages[0]

    def test_hc1_reject_no_reread_hint(self, loop, tmp_path):
        """HC1 打回时不应包含回读提示。"""
        runner, store, sender = _make_runner(tmp_path, loop)
        job = store.create_job(
            chat_id="c1", thread_id="", user_open_id="u1",
            ma_session_id="s1", mode="demo", project_dir="/workspace/Projects/W42",
        )
        payload = {"hc": "HC1", "wide_table_path": "/mnt/session/outputs/W42/wide_table.xlsx"}
        event_id = store.append_hc_event(job.job_id, "HC1", payload)
        store.mark_wait_hc(job.job_id, "HC1")

        hitl = hitl_mod.Topic6Hitl(hitl_mod.Topic6HitlDeps(store=store, runner=runner))
        action = {
            "action": {
                "value": {"job_id": job.job_id, "hc": "HC1", "event_id": event_id, "decision": "reject"},
                "form_value": {"reject_reason": "C0 有效率偏低"},
            },
            "open_message_id": "msg-1",
            "chat_id": "c1",
            "operator": {"open_id": "u1"},
        }

        captured_messages = []
        async def _spy_resume(job, msg):
            captured_messages.append(msg)
        runner.resume_job = _spy_resume

        async def _noop_patch(*a, **kw):
            pass
        runner.patch_job_card = _noop_patch

        loop.run_until_complete(hitl.handle_card_action(action))

        assert len(captured_messages) == 1
        assert "reject" in captured_messages[0]
        assert "飞书在线表格" not in captured_messages[0]

    def test_progress_card_still_renders_tool_lines(self):
        """session_id 新增不应破坏原有的 tool_lines 渲染。"""
        job = _make_job()
        card = build_progress_card(
            job=job, status="running", elapsed_sec=120,
            tool_lines=["10:00:01 · bash · ls"], overflow=0,
        )
        body_text = json.dumps(card, ensure_ascii=False)
        assert "bash" in body_text
        assert "session_test456" in body_text  # 新增的 session_id 也在
