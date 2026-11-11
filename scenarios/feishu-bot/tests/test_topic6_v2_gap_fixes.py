"""2026-11-10 客户 V2 对照遗漏修复回归。

FX01 发布日缺省 = run_config.period.date_end + 1(不再随运行当天漂移)
FX02 E2 节点日历与客户 V2 byday.md 节点集合/类型一致(补 4 个电商节点),文档不再指向已删除的外部日历
FX06 飞书发布 warnings(admin 授权失败等)必须进入 HC3 issues_detected
FX09 E3 口径文档为 v2(与 ACTIVE_SCORING_VERSION 一致);错误案例库含 E5~E17 MA 摘要
"""

from __future__ import annotations

import csv
import importlib.util
import io
import subprocess
import sys
import types
from datetime import date
from pathlib import Path

import pytest

TOPIC6_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
MA = TOPIC6_DIR / "ma-resources"
SKILLS = MA / "skills"
INSIGHT = SKILLS / "topic6-insight"
RUN_STATS = INSIGHT / "01_统计" / "run_stats.py"
E2_NODES = INSIGHT / "01_统计" / "e2_nodes.py"
CALENDAR = SKILLS / "topic6-fetch-normalize" / "references" / "marketing_calendar_2026.csv"
COORDINATOR = MA / "agents" / "coordinator.system.md"


def _load(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    old_out, old_err = sys.stdout, sys.stderr
    try:  # run_stats 导入时重绑 stdout/stderr,与 pytest 捕获冲突
        sys.stdout = types.SimpleNamespace(buffer=io.BytesIO())
        sys.stderr = types.SimpleNamespace(buffer=io.BytesIO())
        spec.loader.exec_module(module)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return module


@pytest.fixture(scope="module")
def run_stats():
    return _load(RUN_STATS, "t6_run_stats_v2gap")


# ---------------------------------------------------------------- FX01

def _project(tmp_path: Path, date_end: str | None) -> Path:
    proj = tmp_path / "W43热点周报_20261019-20261025"
    proj.mkdir()
    if date_end is not None:
        (proj / "run_config.yaml").write_text(
            f"period:\n  date_start: \"2026-10-19\"\n  date_end: \"{date_end}\"\n", encoding="utf-8")
    return proj


def test_publish_date_defaults_to_period_end_plus_one(run_stats, tmp_path, capsys):
    proj = _project(tmp_path, "2026-10-25")
    assert run_stats.resolve_publish_date(proj, None) == date(2026, 10, 26)


def test_publish_date_explicit_wins_but_warns_on_mismatch(run_stats, tmp_path, capsys):
    proj = _project(tmp_path, "2026-10-25")
    assert run_stats.resolve_publish_date(proj, "2026-10-26") == date(2026, 10, 26)
    assert "不一致" not in capsys.readouterr().err
    assert run_stats.resolve_publish_date(proj, "2026-11-10") == date(2026, 11, 10)
    assert "不一致" in capsys.readouterr().err


def test_publish_date_without_run_config_falls_back_to_today(run_stats, tmp_path, capsys):
    proj = _project(tmp_path, None)
    assert run_stats.resolve_publish_date(proj, None) == date.today()
    assert "退回今天" in capsys.readouterr().err


def test_coordinator_defines_publish_date():
    text = COORDINATOR.read_text(encoding="utf-8")
    assert "`{publish_date}` = `run_config.period.date_end` + 1 天" in text
    assert "不是运行当天" in text


# ---------------------------------------------------------------- FX02

CUSTOMER_NODES_ADDED = {
    ("2026-01-19", "年货节"), ("2026-05-21", "618预售启动"),
    ("2026-09-05", "99大促"), ("2026-10-20", "双11预售启动"),
}


def _calendar_rows() -> list[dict]:
    return list(csv.DictReader(io.StringIO(CALENDAR.read_text(encoding="utf-8-sig"))))


def test_calendar_matches_customer_v2_node_set():
    rows = _calendar_rows()
    keys = {(r["date"], r["name"]) for r in rows}
    assert len(rows) == len(keys) == 223
    assert CUSTOMER_NODES_ADDED <= keys
    types_ = {r["name"]: r["type"] for r in rows}
    for name in ("年货节", "618预售启动", "99大促", "双11预售启动", "618大促", "双11大促", "双12大促"):
        assert types_[name] == "电商大促", name
    for r in rows:  # week_num 口径 = ISO 周
        assert int(r["week_num"]) == date.fromisoformat(r["date"]).isocalendar()[1], r
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    raw = CALENDAR.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\r\n") == 224


def test_calendar_matches_c3_node_definitions():
    """C3 标注能提取的电商节点,E2 日历必须能查到锚点(否则落入「锚点未核实」)。"""
    nodes_def = SKILLS / "topic6-annotation" / "references" / "marketing_calendar" / "nodes_definition.csv"
    defs = {r["name"]: r["type"] for r in csv.DictReader(io.StringIO(nodes_def.read_text(encoding="utf-8")))}
    names = {r["name"] for r in _calendar_rows()}
    missing = sorted(n for n, t in defs.items() if t == "电商大促" and n not in names)
    assert missing == []


def test_e2_week_preview_shows_double11_presale(monkeypatch):
    e2 = _load(E2_NODES, "t6_e2_nodes_v2gap")
    monkeypatch.setattr(e2, "CALENDAR_PATH", CALENDAR)
    rows = e2._load_calendar_rows()
    window = [(d, n, t) for d, n, t in rows if date(2026, 10, 19) <= d <= date(2026, 10, 25)]
    assert (date(2026, 10, 20), "双11预售启动", "电商大促") in window


def test_calendar_docs_no_longer_point_to_removed_external_skill():
    for rel in ("topic6-annotation/ks/04_节点判定.md", "topic6-annotation/ks/07_报告结构.md",
                "topic6-annotation/prompts/06_阶段_E洞察.md", "topic6-annotation/prompts/运行前置.md"):
        text = (SKILLS / rel).read_text(encoding="utf-8")
        assert "marketing-node-tagging" not in text, rel
        assert "静默跳过" not in text, rel


# ---------------------------------------------------------------- FX06

def test_feishu_publish_warnings_go_to_hc3_issues():
    text = COORDINATOR.read_text(encoding="utf-8")
    assert "原样记录即可" not in text
    assert "MUST 逐条原文写入 HC3 `issues_detected`" in text
    hc3 = text[text.index("**HC3 payload schema:**"):text.index("**HC3 打回**")]
    assert "飞书发布 warnings 逐条" in hc3


# ---------------------------------------------------------------- FX09

def test_e3_scoring_doc_matches_active_version():
    doc = (INSIGHT / "references" / "E3_统计口径说明.md").read_text(encoding="utf-8")
    code = (INSIGHT / "01_统计" / "e3_platforms.py").read_text(encoding="utf-8")
    assert 'ACTIVE_SCORING_VERSION = "v2"' in code
    assert "0.6×H + 0.4×F" in doc and "(1 + 0.5×B)" in doc
    note = (INSIGHT / "references" / "综合热度指数算法说明.md").read_text(encoding="utf-8")
    assert "v1_legacy历史版本" in note


def test_error_case_library_has_v2_runtime_lessons():
    text = (MA / "memory" / "topic6" / "错误案例库.md").read_text(encoding="utf-8")
    for tag in ("E5 ·", "E6 ·", "E7 / E8 / E9", "E10 ·", "E13 ·", "E14 ·", "E15 ·", "E16 ·", "E17 ·", "E11", "E12"):
        assert tag in text, tag
    assert "不要设置 `MARKETING_CALENDAR_PATH`" in text
