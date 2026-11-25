"""relay 返回 4xx/5xx 时把响应体带进异常（job_bf3c6b52db7b n09b 只留下「HTTP Error 400: Bad Request」，
无法区分参数、内容审核还是渠道问题）。重试次数与行为不变。"""
from __future__ import annotations

import io
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic  # noqa: E402


class HttpErrorBodyTest(unittest.TestCase):
    def test_error_body_and_status_surface_after_retries(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO('{"error":{"message":"prompt is too long","type":"invalid_request_error"}}'.encode()))

        with patch.dict(os.environ, {"LLM_RELAY_BASE_URL": "https://relay/v1",
                                     "LLM_RELAY_API_KEY": "rk"}, clear=True), \
             patch.object(semantic.urllib.request, "urlopen", side_effect=boom), \
             patch.object(semantic.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                semantic.chat("s", "u", retries=2)
        self.assertEqual(len(calls), 2)
        message = str(raised.exception)
        self.assertIn("400", message)
        self.assertIn("prompt is too long", message)
        self.assertIn("claude-haiku-4-5-20251001", message)


if __name__ == "__main__":
    unittest.main()
