"""FIX-58：数据异常分级（KS_Exception_Handling）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import excel_source as ES  # noqa: E402
import handlers  # noqa: E402
import hc_bridge  # noqa: E402
from _util import make_state  # noqa: E402

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover
    Workbook = None

HEAD = ["站点名称", "标题", "内容", "发表时间", "内容类型", "url", "总互动量"]


def _book(path: Path, sheets: dict):
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)


def _row(i, date="2026-10-01 10:00:00"):
    return ["小红书", f"t{i}", f"联名 {i}", date, "UGC", f"https://x/{i}", 1]


@unittest.skipIf(Workbook is None, "openpyxl missing")
class ExcelTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.plan = {"source_file": {"mount_path": "a.xlsx", "name": "a.xlsx"},
                     "campaigns": [{"id": "A"}], "period_start": "2026-10-01", "period_end": "2026-10-31"}

    def _load(self, sheets):
        _book(self.dir / "a.xlsx", sheets)
        with patch.object(ES, "resolve_activity_xlsx", return_value=self.dir / "a.xlsx"):
            return ES.load_activity_rows(self.plan)

    def test_bad_dates_excluded_not_fatal(self):
        data = self._load({"数据": [HEAD, _row(1), _row(2, "坏日期"), _row(3, "")]})
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["meta"]["date_error_rows"], 2)

    def test_preferred_sheet_and_note(self):
        data = self._load({"说明": [["备注"]], "数据": [HEAD, _row(1)]})
        self.assertEqual(len(data["rows"]), 1)
        self.assertIn("已读取 Sheet：数据，共 2 个 Sheet", data["meta"]["sheet_note"])

    def test_same_structure_sheets_require_merge(self):
        with self.assertRaises(ES.SheetMergeRequired):
            self._load({"10月": [HEAD, _row(1)], "11月": [HEAD, _row(2)]})


class ProfileTest(unittest.TestCase):
    def test_full_pauses_demo_notes(self):
        rows = [{"_platform": "xiaohongshu"}] * 80
        self.assertTrue(ES.profile(rows, {}, "full")["severe"])
        demo = ES.profile(rows, {}, "demo")
        self.assertFalse(demo["severe"])
        self.assertTrue(any("低于 100" in n for n in demo["notes"]))

    def test_limited_platform_and_empty_interaction(self):
        rows = [{"_platform": "xiaohongshu"}] * 120 + [{"_platform": "douyin"}] * 10
        prof = ES.profile(rows, {"interaction_available": False}, "full")
        self.assertEqual(prof["limited_platforms"], {"douyin": 10})
        self.assertTrue(any("仅统计声量" in n for n in prof["notes"]))


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.st = make_state(tempfile.mkdtemp())
        rows = [{"id": str(i), "publish_time": 1790000000000, "_platform": "xiaohongshu"} for i in range(5)]
        self.payload = {"rows": rows, "meta": {}}

    def test_severe_pause_then_ack_resume(self):
        with patch("handlers.excel_source.load_activity_rows", return_value=self.payload):
            with self.assertRaises(handlers.HumanPause):
                handlers.clean_campaign({"id": "n07"}, self.st, {})
            self.st.human = {"node": "n07_clean", "expect": ["data_exception_ack"]}
            out = hc_bridge.build_human_outputs(self.st, "pass", "知情继续")
            self.assertEqual(out["_control"]["next"], "n07_clean")
            self.st.write_artifact("data_exception_ack", out["data_exception_ack"])
            cleaned = handlers.clean_campaign({"id": "n07"}, self.st, {})["artifacts"]["cleaned_data"]
        self.assertEqual(len(cleaned["rows"]), 5)
        self.assertTrue(cleaned["clean_record"]["profile"]["severe"])

    def test_dedup_by_author_time_without_id(self):
        a = {"author": "u", "publish_time": 1, "content": "同一条"}
        self.assertEqual(handlers._dedup_key(a), handlers._dedup_key(dict(a)))
        self.assertNotEqual(handlers._dedup_key(a), handlers._dedup_key({**a, "content": "另一条"}))


class ReportNoteTest(unittest.TestCase):
    def test_note_in_report(self):
        import report_contract as RC
        stats = {"clean_record": {"profile": {"notes": ["抖音仅 10 条，样本量有限，结论权重降低",
                                                        "已读取 Sheet：数据，共 2 个 Sheet"]}}}
        note = RC.data_exception_note(stats)
        self.assertIn("样本量有限", note)
        self.assertNotIn("Sheet", note)
        self.assertIsNone(RC.data_exception_note({}))


if __name__ == "__main__":
    unittest.main()
