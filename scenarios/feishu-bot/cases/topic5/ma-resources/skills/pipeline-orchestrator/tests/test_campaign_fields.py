"""FIX-10：需求确认单业务字段、活动类型推断、品牌补入口径注。

原版依据：
  - KS_Report_Required_Fields_List.md:45-53  product_category / campaign_type / campaign_goal
  - Phase1_需求梳理_Topic5_v1.10.md:129     「补入品牌关键词的活动关键词」未补入填「无」
  - Phase1_需求梳理_Topic5_v1.10.md:181     campaign_goal 最多 2 个，不知道填「待数据推断」
  - KS_Classification_Rules.md:194-213       信号矩阵与推断步骤
  - KS_Report_Template.md:189 / :642         1.1 与拉升节的口径注（灰色斜体，一句话）

运行：python3 -m unittest tests.test_campaign_fields
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
TOPIC = os.path.dirname(os.path.dirname(os.path.dirname(ORCH)))
sys.path.insert(0, ORCH)
sys.path.insert(0, TOPIC)

import campaign_type_inference as cti  # noqa: E402
import report_contract as rc            # noqa: E402
import topic5_intake as intake          # noqa: E402

TS = 1790956800000  # 2026-10-03 CST
DAY = 86_400_000


def _req(**extra):
    return intake.normalize_requirement({**intake.DEMO_REQUIREMENT_DEFAULTS, **extra})


class IntakeFieldsTest(unittest.TestCase):
    def test_empty_fields_default_to_pending(self):
        req = _req()
        self.assertEqual(req["product_category"], "待数据推断")
        self.assertEqual(req["campaign_type"], "待数据推断")
        self.assertEqual(req["campaign_goal"], ["待数据推断"])
        self.assertEqual(req["brand_supplement_activity_terms"], "无")
        self.assertEqual(req["actual_event_period"], {})
        self.assertEqual(req["activity_boundary"], "")

    def test_goal_split_without_breaking_spaces(self):
        req = _req(campaign_goal="品牌声量提升/产品种草")
        self.assertEqual(req["campaign_goal"], ["品牌声量提升", "产品种草"])

    def test_more_than_two_goals_rejected(self):
        with self.assertRaisesRegex(intake.IntakeValidationError, "最多 2 个"):
            _req(campaign_goal="声量、种草、拉新")

    def test_pending_marker_not_counted_as_goal(self):
        req = _req(campaign_goal=["待数据推断", "产品种草"])
        self.assertEqual(req["campaign_goal"], ["产品种草"])

    def test_actual_period_parsed_and_does_not_override_window(self):
        req = _req(actual_event_period="2026-09-30 ~ 2026-10-05")
        self.assertEqual(req["actual_event_period"],
                         {"start": "2026-09-30", "end": "2026-10-05"})
        self.assertEqual(req["period_start"], "2026-09-28")
        self.assertEqual(req["period_end"], "2026-10-07")

    def test_type_and_supplement_kept(self):
        req = _req(campaign_type="IP联名", brand_supplement_activity_terms="#暹罗厘普#")
        self.assertEqual(req["campaign_type"], "IP联名")
        self.assertEqual(req["brand_supplement_activity_terms"], "#暹罗厘普#")

    def test_form_has_controls_and_custom_type_option(self):
        form = intake.build_requirement_form(
            1, lambda d: {"v": d}, defaults={"campaign_type": "公益项目（主）"})
        names = [el.get("name") for el in form["elements"]]
        for name in ("campaign_type", "product_category", "campaign_goal",
                     "activity_boundary", "actual_event_period",
                     "brand_supplement_activity_terms"):
            self.assertIn(name, names)
        select = next(el for el in form["elements"] if el.get("name") == "campaign_type")
        self.assertIn("公益项目（主）", str(select))

    def test_form_does_not_prefill_pending_marker(self):
        form = intake.build_requirement_form(
            1, lambda d: {"v": d}, defaults={"product_category": "待数据推断"})
        field = next(el for el in form["elements"] if el.get("name") == "product_category")
        self.assertNotIn("待数据推断", str(field.get("default_value") or ""))


def _row(platform="xiaohongshu", mode="UGC", title="", day=0, fans=0):
    return {"_platform": platform, "content_mode": mode, "title": title,
            "content": "", "publish_time": TS + day * DAY, "fans": fans}


class InferenceTest(unittest.TestCase):
    def test_pgc_90_percent_basis_contains_pgc_ratio(self):
        rows = [_row("news", "PGC", "新闻稿") for _ in range(9)] + [_row()]
        out = cti.infer_campaign_type(rows)
        self.assertEqual(out["value"], "新闻公关稿传播")
        self.assertEqual(out["confidence"], "高置信度")
        self.assertTrue(any("PGC 占比" in b for b in out["basis"]))
        self.assertEqual(out["signals"]["pgc_ratio"], 0.9)

    def test_topic_activity(self):
        rows = [_row(title="#联名# 打卡参与", day=i * 5) for i in range(10)]
        out = cti.infer_campaign_type(rows)
        self.assertEqual(out["value"], "话题活动")

    def test_one_strong_plus_support_is_likely(self):
        # 新闻公关稿：新闻占比 60% 为唯一强信号，话题标签稀少 + 发布集中为佐证
        rows = [_row("news", "PGC", "稿", day=i) for i in range(6)] + \
               [_row("xiaohongshu", "UGC", "日常", day=i * 9) for i in range(4)]
        signals = cti.compute_signals(rows)
        self.assertEqual(signals["news_wechat_ratio"], 0.6)
        self.assertLess(signals["pgc_ratio"], 0.8)
        out = cti.infer_campaign_type(rows)
        self.assertEqual(out["value"], "新闻公关稿传播")
        self.assertEqual(out["confidence"], "较可能")

    def test_insufficient_signals_pending(self):
        """零散内容无任何信号命中 → 待确认。

        注意：原版「内容含 X」只要有命中就算，所以这里必须用不含任何特征词的文本，
        且平台分散，否则会被判成某类活动。
        """
        rows = [_row("weibo", "UKN", "日常", day=i * 4) for i in range(2)] + \
               [_row("zhihu", "UKN", "日常", day=i * 7) for i in range(2)]
        out = cti.infer_campaign_type(rows)
        self.assertEqual(out["value"], "待数据推断")
        self.assertEqual(out["confidence"], "待确认")

    def test_single_keyword_hit_is_enough(self):
        """原版无比例门槛：10 条里 1 条含「安利」即满足该强信号。"""
        rows = [_row(title="安利一下", day=0)] + \
               [_row("weibo", "UGC", "日常", day=i * 2) for i in range(9)]
        signals = cti.compute_signals(rows)
        self.assertEqual(signals["kw_seeding_ratio"], 0.1)
        strong = cti._rules(signals)["KOL/KOC 种草"]["strong"]
        hit = [label for label, ok in strong if ok and "推荐/安利/测评" in label]
        self.assertEqual(len(hit), 1, strong)

    def test_empty_rows(self):
        self.assertEqual(cti.infer_campaign_type([])["value"], "待数据推断")

    def test_few_active_days_not_counted_as_concentration(self):
        """活跃天数 ≤3 时前 3 天占比恒 100%，不能据此判「集中」。"""
        rows = [_row("douyin", "UGC", "日常") for _ in range(4)]
        out = cti.infer_campaign_type(rows)
        self.assertIsNone(out["signals"]["top3_days_share"])
        self.assertIsNone(out["signals"]["top2_days_share"])

    def test_deterministic(self):
        rows = [_row("news", "PGC", "稿") for _ in range(9)] + [_row()]
        self.assertEqual(cti.infer_campaign_type(rows), cti.infer_campaign_type(rows))


class SupplementNoteTest(unittest.TestCase):
    def test_none_means_no_note(self):
        self.assertEqual(rc.brand_supplement_notes({"brand_supplement_activity_terms": "无"}),
                         (None, None))
        self.assertEqual(rc.brand_supplement_notes({}), (None, None))

    def test_note_text_matches_template(self):
        first, lift = rc.brand_supplement_notes(
            {"brand_supplement_activity_terms": "#暹罗厘普#|多栋"})
        assert first is not None and lift is not None
        self.assertIn("品牌口径含活动关键词「#暹罗厘普#、多栋」", first)
        self.assertIn("统计范围包含参与该活动但未提及品牌名的内容", first)
        self.assertIn("基准期使用同一套检索词", lift)
        self.assertIn("{{muted}}", first)

    def test_prepend_puts_note_at_section_start(self):
        md = "# T\n\n## 1. 品牌\n\n### 1.1 概况\n\n正文段落\n\n### 1.2 口碑\n\nx\n"
        first, _ = rc.brand_supplement_notes({"brand_supplement_activity_terms": "话题A"})
        assert first is not None
        out = rc._prepend(md, "1.1", first)
        bounds = rc.section(out, "1.1")
        assert bounds is not None
        _, end, stop = bounds
        body = out[end:stop].strip()
        self.assertTrue(body.startswith(first))
        self.assertIn("正文段落", body)
        self.assertIn("### 1.2 口碑", out)


if __name__ == "__main__":
    unittest.main()
