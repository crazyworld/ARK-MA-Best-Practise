"""FIX-27：轮次一标注的输入、上下文与判定标准对齐原版。

原版依据：
  - annot_r1.py            SYS 注入活动名+背景；每条「平台+标题200+正文800」；
                           每批 15、重试 3 次；只接受合法值；空内容标「标注跳过_无内容」
  - annotation_prompt.md   项目配置区三项必填，未填禁止标注；输出只有两字段；
                           赞助型活动提及被赞助内容也算相关；负面不等于噪音
  - KS_Data_Processing.md:185  三个识别字段非空时必须传入

运行：python3 -m unittest tests.test_r1_contract
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
sys.path.insert(0, ORCH)

import annotate as A  # noqa: E402

CTX = {
    "campaign_name": "瑞幸×暹罗厘普联名活动",
    "campaign_context": "活动边界：仅统计本次联名；统计窗口：2026-09-28 ~ 2026-10-07",
    "activity_keywords": "瑞幸+(暹罗厘普|多栋)",
    "campaign_type": "IP联名",
}


def _row(i=0, content="瑞幸联名好喝", title="标题", **extra):
    row = {"_dedup_key": f"k{i}", "content": content, "title": title,
           "_platform": "xiaohongshu", "_publish_ymd": "20261001"}
    row.update(extra)
    return row


class ContextRequiredTest(unittest.TestCase):
    def test_missing_context_raises(self):
        for key in ("campaign_name", "campaign_context", "activity_keywords"):
            ctx = dict(CTX)
            ctx.pop(key)
            with self.subTest(missing=key):
                with self.assertRaisesRegex(ValueError, "禁止开始标注"):
                    A.annotate_round1([_row()], context=ctx)

    def test_blank_value_counts_as_missing(self):
        ctx = dict(CTX, campaign_name="   ")
        with self.assertRaises(ValueError):
            A._r1_context(ctx)

    def test_system_prompt_injects_campaign_and_keywords(self):
        prompt = A._r1_sys(CTX)
        self.assertIn(CTX["campaign_name"], prompt)
        self.assertIn(CTX["campaign_context"], prompt)
        self.assertIn(CTX["activity_keywords"], prompt)
        self.assertIn("IP联名", prompt)

    def test_system_prompt_states_judgement_rules(self):
        prompt = A._r1_sys(CTX)
        # 情感三档与正负并存取主导
        for word in ("正面", "中性", "负面", "主导情感"):
            self.assertIn(word, prompt)
        # 赞助型例外与负面不等于噪音（annotation_prompt.md）
        self.assertIn("赞助型", prompt)
        self.assertIn("负面不等于噪音", prompt)
        # 上下文窗口规则
        self.assertIn("当句", prompt)
        self.assertIn("前一句", prompt)
        self.assertIn("后一句", prompt)


class PayloadTest(unittest.TestCase):
    def test_truncates_title_200_content_800(self):
        row = _row(content="正" * 2000, title="标" * 500)
        A._tag_ids([row])
        item = A._r1_payload_row(row)
        self.assertEqual(len(item["title"]), A.R1_TITLE_CHARS)
        self.assertEqual(len(item["content"]), A.R1_CONTENT_CHARS)
        self.assertEqual(item["platform"], "xiaohongshu")

    def test_media_fields_passed_when_present(self):
        row = _row(video_cover_text="封面有活动字样", audio_text="音频提到联名",
                   video_text="")
        A._tag_ids([row])
        item = A._r1_payload_row(row)
        self.assertIn("video_cover_text", item["media_text"])
        self.assertIn("audio_text", item["media_text"])
        self.assertNotIn("video_text", item["media_text"])

    def test_media_text_budget(self):
        row = _row(video_cover_text="甲" * 1000, audio_text="乙" * 1000)
        A._tag_ids([row])
        item = A._r1_payload_row(row)
        body = item["media_text"]
        self.assertLessEqual(len(body) - len("video_cover_text："), A.R1_MEDIA_CHARS + 40)

    def test_empty_row_not_sent(self):
        row = _row(content="", title="")
        A._tag_ids([row])
        self.assertIsNone(A._r1_payload_row(row))
        to_annotate, skipped = A._r1_skip_empty([row])
        self.assertEqual(to_annotate, [])
        self.assertEqual(skipped[0]["annotation_skipped"], A.SKIP_EMPTY_LABEL)

    def test_media_only_row_is_annotated(self):
        """正文为空但封面有识别文本 → 仍送模型（KS_Data_Processing:185）。"""
        row = _row(content="", title="", video_cover_text="活动现场打卡")
        A._tag_ids([row])
        self.assertIsNotNone(A._r1_payload_row(row))


class BatchTest(unittest.TestCase):
    def test_batch_size_15_and_retries_3(self):
        rows = [_row(i=i) for i in range(40)]
        A._tag_ids(rows)
        calls = []

        def fake_chat_json(_system, user, **_kwargs):
            payload = json.loads(user)
            calls.append(len(payload))
            return []

        with patch("semantic.chat_json", side_effect=fake_chat_json):
            A._small_engine_r1(rows, CTX)

        # 40 条 → 15/15/10；每批 3 次重试 → 共 9 次调用
        self.assertEqual(calls[:3], [15, 15, 15])
        self.assertEqual(len(calls), 9)

    def test_illegal_sentiment_dropped_and_retried(self):
        rows = [_row(i=0)]
        A._tag_ids(rows)
        responses = [
            [{"id": "k0", "sentiment": "很好", "campaign_relevant": "是"}],
            [{"id": "k0", "sentiment": "正面", "campaign_relevant": "是"}],
        ]
        with patch("semantic.chat_json", side_effect=responses):
            A._small_engine_r1(rows, CTX)
        self.assertEqual(rows[0]["ai_sentiment"], 1)
        self.assertTrue(rows[0]["relevance"])

    def test_campaign_relevant_yes_no(self):
        row = _row()
        A._apply_r1(row, {"sentiment": "负面", "campaign_relevant": "是"})
        self.assertEqual(row["ai_sentiment"], -1)
        self.assertTrue(row["relevance"])
        other = _row()
        A._apply_r1(other, {"sentiment": "中性", "campaign_relevant": "否"})
        self.assertFalse(other["relevance"])

    def test_legacy_relevant_key_still_accepted(self):
        row = _row()
        A._apply_r1(row, {"sentiment": "正面", "relevant": True})
        self.assertTrue(row["relevance"])

    def test_uncertain_marked_for_review(self):
        row = _row()
        A._apply_r1(row, {"sentiment": "中性", "campaign_relevant": "是",
                          "uncertain": "true"})
        self.assertTrue(row["r1_uncertain"])


class CoverageTest(unittest.TestCase):
    def test_skipped_rows_excluded_from_coverage(self):
        rows = [_row(i=0), _row(i=1, content="", title="")]
        result = A.annotate_round1(rows, context=CTX)
        self.assertEqual(result["coverage"]["skipped_empty"], 1)
        self.assertEqual(result["coverage"]["total"], 1)

    def test_context_echoed(self):
        result = A.annotate_round1([_row()], context=CTX)
        self.assertEqual(result["context"]["campaign_name"], CTX["campaign_name"])


class HandlerContextTest(unittest.TestCase):
    def test_context_built_from_plan(self):
        import handlers as H
        plan = {"activity_name": "瑞幸×暹罗厘普联名活动",
                "activity_query": "瑞幸+(暹罗厘普|多栋)",
                "period_start": "2026-09-28", "period_end": "2026-10-07",
                "activity_boundary": "仅统计本次联名",
                "campaign_type": "IP联名",
                "actual_event_period": {"start": "2026-09-30", "end": "2026-10-05"}}
        ctx = H._r1_context_from_plan(plan)
        self.assertEqual(ctx["campaign_name"], "瑞幸×暹罗厘普联名活动")
        self.assertIn("仅统计本次联名", ctx["campaign_context"])
        self.assertIn("2026-09-30 ~ 2026-10-05", ctx["campaign_context"])
        self.assertEqual(ctx["activity_keywords"], "瑞幸+(暹罗厘普|多栋)")

    def test_pending_inference_not_leaked_into_context(self):
        import handlers as H
        ctx = H._r1_context_from_plan({
            "activity_name": "A", "activity_query": "A",
            "campaign_type": "待数据推断"})
        self.assertNotIn("待数据推断", ctx["campaign_context"])
        self.assertEqual(ctx["campaign_type"], "")

    def test_multi_campaign_names_joined(self):
        import handlers as H
        ctx = H._r1_context_from_plan({"campaigns": [
            {"name": "秋季活动"}, {"name": "国庆活动"}], "activity_query": "A+B"})
        self.assertIn("秋季活动", ctx["campaign_name"])
        self.assertIn("国庆活动", ctx["campaign_name"])


if __name__ == "__main__":
    unittest.main()
