"""用户上传活动明细 Excel 的列映射测试（不依赖 openpyxl）。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import excel_source


class TestExcelRowMapping(unittest.TestCase):
    def test_maps_customer_sample_columns(self):
        row = excel_source.normalize_excel_row(
            {
                "id": "6ab9ad6a",
                "数据类型": "电商笔记",
                "分析对象": "瑞幸联名",
                "站点名称": "小红书",
                "标题": "多栋冰箱贴太可爱了",
                "内容": "瑞幸联名新品",
                "内容类型": "UGC",
                "话题": "#比格多栋 #瑞幸联名|暹罗厘普",
                "作者": "summer",
                "url": "https://www.xiaohongshu.com/discovery/item/1",
                "发表时间": datetime(2026, 9, 28, 7, 57, 30),
                "总互动量": 91,
                "转发数": 2,
                "评论数": 0,
                "点赞数": 10,
                "阅读数": 75,
                "收藏数": 4,
                "粉丝数": 12345,
            }
        )
        self.assertEqual(row["_platform"], "xiaohongshu")
        self.assertEqual(row["content_mode"], "UGC")
        self.assertEqual(row["interaction_cnt"], 91)
        self.assertEqual(row["fans"], 12345)
        self.assertEqual(
            row["keywords"], ["比格多栋", "瑞幸联名", "暹罗厘普"]
        )
        self.assertIsInstance(row["publish_time"], int)
        self.assertEqual(row["_source"], "user_uploaded_excel")

    def test_computes_interaction_when_total_missing(self):
        row = excel_source.normalize_excel_row(
            {
                "id": "x",
                "站点名称": "小红书",
                "内容类型": "PGC",
                "转发数": 2,
                "评论数": 3,
                "点赞数": 5,
                "收藏数": 7,
            }
        )
        self.assertEqual(row["interaction_cnt"], 17)
        self.assertEqual(row["content_mode"], "PGC")

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_unknown_site_goes_to_other_without_raising(self):
        """FIX-17：映射表外站点归「其他」，计入声量，不再整单 raise。"""
        headers = ["站点名称", "标题", "内容", "发表时间", "内容类型",
                   "分析对象", "url", "总互动量"]
        path = self._write_xlsx(headers, site_names=["某未知站"])
        plan = {**self._plan(), "platforms": []}
        with patch.dict(os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}):
            data = excel_source.load_activity_rows(plan)
        self.assertEqual(data["rows"][0]["_platform"], "other")
        self.assertEqual(data["meta"]["unknown_sites"], {"某未知站": 1})
        self.assertEqual(data["meta"]["unknown_site_samples"], ["某未知站"])

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_news_site_maps_to_news(self):
        """UC大鱼号/今日头条统一为「新闻」，不再作为站点名参与平台分布。"""
        headers = ["站点名称", "标题", "内容", "发表时间", "内容类型",
                   "分析对象", "url", "总互动量"]
        path = self._write_xlsx(headers, site_names=["UC大鱼号"])
        plan = {**self._plan(), "platforms": []}
        with patch.dict(os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}):
            data = excel_source.load_activity_rows(plan)
        self.assertEqual(data["rows"][0]["_platform"], "news")
        self.assertEqual(data["meta"]["unknown_sites"], {})

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_unknown_site_ratio_over_ten_percent_is_reported(self):
        """未知站点占比 > 10% 时可判定需要告知用户。"""
        import platforms
        headers = ["站点名称", "标题", "内容", "发表时间", "内容类型",
                   "分析对象", "url", "总互动量"]
        path = self._write_xlsx(
            headers,
            analysis_objects=["测试活动"] * 10,
            site_names=["小红书"] * 8 + ["某A站", "某B站"],
        )
        plan = {**self._plan(), "platforms": []}
        with patch.dict(os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}):
            data = excel_source.load_activity_rows(plan)
        meta = data["meta"]
        self.assertEqual(sum(meta["unknown_sites"].values()), 2)
        self.assertTrue(platforms.should_alert_unknown(2, 10))

    def test_rejects_unsafe_mount_path(self):
        with self.assertRaisesRegex(ValueError, "mount_path"):
            excel_source.resolve_activity_xlsx(
                {"source_file": {"mount_path": "../outside.xlsx"}}
            )

    def _write_xlsx(
        self, headers, *, analysis_objects=None, content_modes=None,
        site_names=None,
    ):
        fd, name = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        path = Path(name)
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(headers)
        analysis_objects = analysis_objects or ["测试活动"]
        content_modes = content_modes or ["UGC"] * len(analysis_objects)
        site_names = site_names or ["小红书"] * len(analysis_objects)
        for index, (analysis_object, content_mode, site_name) in enumerate(
            zip(analysis_objects, content_modes, site_names), 1
        ):
            values = {
                "站点名称": site_name,
                "标题": f"标题{index}",
                "内容": "正文",
                "发表时间": "2026-01-02",
                "内容类型": content_mode,
                "分析对象": analysis_object,
                "url": f"https://example.com/post/{index}",
                "总互动量": 12,
            }
            sheet.append([values.get(header) for header in headers])
        workbook.save(path)
        workbook.close()
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def _plan(self):
        return {
            "source_file": {"name": "test.xlsx", "mount_path": "test.xlsx"},
            "content_scope": "all",
            "platforms": ["xiaohongshu"],
            "campaigns": [{"id": "测试活动"}],
            "period_start": "2026-01-01",
            "period_end": "2026-01-07",
        }

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_load_rejects_missing_analysis_object_or_interaction_columns(self):
        base = ["站点名称", "标题", "内容", "发表时间", "内容类型", "url"]
        for headers, expected in (
            # FIX-12：缺「分析对象」列在多活动时仍应报错。
            (base + ["总互动量"], "分析对象"),
            (base + ["分析对象"], "互动量列"),
        ):
            with self.subTest(expected=expected):
                path = self._write_xlsx(headers)
                plan = {**self._plan(), "campaigns": [
                    {"id": "活动A"}, {"id": "活动B"},
                ]}
                with patch.dict(
                    os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
                ):
                    with self.assertRaisesRegex(ValueError, expected):
                        excel_source.load_activity_rows(plan)

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_load_validates_scope_and_returns_reconciliation(self):
        path = self._write_xlsx([
            "站点名称", "标题", "内容", "发表时间", "内容类型",
            "分析对象", "url", "总互动量",
        ])
        with patch.dict(
            os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
        ):
            result = excel_source.load_activity_rows(self._plan())
        self.assertEqual(result["meta"]["source_row_count"], 1)
        self.assertEqual(result["meta"]["accepted_row_count"], 1)
        self.assertEqual(result["meta"]["campaigns"], ["测试活动"])

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_single_source_group_maps_to_single_confirmed_activity(self):
        path = self._write_xlsx([
            "站点名称", "标题", "内容", "发表时间", "内容类型",
            "分析对象", "url", "总互动量",
        ], analysis_objects=["暹罗厘普"])
        with patch.dict(
            os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
        ):
            result = excel_source.load_activity_rows(self._plan())
        self.assertEqual(result["rows"][0]["analysis_object"], "测试活动")
        self.assertEqual(
            result["rows"][0]["_source_analysis_object"], "暹罗厘普"
        )
        self.assertEqual(
            result["meta"]["campaign_mapping"],
            {"暹罗厘普": "测试活动"},
        )

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_multiple_source_groups_still_require_exact_confirmation(self):
        path = self._write_xlsx([
            "站点名称", "标题", "内容", "发表时间", "内容类型",
            "分析对象", "url", "总互动量",
        ], analysis_objects=["活动甲", "活动乙"])
        with patch.dict(
            os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
        ):
            with self.assertRaisesRegex(ValueError, "不一致"):
                excel_source.load_activity_rows(self._plan())

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_unknown_content_mode_is_valid_and_counted_separately(self):
        path = self._write_xlsx([
            "站点名称", "标题", "内容", "发表时间", "内容类型",
            "分析对象", "url", "总互动量",
        ], content_modes=["未知"])
        with patch.dict(
            os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
        ):
            result = excel_source.load_activity_rows(self._plan())
        self.assertEqual(result["rows"][0]["content_mode"], "UKN")
        self.assertEqual(
            result["meta"]["invalid_content_modes_mapped_to_unknown"], {}
        )

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_large_invalid_content_mode_ratio_is_rejected(self):
        path = self._write_xlsx([
            "站点名称", "标题", "内容", "发表时间", "内容类型",
            "分析对象", "url", "总互动量",
        ], analysis_objects=["测试活动"] * 20,
            content_modes=["媒体"] + ["UGC"] * 19)
        with patch.dict(
            os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
        ):
            with self.assertRaisesRegex(ValueError, "大量非 PGC/UGC/未知"):
                excel_source.load_activity_rows(self._plan())

    @unittest.skipUnless(Workbook is not None, "openpyxl not installed")
    def test_small_invalid_content_mode_ratio_maps_to_unknown(self):
        path = self._write_xlsx([
            "站点名称", "标题", "内容", "发表时间", "内容类型",
            "分析对象", "url", "总互动量",
        ], analysis_objects=["测试活动"] * 21,
            content_modes=["媒体"] + ["UGC"] * 20)
        with patch.dict(
            os.environ, {"TOPIC5_ACTIVITY_XLSX_PATH": str(path)}
        ):
            result = excel_source.load_activity_rows(self._plan())
        self.assertEqual(result["rows"][0]["content_mode"], "UKN")
        self.assertEqual(
            result["meta"]["invalid_content_modes_mapped_to_unknown"],
            {"媒体": 1},
        )


class CheckFilterwordsTest(unittest.TestCase):
    """FIX-11：活动过滤词核对，不区分大小写（原版 check_filterwords.py 口径）。"""

    def test_case_insensitive_title_hit(self):
        rows = [{"title": "ABC新品上市", "content": "正文无关", "topics": []}]
        result = excel_source.check_filterwords(rows, "ABC")
        self.assertEqual(result["hit_rows"], 1)
        self.assertEqual(result["total_rows"], 1)
        self.assertEqual(result["hits"][0]["field"], "标题")
        self.assertEqual(result["hits"][0]["word"], "ABC")

    def test_hit_in_content_field(self):
        rows = [{"title": "无关标题", "content": "文中出现ABC关键词", "topics": []}]
        result = excel_source.check_filterwords(rows, "abc")
        self.assertEqual(result["hit_rows"], 1)
        self.assertEqual(result["hits"][0]["field"], "正文")

    def test_no_hit_returns_zero(self):
        rows = [{"title": "nothing here", "content": "clean", "topics": []}]
        result = excel_source.check_filterwords(rows, "活动专属词")
        self.assertEqual(result["hit_rows"], 0)
        self.assertEqual(result["ratio"], 0.0)

    def test_multiple_rows_ratio(self):
        rows = [
            {"title": "命中词A内容", "content": "", "topics": []},
            {"title": "无关", "content": "", "topics": []},
            {"title": "无关2", "content": "", "topics": []},
            {"title": "无关3", "content": "", "topics": []},
        ]
        result = excel_source.check_filterwords(rows, "命中词A")
        self.assertEqual(result["hit_rows"], 1)
        self.assertEqual(result["total_rows"], 4)
        self.assertAlmostEqual(result["ratio"], 0.25)

    def test_empty_rows_returns_zero(self):
        result = excel_source.check_filterwords([], "词")
        self.assertEqual(result["hit_rows"], 0)

    def test_empty_words_returns_zero(self):
        rows = [{"title": "任何内容", "content": "", "topics": []}]
        result = excel_source.check_filterwords(rows, [])
        self.assertEqual(result["hit_rows"], 0)
        self.assertEqual(result["words"], [])

    def test_hit_in_topics(self):
        rows = [{"title": "正常标题", "content": "正常正文",
                 "topics": ["#活动话题#", "#ABC合作#"]}]
        result = excel_source.check_filterwords(rows, "ABC")
        self.assertEqual(result["hit_rows"], 1)
        self.assertEqual(result["hits"][0]["field"], "话题")


class MergeFilterwordImpactTest(unittest.TestCase):
    """FIX-11：A−B 误伤评估，≤5% 可并入，>5% 需逐词定位。"""

    def test_within_threshold(self):
        out = excel_source.merge_filterword_impact(1000, 960)
        self.assertAlmostEqual(out["delta_ratio"], 0.04)
        self.assertTrue(out["within_threshold"])

    def test_exceeds_threshold(self):
        out = excel_source.merge_filterword_impact(1000, 900)
        self.assertAlmostEqual(out["delta_ratio"], 0.10)
        self.assertFalse(out["within_threshold"])

    def test_zero_brand_total(self):
        out = excel_source.merge_filterword_impact(0, 500)
        self.assertTrue(out["within_threshold"])

    def test_threshold_constant(self):
        self.assertEqual(excel_source.MERGE_HIT_THRESHOLD, 0.05)


if __name__ == "__main__":
    unittest.main()
