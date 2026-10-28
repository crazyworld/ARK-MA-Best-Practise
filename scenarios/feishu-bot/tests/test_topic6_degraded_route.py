"""单路标注整批缺失(wait_datahub_batch=degraded)端到端:merge → run_stats 必须跑通。

review 发现:merge 只给缺失的 C2 补空列,C3/R2~R5 缺失时宽表少列,
01_统计/ 按列名直接取值 → KeyError,流程在 HC2 之后的 Phase E 才崩。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

TOPIC6 = Path(__file__).resolve().parents[1] / "cases" / "topic6"
MERGE = TOPIC6 / "ma-resources/skills/topic6-annotation/scripts/merge_annotations.py"
RUN_STATS = TOPIC6 / "ma-resources/skills/topic6-insight/01_统计/run_stats.py"
MOCK_DIR = Path(os.environ.get(
    "TOPIC6_MOCK_DIR", Path.home() / "work" / "mock_data" / "ada_topic6_mock_data"))

sys.path.insert(0, str(TOPIC6 / "tools"))
from mock_project import materialize  # noqa: E402

pytestmark = pytest.mark.skipif(not MOCK_DIR.is_dir(), reason=f"mock 数据目录不存在: {MOCK_DIR}")


@pytest.fixture(scope="module")
def full_project(tmp_path_factory) -> Path:
    return materialize(MOCK_DIR, tmp_path_factory.mktemp("degraded"), "full")


def _merge(project: Path) -> dict:
    out = subprocess.run([sys.executable, str(MERGE), "--project-dir", str(project),
                          "--mode", "skip_sampling", "--run-id", "1"],
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr[-1500:]
    start = out.stdout.rfind("\n{")
    return json.loads(out.stdout[start:])


@pytest.mark.parametrize("subdir, col", [
    ("C3_节点标注", "提取节点"),
    ("R1_平台借势", "是否平台玩法"),
    ("R2_商业合作", "是否商业合作"),
    ("R3_风险预警", "是否风险预警"),
    ("R4_创意借鉴", "是否营销发现"),
    ("R5_消费者行为", "是否消费者行为"),
    ("C2_事件归档", "事件簇名"),
])
def test_single_route_missing_runs_through_stats(full_project, tmp_path, subdir, col):
    ann = full_project / "04_标注" / subdir
    hidden = tmp_path / "hidden"
    shutil.move(str(ann), str(hidden))
    try:
        result = _merge(full_project)
        assert result["gate_ok"] is True
        assert result["quality_status"] in {"degraded", "fail"}  # fail 仅当另有有效率 <80%
        assert any("整批缺失" in w for w in result["quality_warnings"])
        wide = pd.read_excel(result["out_file"])
        assert col in wide.columns, f"缺失路 {subdir} 应补空占位列「{col}」"
        assert len(wide.columns) == 28
        stats = subprocess.run([sys.executable, str(RUN_STATS), "--project-dir", str(full_project),
                                "--mode", "skip_sampling", "--out-dir", str(tmp_path / "stats"),
                                "--run-id", "1"],
                               capture_output=True, text=True, check=False)
        assert stats.returncode == 0, stats.stderr[-1500:]
        assert (tmp_path / "stats" / "e4_data.md").exists()
    finally:
        shutil.move(str(hidden), str(ann))
