"""Phase1 自然语言入口（原版 Phase1_需求梳理 v1.12 Step0–5）回归。

覆盖：Step0 模板/唤起判定、Skill ready 结果 → 需求快照（塔斯汀样例）、HC-T5-0 不展示口径、
INTAKE_RESULT 各状态的 Gateway 处理、话题内补充（无需 @）、附件门禁、编排器 G1 自动采用。
全部 mock，不调用真实 Ark / 飞书 / SSJH。

运行：cd scenarios/feishu-bot/cases/topic5/tests && python3 -m unittest test_natural_language_intake
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

_CASE_DIR = Path(__file__).resolve().parent.parent
_ORCH = _CASE_DIR / "ma-resources" / "skills" / "pipeline-orchestrator"
for _p in (str(_CASE_DIR), str(_ORCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pipeline_store as ps  # noqa: E402
import topic5_intake as intake  # noqa: E402
import intake_result  # noqa: E402

import test_host_stdlib as _base  # noqa: E402

TASTIN_BRAND_FILTER = (
    "塔斯汀地貌|(塔斯汀·比伯+(justinbieber|贾斯汀比伯|baby|仿妆|pubg|合拍))|"
    "(托尼塔斯汀+复仇者联盟)|(怪奇物语+Mike)|(塔斯汀比伯+淇听晚舟)"
)


def tastin_skill_result() -> dict:
    """contract.md「返回约定」形状；口径取用户给定式（confirmation_state=carried）。"""
    return {
        "status": "ready",
        "summary": "复盘塔斯汀×Pingu联名在小红书、抖音的传播表现。",
        "resolved_request": {
            "user_description": "品牌名称：塔斯汀 活动名称：塔斯汀×Pingu联名活动 ...",
            "brand": "塔斯汀",
            "campaign_name": "塔斯汀×Pingu联名活动",
            "campaign_period": {"start": "2026-10-22", "end": "2026-11-04"},
            "platforms": ["小红书", "抖音"],
            "content_scope": "ALL",
            "field_sources": {"brand": "原话", "platforms": "原话", "content_scope": "原话"},
        },
        "campaign_name": "塔斯汀×Pingu联名活动",
        "campaign_type": {"primary": "IP联名", "secondary": [], "basis": "用户原话",
                          "state": "explicit"},
        "activity_boundary": {"included": ["塔斯汀与Pingu联名产品及周边传播"],
                              "excluded_or_pending": [], "query_period": {},
                              "actual_event_period": {}},
        "brand_keyword": "塔斯汀",
        "brand_filter": TASTIN_BRAND_FILTER,
        "activity_keywords": "塔斯汀+pingu",
        "activity_filter": "",
        "product_keywords": [],
        "dependency_state": {"metaso": "unavailable", "ssjh": "not_used", "details": ""},
        "confirmation_state": "carried_from_user_confirmation",
        "clarification_questions": [],
        "missing_fields": [],
    }


def handoff(result: dict) -> dict:
    meta = {"skill_name": "campaign-keyword-generation", "skill_version": "1.1.0",
            "rule_baseline": "Topic5 v1.11", "resolved_skill_path": "/mnt/skills/x/SKILL.md"}
    return intake_result.build_intake_result(
        result, result_path="/ws/round-1/keyword_generation_result.json",
        metadata=meta, executed_at="2026-11-25T10:00:00+08:00")


class Step0Test(unittest.TestCase):
    def test_template_is_original_code_block(self):
        text = intake.intake_template_text()
        self.assertTrue(text.startswith("请用一段话描述你的复盘需求"))
        self.assertIn("检索关键词、过滤词、活动类型由我生成", text)
        self.assertNotIn("```", text)

    def test_bare_trigger_has_no_description(self):
        for text in ("复盘报告", "复盘报告 full", "@Ada 活动复盘", "帮我做个营销复盘吧"):
            with self.subTest(text=text):
                self.assertFalse(intake.has_requirement_description(text))

    def test_brand_only_counts_as_description(self):
        # 原版：即使只有品牌也立即调用 Skill。
        self.assertTrue(intake.has_requirement_description("复盘报告 瑞幸"))
        self.assertTrue(intake.has_requirement_description("复盘报告\n品牌名称：塔斯汀"))


class IntakeResultContractTest(unittest.TestCase):
    def test_ready_handoff_keeps_expressions(self):
        out = handoff(tastin_skill_result())
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["result"]["brand_filter"], TASTIN_BRAND_FILTER)
        self.assertEqual(out["skill"]["skill_version"], "1.1.0")

    def test_ready_without_activity_keyword_rejected(self):
        bad = tastin_skill_result()
        bad["activity_keywords"] = ""
        with self.assertRaisesRegex(intake_result.IntakeResultError, "activity_keywords"):
            handoff(bad)

    def test_legacy_status_mapped(self):
        legacy = tastin_skill_result()
        legacy.pop("status")
        legacy["legacy_status"] = "ready_for_confirmation"
        self.assertEqual(handoff(legacy)["status"], "ready")

    def test_clarification_requires_questions(self):
        result = {"status": "need_clarification", "clarification_questions": []}
        with self.assertRaises(intake_result.IntakeResultError):
            handoff(result)
        result["clarification_questions"] = ["本次统计哪段时间？"]
        self.assertEqual(handoff(result)["questions"], ["本次统计哪段时间？"])

    def test_blocked_requires_reason(self):
        with self.assertRaises(intake_result.IntakeResultError):
            handoff({"status": "blocked"})

    def test_skill_version_read_from_mounted_skill(self):
        meta = intake_result.skill_metadata()
        self.assertEqual(meta["skill_name"], "campaign-keyword-generation")
        self.assertEqual(meta["skill_version"], "1.1.0")
        self.assertEqual(meta["rule_baseline"], "Topic5 v1.11")

    def test_cli_emit_writes_invocation_and_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keyword_generation_result.json"
            path.write_text(json.dumps(tastin_skill_result(), ensure_ascii=False), "utf-8")
            proc = subprocess.run(
                [sys.executable, str(_ORCH / "intake_result.py"), "emit", "--result", str(path)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            line = proc.stdout.strip().splitlines()[-1]
            self.assertTrue(line.startswith("INTAKE_RESULT "))
            invocation = json.loads((Path(tmp) / "invocation.json").read_text("utf-8"))
            self.assertEqual(invocation["status"], "ready")
            self.assertEqual(invocation["skill_version"], "1.1.0")


class SkillToRequirementTest(unittest.TestCase):
    def test_tastin_requirement(self):
        req = intake.requirement_from_skill_result(handoff(tastin_skill_result()))
        self.assertEqual(req["brand"], "塔斯汀")
        self.assertEqual(req["activity_name"], "塔斯汀×Pingu联名活动")
        self.assertEqual((req["period_start"], req["period_end"]), ("2026-10-22", "2026-11-04"))
        self.assertEqual(req["platforms"], ["小红书", "抖音"])
        self.assertEqual(req["brand_query"], "塔斯汀")
        self.assertEqual(req["brand_filter"], TASTIN_BRAND_FILTER)
        self.assertEqual(req["activity_query"], "塔斯汀+pingu")
        self.assertEqual(req["content_scope"], "all")
        self.assertEqual(req["campaign_type"], "IP联名")
        self.assertEqual(req["requirement_source"], intake.REQUIREMENT_SOURCE_SKILL)
        self.assertEqual(req["keyword_generation"]["status"], "ready")
        self.assertEqual(req["keyword_generation"]["skill_version"], "1.1.0")
        self.assertEqual(req["skill_invocations"][0]["skill"], "campaign-keyword-generation")

    def test_multi_activity_keywords(self):
        result = tastin_skill_result()
        result["activity_keywords"] = [
            {"campaign_name": "联名一期", "activity_keywords": "塔斯汀+pingu",
             "campaign_period": {"start": "2026-10-22", "end": "2026-10-28"}},
            {"campaign_name": "联名二期", "activity_keywords": "塔斯汀+企鹅家族",
             "campaign_period": {"start": "2026-10-29", "end": "2026-11-04"}},
        ]
        req = intake.requirement_from_skill_result(handoff(result))
        self.assertEqual([c["name"] for c in req["campaigns"]], ["联名一期", "联名二期"])
        self.assertEqual(req["merge_window"], {"start": "2026-10-22", "end": "2026-11-04"})

    def test_invalid_expression_raises_for_skill_repair(self):
        result = tastin_skill_result()
        result["activity_keywords"] = "塔斯汀+pingu|企鹅"   # 同层混用
        with self.assertRaises(intake.IntakeValidationError):
            intake.requirement_from_skill_result(handoff(result))

    def test_summary_hides_expressions(self):
        req = intake.requirement_from_skill_result(handoff(tastin_skill_result()))
        text = intake.intake_summary_text(req)
        self.assertIn("需求解析结果", text)
        self.assertIn("塔斯汀×Pingu联名活动", text)
        for expr in ("塔斯汀+pingu", "贾斯汀比伯", "关键词"):
            self.assertNotIn(expr, text)
        snapshot = intake.format_requirement_snapshot(req)
        self.assertNotIn("塔斯汀+pingu", snapshot)
        self.assertNotIn("贾斯汀比伯", snapshot)
        self.assertIn("关键词生成能力自动采用", snapshot)

    def test_form_snapshot_still_shows_expressions(self):
        req = intake.normalize_requirement(intake.DEMO_REQUIREMENT_DEFAULTS)
        self.assertIn("暹罗厘普", intake.format_requirement_snapshot(req))


class OrchestratorAutoAdoptTest(unittest.TestCase):
    def test_g1_does_not_pause_with_skill_requirement(self):
        import engine
        import hc_bridge
        req = intake.requirement_from_skill_result(handoff(tastin_skill_result()))
        note = json.dumps(req, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as tmp:
            dag = engine.Dag.load(_CASE_DIR / "ma-resources/io/contracts/pipeline.v0.2.yaml")
            st = engine.create_run(dag, Path(tmp), "t1", "r1")
            st.write_artifact("requirement_preset", {"note": note})
            node = dag.nodes["n01_hc_requirement"]
            import handlers
            out = handlers.human_gate(node, st, {})
            confirmed = out["artifacts"]["requirement_confirmed"]
            self.assertEqual(confirmed["validator_precheck"]["authority"],
                             hc_bridge.G1_AUTHORITY_SKILL)
            self.assertEqual(confirmed["brand_filter"], TASTIN_BRAND_FILTER)

    def test_skill_authority_requires_ready_trace(self):
        import hc_bridge
        req = intake.normalize_requirement(intake.DEMO_REQUIREMENT_DEFAULTS)
        with self.assertRaisesRegex(ValueError, "keyword_generation"):
            hc_bridge.confirmed_requirement(json.dumps(req, ensure_ascii=False),
                                            authority=hc_bridge.G1_AUTHORITY_SKILL)

    def test_g1_pauses_without_preset(self):
        import engine
        import handlers
        with tempfile.TemporaryDirectory() as tmp:
            dag = engine.Dag.load(_CASE_DIR / "ma-resources/io/contracts/pipeline.v0.2.yaml")
            st = engine.create_run(dag, Path(tmp), "t1", "r1")
            with self.assertRaises(engine.HumanPause if hasattr(engine, "HumanPause")
                                   else handlers.HumanPause):
                handlers.human_gate(dag.nodes["n01_hc_requirement"], st, {})


class StoreIntakeStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ps.PipelineStore(str(Path(self._tmp.name) / "t5.db"))
        self.job = self.store.create_job("c", "th", "u", "s", "full", "")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_wait_intake_is_active_and_claim_once(self):
        self.store.mark_wait_intake(self.job.job_id, ps.INTAKE_PHASE_TEMPLATE)
        self.assertEqual(self.store.get_active_job_by_topic("c", "th").job_id, self.job.job_id)
        self.assertIn(self.job.job_id, [j.job_id for j in self.store.list_active_jobs()])
        # 话题内不限发送人
        self.assertIsNotNone(self.store.get_wait_intake_job("c", "th", "someone"))
        self.assertEqual(self.store.claim_intake_reply(self.job.job_id),
                         ps.INTAKE_PHASE_TEMPLATE)
        self.assertIsNone(self.store.claim_intake_reply(self.job.job_id))
        job = self.store.get_job(self.job.job_id)
        self.assertEqual((job.status, job.current_phase), (ps.STATUS_RUNNING, "intake"))

    def test_wait_intake_can_be_paused(self):
        self.store.mark_wait_intake(self.job.job_id)
        self.assertTrue(self.store.mark_stopping(self.job.job_id, "u"))


class _FakeRunnerMixin:
    @classmethod
    def setUpClass(cls):
        holder = types.SimpleNamespace()
        _base.RunnerFinalResultParsingTest.setUpClass.__func__(holder)
        cls.mod = holder.runner

    def make_runner(self, store):
        runner = self.mod.Topic5Runner(object(), object(), store, self.mod.Topic5Config("a", "e"))
        self.replies, self.resumed, self.renders = [], [], []

        async def reply(_job, text):
            self.replies.append(text)

        async def resume(job, message):
            self.resumed.append((job.job_id, message))

        async def render(job_id, **kwargs):
            self.renders.append(kwargs.get("status"))

        async def attach(_job, requirement, _event_id):
            return json.dumps(requirement, ensure_ascii=False)

        runner.reply_for_job = reply
        runner.resume_job = resume
        runner._render_and_patch = render
        runner.attach_requirement_payload = attach
        return runner


class RunnerIntakeTest(_FakeRunnerMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ps.PipelineStore(str(Path(self._tmp.name) / "t5.db"))
        self.job = self.store.create_job("c", "th", "u", "s", "full", "")
        self.store.update_phase(self.job.job_id, ps.INTAKE_PHASE_CLARIFY)
        self.runner = self.make_runner(self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def idle(self, payload):
        text = "INTAKE_RESULT " + json.dumps(payload, ensure_ascii=False)
        asyncio.run(self.runner._handle_idle(self.job.job_id, [text]))
        return self.store.get_job(self.job.job_id)

    def test_extract_intake_result_from_tool_stdout(self):
        line = "x\nINTAKE_RESULT " + json.dumps({"status": "blocked", "reason": "r"})
        self.assertEqual(self.mod.extract_intake_result([line])["status"], "blocked")
        self.assertEqual(self.mod.extract_intake_result(["no marker"]), {})

    def test_clarification_relayed_once_and_waits(self):
        job = self.idle({"status": "need_clarification", "summary": "已理解：复盘瑞幸联名",
                         "questions": ["本次统计哪段时间？", "看哪些平台？"]})
        self.assertEqual(job.status, ps.STATUS_WAIT_INTAKE)
        self.assertEqual(len(self.replies), 1)
        self.assertIn("1. 本次统计哪段时间？", self.replies[0])
        self.assertIn("无需 @机器人", self.replies[0])
        self.assertEqual(self.resumed, [])

    def test_blocked_fails_job(self):
        job = self.idle({"status": "blocked", "reason": "SSJH 不可用且无可用核心式"})
        self.assertEqual(job.status, ps.STATUS_FAILED)
        self.assertIn("intake_blocked", job.last_error)

    def test_ready_shows_summary_freezes_g1_and_starts(self):
        job = self.idle(handoff(tastin_skill_result()))
        self.assertEqual(job.current_phase, "n01_hc_requirement")
        self.assertEqual(len(self.replies), 1)
        self.assertIn("需求解析结果", self.replies[0])
        self.assertNotIn("塔斯汀+pingu", self.replies[0])
        self.assertEqual(len(self.resumed), 1)
        self.assertTrue(self.resumed[0][1].startswith("[topic5-intake] ready requirement: "))
        g1 = self.store.latest_hc_for_job(self.job.job_id, "G1")
        self.assertEqual(g1.user_decision, "pass")
        self.assertEqual(g1.operator_label, "关键词生成能力自动采用")

    def test_invalid_ready_is_sent_back_then_fails(self):
        bad = handoff(tastin_skill_result())
        bad["result"]["activity_keywords"] = "塔斯汀+pingu|企鹅"
        for _ in range(self.mod.INTAKE_REPAIR_MAX):
            self.idle(bad)
        self.assertTrue(all("口径校验失败" in m for _, m in self.resumed))
        job = self.idle(bad)
        self.assertEqual(job.status, ps.STATUS_FAILED)
        self.assertIn("intake_invalid", job.last_error)

    def test_idle_without_intake_result_during_intake_fails_not_stopped(self):
        asyncio.run(self.runner._handle_idle(self.job.job_id, ["我在理解需求"]))
        job = self.store.get_job(self.job.job_id)
        self.assertEqual(job.status, ps.STATUS_FAILED)
        self.assertIn("INTAKE_RESULT", job.last_error)

    def test_reply_after_template_starts_skill(self):
        self.store.mark_wait_intake(self.job.job_id, ps.INTAKE_PHASE_TEMPLATE)
        handled = asyncio.run(self.runner.handle_intake_reply(
            chat_id="c", thread_id="th", user_open_id="other", text="复盘塔斯汀×Pingu联名"))
        self.assertTrue(handled)
        self.assertIn("intake=skill", self.resumed[0][1])
        self.assertIn("复盘塔斯汀×Pingu联名", self.resumed[0][1])
        self.assertEqual(self.replies, ["已收到补充，正在更新需求理解。"])

    def test_reply_after_clarification_is_injected_as_supplement(self):
        self.store.mark_wait_intake(self.job.job_id, ps.INTAKE_PHASE_CLARIFY)
        asyncio.run(self.runner.handle_intake_reply(
            chat_id="c", thread_id="th", user_open_id="u", text="2026-10-22 到 11-04，小红书"))
        self.assertEqual(self.resumed[0][1], "[topic5-intake] 用户补充: 2026-10-22 到 11-04，小红书")

    def test_reply_ignored_when_not_waiting(self):
        handled = asyncio.run(self.runner.handle_intake_reply(
            chat_id="c", thread_id="th", user_open_id="u", text="收到"))
        self.assertFalse(handled)

    def test_attachment_rejected_while_waiting_intake(self):
        self.store.mark_wait_intake(self.job.job_id)
        hint = self.mod.activity_attachment_hint(self.store.get_job(self.job.job_id))
        self.assertIn("梳理复盘需求", hint)

    def test_start_prompt_carries_intake_mode(self):
        self.assertIn("intake=skip", self.mod.build_start_prompt("demo", "复盘报告 demo",
                                                                  intake="skip"))
        self.assertIn("intake=skill", self.mod.build_start_prompt("full", "x", intake="skill"))


class CoordinatorPromptTest(unittest.TestCase):
    def test_prompt_wires_skill_and_handoff(self):
        text = (_CASE_DIR / "ma-resources/agents/coordinator.system.md").read_text("utf-8")
        for token in ("intake={skill|skip}", "campaign-keyword-generation/SKILL.md",
                      "references/contract.md", "intake_result.py emit", "INTAKE_RESULT",
                      "[topic5-intake] 用户补充", "[topic5-intake] ready requirement",
                      "--requirement", "metaso=unavailable"):
            self.assertIn(token, text)
        agent = json.loads((_CASE_DIR / "ma-resources/agents/coordinator.json").read_text("utf-8"))
        ids = [s["skill_id"] for s in agent["skills"]]
        self.assertIn("${skill_id.campaign-keyword-generation}", ids)


if __name__ == "__main__":
    unittest.main()
