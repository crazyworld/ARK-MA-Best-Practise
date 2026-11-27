"""恢复包内的 validator_scan.py 必须能独立加载（job_436842fb74da n16：No module named 'policy'）。

pack_skills 把 scan_report.py 复制为 pipeline-orchestrator/validator_scan.py，但 scan_report
按「同目录 import policy」取口径；只复制扫描器、不复制 policy.py，在 Coordinator 沙箱
（不挂 topic5-validator）里加载即失败。仓库目录下两者同处一处，所以本地/预跑发现不了。
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
VALIDATOR_SCRIPTS = HERE.parent / "topic5-validator" / "scripts"
PACK = HERE.parent.parent.parent / "tools" / "pack_skills.sh"


class BundledScanLoadsInIsolationTest(unittest.TestCase):
    def test_pack_copies_policy_next_to_scanner(self):
        src = PACK.read_text(encoding="utf-8")
        i = src.index('"$staged_root/validator_scan.py"')
        self.assertIn('"$staged_root/policy.py"', src[i:i + 600])

    def test_scanner_and_policy_load_from_isolated_dir(self):
        # 模拟恢复包：只有 validator_scan.py + policy.py，且不在 topic5-validator 下
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(VALIDATOR_SCRIPTS / "scan_report.py", Path(tmp) / "validator_scan.py")
            shutil.copy(VALIDATOR_SCRIPTS / "policy.py", Path(tmp) / "policy.py")
            code = ("import importlib.util,sys;"
                    "s=importlib.util.spec_from_file_location('x', sys.argv[1]);"
                    "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                    "print('ok', bool(m.scan_markdown))")
            done = subprocess.run([sys.executable, "-c", code, str(Path(tmp) / "validator_scan.py")],
                                  cwd=tmp, capture_output=True, text=True,
                                  env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
            self.assertEqual(done.returncode, 0, done.stderr[-800:])
            self.assertIn("ok True", done.stdout)


if __name__ == "__main__":
    unittest.main()
