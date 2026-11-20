"""FIX-17：平台标签唯一事实源与未知站点处置契约。

原版依据：
  - KS_Data_Processing.md:44-55「站点名称 → 平台标签」映射表
  - KS_Exception_Handling.md:139-145 映射表外新值 → 归「其他」、计入声量、
    不单独出饼图、超 10% 告知用户
  - KS_Metadata_Structure.md:58 无法确定的平台记录异常，不默认归抖音

运行：python3 -m unittest tests.test_platforms
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import platforms as P  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_news_sites_map_to_news(self):
        """客户错误案例 #5：平台分布出现 UC大鱼号、今日头条，应统一为「新闻」。"""
        for site in ("UC大鱼号", "今日头条", "网易新闻", "澎湃新闻"):
            self.assertEqual(P.normalize(site), ("新闻", True), site)

    def test_known_aliases(self):
        self.assertEqual(P.normalize("bilibili"), ("B站", True))
        self.assertEqual(P.normalize("哔哩哔哩"), ("B站", True))
        self.assertEqual(P.normalize("微信视频号"), ("视频号", True))
        self.assertEqual(P.normalize("微信公众号"), ("微信", True))
        self.assertEqual(P.normalize("电商笔记"), ("小红书", True))
        self.assertEqual(P.normalize("快手"), ("快手", True))

    def test_unknown_site_becomes_other_not_douyin(self):
        """原版明令禁止把未映射站点默认归抖音。"""
        canon, matched = P.normalize("某论坛BBS")
        self.assertEqual(canon, P.OTHER)
        self.assertFalse(matched)
        self.assertNotEqual(canon, "抖音")

    def test_blank_site_is_other(self):
        self.assertEqual(P.normalize(""), (P.OTHER, False))
        self.assertEqual(P.normalize(None), (P.OTHER, False))

    def test_slug_roundtrip(self):
        self.assertEqual(P.normalize_slug("小红书"), ("xiaohongshu", True))
        self.assertEqual(P.normalize_slug("UC大鱼号"), ("news", True))
        self.assertEqual(P.normalize_slug("某站"), (P.OTHER_SLUG, False))
        self.assertEqual(P.display("xiaohongshu"), "小红书")
        self.assertEqual(P.display(P.OTHER_SLUG), "其他")
        self.assertEqual(P.display("news"), "新闻")


class SsjhKeyTest(unittest.TestCase):
    def test_canon_names_to_keys(self):
        self.assertEqual(
            P.ssjh_keys(["小红书", "抖音", "新闻"]),
            ["xiaohongshu", "douyin", "news"],
        )

    def test_slugs_pass_through(self):
        self.assertEqual(
            P.ssjh_keys(["xiaohongshu", "kuaishou"]),
            ["xiaohongshu", "kuaishou"],
        )

    def test_other_and_unknown_are_dropped(self):
        """「其他」无法在 SSJH 检索，必须丢弃而非报错。"""
        self.assertEqual(P.ssjh_keys(["小红书", "其他", "某论坛"]), ["xiaohongshu"])

    def test_covers_all_customer_platforms(self):
        self.assertEqual(len(P.CANON), 11)
        for name in ("小红书", "抖音", "快手", "视频号", "微博", "微信",
                     "知乎", "B站", "新闻", "论坛", "电商"):
            self.assertIn(name, P.CANON, name)


class UnknownRatioTest(unittest.TestCase):
    def test_alert_threshold_is_strictly_greater_than_10_percent(self):
        """原版：未知类型数量 > 全量 10% 时告知用户。"""
        self.assertFalse(P.should_alert_unknown(10, 100))
        self.assertTrue(P.should_alert_unknown(11, 100))
        self.assertFalse(P.should_alert_unknown(0, 0))


if __name__ == "__main__":
    unittest.main()
