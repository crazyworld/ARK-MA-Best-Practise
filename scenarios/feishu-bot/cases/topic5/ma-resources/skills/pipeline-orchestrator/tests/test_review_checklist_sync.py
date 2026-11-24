"""缺陷清单 D3 / 3.10：检查项编号唯一、UGC 冗余表（5.16）、类别 MECE 确定性部分。"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATOR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "topic5-validator")
sys.path.insert(0, os.path.join(VALIDATOR, "scripts"))

import scan_report  # noqa: E402


class ChecklistSyncTest(unittest.TestCase):
    def test_adaptation_ids_unique(self):
        text = open(os.path.join(VALIDATOR, "references", "rule-adaptation.md"), encoding="utf-8").read()
        ids = re.findall(r"(?m)^\| (\d+\.\d+[a-z]?) \|", text)
        self.assertEqual(len(ids), len(set(ids)), [i for i in ids if ids.count(i) > 1])
        self.assertNotIn("80–120 字叙事", text.replace("旧「80–120 字」", ""))

    def test_ugc_redundant_table(self):
        md = ("**② UGC 参与方式分布**\n\n![x](a.png)\n\n| 类别 | 条数 | 占比 |\n|---|---|---|\n"
              "| 开箱测评 | 3 | 30% |\n\n**③ 热帖配方**\n")
        self.assertEqual(scan_report._ugc_redundant_table_issues(md)[0]["id"], "P1-ugc-redundant-table")
        self.assertEqual(scan_report._ugc_redundant_table_issues("**② UGC 参与方式分布**\n\n![x](a.png)\n"), [])

    def test_mece_pairs_and_dimension(self):
        bundle = {"opinion_analysis": {"positive": {"top5": [{"label": "口感好"}, {"label": "小红书种草"}]},
                                       "negative": {"top5": [{"label": "口感差"}]}}}
        ids = [i["id"] for i in scan_report._category_mece_issues({"A": bundle})]
        self.assertTrue(any("pair-A-口感" in i for i in ids), ids)
        self.assertTrue(any("dim-A-观点-平台" in i for i in ids), ids)


if __name__ == "__main__":
    unittest.main()
