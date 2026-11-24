"""FIX-05：Step5.5 去噪（原版 KS_Noise_Review）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import noise_review as NR  # noqa: E402
from _util import make_state  # noqa: E402


def _rows():
    rows = []
    for p_index, platform in enumerate(("xiaohongshu", "douyin", "weibo")):
        for day in range(1, 11):
            for k in range(4):
                rows.append({"id": f"{platform}-{day}-{k}", "_platform": platform,
                             "_publish_ymd": f"202610{day:02d}", "content_mode": "UGC" if k % 2 else "PGC",
                             "interaction_cnt": day * 10 + k + p_index, "content": f"联名活动 {k}"})
    return rows


class SelectionTest(unittest.TestCase):
    def test_covers_top30_and_peak_days_reproducibly(self):
        rows = _rows()
        sample = NR.select_sample(rows)
        for platform in ("xiaohongshu", "douyin", "weibo"):
            top = sorted((r for r in rows if r["_platform"] == platform),
                         key=lambda r: -r["interaction_cnt"])[:30]
            self.assertTrue({r["id"] for r in top} <= set(sample["top_interaction"]))
        self.assertTrue(all(i.split("-")[1] in ("8", "9", "10") for i in sample["interaction_peak"]))
        self.assertEqual(sample, NR.select_sample(rows))

    def test_window_from_ocr_field_not_cross_field(self):
        row = {"content": "今天天气好。", "video_cover_text": "开头。瑞幸联名上新。结尾说明。后面。"}
        wins = NR.windows(row, ["联名"])
        self.assertEqual(wins, [{"field": "video_cover_text", "window": "开头。瑞幸联名上新。结尾说明。"}])

    def test_exit_check_unreviewed_raises(self):
        with self.assertRaisesRegex(RuntimeError, "无结论"):
            NR.exit_check({"a", "b"}, [{"id": "a", "verdict": "keep"}], {"a", "b"})


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        self.st.write_artifact("confirmed_plan", {"activity_name": "联名", "activity_query": "瑞幸+联名",
                                                  "activity_boundary": "瑞幸×X联名"})
        self.rows = [{"id": "n1", "_platform": "xiaohongshu", "_publish_ymd": "20261001",
                      "interaction_cnt": 100, "content": "我的联名车贷"},
                     {"id": "k1", "_platform": "xiaohongshu", "_publish_ymd": "20261001",
                      "interaction_cnt": 50, "content": "瑞幸联名太难喝了，差评"},
                     {"id": "n2", "_platform": "xiaohongshu", "_publish_ymd": "20261002",
                      "interaction_cnt": 1, "content": "我的联名车贷"}]
        self.st.write_artifact("cleaned_data", {"rows": self.rows})

    def _judge(self, _system, user, **_kw):
        verdicts = {"n1": "noise", "n2": "noise", "k1": "keep"}
        return {"decisions": [{"id": i, "verdict": v, "branch": "联名", "reason": "r"}
                              for i, v in verdicts.items() if f'"id": "{i}"' in user]}

    def test_noise_removed_negative_kept_and_expansion(self):
        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat_json", side_effect=self._judge):
            out = handlers.noise_review_step({"id": "n07b"}, self.st, {})["artifacts"]
        kept = {r["id"] for r in out["denoised_data"]["rows"]}
        self.assertEqual(kept, {"k1"})
        self.assertEqual(sorted(out["noise_review"]["removed_ids"]), ["n1", "n2"])
        self.assertEqual(out["denoised_data"]["noise_record"], {"cleaned": 3, "removed": 2, "kept": 1})

    def test_validator_noise_ids_incremental(self):
        self.st.params["qc_noise_ids"] = ["k1"]
        self.st.write_artifact("noise_review", {"decisions": [
            {"id": i, "verdict": "keep", "source": "producer"} for i in ("n1", "n2")]})
        with patch("semantic.llm_available", return_value=True), \
                patch("semantic.chat_json") as model:
            out = handlers.noise_review_step({"id": "n07b"}, self.st, {})["artifacts"]
        model.assert_not_called()
        self.assertEqual(out["noise_review"]["removed_ids"], ["k1"])

    def test_no_semantic_fails_closed(self):
        with patch("semantic.llm_available", return_value=False):
            with self.assertRaises(RuntimeError):
                handlers.noise_review_step({"id": "n07b"}, self.st, {})


if __name__ == "__main__":
    unittest.main()
