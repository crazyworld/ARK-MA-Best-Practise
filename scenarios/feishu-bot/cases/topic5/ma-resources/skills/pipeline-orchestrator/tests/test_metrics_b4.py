"""B4 指标与统计：FIX-14 主讨论对象三分类、FIX-16 达人层级、条均互动倍数。

原版依据：
  - KS_Metadata_Structure.md:110  discussion_dimension 三枚举（品牌/产品/活动）
  - KS_Data_Processing.md:44-55   达人层级平台阈值表；新闻/公众号 N/A
  - KS_Metrics_Calculation.md:204 / :207  品牌主导讨论占比、条均互动倍数
  - aggregate.py:158-171          分维 NSR 固定三类、门槛正+负 ≥30、缺失只警告
  - aggregate.py（kol 段）        效能只算 PGC 的头部/腰部/尾部

运行：python3 -m unittest tests.test_metrics_b4
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.dirname(HERE)
sys.path.insert(0, ORCH)

import annotate   # noqa: E402
import charts     # noqa: E402
import handlers as H  # noqa: E402

TS = 1790956800000
DAY = 86_400_000


def _row(platform="xiaohongshu", mode="PGC", fans=600_000, dim="品牌",
         sentiment=1, day=0, interaction=100, title="瑞幸联名活动体验"):
    return {"_platform": platform, "content_mode": mode, "fans": fans,
            "discussion_dimension": dim, "sentiment": sentiment,
            "ai_sentiment": sentiment, "interaction_cnt": interaction,
            "title": title, "content": "",
            "publish_time": TS + day * DAY, "_publish_ymd": "20261001"}


def _prepare(rows):
    for r in rows:
        r["_kol_level"] = H._derive_kol_level(r)
        r.setdefault("_publish_ymd", "20261001")
    return rows


class KolThresholdTest(unittest.TestCase):
    def test_platform_specific_thresholds(self):
        """同一粉丝数在不同平台层级不同（原版阈值表）。"""
        cases = [("douyin", 600_000, "waist"), ("xiaohongshu", 600_000, "head"),
                 ("zhihu", 600_000, "head"), ("douyin", 1_200_000, "head"),
                 ("xiaohongshu", 600_000, "head"), ("zhihu", 50_000, "waist"),
                 ("xiaohongshu", 20_000, "tail"), ("xiaohongshu", 3_000, "ordinary")]
        for platform, fans, expected in cases:
            with self.subTest(platform=platform, fans=fans):
                self.assertEqual(
                    H._derive_kol_level({"fans": fans, "_platform": platform}),
                    expected)

    def test_news_and_wechat_not_applicable(self):
        for platform in ("news", "weixin"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    H._derive_kol_level({"fans": 9_999_999, "_platform": platform}),
                    "n/a")

    def test_missing_or_zero_fans_is_unknown_not_ordinary(self):
        for fans in (None, 0, "", "abc"):
            with self.subTest(fans=fans):
                self.assertEqual(
                    H._derive_kol_level({"fans": fans, "_platform": "douyin"}),
                    "unknown")

    def test_effectiveness_only_counts_pgc_head_waist_tail(self):
        """分母只含 PGC 的三层；素人、未知、不适用、UGC 全部排除。"""
        rows = _prepare(
            [_row(fans=2_000_000) for _ in range(2)]          # 头部 2
            + [_row(fans=200_000) for _ in range(3)]          # 腰部 3
            + [_row(mode="UGC", fans=2_000_000) for _ in range(4)]   # UGC 头部，排除
            + [_row(mode="UGC", fans=None) for _ in range(10)]       # UGC 未知，排除
            + [_row(platform="news", fans=None) for _ in range(5)]   # 新闻，排除
        )
        stats = H._campaign_stats(rows, "瑞幸", [])
        self.assertEqual(stats["kol_effective_total"], 5)
        self.assertEqual(stats["kol_effectiveness"]["head"]["volume"], 2)
        self.assertEqual(stats["kol_effectiveness"]["head"]["volume_share"], 0.4)
        self.assertEqual(stats["kol_effectiveness"]["waist"]["volume_share"], 0.6)
        # 全量分布仍保留，供「未知占比」说明
        self.assertEqual(stats["kol_level_dist"]["unknown"], 10)
        self.assertEqual(stats["kol_level_dist"]["n/a"], 5)
        # unknown_share 的分母是活动相关全量（2+3+4+10+5=24 行），不是 UGC 行数
        self.assertAlmostEqual(stats["kol_notes"]["unknown_share"], 10 / 24, places=4)
        self.assertEqual(stats["kol_notes"]["ugc_total"], 14)

    def test_all_unknown_suppresses_chart(self):
        rows = _prepare([_row(fans=None) for _ in range(3)])
        stats = H._campaign_stats(rows, "瑞幸", [])
        self.assertTrue(stats["kol_notes"]["all_unknown"])
        specs = charts.analysis_specs("c1", stats) if hasattr(charts, "analysis_specs") \
            else charts._analysis_specs("c1", stats)
        self.assertFalse([s for s in specs if s.get("chart_id") == "c1_kol_effectiveness"])


class DiscussionDimensionTest(unittest.TestCase):
    def test_invalid_value_dropped_for_retry(self):
        row = {"title": "t", "content": "c"}
        annotate._apply_r2(row, {"discussion_dimension": "价格"})
        self.assertNotIn("discussion_dimension", row)
        annotate._apply_r2(row, {"discussion_dimension": "活动"})
        self.assertEqual(row["discussion_dimension"], "活动")

    def test_dimension_nsr_keys_fixed_to_three(self):
        rows = _prepare([])
        for _ in range(20):
            rows.append(_row(dim="品牌", sentiment=1))
        for _ in range(10):
            rows.append(_row(dim="品牌", sentiment=-1))
        for _ in range(5):
            rows.append(_row(dim="产品", sentiment=1))
        stats = H._campaign_stats(rows, "瑞幸", [])
        self.assertEqual(list(stats["dimension_nsr"]),
                         ["品牌主导讨论", "产品主导讨论", "活动流程与获取主导讨论"])
        self.assertEqual(stats["dimension_nsr"]["品牌主导讨论"]["status"], "available")
        self.assertEqual(stats["dimension_nsr"]["产品主导讨论"]["status"],
                         "insufficient_sample")
        # 三组之外的行计入 missing，只警告不报错（原版 aggregate.py:161-164）
        self.assertEqual(stats["dimension_coverage"]["covered"], 35)
        self.assertEqual(stats["dimension_coverage"]["missing"], 0)

    def test_missing_dimension_reported_not_raised(self):
        rows = _prepare([_row(dim="品牌") for _ in range(3)]
                        + [{"_platform": "xiaohongshu", "content_mode": "PGC",
                            "sentiment": 1, "ai_sentiment": 1, "interaction_cnt": 1,
                            "publish_time": TS, "_publish_ymd": "20261001",
                            "_kol_level": "unknown"} for _ in range(2)])
        stats = H._campaign_stats(rows, "瑞幸", [])
        self.assertEqual(stats["dimension_coverage"]["missing"], 2)
        self.assertIn("分维度覆盖声量将偏低", stats["dimension_coverage"]["note"])

    def test_no_short_form_names(self):
        """禁止「品牌NSR / 产品NSR / 活动NSR」这类简称。"""
        stats = H._campaign_stats(_prepare([_row()]), "瑞幸", [])
        for key in stats["dimension_nsr"]:
            self.assertNotIn("NSR", key)


class DerivedMetricTest(unittest.TestCase):
    def test_interaction_multiple(self):
        rows = _prepare([_row(interaction=100) for _ in range(4)])  # 条均 100
        stats = H._campaign_stats(rows, "瑞幸", [], brand_avg_interaction=50.0)
        self.assertEqual(stats["metrics"]["条均互动倍数"], 2.0)

    def test_interaction_multiple_none_without_brand_baseline(self):
        stats = H._campaign_stats(_prepare([_row()]), "瑞幸", [],
                                  brand_avg_interaction=None)
        self.assertIsNone(stats["metrics"]["条均互动倍数"])
        stats = H._campaign_stats(_prepare([_row()]), "瑞幸", [],
                                  brand_avg_interaction=0)
        self.assertIsNone(stats["metrics"]["条均互动倍数"])

    def test_brand_dominant_share_uses_main_object(self):
        rows = _prepare([_row(dim="品牌") for _ in range(3)]
                        + [_row(dim="产品") for _ in range(7)])
        stats = H._campaign_stats(rows, "瑞幸", [])
        self.assertEqual(stats["metrics"]["品牌主导讨论占比"], 0.3)
        # 与品牌提及率是两种方法：品牌词 10/10 命中 = 100% ≥ 95% → 提及漏斗退化，
        # 不输出独立的品牌提及率（原版 aggregate.py:191-193）；主对象口径不受影响。
        self.assertTrue(stats["mention_funnel"]["degraded"])
        self.assertIsNone(stats["metrics"]["品牌提及率"])

    def test_no_brand_words_suppresses_mention_rate(self):
        """原版无 brand_name 时只输出 Tier1，品牌提及率不输出（不是 0%）。"""
        stats = H._campaign_stats(_prepare([_row()]), "", [])
        self.assertIsNone(stats["metrics"]["品牌提及率"])
        self.assertIsNone(stats["metrics"]["产品提及占比"])

    def test_brand_words_with_zero_hits_reports_zero(self):
        """给了品牌词但一条没命中 → 0%（原版 pct(0, t1) = '0.0%'）。"""
        stats = H._campaign_stats(_prepare([_row()]), "不存在的品牌", [])
        self.assertEqual(stats["metrics"]["品牌提及率"], 0.0)


if __name__ == "__main__":
    unittest.main()
