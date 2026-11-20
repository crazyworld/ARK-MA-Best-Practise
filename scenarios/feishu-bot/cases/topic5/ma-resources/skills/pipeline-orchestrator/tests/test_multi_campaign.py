"""FIX-12：多活动（合并报告）。

原版依据：
  - Phase1 多活动规则：多活动默认合并成一份报告；第一章品牌数据取合并窗口
    （最早开始 ~ 最晚结束），基准期为合并窗口之前 30 天；第二章各活动按各自活动期统计
  - 相邻活动间隔超过 30 天时提示建议拆分；不同品牌必须拆分
  - Excel 中没有「分析对象」列时，自动用 campaign_name 填充并记录日志
  - 有重叠期时合计声量不能相加

运行：python3 -m unittest tests.test_multi_campaign
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
TOPIC = os.path.dirname(os.path.dirname(os.path.dirname(ORCH)))
sys.path.insert(0, ORCH)
sys.path.insert(0, TOPIC)

import excel_source       # noqa: E402
import topic5_intake as intake  # noqa: E402

try:
    from openpyxl import Workbook
except ImportError:            # pragma: no cover
    Workbook = None


def _defaults(**extra):
    return {**intake.DEMO_REQUIREMENT_DEFAULTS, **extra}


class CampaignLinesParsingTest(unittest.TestCase):
    def test_single_campaign_unchanged(self):
        """未填活动列表时行为与原来完全一致。"""
        req = intake.normalize_requirement(_defaults())
        self.assertEqual(len(req["campaigns"]), 1)
        self.assertEqual(req["campaigns"][0]["id"], req["activity_name"])
        self.assertIsNone(req["split_suggestion"])
        self.assertEqual(req["period_start"], "2026-09-28")
        self.assertEqual(req["period_end"], "2026-10-07")

    def test_two_campaigns_merge_window(self):
        req = intake.normalize_requirement(_defaults(campaign_lines=[
            "活动A；2026-09-01；2026-09-10；联名A；",
            "活动B；2026-10-01；2026-10-07；联名B；",
        ]))
        self.assertEqual([c["id"] for c in req["campaigns"]], ["活动A", "活动B"])
        self.assertEqual(req["period_start"], "2026-09-01")
        self.assertEqual(req["period_end"], "2026-10-07")
        self.assertEqual(req["merge_window"], {"start": "2026-09-01",
                                               "end": "2026-10-07"})
        # 间隔 21 天 < 30，不建议拆分
        self.assertIsNone(req["split_suggestion"])

    def test_gap_over_thirty_days_suggests_split(self):
        req = intake.normalize_requirement(_defaults(campaign_lines=[
            "活动A；2026-09-01；2026-09-10；联名A；",
            "活动B；2026-10-12；2026-10-20；联名B；",
        ]))
        self.assertIsNotNone(req["split_suggestion"])
        self.assertIn("建议拆分", req["split_suggestion"])
        self.assertIn("32 天", req["split_suggestion"])

    def test_reversed_dates_raise(self):
        with self.assertRaisesRegex(intake.IntakeValidationError, "开始日期晚于结束日期"):
            intake.normalize_requirement(_defaults(campaign_lines=[
                "坏活动；2026-10-10；2026-10-01；X；",
            ]))

    def test_at_most_five_campaigns(self):
        lines = [f"活动{i}；2026-09-0{i}；2026-09-0{i}；Q；" for i in range(1, 8)]
        req = intake.normalize_requirement(_defaults(campaign_lines=lines))
        self.assertEqual(len(req["campaigns"]), intake.MAX_CAMPAIGNS)

    def test_bad_expression_in_campaign_line_raises(self):
        with self.assertRaisesRegex(intake.QueryExpressionError, "活动「坏式」检索式"):
            intake.normalize_requirement(_defaults(campaign_lines=[
                "坏式；2026-09-01；2026-09-10；A+B|C；",
            ]))

    def test_or_operator_inside_expression_is_not_split(self):
        """检索式里的 | 是「或」，不能被当成字段分隔符。"""
        req = intake.normalize_requirement(_defaults(campaign_lines=[
            "活动A；2026-09-01；2026-09-10；瑞幸+(暹罗厘普|多栋)；贴膜|维修",
        ]))
        camp = req["campaigns"][0]
        self.assertEqual(camp["activity_query"], "瑞幸+(暹罗厘普|多栋)")
        self.assertEqual(camp["filter_words"], ["贴膜", "维修"])

    def test_dict_lines_are_accepted(self):
        req = intake.normalize_requirement(_defaults(campaign_lines=[
            {"name": "活动A", "period_start": "2026-09-01",
             "period_end": "2026-09-10", "activity_query": "联名A"},
        ]))
        self.assertEqual(req["campaigns"][0]["id"], "活动A")
        self.assertEqual(req["campaigns"][0]["period_start"], "2026-09-01")


def _write_xlsx(path, headers, rows):
    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.save(path)
    book.close()


ROW_HEADERS = ["站点名称", "标题", "内容", "发表时间", "内容类型",
               "分析对象", "url", "总互动量", "互动分项-点赞"]

# 2026-09-05 与 2026-10-03（毫秒时间戳，CST）
TS_A = 1788537600000
TS_B = 1790956800000


@unittest.skipUnless(Workbook is not None, "openpyxl not installed")
class ExcelMultiCampaignTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()

    def _plan(self, campaigns, period_start="2026-09-01", period_end="2026-10-07"):
        return {
            "period_start": period_start,
            "period_end": period_end,
            "platforms": ["小红书"],
            "content_scope": "all",
            "campaigns": campaigns,
        }

    def _load(self, headers, rows, plan):
        import os
        from unittest.mock import patch
        path = os.path.join(self.dir, "a.xlsx")
        _write_xlsx(path, headers, rows)
        with patch.dict(os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": path}):
            return excel_source.load_activity_rows(plan)

    def test_two_campaigns_matching_objects_pass(self):
        out = self._load(ROW_HEADERS, [
            ["小红书", "A1", "活动A内容", TS_A, "UGC", "活动A", "https://e/1", 10, 5],
            ["小红书", "B1", "活动B内容", TS_B, "UGC", "活动B", "https://e/2", 20, 6],
        ], self._plan([{"id": "活动A", "period_start": "2026-09-01",
                        "period_end": "2026-09-10"},
                       {"id": "活动B", "period_start": "2026-10-01",
                        "period_end": "2026-10-07"}]))
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["meta"]["campaigns"], ["活动A", "活动B"])

    def test_row_outside_its_own_campaign_period_is_dropped(self):
        """方案要求：某行日期在本活动周期之外、但在另一活动周期之内时被剔除。"""
        out = self._load(ROW_HEADERS, [
            # 活动A 的周期是 09-01~09-10，这行在 10-03，落在活动B周期内 → 剔除
            ["小红书", "A1", "活动A越界", TS_B, "UGC", "活动A", "https://e/1", 10, 5],
            ["小红书", "B1", "活动B内容", TS_B, "UGC", "活动B", "https://e/2", 20, 6],
        ], self._plan([{"id": "活动A", "period_start": "2026-09-01",
                        "period_end": "2026-09-10"},
                       {"id": "活动B", "period_start": "2026-10-01",
                        "period_end": "2026-10-07"}]))
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["analysis_object"], "活动B")

    def test_missing_object_column_autofills_for_single_campaign(self):
        headers = [h for h in ROW_HEADERS if h != "分析对象"]
        out = self._load(headers, [
            ["小红书", "A1", "内容", TS_A, "UGC", "https://e/1", 10, 5],
        ], self._plan([{"id": "活动A"}]))
        self.assertEqual(out["rows"][0]["analysis_object"], "活动A")
        self.assertEqual(out["meta"]["auto_filled_analysis_object"], "活动A")

    def test_missing_object_column_still_raises_for_multi_campaign(self):
        headers = [h for h in ROW_HEADERS if h != "分析对象"]
        with self.assertRaisesRegex(ValueError, "分析对象"):
            self._load(headers, [
                ["小红书", "A1", "内容", TS_A, "UGC", "https://e/1", 10, 5],
            ], self._plan([{"id": "活动A"}, {"id": "活动B"}]))

    def test_object_mismatch_raises(self):
        with self.assertRaisesRegex(ValueError, "不一致"):
            self._load(ROW_HEADERS, [
                ["小红书", "X1", "内容", TS_A, "UGC", "活动X", "https://e/1", 10, 5],
            ], self._plan([{"id": "活动A", "period_start": "2026-09-01",
                            "period_end": "2026-09-30"},
                           {"id": "活动B", "period_start": "2026-10-01",
                            "period_end": "2026-10-07"}]))


if __name__ == "__main__":
    unittest.main()
