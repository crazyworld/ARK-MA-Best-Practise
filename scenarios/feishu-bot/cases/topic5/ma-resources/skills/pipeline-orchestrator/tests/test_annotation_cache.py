"""FIX-30 第 6 条：标注缓存（原版 annot_r1.py checkpoint 的可失效版本）。

原版依据：annot_r1.py:13-15/61-66
  - 已有结果的行不再送模型（断点续跑）
  - 只缓存合法结果，失败行下次重试
扩展：键 = sha256(行内容指纹 + 任务 + prompt 指纹)，prompt 或内容一变即失效。

运行：python3 -m unittest tests.test_annotation_cache
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import annotate as A  # noqa: E402
from annotation_cache import AnnotationCache, row_fingerprint  # noqa: E402

CTX = {"campaign_name": "A活动", "campaign_context": "边界B", "activity_keywords": "A+B"}


def _rows(n=3):
    return [{"_annot_id": f"k{i}", "title": f"标题{i}", "content": f"内容{i}",
             "_platform": "xiaohongshu"} for i in range(n)]


class CacheUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip_persists(self):
        item = {"platform": "x", "title": "t", "content": "c"}
        cache = AnnotationCache(self.dir, "round1", "prompt")
        cache.put(item, {"sentiment": "正面", "campaign_relevant": "是"})
        cache.save()
        again = AnnotationCache(self.dir, "round1", "prompt")
        hit = again.get(item)
        assert hit is not None
        self.assertEqual(hit["sentiment"], "正面")
        self.assertEqual(again.hits, 1)

    def test_prompt_change_invalidates(self):
        item = {"platform": "x", "title": "t", "content": "c"}
        cache = AnnotationCache(self.dir, "round1", "prompt-v1")
        cache.put(item, {"sentiment": "正面"})
        cache.save()
        self.assertIsNone(AnnotationCache(self.dir, "round1", "prompt-v2").get(item))

    def test_content_change_invalidates(self):
        cache = AnnotationCache(self.dir, "round1", "p")
        cache.put({"title": "t", "content": "旧"}, {"sentiment": "正面"})
        self.assertIsNone(cache.get({"title": "t", "content": "新"}))

    def test_fingerprint_ignores_id(self):
        self.assertEqual(row_fingerprint({"id": "a", "title": "t"}),
                         row_fingerprint({"id": "b", "title": "t"}))

    def test_corrupt_file_treated_as_empty(self):
        path = self.dir / "annotation_cache" / "round1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(AnnotationCache(self.dir, "round1", "p").get({"title": "t"}))


class Round1CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, rows, responder, **kw):
        sent = []

        def fake(_system, user, **_k):
            payload = json.loads(user)
            sent.extend(item["id"] for item in payload)
            return responder(payload)

        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=fake):
            result = A.annotate_round1(rows, context=CTX, cache_dir=self.dir, **kw)
        return sent, result

    @staticmethod
    def _ok(payload):
        return [{"id": i["id"], "sentiment": "正面", "campaign_relevant": "是"}
                for i in payload]

    def test_second_run_hits_cache_and_sends_nothing(self):
        self._run(_rows(), self._ok)
        sent, result = self._run(_rows(), self._ok)
        self.assertEqual(sent, [])
        self.assertEqual(result["cache_hits"], 3)
        self.assertEqual(result["coverage"]["sentiment_missing"], 0)

    def test_invalid_results_not_cached(self):
        """原版只缓存合法值；非法输出下次仍要送模型。"""
        bad = lambda payload: [{"id": i["id"], "sentiment": "很好",
                                "campaign_relevant": "是"} for i in payload]
        self._run(_rows(1), bad)
        sent, _ = self._run(_rows(1), self._ok)
        self.assertEqual(sent, ["k0"])

    def test_calibration_change_bypasses_cache(self):
        self._run(_rows(), self._ok)
        sent, _ = self._run(_rows(), self._ok, calibration=[
            {"content": "x", "field": "sentiment", "wrong": "负面", "correct": "中性"}])
        self.assertEqual(sorted(sent), ["k0", "k1", "k2"])

    def test_cache_outputs_match_fresh_run(self):
        """缓存只加速、不改结果：命中与否产出一致。"""
        _, first = self._run(_rows(), self._ok)
        _, second = self._run(_rows(), self._ok)
        pick = lambda res: [(r["_annot_id"], r["ai_sentiment"], r["relevance"])
                            for r in res["rows"]]
        self.assertEqual(pick(first), pick(second))

    def test_no_cache_dir_means_no_cache(self):
        with patch("semantic.llm_available", return_value=True), \
             patch("semantic.chat_json", side_effect=lambda s, u, **k: self._ok(json.loads(u))):
            result = A.annotate_round1(_rows(), context=CTX)
        self.assertEqual(result["cache_hits"], 0)
        self.assertFalse((self.dir / "annotation_cache").exists())


if __name__ == "__main__":
    unittest.main()
