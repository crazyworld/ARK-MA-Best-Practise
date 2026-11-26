"""人工关卡必须通知到人（job_436842fb74da N10 未收到通知）。

卡片多数是原地 patch 进度卡，飞书不为编辑消息推通知；发卡后需在话题内额外发一条
带 `<at user_id=...>` 的纯文本提醒。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_store as ps  # noqa: E402
import test_host_stdlib as _base  # noqa: E402


def _load_hitl():
    holder = types.SimpleNamespace()
    _base.HitlActionParsingTest.setUpClass.__func__(holder)
    return holder.hitl


class HcNotifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hitl = _load_hitl()
        cls.cls = cls.hitl.Topic5Hitl

    def _job(self):
        job = ps.PipelineJob(job_id="job_x", chat_id="c", thread_id="", user_open_id="ou_abc",
                             ma_session_id="s", mode="full", project_dir="d")
        job.root_message_id = "om_root"
        return job

    def _run(self, hc_kind, sender, job=None):
        sent = []
        obj = self.cls.__new__(self.cls)
        obj._runner = types.SimpleNamespace(
            _feishu=types.SimpleNamespace(reply_in_thread_plain=sender(sent)))
        job = job or self._job()
        import asyncio
        asyncio.run(obj._notify_hc_operator(job, hc_kind))
        return sent

    def test_mentions_operator_with_gate_label(self):
        sent = self._run("N10", lambda bag: (lambda mid, text: bag.append((mid, text))))
        self.assertEqual(len(sent), 1)
        mid, text = sent[0]
        self.assertIn('<at user_id="ou_abc"></at>', text)
        self.assertIn("N10", text)
        self.assertIn("观点与 UGC 标签待你审核", text)
        self.assertIn("job_x", text)

    def test_targets_root_message(self):
        job = self._job()
        job.root_message_id = "om_root"
        sent = self._run("G2", lambda bag: (lambda mid, text: bag.append((mid, text))), job)
        self.assertEqual(sent[0][0], "om_root")

    def test_no_open_id_skips(self):
        job = self._job()
        job.user_open_id = ""
        sent = self._run("G2", lambda bag: (lambda mid, text: bag.append((mid, text))), job)
        self.assertEqual(sent, [])

    def test_sender_missing_skips_silently(self):
        obj = self.cls.__new__(self.cls)
        obj._runner = types.SimpleNamespace(_feishu=types.SimpleNamespace())
        import asyncio
        asyncio.run(obj._notify_hc_operator(self._job(), "G2"))

    def test_send_failure_does_not_raise(self):
        def boom(_mid, _text):
            raise RuntimeError("feishu 500")
        self._run("G2", lambda bag: boom)

    def test_every_user_gate_has_label(self):
        for kind in ("G1", "G2", "N10", "G4", "HC7", "USER_ESCALATE", "HC_FILTER",
                     "HC_DATA", "HC_ROUTE", "R1_REVIEW", "R2_REVIEW"):
            self.assertIn(kind, self.cls._HC_NOTIFY_LABELS)


class PlainThreadTextTest(unittest.TestCase):
    """reply_in_thread_plain 必须是 text 类型，post 转换会吞掉 <at>。"""

    def test_uses_text_msg_type(self):
        import importlib.util
        from pathlib import Path as P
        src = P(__file__).resolve().parents[3] / "arkagent" / "feishu.py"
        text = src.read_text(encoding="utf-8")
        start = text.index("def reply_in_thread_plain")
        block = text[start:start + 500]
        self.assertIn('"text"', block)
        self.assertIn("_reply_in_thread_with", block)


if __name__ == "__main__":
    unittest.main()
