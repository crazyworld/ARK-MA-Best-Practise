"""Gateway 侧全流程断点回归（topic5 job_6aef8ac1ce75 暴露）。

1. 协调器未输出 FINAL_RESULT 时，兜底必须保住原始错误标记，否则 n03b/n04 无法换源。
2. 失败节点续跑在 /tmp 解压编排器，必须把其他已挂载 Skill 放到同级，否则 n04/n05/DataHub 找不到依赖。
3. 新暂停点（过滤词 / 数据异常 / 标注路由）要有用户卡片和合法决定；标注审核是内部门，不能变成用户卡片。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_CASE_DIR = Path(__file__).resolve().parent.parent
if str(_CASE_DIR) not in sys.path:
    sys.path.insert(0, str(_CASE_DIR))

import pipeline_store as ps  # noqa: E402

import test_host_stdlib as _base  # noqa: E402  按模块引用，避免基类测试被重复收集

SKILL_ERROR = ("activity_pull: 自动采集不可用（缺少采集凭证 SSJH_USERNAME、SSJH_PASSWORD、"
               "SSJH_FIXED_TOKEN），可在本话题上传活动明细 .xlsx 后重试")


def _load_runner():
    holder = types.SimpleNamespace()
    _base.RunnerFinalResultParsingTest.setUpClass.__func__(holder)
    return holder.runner


def _load_hitl():
    holder = types.SimpleNamespace()
    _base.HitlActionParsingTest.setUpClass.__func__(holder)
    return holder.hitl


class FailureReasonRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def _job(self, error):
        return types.SimpleNamespace(status=ps.STATUS_FAILED, current_phase="n03b_activity_pull",
                                     failure_scope="recoverable",
                                     retry_strategy=ps.RETRY_STRATEGY_NODE, last_error=error)

    def test_reason_recovered_from_coordinator_prose(self):
        tool = "[run run-1] status=failed node=n03b_activity_pull completed=4/5 qc_rounds=0"
        prose = ("## 诊断\n**失败原因(attempt 1/2 相同,引擎原文)**:\n> `" + SKILL_ERROR + "`\n"
                 "- 运行状态:`status=failed`,`current_node=n03b_activity_pull`")
        envelope = self.runner.extract_nonstructured_failure([tool, prose])
        self.assertEqual(envelope["node"], "n03b_activity_pull")
        self.assertEqual(envelope["reason"], SKILL_ERROR)
        self.assertTrue(self.runner.is_source_repairable_failure(self._job(envelope["reason"])))

    def test_reason_recovered_from_run_log_event(self):
        tool = ("n03b_activity_pull failed " + SKILL_ERROR + " -> pause\n"
                "[run run-1] status=failed node=n03b_activity_pull completed=4/5")
        envelope = self.runner.extract_nonstructured_failure([tool])
        self.assertEqual(envelope["reason"], SKILL_ERROR)

    def test_other_nodes_keep_generic_reason(self):
        texts = ["status=failed node=n05_brand_pull", "> `" + SKILL_ERROR + "`"]
        self.assertEqual(self.runner.extract_nonstructured_failure(texts)["reason"],
                         "Durable pipeline failed at n05_brand_pull")


class RetryBundleSiblingSkillsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def test_retry_command_links_mounted_sibling_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ps.PipelineStore(str(Path(temp_dir) / "r.db"))
            job = store.create_job("c", "thread", "u", "session-1", "full",
                                   "ma-workspaces/projects/topic5/tasks/T1/runs/R1")
            store.update_phase(job.job_id, "n05_brand_pull")
            store.mark_failed(job.job_id, "x", retry_strategy=ps.RETRY_STRATEGY_NODE)
            sent = []

            class Ark:
                async def get_session(self, _sid):
                    return {"status": "idle"}

                async def upload_file(self, *_a):
                    return "file-1"

                async def add_session_file(self, *_a):
                    return None

            runner = self.runner.Topic5Runner(Ark(), object(), store,
                                              self.runner.Topic5Config("agent", "env"))

            async def capture(_job, message):
                sent.append(message)

            runner.resume_job = capture
            bundle = Path(temp_dir) / "pipeline-orchestrator.zip"
            bundle.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            old = (self.runner.is_session_resumable, self.runner.RECOVERY_BUNDLE_PATH)
            self.runner.is_session_resumable = lambda _s: True
            self.runner.RECOVERY_BUNDLE_PATH = bundle
            try:
                asyncio.run(runner.continue_job("c", "thread", "u"))
            finally:
                self.runner.is_session_resumable, self.runner.RECOVERY_BUNDLE_PATH = old
                store.close()
            command = sent[0]
            link = command.index("/mnt/skills/*")
            self.assertLess(command.index("unzip"), link)
            self.assertLess(link, command.index("run_pipeline.py retry"))
            self.assertIn('ln -sfn', command)
            self.assertIn('[ "$name" = pipeline-orchestrator ]', command)


class NewGateCardsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hitl = _load_hitl()

    def _job(self):
        return ps.PipelineJob(job_id="job_x", chat_id="c", thread_id="", user_open_id="u",
                              ma_session_id="s", mode="full", project_dir="d")

    def _decisions(self, card):
        found = []

        def walk(node):
            if isinstance(node, dict):
                value = node.get("value")
                if isinstance(value, dict) and value.get("decision"):
                    found.append(value["decision"])
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(card)
        return sorted(set(found))

    def test_store_accepts_new_hc_kinds(self):
        for kind in ("HC_FILTER", "HC_DATA", "HC_ROUTE", "R1_REVIEW", "R2_REVIEW"):
            self.assertIn(kind, ps.HC_KINDS)

    def test_filter_card_offers_keep_and_exclude(self):
        card = self.hitl.build_hc_card(self._job(), "HC_FILTER", 3,
                                       {"node": "n04_run_init", "prompt": "活动明细中有 3 条命中活动过滤词"})
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("命中活动过滤词", rendered)
        self.assertEqual(self._decisions(card), ["exclude", "keep"])

    def test_data_card_only_offers_decisions_the_bridge_accepts(self):
        # hc_bridge._resume_data_exception 只接受 pass；reject 会报错，任务停在已处理的卡片上。
        card = self.hitl.build_hc_card(self._job(), "HC_DATA", 4,
                                       {"node": "n07_clean", "prompt": "有效数据量 80 条，少于 100 条"})
        self.assertEqual(self._decisions(card), ["pass"])
        self.assertIn("新建任务", json.dumps(card, ensure_ascii=False))

    def test_route_card_offers_small_engine_and_datahub(self):
        card = self.hitl.build_hc_card(self._job(), "HC_ROUTE", 5,
                                       {"node": "n09_annotate_r1", "prompt": "DataHub 不可用"})
        self.assertEqual(self._decisions(card), ["pass"])
        self.assertIn("改用直连模型标注", json.dumps(card, ensure_ascii=False))

    def test_annotation_review_is_internal(self):
        self.assertIn("R1_REVIEW", self.hitl.INTERNAL_HC_KINDS)
        self.assertIn("R2_REVIEW", self.hitl.INTERNAL_HC_KINDS)


class InternalReviewGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def test_leaked_annotation_review_gate_fails_instead_of_card(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ps.PipelineStore(str(Path(temp_dir) / "r.db"))
            job = store.create_job("c", "", "u", "session-1", "full", "")
            runner = self.runner.Topic5Runner(object(), object(), store,
                                              self.runner.Topic5Config("agent", "env"))

            async def no_render(*_a, **_k):
                return None

            class RejectCards:
                async def send_hc_card(self, *_a, **_k):
                    raise AssertionError("annotation review must not reach users")

            runner._render_and_patch = no_render
            runner._card_sender = RejectCards()
            payload = {"hc": "R1_REVIEW", "node": "n09r_review_r1", "status": "awaiting_human",
                       "prompt": "轮次一标注审核：40 帖待抽检", "expect": ["r1_review"]}
            asyncio.run(runner._handle_idle(job.job_id, [json.dumps(payload, ensure_ascii=False)]))
            failed = store.get_job(job.job_id)
            assert failed is not None
            self.assertEqual(failed.status, ps.STATUS_FAILED)
            self.assertIn("标注审核", failed.last_error)
            store.close()


if __name__ == "__main__":
    unittest.main()
