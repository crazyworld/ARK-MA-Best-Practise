"""第 2 批：n16–n19 撰写与质检循环对齐原版（审计 P0-5/6/7 + P1 模型/超时）。

原版依据：
  - KS_Content_Analysis_Prompt.md:142  经验总结 max_tokens ≥ 3000，推荐 claude-sonnet-4-6 或以上
  - Producer 提示词 v0.60:286         QC 最多 3 轮，超限触发 HC-7 由用户处置（不是 run 失败）
  - QC_Protocol:157-163               G3 结果须来自独立审计产物，绑定当前报告版本
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _util import make_state  # noqa: E402
import handlers  # noqa: E402
import hc_bridge  # noqa: E402
import semantic  # noqa: E402

RELAY = {"LLM_RELAY_BASE_URL": "https://relay/v1", "LLM_RELAY_API_KEY": "k"}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stream(text="ok"):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    return _Resp(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n").encode())


class ReportModelTest(unittest.TestCase):
    """P1：撰写/经验总结/返修用 TOPIC5_REPORT_MODEL（默认 claude-sonnet-4-6），标注仍用 haiku。"""

    def _model_for(self, **kw):
        with patch.dict(os.environ, RELAY, clear=True), \
             patch.object(semantic.urllib.request, "urlopen", return_value=_stream()) as op:
            semantic.chat("s", "u", **kw)
        return json.loads(op.call_args.args[0].data)["model"]

    def test_default_semantic_model_unchanged(self):
        self.assertEqual(self._model_for(), "claude-haiku-4-5-20251001")

    def test_report_model_default_is_sonnet(self):
        self.assertEqual(semantic.report_model(), "claude-sonnet-4-6")
        self.assertEqual(self._model_for(model=semantic.report_model()), "claude-sonnet-4-6")

    def test_report_model_env_override(self):
        with patch.dict(os.environ, {"TOPIC5_REPORT_MODEL": "x-model"}):
            self.assertEqual(semantic.report_model(), "x-model")

    def test_writing_calls_pass_report_model_and_long_timeout(self):
        src = open(handlers.__file__, encoding="utf-8").read()
        for scope in ('lm_scope="write_report"', 'lm_scope="fix_report"'):
            for line in [l for l in src.splitlines() if scope in l]:
                self.assertIn("**_REPORT_LLM", line, line)
        self.assertGreaterEqual(handlers._REPORT_LLM["timeout"], 600)
        self.assertEqual(handlers._REPORT_LLM["model"], semantic.report_model())


class TimeoutTest(unittest.TestCase):
    """P0-5：显式 timeout 不被全局环境变量压低；环境变量只作默认值。"""

    def test_explicit_timeout_beats_env_default(self):
        with patch.dict(os.environ, {**RELAY, "TOPIC5_SEMANTIC_TIMEOUT_SEC": "180"}, clear=True):
            self.assertEqual(semantic._overall_timeout(900), 900)
            self.assertEqual(semantic._overall_timeout(None), 180)
        with patch.dict(os.environ, RELAY, clear=True):
            self.assertEqual(semantic._overall_timeout(None), 180)


class RelayWordingTest(unittest.TestCase):
    def test_no_ark_wording(self):
        src = open(semantic.__file__, encoding="utf-8").read()
        self.assertNotIn("方舟流式", src)


class FixReportMaxedTest(unittest.TestCase):
    """P0-6：节点内 3 次返修仍有 P0 → 返回带 unresolved_p0 的报告，交 n17/n18 路由 HC7，不判 run 失败。"""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.st = make_state(self.work.name)
        row = {"title": "实测", "content": "原始正文", "site_name": "小红书", "author": "机构",
               "url": "https://example.com/p", "interaction_cnt": 1}
        self.stats = {"brand": {"top_content": {"pgc": [row], "ugc": [row]}},
                      "campaigns": {"campA": {}}}
        self.base = handlers._fallback_report_md(self.stats, {}, {}, "four", "0.0")
        self.st.write_artifact("stats_bundle", self.stats)
        self.st.write_artifact("chart_urls", {"charts": []})
        self.st.write_artifact("qc_report", {"issues": [{"severity": "P0", "id": "P0-x"}]})
        self.st.write_artifact("report_md", {"markdown": self.base})

    def test_structure_errors_return_previous_with_unresolved(self):
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat", return_value="# 截断报告") as model:
            out = handlers.fix_report({"id": "n19"}, self.st, {})
        self.assertEqual(model.call_count, 3)
        report = out["artifacts"]["report_md"]
        self.assertTrue(report["unresolved_p0"])
        self.assertIn("P0-repair-structure", json.dumps(report["unresolved_p0"], ensure_ascii=False))
        self.assertIn("1.4", report["markdown"])

    def test_prompt_excludes_generated_modules(self):
        assembled = handlers.report_contract.assemble(
            self.base, plan={}, stats=self.stats, charts={"charts": []}, insight={})
        self.assertIn("report-module:start", assembled)
        self.st.write_artifact("report_md", {"markdown": assembled})
        passed = {"p0_count": 0, "issues": [], "scan_mode": "full_scan"}
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat", return_value=self.base) as model, \
             patch("handlers.validator_mod.validate_report", return_value=passed):
            handlers.fix_report({"id": "n19"}, self.st, {})
        self.assertNotIn("report-module:start", model.call_args.args[1])


class G3StaleQcTest(unittest.TestCase):
    """P0-7：G3 暂停载荷只带绑定当前报告版本的 qc_report；旧轮结果不得用于判定。"""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.st = make_state(self.work.name)
        self.st.write_artifact("report_md", {"markdown": "# r"})

    def _payload(self, qc):
        path = self.st.run_dir / "validation" / "qc_report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(qc), encoding="utf-8")
        self.st.human = {"node": "n17_validate", "prompt": "G3", "expect": ["qc_report"]}
        return hc_bridge.write_hc_pending(self.st, self.st.run_dir)

    def test_stale_qc_report_is_not_forwarded(self):
        payload = self._payload({"validator": "topic5-validator",
                                 "subject": {"artifact_id": "report_md@v0"}})
        self.assertIsNone(payload["review_context"]["qc_report"])
        self.assertTrue(payload["review_context"]["qc_report_stale"])

    def test_current_qc_report_is_forwarded(self):
        qc = {"validator": "topic5-validator", "subject": self.st.artifact_ref("report_md")}
        payload = self._payload(qc)
        self.assertEqual(payload["review_context"]["qc_report"]["validator"], "topic5-validator")


if __name__ == "__main__":
    unittest.main()
