"""第 4 批：n14 图表 / 封面 / n15 词云（审计 P0-12/13 + P1-1/3/4）。

依据：
  - Producer v0.60 Step 9「封面生成 → cover.png 非空」；report-cover SKILL：图像模型经 bmc-llm-relay
  - 212 实测（2026-11-26）：topicX 未配置 REPORT_COVER_*；relay 无 gemini-3-pro-image-preview
    （503 model_not_found），gpt-image-2 /images/generations 9s 返回 b64 → 复用 LLM_RELAY_*
  - KS_Chart_Spec.md:65 达人层级双轴（声量占比左轴 + 条均互动量右轴）；:67 拉升分组柱图
  - KS_Wordcloud_Skill_Integration.md:62 有原帖时须读取，不能静默跳过
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import charts  # noqa: E402
import wordcloud_adapter as adapter  # noqa: E402

COVER_SCRIPTS = Path(charts.COVER_DIR) / "scripts"
sys.path.insert(0, str(COVER_SCRIPTS))
import generate_cover_gpt as cover  # noqa: E402


def _png_b64():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1536, 512), (200, 220, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class CoverCredentialTest(unittest.TestCase):
    """P0-12：凭据缺省复用 LLM_RELAY_*；空串不吞默认值；错误原文透传。"""

    def test_falls_back_to_relay_credentials(self):
        env = {"LLM_RELAY_BASE_URL": "https://relay.example/v1", "LLM_RELAY_API_KEY": "rk",
               "REPORT_COVER_API_KEY": "", "REPORT_COVER_API_BASE_URL": ""}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(charts.cover_available())
            key, url = cover.cover_endpoint()
        self.assertEqual(key, "rk")
        self.assertEqual(url, "https://relay.example/v1/images/generations")

    def test_explicit_cover_base_without_v1(self):
        env = {"REPORT_COVER_API_KEY": "ck", "REPORT_COVER_API_BASE_URL": "https://c.example"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(cover.cover_endpoint(), ("ck", "https://c.example/v1/images/generations"))

    def test_generate_cover_error_is_raised_with_reason(self):
        env = {"LLM_RELAY_BASE_URL": "https://relay.example/v1", "LLM_RELAY_API_KEY": "rk"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(charts, "_load_cover_fn",
                          return_value=lambda **kw: {"success": False, "error": "HTTP 403: denied"}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403: denied"):
                charts._gen_cover("标题", "版本 1.0", os.path.join(tempfile.mkdtemp(), "c.png"))


class CoverPromptEnglishTest(unittest.TestCase):
    """P0-13：发给图像模型的 prompt 不含中文（含中文会被审核拦截）。"""

    def test_prompt_sent_is_ascii(self):
        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent.update(json)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"data": [{"b64_json": _png_b64()}]}
            return resp

        out = os.path.join(tempfile.mkdtemp(), "c.png")
        env = {"LLM_RELAY_BASE_URL": "https://relay.example/v1", "LLM_RELAY_API_KEY": "rk"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(cover.requests, "post", side_effect=fake_post):
            charts._gen_cover("塔斯汀×Pingu联名活动复盘报告", "版本 1.0", out)
        self.assertTrue(os.path.getsize(out) > 0)
        self.assertTrue(all(ord(ch) < 128 for ch in sent["prompt"]), sent["prompt"][:200])


class ChartRenderErrorTest(unittest.TestCase):
    """P1-4：渲染失败带出 node 的 stderr。"""

    def test_stderr_surfaces(self):
        err = subprocess.CalledProcessError(1, ["node"], output=b"", stderr=b"unsupported chart_type: x")
        with patch.object(charts.subprocess, "run", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "unsupported chart_type: x"):
                charts._render_png({"chart_id": "c"}, os.path.join(tempfile.mkdtemp(), "c.png"))


class DualSeriesChartTest(unittest.TestCase):
    """P1-3：达人层级用柱线双轴，拉升用分组柱，第二条数据不再丢失。"""

    def test_kol_and_lift_chart_types(self):
        src = Path(charts.__file__).read_text(encoding="utf-8")
        at = src.index('_kol_effectiveness"')
        kol = src[at:at + 300]
        self.assertIn('"bar_line_combo"', kol)
        lift = src[src.index('"chart_id": "baseline_lift"'):src.index('"chart_id": "baseline_lift"') + 300]
        self.assertIn('"grouped_bar"', lift)


class WordcloudAllPlatformTest(unittest.TestCase):
    """P1-1：活动词云 platform=all 时读取所选平台的全部原帖。"""

    def test_all_platform_keeps_posts(self):
        rows = [{"content": "联名周边很可爱", "id": "p1", "_platform": "xiaohongshu"},
                {"content": "汉堡好吃", "id": "p2", "_platform": "douyin"}]
        kept = [r for _p, r, inherited in adapter.walk_posts({"articles": {"xiaohongshu": rows}})
                if adapter.platform_matches("all", r, inherited)]
        self.assertEqual(len(kept), 2)
        self.assertFalse(adapter.platform_matches("douyin", rows[0], "xiaohongshu"))


if __name__ == "__main__":
    unittest.main()
