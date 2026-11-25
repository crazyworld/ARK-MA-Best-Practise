"""轮次一 DataHub 行级缺口补标（对齐原版 check_coverage.py：缺失/非法 → ERROR → 补标后重查）。

回归 job_bf3c6b52db7b：9094 行走 DataHub，每次 5-9 行行级解析失败 → 覆盖检查报缺口，
节点重试又整批重跑 DataHub（每次约 9 分钟）。修复：
  1. DataHub 后只把缺口行交直连模型（原版 2A，batch 15、最多 3 次）补标；
  2. DataHub 合法结果落 run_dir 缓存，节点重试/续跑不重复提交 DataHub；
  3. 补标后仍缺 → 覆盖检查照旧报错（门槛不放宽）。
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

CTX = {"campaign_name": "A", "campaign_context": "B", "activity_keywords": "C"}


def _rows(n):
    return [{"id": f"x{i}", "title": "t", "content": f"c{i}"} for i in range(n)]


def _dh(missing=()):
    calls = []

    def fake(rows, prompt, **kw):
        calls.append([r["_annot_id"] for r in rows])
        return {r["_annot_id"]: ({"_error": "parse"} if r["_annot_id"] in missing
                                 else {"sentiment": "正面", "campaign_relevant": "是"})
                for r in rows}
    return fake, calls


def _llm(_system, user, **_kw):
    return [{"id": i["id"], "sentiment": "负面", "campaign_relevant": "否"} for i in json.loads(user)]


class R1BackfillTest(unittest.TestCase):
    def _run(self, rows, fake, *, llm=_llm, cache_dir=None, calibration=None):
        with patch.object(datahub, "api_available", return_value=True), \
             patch.object(datahub, "batch_annotate", side_effect=fake), \
             patch.object(semantic, "llm_available", return_value=True), \
             patch.object(semantic, "chat_json", side_effect=llm) as chat:
            result = A.annotate_round1(rows, context=CTX, threshold=3, cache_dir=cache_dir,
                                       calibration=calibration)
        return result, chat

    def test_only_missing_rows_backfilled_by_direct_model(self):
        fake, calls = _dh(missing={"x1", "x4"})
        result, chat = self._run(_rows(6), fake)
        self.assertEqual(result["routed_via"], "datahub")
        self.assertEqual(result["coverage"]["sentiment_missing"], 0)
        self.assertEqual(result["coverage"]["relevance_missing"], 0)
        self.assertEqual(result["backfill"]["rows"], 2)
        self.assertEqual(result["backfill"]["remaining"], 0)
        sent = [item["id"] for c in chat.call_args_list for item in json.loads(c.args[1])]
        self.assertEqual(sorted(sent), ["x1", "x4"])
        by_id = {r["_annot_id"]: r for r in result["rows"]}
        self.assertEqual(by_id["x1"]["ai_sentiment"], -1)   # 补标结果
        self.assertEqual(by_id["x0"]["ai_sentiment"], 1)    # DataHub 结果不被覆盖

    def test_no_backfill_when_datahub_complete(self):
        fake, _ = _dh()
        result, chat = self._run(_rows(4), fake)
        self.assertEqual(result["backfill"]["rows"], 0)
        chat.assert_not_called()

    def test_still_missing_after_backfill_keeps_gap(self):
        fake, _ = _dh(missing={"x2"})
        result, _ = self._run(_rows(4), fake, llm=lambda *_a, **_k: [])
        self.assertEqual(result["coverage"]["sentiment_missing"], 1)
        self.assertEqual(result["backfill"]["remaining"], 1)
        self.assertEqual(result["backfill"]["remaining_ids"], ["x2"])

    def test_retry_reuses_datahub_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, calls = _dh(missing={"x1"})
            self._run(_rows(5), fake, cache_dir=tmp, llm=lambda *_a, **_k: [])
            fake2, calls2 = _dh()
            result, _ = self._run(_rows(5), fake2, cache_dir=tmp)
        self.assertEqual(calls2, [["x1"]], "已缓存的合法结果不重提，只提交缺口行")
        self.assertEqual(result["coverage"]["sentiment_missing"], 0)

    def test_calibration_change_invalidates_datahub_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, _ = _dh()
            self._run(_rows(4), fake, cache_dir=tmp)
            fake2, calls2 = _dh()
            self._run(_rows(4), fake2, cache_dir=tmp, calibration=[{"id": "x0", "note": "fix"}])
        self.assertEqual(len(calls2), 1)


if __name__ == "__main__":
    unittest.main()
