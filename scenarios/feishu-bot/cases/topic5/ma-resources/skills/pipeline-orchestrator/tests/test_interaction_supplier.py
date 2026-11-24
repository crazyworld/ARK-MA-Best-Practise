"""缺陷清单 Q2：供应商互动量口径（缺失不补零、不进 Top、不进条均分母）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_contract  # noqa: E402


class SupplierInteractionTest(unittest.TestCase):
    def test_stats_skip_missing(self):
        rows = [{"interaction_cnt": 10}, {"interaction_cnt": None}, {"interaction_cnt": "30"}]
        stats = report_contract.interaction_stats(rows)
        self.assertEqual((stats["total"], stats["available"], stats["missing"], stats["avg"]),
                         (40, 2, 1, 20.0))

    def test_all_missing_is_unavailable(self):
        stats = report_contract.interaction_stats([{"interaction_cnt": None}])
        self.assertIsNone(stats["total"])
        self.assertIsNone(stats["avg"])

    def test_top_excludes_missing(self):
        rows = [{"id": "a", "interaction_cnt": None}, {"id": "b", "interaction_cnt": 0},
                {"id": "c", "interaction_cnt": 5}]
        self.assertEqual([r["id"] for r in report_contract._rank(rows)], ["c", "b"])



class KolUnknownNoteTest(unittest.TestCase):
    """缺陷清单 D2：达人层级「未知」数量及占比可追溯。"""

    def test_note_when_unknown(self):
        note = report_contract.kol_unknown_note(
            {"kol_notes": {"unknown_share": 0.25}, "kol_level_dist": {"unknown": 5}})
        self.assertIn("5 条", note)
        self.assertIn("25.0%", note)
        self.assertIsNone(report_contract.kol_unknown_note({"kol_notes": {"unknown_share": 0}}))


if __name__ == "__main__":
    unittest.main()
