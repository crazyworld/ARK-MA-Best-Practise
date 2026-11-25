"""轮次二按原版路由（KS_Data_Processing「2B（≥ 1000 行）DataHub Pass 2」）。

回归 job_bf3c6b52db7b：9094 行的轮次二一律走直连（正面 7289 + 负面 476 + 属性 9083 ≈ 1100 次调用），
任一批异常被 `except Exception` 吞掉 → 报「标注能力不可用」，看不到真实原因。修复：
  1. 行数 ≥ 阈值：三个独立 DataHub 任务（正面观点 / 负面观点 / 属性），model=gpt-5.4-mini；
  2. DataHub 行级缺口只交直连模型补标（与轮次一一致），合法结果缓存，重试只提交未命中行；
  3. DataHub 不可用 → DataHubRouteRequired（先问用户，禁止自行降级）；
  4. 直连异常不再吞掉：degraded_reason 带出异常类型与信息。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import annotate as A  # noqa: E402
import datahub  # noqa: E402
import semantic  # noqa: E402

POS, NEG, UGC = ["口感风味"], ["价格偏高"], ["晒单分享"]


def _rows():
    rows = []
    for i, senti in enumerate((1, 1, 1, -1, -1, 0)):
        rows.append({"id": f"r{i}", "title": "t", "content": f"c{i}", "relevance": True,
                     "ai_sentiment": senti, "content_mode": "UGC"})
    rows.append({"id": "r9", "title": "t", "content": "x", "relevance": False, "ai_sentiment": 0})
    return rows


def _fake_dh(missing=()):
    calls = []

    def fake(rows, prompt, **kw):
        task = "attr" if "discussion_dimension" in prompt else ("neg" if "负面" in prompt else "pos")
        calls.append((task, sorted(r["_annot_id"] for r in rows)))
        out = {}
        for r in rows:
            rid = r["_annot_id"]
            if rid in missing:
                out[rid] = {"_error": "parse"}
            elif task == "attr":
                out[rid] = {"ugc_category": "晒单分享", "discussion_dimension": "产品"}
            else:
                out[rid] = {"opinion": POS[0] if task == "pos" else NEG[0]}
        return out
    return fake, calls


def _llm(system, user, **_kw):
    items = json.loads(user)
    if "discussion_dimension" in system:
        return [{"id": i["id"], "ugc_category": "其他", "discussion_dimension": "活动"} for i in items]
    return [{"id": i["id"], "opinion": "其他"} for i in items]


class R2DataHubRouteTest(unittest.TestCase):
    def _run(self, rows, fake, *, threshold=3, available=True, llm=_llm, cache_dir=None,
             route_decision=None):
        with patch.object(datahub, "api_available", return_value=available), \
             patch.object(datahub, "batch_annotate", side_effect=fake), \
             patch.object(semantic, "llm_available", return_value=True), \
             patch.object(semantic, "chat_json", side_effect=llm) as chat:
            result = A.annotate_round2(rows, opinion_labels=POS, negative_labels=NEG,
                                       ugc_categories=UGC, threshold=threshold,
                                       campaign_name="活动", cache_dir=cache_dir,
                                       route_decision=route_decision)
        return result, chat

    def test_large_runs_three_datahub_tasks_without_direct_calls(self):
        fake, calls = _fake_dh()
        result, chat = self._run(_rows(), fake)
        self.assertEqual(result["routed_via"], "datahub")
        self.assertEqual(sorted(task for task, _ in calls), ["attr", "neg", "pos"])
        ids = dict(calls)
        self.assertEqual(ids["pos"], ["r0", "r1", "r2"])
        self.assertEqual(ids["neg"], ["r3", "r4"])
        self.assertEqual(ids["attr"], ["r0", "r1", "r2", "r3", "r4", "r5"])  # 含中性
        chat.assert_not_called()
        self.assertFalse(result["coverage"]["has_error"])
        self.assertNotIn("degraded", result)

    def test_datahub_prompts_are_single_object_templates(self):
        fake, _ = _fake_dh()
        prompts = []

        def capture(rows, prompt, **kw):
            prompts.append(prompt)
            return fake(rows, prompt, **kw)

        self._run(_rows(), capture)
        for prompt in prompts:
            self.assertIn("{{content}}", prompt)
            self.assertIn("{{title}}", prompt)
            self.assertNotIn("JSON 数组", prompt)

    def test_datahub_gaps_backfilled_by_direct_model(self):
        fake, _ = _fake_dh(missing={"r1", "r5"})
        result, chat = self._run(_rows(), fake)
        by_id = {r["_annot_id"]: r for r in result["rows"]}
        self.assertEqual(by_id["r1"]["opinion"], "其他")
        self.assertEqual(by_id["r5"]["discussion_dimension"], "活动")
        self.assertEqual(by_id["r0"]["opinion"], POS[0])
        sent = sorted(i["id"] for c in chat.call_args_list for i in json.loads(c.args[1]))
        self.assertEqual(sent, ["r1", "r1", "r5"])  # r1 观点 + r1 属性 + r5 属性
        self.assertEqual(result["backfill"]["remaining"], 0)

    def test_retry_reuses_datahub_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, _ = _fake_dh(missing={"r2"})
            self._run(_rows(), fake, cache_dir=tmp, llm=lambda *_a, **_k: [])
            fake2, calls2 = _fake_dh()
            self._run(_rows(), fake2, cache_dir=tmp)
        self.assertEqual(sorted(calls2), [("attr", ["r2"]), ("pos", ["r2"])])

    def test_large_without_datahub_asks_user(self):
        fake, _ = _fake_dh()
        with self.assertRaises(A.DataHubRouteRequired):
            self._run(_rows(), fake, available=False)

    def test_small_still_uses_direct_model(self):
        fake, calls = _fake_dh()
        result, chat = self._run(_rows(), fake, threshold=1000)
        self.assertEqual(calls, [])
        self.assertEqual(result["routed_via"], "small_engine")
        self.assertTrue(chat.called)

    def test_direct_failure_reason_is_surfaced(self):
        fake, _ = _fake_dh()

        def boom(*_a, **_k):
            raise RuntimeError("relay 400 body=quota")

        result, _ = self._run(_rows(), fake, threshold=1000, llm=boom)
        self.assertTrue(result["degraded"])
        self.assertIn("relay 400 body=quota", result["degraded_reason"])


class R2HandlerPauseTest(unittest.TestCase):
    def test_handler_turns_route_required_into_human_pause(self):
        import handlers
        src = open(handlers.__file__, encoding="utf-8").read()
        r2 = src[src.index("annotate_mod.annotate_round2("):]
        self.assertLess(r2.index("except annotate_mod.DataHubRouteRequired"), r2.index("if result.get(\"degraded\")"))


if __name__ == "__main__":
    unittest.main()
