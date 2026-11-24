"""沙箱环境变量渲染（create_all.sh --update-env）。

回归：Gateway 的 config.env 不写 ARK_BASE_URL（Gateway 用默认值），渲染时 ${ARK_BASE_URL}
被替成空串 → 沙箱内 semantic.llm_available() 为假，n07b 去噪、标注、写作全部失败。
同时：必需凭证为空时不得把空值推上环境（否则会清空已生效的 token）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_CASE_DIR = Path(__file__).resolve().parent.parent
SCRIPT = _CASE_DIR / "ma-resources" / "create_all.sh"


def _render_block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("# ---- 沙箱环境变量渲染 ----")
    end = text.index("# ---- 沙箱环境变量渲染结束 ----")
    return text[start:end]


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "需要 jq 与 bash")
class EnvironmentRenderTest(unittest.TestCase):
    def _render(self, extra: dict) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "render.sh"
            script.write_text(
                "set -euo pipefail\nROOT=" + json.dumps(str(SCRIPT.parent)) + "\n"
                'BASE_URL="${TEST_BASE_URL}"\n' + _render_block()
                + '\necho "$env_payload"\n', encoding="utf-8")
            env = {"PATH": os.environ["PATH"], "TEST_BASE_URL": "https://ark.example/api/v3",
                   "ARK_API_KEY": "k", "SSJH_API_TOKEN": "t", "FEISHU_APP_ID": "a",
                   "FEISHU_APP_SECRET": "s", **extra}
            return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)

    def test_missing_ark_base_url_uses_gateway_base(self):
        result = self._render({})
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads(result.stdout)["config"]["env"]
        self.assertEqual(env["ARK_BASE_URL"], "https://ark.example/api/v3")

    def test_explicit_base_is_normalized_to_api_v3(self):
        result = self._render({"ARK_BASE_URL": "https://ark.example", "TEST_BASE_URL": "https://ark.example/api/v3"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["config"]["env"]["ARK_BASE_URL"],
                         "https://ark.example/api/v3")

    def test_empty_required_credential_aborts_without_printing_values(self):
        result = self._render({"SSJH_API_TOKEN": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSJH_API_TOKEN", result.stderr)
        self.assertNotIn('"ARK_API_KEY"', result.stdout)

    def test_optional_collect_credentials_may_stay_empty(self):
        result = self._render({})
        env = json.loads(result.stdout)["config"]["env"]
        self.assertEqual(env["SSJH_USERNAME"], "")


if __name__ == "__main__":
    unittest.main()
