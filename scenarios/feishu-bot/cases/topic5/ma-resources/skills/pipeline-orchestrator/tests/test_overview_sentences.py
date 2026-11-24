"""FIX-47：传播概况结构结论与高点话术（KS_Writing_Standards 填空模板）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import overview  # noqa: E402


def _row(platform, interaction, day="20261001", mode="UGC", title=""):
    return {"_platform": platform, "interaction_cnt": interaction, "_publish_ymd": day,
            "content_mode": mode, "title": title}


class StructureTest(unittest.TestCase):
    def test_volume_and_interaction_use_own_denominators(self):
        text = overview.platform_sentence({"xiaohongshu": 60, "douyin": 30, "weibo": 10},
                                          {"xiaohongshu": 20, "douyin": 70, "weibo": 10}, 100, 100)
        self.assertIn("小红书占声量的 60.0%", text)
        self.assertIn("互动量有 70.0% 来自抖音，互动主要集中在抖音", text)

    def test_single_platform_template(self):
        text = overview.platform_sentence({"xiaohongshu": 5}, {"xiaohongshu": 9}, 5, 9)
        self.assertEqual(text, "平台分布：本次仅监测小红书，共 5 条（100%），9 次互动。")

    def test_close_shares_not_forced(self):
        text = overview.platform_sentence({"xiaohongshu": 52, "douyin": 48}, None, 100, None)
        self.assertIn("平台声量分布接近", text)
        self.assertIn("监测数据未提供平台互动分布", text)

    def test_content_structure_with_unknown(self):
        text = overview.content_sentence({"UGC": 70, "PGC": 20, "UKN": 10}, "all", 100)
        self.assertEqual(text, "讨论以UGC为主，占 70.0%；PGC占 20.0%，另有 10.0%的内容类型未知。")

    def test_scope_only_template(self):
        self.assertEqual(overview.content_sentence({}, "ugc", 8), "本次仅分析UGC内容，共 8 帖。")


class PeakTest(unittest.TestCase):
    def test_single_post_concentration(self):
        rows = [_row("xiaohongshu", 700, title="联名开箱"), _row("xiaohongshu", 300),
                _row("xiaohongshu", 5, day="20261002"), _row("xiaohongshu", 5, day="20261002"),
                _row("xiaohongshu", 5, day="20261002")]
        sentences = overview.campaign_overview(rows, "all")["peak_sentences"]
        joined = "".join(sentences)
        self.assertIn("10月2日讨论量达到 3 帖，为监测窗口峰值", joined)
        self.assertIn("10月1日互动量达到 1000 次，其中「联名开箱」获得 700 次互动，占当天 70.0%", joined)

    def test_multi_post_peak(self):
        rows = [_row("douyin", 40, title="A"), _row("douyin", 35, title="B"), _row("douyin", 25, title="C")]
        joined = "".join(overview.campaign_overview(rows, "all")["peak_sentences"])
        self.assertIn("两项指标均为监测窗口峰值", joined)
        self.assertIn("分别获得 40、35、25 次互动，合计占当天 100.0%", joined)

    def test_no_data_no_sentence(self):
        self.assertEqual(overview.peak_sentences({}, {}, []), [])


if __name__ == "__main__":
    unittest.main()
