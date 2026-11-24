"""供应商导出的站点名「抖音app」按精确别名归抖音；不做模糊包含匹配。"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import platforms


class DouyinAppAliasTest(unittest.TestCase):
    def test_verified_alias_maps_to_douyin(self):
        for name in ("抖音app", "抖音APP"):
            self.assertEqual(platforms.normalize_slug(name), ("douyin", True))

    def test_no_fuzzy_match(self):
        self.assertEqual(platforms.normalize_slug("某抖音app资讯站"), ("other", False))
        self.assertEqual(platforms.normalize_slug("短视频"), ("other", False))


if __name__ == "__main__":
    unittest.main()
