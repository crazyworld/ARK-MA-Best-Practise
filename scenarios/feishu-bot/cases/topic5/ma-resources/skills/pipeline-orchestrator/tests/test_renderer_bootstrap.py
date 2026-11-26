"""图表渲染器自愈：node_modules 不入 git，缺依赖时要能自己在可写目录装一次。

背景：echarts 依赖（echarts + @resvg/resvg-js）体量与文件数超 MA 上传限制，打包和沙箱
都没有，而 n14 是真渲染、无图不许交付。沙箱里 skill 目录通常只读，所以要么原地装，
要么整份复制到 /tmp 再装。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charts  # noqa: E402


class EnsureRendererTest(unittest.TestCase):
    def setUp(self):
        charts._RENDER_ROOT = None
        charts._RENDER_ERROR = ""
        self.addCleanup(setattr, charts, "_RENDER_ROOT", None)
        self.addCleanup(setattr, charts, "_RENDER_ERROR", "")
        self.addCleanup(setattr, charts, "_ECHARTS_NODE_MODULES",
                        charts._ECHARTS_NODE_MODULES)
        self.addCleanup(setattr, charts, "_RENDER_SCRIPT", charts._RENDER_SCRIPT)

    def test_missing_node_raises_with_reason(self):
        with patch.object(charts, "_node_ok", return_value=False), \
             patch.object(charts.os.path, "isfile", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "缺少 node"):
                charts.ensure_renderer()
        self.assertIn("node", charts._RENDER_ERROR)

    def test_missing_render_script_raises(self):
        with patch.object(charts.os.path, "isfile", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "缺少渲染脚本"):
                charts.ensure_renderer()

    def test_already_installed_returns_skill_dir_without_npm(self):
        with patch.object(charts.os.path, "isfile", return_value=True), \
             patch.object(charts.os.path, "isdir", return_value=True), \
             patch.object(charts, "_node_ok", return_value=True), \
             patch.object(charts, "_npm_install") as npm:
            self.assertEqual(charts.ensure_renderer(), charts.ECHARTS_DIR)
        npm.assert_not_called()

    def test_in_place_install_success(self):
        with patch.object(charts.os.path, "isfile", return_value=True), \
             patch.object(charts, "_node_ok", return_value=True), \
             patch.object(charts, "_ECHARTS_NODE_MODULES", "/nonexistent/node_modules"), \
             patch.object(charts.os.path, "isdir", side_effect=lambda p: p.endswith("echarts")), \
             patch.object(charts, "_npm_install", return_value="") as npm:
            charts.ensure_renderer()
        self.assertEqual(npm.call_count, 1)
        self.assertEqual(charts._RENDER_ROOT, charts.ECHARTS_DIR)

    def test_readonly_skill_falls_back_to_writable_copy(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(src, "scripts"))
            open(os.path.join(src, "scripts", "render_from_spec.js"), "w").close()
            os.makedirs(os.path.join(src, "assets", "fonts"))

            def npm(dir_path):
                if dir_path.startswith(src):
                    return "npm install 失败：EACCES permission denied"
                os.makedirs(os.path.join(dir_path, "node_modules", "echarts"))
                return ""

            with patch.object(charts, "ECHARTS_DIR", src), \
                 patch.object(charts, "_RENDER_SCRIPT",
                              os.path.join(src, "scripts", "render_from_spec.js")), \
                 patch.object(charts, "_ECHARTS_NODE_MODULES",
                              os.path.join(src, "scripts", "node_modules")), \
                 patch.object(charts, "_node_ok", return_value=True), \
                 patch.object(charts, "_npm_install", side_effect=npm), \
                 patch.object(charts.tempfile, "gettempdir", return_value=tmp):
                root = charts.ensure_renderer()
                self.assertEqual(root, os.path.join(tmp, "topic5-echarts"))
                self.assertTrue(os.path.isdir(os.path.join(root, "assets", "fonts")))
                self.assertTrue(os.path.isfile(os.path.join(root, "scripts", "render_from_spec.js")))

    def test_both_install_fail_raises_with_both(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(src, "scripts"))
            open(os.path.join(src, "scripts", "render_from_spec.js"), "w").close()
            with patch.object(charts, "ECHARTS_DIR", src), \
                 patch.object(charts, "_RENDER_SCRIPT",
                              os.path.join(src, "scripts", "render_from_spec.js")), \
                 patch.object(charts, "_ECHARTS_NODE_MODULES",
                              os.path.join(src, "scripts", "node_modules")), \
                 patch.object(charts, "_node_ok", return_value=True), \
                 patch.object(charts, "_npm_install", return_value="npm install 失败：ENETUNREACH"), \
                 patch.object(charts.tempfile, "gettempdir", return_value=tmp):
                with self.assertRaisesRegex(RuntimeError, "原地与副本安装均失败"):
                    charts.ensure_renderer()

    def test_handler_installs_before_building(self):
        """n14 handler 必须先 ensure_renderer（装依赖），再判断可用性；装不上走 fallback。"""
        import handlers
        src = open(handlers.__file__, encoding="utf-8").read()
        body = src[src.index("def charts_render("):]
        body = body[:body.index("result = _build()")]
        self.assertIn("charts.ensure_renderer()", body)
        self.assertLess(body.index("charts.ensure_renderer()"),
                        body.index("charts.renderer_available()"))

    def test_render_charts_installs_before_rendering(self):
        """render_charts 必须先 ensure_renderer，不能直接看 renderer_available。"""
        src = open(charts.__file__, encoding="utf-8").read()
        body = src[src.index("def render_charts("):]
        head = body[:body.index("specs = build_chart_specs")]
        self.assertIn("ensure_renderer()", head)
        self.assertNotIn("renderer_available()", head)


if __name__ == "__main__":
    unittest.main()
