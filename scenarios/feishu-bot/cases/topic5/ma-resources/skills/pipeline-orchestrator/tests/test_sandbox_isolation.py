"""沙箱只挂载 skill 目录：编排器在脱离 case 根的副本里必须能完成 G1 续跑解析。

回归：hc_bridge 曾按 parents[3] 加载 case 根的 topic5_intake.py，本地测试因
仓库目录完整而通过，线上沙箱 G1 通过后 run_pipeline 直接 ModuleNotFoundError。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILLS_ROOT = SKILL_DIR.parent

_PROBE = r"""
import json, sys
sys.path.insert(0, ".")
import hc_bridge
note = json.dumps({
    "brand": "瑞幸", "activity_name": "瑞幸×暹罗厘普联名活动",
    "period_start": "2026-09-28", "period_end": "2026-10-07",
    "platforms": ["小红书"], "brand_query": "瑞幸",
    "activity_query": "瑞幸+(暹罗厘普|多栋)", "content_scope": "all",
    "business_questions": "活动表现", "delivery_expectation": "飞书报告",
    "allowed_actions": "读取本任务数据",
    "corrections": [{"field": "brand", "new": "瑞幸"}],
}, ensure_ascii=False)
out = hc_bridge._parse_requirement(note)
print(json.dumps({"corrections": out["corrections"], "field_sources": out["field_sources"]}, ensure_ascii=False))
"""


class SandboxIsolationTest(unittest.TestCase):
    def test_g1_requirement_parse_without_case_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # 与沙箱一致：只挂载 skills/ 下各 skill，不含 case 根（topic5_intake.py 等）。
            skills = Path(temp_dir) / "mnt" / "skills"
            shutil.copytree(
                SKILLS_ROOT, skills,
                ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", "node_modules"),
            )
            mounted = skills / "pipeline-orchestrator"
            result = subprocess.run(
                [sys.executable, "-I", "-c", _PROBE],
                cwd=mounted, capture_output=True, text=True, timeout=60,
                env={"PYTHONPATH": str(mounted), "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["corrections"][0]["field"], "brand")
            self.assertEqual(payload["field_sources"], {})


if __name__ == "__main__":
    unittest.main()
