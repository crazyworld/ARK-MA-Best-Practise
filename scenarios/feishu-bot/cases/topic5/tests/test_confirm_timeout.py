"""FIX-37：G4 终验超时——工作日（含 2026 法定节假日/调休）、催办/升级/归档暂停、用户回复恢复。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_store as ps  # noqa: E402


def _ts(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp())


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


class BusinessDayTest(unittest.TestCase):
    def test_weekday_reminder_and_escalate(self):
        wed = _ts("2026-11-18 10:00")                       # 周三
        self.assertEqual(_day(ps._add_business_days(wed, 2)), "2026-11-20")   # 周五催办
        self.assertEqual(_day(ps._add_business_days(wed, 3)), "2026-11-23")   # 下周一升级

    def test_national_day_and_makeup_workday(self):
        # 9/30（周三）+2 工作日：10/1-7 放假，10/8（周四）、10/9（周五）
        self.assertEqual(_day(ps._add_business_days(_ts("2026-09-30 10:00"), 2)), "2026-10-09")
        # 10/10（周六）调休上班
        self.assertEqual(_day(ps._add_business_days(_ts("2026-10-09 10:00"), 1)), "2026-10-10")

    def test_uncovered_year_falls_back_to_weekend_rule(self):
        self.assertEqual(_day(ps._add_business_days(_ts("2027-10-01 10:00"), 1)), "2027-10-04")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ps.PipelineStore(str(Path(self._tmp.name) / "t5.db"))
        self.job = self.store.create_job("c", "", "u", "s", "full", "d")
        self.store.mark_wait_hc(self.job.job_id, "G4")
        self.event_id = self.store.append_hc_event(self.job.job_id, "G4", {"node": "n21_hc_user"})
        self.created = self.store.get_hc_event(self.event_id).created_at

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_archive_after_five_business_days_and_restore(self):
        before = ps._add_business_days(self.created, ps.G4_ARCHIVE_BUSINESS_DAYS) - 60
        self.assertEqual(self.store.list_g4_archive_due(before), [])
        due = self.store.list_g4_archive_due(before + 120)
        self.assertEqual([e.id for e in due], [self.event_id])
        self.assertTrue(self.store.mark_archived_paused(self.job.job_id))
        job = self.store.get_job(self.job.job_id)
        self.assertEqual(job.status, ps.STATUS_ARCHIVED_PAUSED)
        # 归档任务仍可被话题定位（用户回复可恢复），但不在自动恢复列表中
        self.assertEqual(self.store.get_active_job_by_session_key("c", "", "u").job_id, self.job.job_id)
        self.assertNotIn(self.job.job_id, [j.job_id for j in self.store.list_active_jobs()])
        self.assertTrue(self.store.restore_from_archive(self.job.job_id))
        job = self.store.get_job(self.job.job_id)
        self.assertEqual((job.status, job.current_phase), (ps.STATUS_WAIT_HC, "G4"))


if __name__ == "__main__":
    unittest.main()
