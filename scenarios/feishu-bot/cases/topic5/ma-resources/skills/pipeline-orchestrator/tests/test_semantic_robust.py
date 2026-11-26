"""模型输出解析与 relay 重试的稳健性（job_436842fb74da n07b 真实失败样本）。

212 重放 n07b 全部 34 批（每批 2 次）：
  - 3 批解析失败：reason 内引用原文的英文引号未转义；流式输出中途重来（前一段截断后
    重新输出完整 JSON）；
  - 2 批 relay 400「The provided model identifier is invalid」（Bedrock ValidationException），
    同一请求单独重放 200、同模型 12/12 探测正常 → 偶发，需重试。
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic  # noqa: E402


class ParseJsonRepairTest(unittest.TestCase):
    def test_unescaped_quotes_in_value(self):
        raw = ('```json\n{\n  "decisions": [\n    {\n      "id": "4b77",\n      "verdict": "keep",\n'
               '      "reason": "内容直接介绍角色设定"汉堡主理鹅"，属于活动相关传播"\n    }\n  ]\n}\n```')
        data = semantic.parse_json(raw)
        self.assertEqual(data["decisions"][0]["reason"], '内容直接介绍角色设定"汉堡主理鹅"，属于活动相关传播')

    def test_quote_before_comma_inside_value(self):
        raw = '{"decisions":[{"id":"a","reason":"视频封面显示"汉堡主理鹅登场"，属于"},{"id":"b"}]}'
        data = semantic.parse_json(raw)
        self.assertEqual([d["id"] for d in data["decisions"]], ["a", "b"])

    def test_restarted_stream_takes_last_complete_block(self):
        raw = ('```json\n{\n  "decisions": [\n    {\n      "id": "5455```json\n{\n  "decisions": [\n'
               '    {\n      "id": "0a28",\n      "verdict": "keep"\n    }\n  ]\n}\n```')
        self.assertEqual(semantic.parse_json(raw)["decisions"][0]["id"], "0a28")

    def test_raw_newline_in_string(self):
        self.assertEqual(semantic.parse_json('{"r":"第一行\n第二行"}')["r"], "第一行\n第二行")

    def test_valid_json_untouched(self):
        raw = '{"a": "含\\"转义\\"引号", "b": [1, 2]}'
        self.assertEqual(semantic.parse_json(raw), json.loads(raw))

    def test_garbage_still_raises(self):
        with self.assertRaises(ValueError):
            semantic.parse_json("抱歉，我无法完成")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stream(text):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    return _Resp(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n".encode())


def _http400():
    body = io.BytesIO(b'{"error":{"message":"ValidationException: The provided model identifier is invalid."}}')
    return urllib.error.HTTPError("u", 400, "Bad Request", {}, body)


class RelayRetryTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"LLM_RELAY_BASE_URL": "https://r/v1", "LLM_RELAY_API_KEY": "k"})
        self.env.start()
        self.sleep = patch.object(semantic.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()

    def test_two_transient_400_then_success(self):
        seq = [_http400(), _http400(), _stream('{"ok": 1}')]

        def fake(req, timeout=None):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        with patch.object(semantic.urllib.request, "urlopen", side_effect=fake):
            self.assertEqual(semantic.chat_json("s", "u"), {"ok": 1})

    def test_persistent_400_raises_with_body(self):
        with patch.object(semantic.urllib.request, "urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(_http400())):
            with self.assertRaisesRegex(urllib.error.HTTPError, "model identifier is invalid"):
                semantic.chat("s", "u")


class TruncationRetryTest(unittest.TestCase):
    """输出被 max_tokens 截断时放大预算重试（来源审核 13 个类别实测 2048 不够）。"""

    def setUp(self):
        self.env = patch.dict(os.environ, {"LLM_RELAY_BASE_URL": "https://r/v1", "LLM_RELAY_API_KEY": "k"})
        self.env.start()
        self.sleep = patch.object(semantic.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()

    def _truncated(self):
        chunk = {"choices": [{"delta": {"content": "{\"a\":"}, "finish_reason": "length"}]}
        return _Resp(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n".encode())

    def test_retries_with_bigger_budget_then_succeeds(self):
        seq = [self._truncated(), _stream('{"ok": 1}')]
        with patch.object(semantic.urllib.request, "urlopen",
                          side_effect=lambda *a, **k: seq.pop(0)) as urlopen:
            self.assertEqual(semantic.chat("s", "u", max_tokens=2048), '{"ok": 1}')
        bodies = [json.loads(c.kwargs["data"]) if "data" in c.kwargs else json.loads(c.args[0].data)
                  for c in urlopen.call_args_list]
        self.assertEqual([b["max_tokens"] for b in bodies], [2048, 4096])

    def test_budget_capped_at_ceiling(self):
        # 到上限后不再放大：继续截断说明是内容问题，直接抛，不无限重试。
        with patch.object(semantic.urllib.request, "urlopen",
                          side_effect=lambda *a, **k: self._truncated()) as urlopen:
            with self.assertRaisesRegex(ValueError, "max_tokens=16384"):
                semantic.chat("s", "u", max_tokens=8192)
        bodies = [json.loads(c.args[0].data) for c in urlopen.call_args_list]
        self.assertEqual([b["max_tokens"] for b in bodies], [8192, 16384])

    def test_all_truncated_raises(self):
        with patch.object(semantic.urllib.request, "urlopen",
                          side_effect=lambda *a, **k: self._truncated()):
            with self.assertRaisesRegex(ValueError, "截断"):
                semantic.chat("s", "u", max_tokens=16384)


class SourceReviewBudgetTest(unittest.TestCase):
    """来源审核按类别数给输出预算。"""

    def test_budget_scales_with_items(self):
        import label_review
        src = open(label_review.__file__, encoding="utf-8").read()
        self.assertIn("1024 + 512 * len(items)", src)


if __name__ == "__main__":
    unittest.main()
