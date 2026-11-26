"""轮次二直连批次：整批失败时对半拆分（job_436842fb74da n11 空流/超时）。

212 实测：单批 payload >2 万字符时 relay 会超时或返回空流；拆小后正常。
另 semantic.chat 对「200 + 空流」按可重试处理，不当成模型的正常空回答。
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import annotate  # noqa: E402
import semantic  # noqa: E402


def _rows(n):
    return [{"_annot_id": f"r{i}", "title": "t", "content": "c"} for i in range(n)]


class BatchSplitTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.applied = []

    def _run(self, rows, fail_over=0):
        def fake_chat_json(_prompt, user, **_kw):
            size = len(json.loads(user))
            self.calls.append(size)
            if fail_over and size > fail_over:
                raise RuntimeError("relay 返回空内容流（无 content 片段）")
            return [{"id": json.loads(user)[0]["id"]}] if False else [
                {"id": item["id"]} for item in json.loads(user)]

        def apply_fn(row, parsed):
            self.applied.append(row["_annot_id"])

        with patch.object(annotate.semantic, "chat_json", side_effect=fake_chat_json):
            annotate._run_task(rows, prompt="p", apply_fn=apply_fn,
                               is_missing=lambda r: r["_annot_id"] not in self.applied)
        return self.calls

    def test_normal_batch_not_split(self):
        self._run(_rows(15))
        self.assertEqual(self.calls, [15])

    def test_failed_batch_splits_until_it_fits(self):
        self._run(_rows(15), fail_over=6)
        self.assertEqual(self.calls[0], 15)
        self.assertIn(7, self.calls)      # 15 失败 → 7/8
        self.assertIn(4, self.calls)      # 8 仍超限 → 4/4
        self.assertEqual(len(self.applied), 15)
        self.assertEqual(len(self.applied), len(set(self.applied)))  # 不重复标

    def test_single_row_failure_raises_with_id(self):
        def fake(_prompt, user, **_kw):
            raise RuntimeError("relay 返回空内容流")
        with patch.object(annotate.semantic, "chat_json", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "首行 r0"):
                annotate._run_task(_rows(1), prompt="p", apply_fn=lambda *a: None,
                                   is_missing=lambda r: True)


class EmptyStreamRetryTest(unittest.TestCase):
    def test_empty_stream_is_retried_then_raises(self):
        from unittest.mock import patch as p
        env = p.dict(os.environ, {"LLM_RELAY_BASE_URL": "https://r/v1", "LLM_RELAY_API_KEY": "k"})
        env.start()
        empty = b"data: [DONE]\n"

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def __iter__(self):
                return iter([empty])

        try:
            with p.object(semantic.urllib.request, "urlopen", return_value=Resp()), \
                 p.object(semantic.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "空内容流"):
                    semantic.chat("s", "u")
        finally:
            env.stop()


if __name__ == "__main__":
    unittest.main()
