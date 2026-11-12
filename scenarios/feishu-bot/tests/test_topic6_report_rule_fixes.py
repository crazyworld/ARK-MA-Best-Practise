"""2026-11-11 业务验收问题修复回归。

Q1 E3 热门事件TOPN:第一排序=事件簇热点数量降序;第二排序=簇内标准化热度分平均值降序;
   「热度值」列同步展示平均分(排序依据与展示一致)
Q4 E2 上周节点:日历查不到锚点日期的节点不进报告(只给确定性内容),不再出现「锚点未核实」
Q5 E4 B 组撰写:营销观察「营销发现」100-120 字;消费洞察「行为反应」80-100 字
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

INSIGHT = Path(__file__).resolve().parents[1] / "cases" / "topic6" / "ma-resources" / "skills" / "topic6-insight"
STATS = INSIGHT / "01_统计"
E4_WRITING_B = INSIGHT / "02_洞察" / "E4_营销发现" / "_writing_marketing_consumer"
E2_PROMPTS = INSIGHT / "02_洞察" / "E2_营销节点"


def _load(name: str, file: str):
    if str(STATS) not in sys.path:
        sys.path.insert(0, str(STATS))
    spec = importlib.util.spec_from_file_location(name, STATS / file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _latest(directory: Path) -> Path:
    versions = [p for p in directory.glob("v*.md") if re.fullmatch(r"v\d+", p.stem)]
    return max(versions, key=lambda p: int(p.stem[1:]))


# ---------------------------------------------------------------- Q1

def _rows(cluster: str, scores: list[float]) -> list[dict]:
    return [{"事件簇名": cluster, "标准化热度分": s, "行业归属": "影视综艺", "平台": "微博",
             "热点标题": f"{cluster}-{i}", "热点链接": f"https://weibo.com/{cluster}/{i}"}
            for i, s in enumerate(scores)]


def test_e3_second_sort_key_is_cluster_average_score():
    e3 = _load("t6_e3_q1", "e3_platforms.py")
    # 同为 3 条:A 有一条极高分但均分低;B 均分高。旧口径(最高分)A 在前,新口径 B 在前
    df = pd.DataFrame(_rows("A", [99, 41, 40]) + _rows("B", [80, 80, 80]) + _rows("C", [50] * 4))
    agg = e3._rank_top5_events(df)
    assert list(agg["事件簇名"]) == ["C", "B", "A"]  # 第一排序数量;同数量按均分
    assert agg.set_index("事件簇名").loc["A", "平均标准化分"] == pytest.approx(60.0)


def test_e3_table_keeps_original_columns_and_hides_average_sort_key():
    """「平均热度值」只是排序用的临时值:按它排序,但不进 e3_data 表格,飞书报告/妙搭页面不出现该列。
    表格保持原版 6 列,「热度值」仍为簇内最高分。"""
    e3 = _load("t6_e3_q1b", "e3_platforms.py")
    df = pd.DataFrame(_rows("A", [99, 41, 40]))
    md = e3.prep_e3_platforms(df)
    header = next(line for line in md.splitlines() if line.startswith("| 排名 |"))
    assert header == "| 排名 | 事件名称 | 行业 | 热点总计 | 热度值 | 具体热搜 |"
    cells = [c.strip() for c in next(line for line in md.splitlines() if line.startswith("| 1 |")).strip("|").split("|")]
    assert len(cells) == 6 and cells[4] == "99.0"
    assert "平均热度值" not in md and "60.0" not in md


def test_e3_active_prompt_has_no_average_column():
    body = _latest(INSIGHT / "02_洞察" / "E3_平台新鲜事").read_text(encoding="utf-8").split("<!-- PROMPT_START -->", 1)[1]
    assert "平均热度值" not in body
    assert "排名/事件名称/行业/热点总计/热度值/具体热搜）" in body


# ---------------------------------------------------------------- Q4

def test_e2_unverified_nodes_are_excluded_not_marked(monkeypatch):
    e2 = _load("t6_e2_q4", "e2_nodes.py")
    monkeypatch.setattr(e2, "_load_node_calendar_full", lambda: {"霜降": date(2026, 10, 23)})
    monkeypatch.setattr(e2, "_load_calendar_rows", lambda: [])
    df = pd.DataFrame([
        {"提取节点": "霜降", "标准化热度分": 70, "商业实体": "", "行业归属": "医疗健康",
         "事件簇名": "霜降养生", "平台": "微博", "热点标题": "霜降吃什么", "热点链接": "https://weibo.com/1"},
        {"提取节点": "开学季", "标准化热度分": 90, "商业实体": "", "行业归属": "教育",
         "事件簇名": "开学", "平台": "微博", "热点标题": "开学第一课", "热点链接": "https://weibo.com/2"},
    ])
    result = e2.prep_e2_nodes(df, date(2026, 10, 26))
    md = result["last_week"]["data_md"]
    assert "霜降" in md
    assert "开学季" not in md and "锚点未核实" not in md


def test_e2_only_unverified_nodes_gives_empty_last_week(monkeypatch):
    e2 = _load("t6_e2_q4b", "e2_nodes.py")
    monkeypatch.setattr(e2, "_load_node_calendar_full", lambda: {})
    monkeypatch.setattr(e2, "_load_calendar_rows", lambda: [])
    df = pd.DataFrame([{"提取节点": "开学季", "标准化热度分": 90, "商业实体": "", "行业归属": "教育",
                        "事件簇名": "开学", "平台": "微博", "热点标题": "t", "热点链接": "https://weibo.com/2"}])
    assert e2.prep_e2_nodes(df, date(2026, 10, 26))["last_week"]["has_data"] is False


def test_e2_active_prompt_no_longer_mentions_unverified_marker():
    body = _latest(E2_PROMPTS).read_text(encoding="utf-8").split("<!-- PROMPT_START -->", 1)[1]
    assert "锚点未核实" not in body  # header 变更说明不进入模型输入


# ---------------------------------------------------------------- Q5

def test_e4_writing_b_word_ranges():
    text = _latest(E4_WRITING_B).read_text(encoding="utf-8")
    body = text.split("<!-- PROMPT_START -->", 1)[1]
    case_line = next(l for l in body.splitlines() if l.startswith("- 营销发现：{1段整合分析"))
    behavior_line = next(l for l in body.splitlines() if l.startswith("- 行为反应："))
    consumer_line = next(l for l in body.splitlines() if l.startswith("- 营销发现：{这个消费心理"))
    assert "100-120字" in case_line
    assert "80-100字" in behavior_line
    assert "100-120字" in consumer_line  # 业务口径「营销发现100-120字」覆盖两处营销发现
    assert "≤70字" not in body and "行为反应：{消费者的行为/集体反应，只写数据说明字段能支撑的内容，不编造具体发言，≤50字}" not in body


# ---------------------------------------------------------------- 命令列表

def test_user_facing_usage_hides_debug_commands():
    """用法提示只列业务命令；/test_feishu、/test_phase_f 是运维调试命令，不对用户展示。"""
    gw = (Path(__file__).resolve().parents[1] / "cases" / "topic6" / "gateway.py").read_text(encoding="utf-8")
    usage = gw.split("START_USAGE = (", 1)[1].split("\n)\n", 1)[0]
    for cmd in ("/start", "/continue", "/republish", "/new", "/log"):
        assert cmd in usage
    assert "/test_feishu" not in usage and "/test_phase_f" not in usage


# ---------------------------------------------------------------- Q2 补充:E1 主要驱动话题同次数按热度确定性排序

def test_e1_driver_topics_tie_broken_by_heat_deterministically():
    """次数仍是第一排序;同次数时按簇内最高标准化热度分降序,再按事件名,结果与输入行序无关。"""
    e1 = _load("t6_e1_tie", "e1_industry.py")
    rows = ([{"行业归属": "医疗健康", "事件簇名": "低热A", "标准化热度分": 40}]
            + [{"行业归属": "医疗健康", "事件簇名": "高热B", "标准化热度分": 95}]
            + [{"行业归属": "医疗健康", "事件簇名": "中热C", "标准化热度分": 70}]
            + [{"行业归属": "医疗健康", "事件簇名": "双条D", "标准化热度分": 30}] * 2)
    for order in (rows, list(reversed(rows))):
        md = e1.prep_e1_industry(pd.DataFrame(order), Path("/nonexistent"))
        line = next(l for l in md.splitlines() if l.startswith("| 医疗健康 |"))
        assert line.endswith("| 双条D(2次) | 高热B(1次) | 中热C(1次) |"), line



# ---------------------------------------------------------------- E4 说明字段不再程序截断(字数只由 Prompt 约束)

def test_e4_explanation_fields_are_not_truncated():
    e4 = _load("t6_e4_no_trunc", "e4_marketing.py")
    long = "说" * 150
    row = {"事件簇名": "某品牌联名", "商业实体": "某品牌", "行业归属": "快消", "标准化热度分": 90,
           "热点标题": "t", "链接": "u", "平台": "微博",
           "是否商业合作": "是", "合作动态说明": long, "是否风险预警": "是", "风险判断说明": long,
           "是否营销发现": "是", "营销发现说明": long, "是否消费者行为": "是", "消费者行为说明": long}
    df = pd.DataFrame([row])
    cand = e4.prep_e4_candidates(df)
    for section, field in (("合作动态", "合作动态说明"), ("舆情风险", "风险判断说明"),
                           ("营销观察", "营销发现说明"), ("消费洞察", "消费者行为说明")):
        assert cand[section], section
        assert cand[section][0][field] == long, (section, len(cand[section][0][field]))
    md = e4.prep_e4_marketing(df)
    assert md.count(long) == 4
    src = (INSIGHT / "01_统计" / "e4_marketing.py").read_text(encoding="utf-8")
    assert "[:60]" not in src and "[:80]" not in src
