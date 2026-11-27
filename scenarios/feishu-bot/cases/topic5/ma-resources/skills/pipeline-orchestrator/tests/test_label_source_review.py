"""FIX-30 第 5 条：n10 的 D1/D2 来源审核（策略 + Validator + 必要时人工）。

方案依据：
  - 确定性策略先过；通过后委派独立 Validator，对照 review_items 的原帖
    （evidence[].excerpt，代码复制非模型文本）审核每个类别定义是否与原帖一致。
  - 结果写入 confirmed_opinion_labels.validator_review；有 P0 时转人工。
  - 动态 UGC 类别不再一律转人工：Validator 审核通过即自动确认。
  - 审核无法完成（无语义能力 / 调用异常 / 无结论）时不得当作通过。

运行：python3 -m unittest tests.test_label_source_review
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import label_review as L  # noqa: E402
from handlers import HumanPause  # noqa: E402
from _util import make_state  # noqa: E402

OPINION_NAMES = ["口味体验", "包装设计", "价格感知", "购买便利", "品牌联想"]
UGC_NAMES = ["模仿打卡", "创意改编", "测评分享", "晒单分享"]


def _item(name, status="grounded", excerpt=None):
    return {
        "name": name,
        "definition": f"{name}的定义",
        "boundary": f"{name}的边界",
        "evidence_status": status,
        "evidence": ([{"usage": "representative", "excerpt": excerpt or f"{name}原帖"}]
                     if status == "grounded" else []),
    }


def _opinion(names=OPINION_NAMES):
    return {"candidates": list(names), "source": "llm", "evidence_count": 12,
            "review_schema_version": 1,
            "review_items": [_item(n) for n in names]}


def _ugc(names=UGC_NAMES, mode="dynamic"):
    return {"mode": mode, "categories": list(names), "review_schema_version": 1,
            "review_items": [_item(n) for n in names]}


def _all_pass(names):
    return [{"name": n, "verdict": "pass", "severity": "P1", "reason": "一致"}
            for n in names]


class PromptTest(unittest.TestCase):
    def test_prompt_carries_definition_boundary_and_excerpt(self):
        prompt = L.source_review_prompt("opinion", [_item("口味体验", excerpt="奶香很浓")])
        self.assertIn("口味体验", prompt)
        self.assertIn("口味体验的定义", prompt)
        self.assertIn("口味体验的边界", prompt)
        self.assertIn("奶香很浓", prompt)
        self.assertIn("P0", prompt)

    def test_ungrounded_items_not_sent(self):
        artifact = {"review_items": [_item("A"), _item("B", status="missing")]}
        self.assertEqual([i["name"] for i in L._review_subject("opinion", artifact)], ["A"])


class ApplyTest(unittest.TestCase):
    def test_all_pass_has_no_p0(self):
        art = _opinion()
        p0, meta = L.apply_source_review("opinion", art, _all_pass(OPINION_NAMES))
        self.assertEqual(p0, [])
        self.assertFalse(meta["has_p0"])
        self.assertEqual(meta["actor_role"], "Validator")
        self.assertEqual(meta["mode"], "independent-agent")
        self.assertEqual(meta["reviewed"], len(OPINION_NAMES))

    def test_fail_p0_reported(self):
        verdicts = _all_pass(OPINION_NAMES)
        verdicts[0] = {"name": "口味体验", "verdict": "fail", "severity": "P0",
                       "reason": "原帖讲的是包装"}
        p0, meta = L.apply_source_review("opinion", _opinion(), verdicts)
        self.assertEqual(len(p0), 1)
        self.assertIn("原帖讲的是包装", p0[0])
        self.assertTrue(meta["has_p0"])

    def test_fail_p1_not_blocking(self):
        verdicts = _all_pass(OPINION_NAMES)
        verdicts[0] = {"name": "口味体验", "verdict": "fail", "severity": "P1",
                       "reason": "措辞可再精炼"}
        p0, meta = L.apply_source_review("opinion", _opinion(), verdicts)
        self.assertEqual(p0, [])
        self.assertEqual(meta["decisions"]["口味体验"]["severity"], "P1")

    def test_missing_verdict_counts_as_unreviewed_p0(self):
        verdicts = _all_pass(OPINION_NAMES[1:])
        p0, meta = L.apply_source_review("opinion", _opinion(), verdicts)
        self.assertEqual(meta["decisions"]["口味体验"]["verdict"], "unreviewed")
        self.assertTrue(any("口味体验" in x for x in p0))


class RunTest(unittest.TestCase):
    def test_no_items_needs_no_review(self):
        p0, meta = L.run_source_review("opinion", {"review_items": []})
        self.assertEqual(p0, [])
        self.assertFalse(meta["has_p0"])

    def test_llm_unavailable_blocks(self):
        with patch("semantic.llm_available", return_value=False):
            p0, meta = L.run_source_review("opinion", _opinion())
        self.assertTrue(meta["has_p0"])
        self.assertEqual(meta["reason"], "llm_unavailable")

    def test_llm_error_blocks(self):
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=RuntimeError("boom")):
            p0, meta = L.run_source_review("opinion", _opinion())
        self.assertTrue(meta["has_p0"])
        self.assertTrue(meta["reason"].startswith("llm_error"))

    def test_garbage_output_blocks(self):
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", return_value={"oops": 1}):
            p0, meta = L.run_source_review("opinion", _opinion())
        self.assertEqual(meta["reason"], "no_verdicts")

    def test_valid_output_passes(self):
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", return_value=_all_pass(OPINION_NAMES)):
            p0, meta = L.run_source_review("opinion", _opinion())
        self.assertEqual(p0, [])


class GateTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _gate(self):
        return handlers.opinion_policy_gate(
            {"id": "n10_hc_opinion", "outputs": ["confirmed_opinion_labels"]},
            self.st, {})

    def _fake_chat(self, fail_name=None):
        def fake(system, _user, **_kw):
            names = OPINION_NAMES if "观点类别" in system else UGC_NAMES
            out = _all_pass(names)
            if fail_name in names:
                out[names.index(fail_name)] = {
                    "name": fail_name, "verdict": "fail", "severity": "P0",
                    "reason": "定义与原帖矛盾"}
            return out
        return fake

    def test_dynamic_ugc_auto_confirms_when_review_passes(self):
        """原来动态 UGC 一律转人工；现在 Validator 审核通过即自动确认。"""
        self.st.write_artifact("opinion_candidates", _opinion())
        self.st.write_artifact("ugc_category_list", _ugc(mode="dynamic"))
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=self._fake_chat()):
            out = self._gate()
        confirmed = out["artifacts"]["confirmed_opinion_labels"]
        self.assertTrue(confirmed["automatic"])
        review = confirmed["validator_review"]
        self.assertFalse(review["has_p0"])
        self.assertEqual(review["opinion"]["reviewed"], len(OPINION_NAMES))
        self.assertEqual(review["ugc"]["reviewed"], len(UGC_NAMES))

    def test_source_review_p0_pauses_for_human(self):
        self.st.write_artifact("opinion_candidates", _opinion())
        self.st.write_artifact("ugc_category_list", _ugc())
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=self._fake_chat("测评分享")):
            out = self._gate()
        review = out["artifacts"]["confirmed_opinion_labels"]["label_review"]
        self.assertFalse(review["passed"])
        joined = "；".join(review["reasons"])
        self.assertIn("来源审核", joined)
        self.assertIn("测评分享", joined)

    def test_no_semantic_capability_pauses(self):
        self.st.write_artifact("opinion_candidates", _opinion())
        self.st.write_artifact("ugc_category_list", _ugc())
        with patch("semantic.llm_available", return_value=False):
            out = self._gate()
        review = out["artifacts"]["confirmed_opinion_labels"]["label_review"]
        self.assertFalse(review["passed"])
        self.assertTrue(any("来源审核" in r for r in review["reasons"]))

    def test_pgc_only_skips_ugc_review(self):
        self.st.write_artifact("opinion_candidates", _opinion())
        self.st.write_artifact("ugc_category_list", {"not_applicable": True})
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=self._fake_chat()):
            out = self._gate()
        self.assertEqual(out["artifacts"]["confirmed_opinion_labels"]
                         ["validator_review"]["ugc"], {})

    def test_policy_failure_pauses_before_review(self):
        """确定性策略没过时直接转人工，不浪费一次语义审核。"""
        self.st.write_artifact("opinion_candidates",
                               {"candidates": ["其他"], "source": "llm",
                                "evidence_count": 1})
        self.st.write_artifact("ugc_category_list", _ugc())
        with patch("semantic.chat_json") as chat:
            out = self._gate()
        chat.assert_not_called()
        review = out["artifacts"]["confirmed_opinion_labels"]["label_review"]
        self.assertFalse(review["passed"])


if __name__ == "__main__":
    unittest.main()
