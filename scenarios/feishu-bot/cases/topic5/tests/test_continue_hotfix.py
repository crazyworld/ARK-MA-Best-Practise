"""修复后在原 job/Session 上续跑（/continue），不新开 job。

原理：失败节点经 /continue → retry_failed 时，Gateway 把本地最新的
tools/out/pipeline-orchestrator.zip（恢复包）挂进原 Session，沙箱校验 sha256 后用它重试
当前失败节点，已完成节点与产物全部保留。

约束：全量部署会「先删后建」两个 Agent，旧 Session 绑定的 Agent 被删即无法续跑；
因此只改编排器代码时用 TOPIC5_BUNDLE_ONLY=1：跑门禁 + 打包，不上传 Skill、不重建 Agent。
"""
from __future__ import annotations

import unittest
from pathlib import Path

_CASE = Path(__file__).resolve().parent.parent
SCRIPT = _CASE / "update_ma.sh"


class BundleOnlyDeployTest(unittest.TestCase):
    def test_bundle_only_packs_after_tests_and_skips_agent_rebuild(self):
        script = SCRIPT.read_text(encoding="utf-8")
        tests_done = script.index('TOPIC5_TEST_ONLY:-0')
        bundle = script.index('TOPIC5_BUNDLE_ONLY:-0')
        upload = script.index("tools/upload_skills.py --force")
        rebuild = script.index("create_all.sh --update-env --update-memory")
        self.assertLess(tests_done, bundle)
        self.assertLess(bundle, upload)
        self.assertLess(bundle, rebuild)
        block = script[bundle:upload]
        self.assertIn("./tools/pack_skills.sh", block)
        self.assertIn("exit 0", block)

    def test_runner_uses_local_bundle_for_retry(self):
        runner = (_CASE / "topic5_runner.py").read_text(encoding="utf-8")
        self.assertIn('"tools" / "out" / "pipeline-orchestrator.zip"', runner)
        self.assertIn("retry_failed node=", runner)


if __name__ == "__main__":
    unittest.main()
