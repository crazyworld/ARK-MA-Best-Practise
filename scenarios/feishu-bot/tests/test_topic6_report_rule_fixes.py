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
SKILLS = INSIGHT.parent
MARKETING_CALENDAR = SKILLS / "topic6-fetch-normalize" / "references" / "marketing_calendar_2026.csv"
ANNOTATION_SCRIPTS = SKILLS / "topic6-annotation" / "scripts"
ANNOTATION_PROMPTS = SKILLS / "topic6-annotation" / "prompts"


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

def _node_row(node: str, title: str, score: float = 80) -> dict:
    return {"提取节点": node, "标准化热度分": score, "商业实体": "", "行业归属": "电商",
            "事件簇名": title, "平台": "微博", "热点标题": title, "热点链接": f"https://weibo.com/{title}"}


def test_e2_unverified_nodes_fall_back_to_last_week_without_marker(monkeypatch):
    """查不到锚点的节点兜底归入上周(对齐原版，W46 双11 曾因丢弃整段消失),
    正文不出现「锚点未核实」,名单记入 e2_flags 供自校验写进 HC3。"""
    e2 = _load("t6_e2_q4", "e2_nodes.py")
    monkeypatch.setattr(e2, "_load_node_calendar_full", lambda: {"霜降": date(2026, 10, 23)})
    monkeypatch.setattr(e2, "_load_calendar_rows", lambda: [])
    df = pd.DataFrame([_node_row("霜降", "霜降吃什么"), _node_row("开学季", "开学第一课")])
    lw = e2.prep_e2_nodes(df, date(2026, 10, 26))["last_week"]
    assert "霜降" in lw["data_md"] and "开学季" in lw["data_md"]
    assert "锚点未核实" not in lw["data_md"]
    assert lw["unverified_nodes"] == ["开学季"]


def test_e2_only_unverified_nodes_still_gives_last_week(monkeypatch):
    e2 = _load("t6_e2_q4b", "e2_nodes.py")
    monkeypatch.setattr(e2, "_load_node_calendar_full", lambda: {})
    monkeypatch.setattr(e2, "_load_calendar_rows", lambda: [])
    lw = e2.prep_e2_nodes(pd.DataFrame([_node_row("开学季", "t")]), date(2026, 10, 26))["last_week"]
    assert lw["has_data"] is True and lw["unverified_nodes"] == ["开学季"]


def test_e2_verified_nodes_have_no_unverified_list(monkeypatch):
    e2 = _load("t6_e2_q4c", "e2_nodes.py")
    monkeypatch.setattr(e2, "_load_node_calendar_full", lambda: {"霜降": date(2026, 10, 23)})
    monkeypatch.setattr(e2, "_load_calendar_rows", lambda: [])
    lw = e2.prep_e2_nodes(pd.DataFrame([_node_row("霜降", "t")]), date(2026, 10, 26))["last_week"]
    assert lw["has_data"] is True and "unverified_nodes" not in lw


def test_e2_old_nodes_outside_window_still_dropped(monkeypatch):
    """兜底只针对查不到日期的节点;查得到且不在上周/未来30天的节点仍不展示(行为不变)。"""
    e2 = _load("t6_e2_q4d", "e2_nodes.py")
    monkeypatch.setattr(e2, "_load_node_calendar_full", lambda: {"国庆节": date(2026, 10, 1)})
    monkeypatch.setattr(e2, "_load_calendar_rows", lambda: [])
    lw = e2.prep_e2_nodes(pd.DataFrame([_node_row("国庆节", "t")]), date(2026, 11, 16))["last_week"]
    assert lw["has_data"] is False and "unverified_nodes" not in lw


# ---------------------------------------------------------------- W46 双11 丢失回归

def test_e2_w46_double11_lands_in_last_week_with_real_calendar():
    """真实日历 + C3 打标名「双11」:发布日 11-16 时必须进上周节点，且按锚点核实(非兜底)。"""
    e2 = _load("t6_e2_w46", "e2_nodes.py")
    assert e2.CALENDAR_PATH is not None
    e2.CALENDAR_PATH = MARKETING_CALENDAR
    lw = e2.prep_e2_nodes(pd.DataFrame([_node_row("双11", f"双11-{i}") for i in range(5)]),
                          date(2026, 11, 16))["last_week"]
    assert lw["has_data"] is True
    assert "| 双11 | 5 |" in lw["data_md"]  # 展示打标原名
    assert "unverified_nodes" not in lw


def test_e2_uses_same_name_map_as_c3():
    """E2 查日历的映射必须就是 C3 算窗口期用的那份(单一来源),不能各自维护。"""
    e2 = _load("t6_e2_map", "e2_nodes.py")
    ann = ANNOTATION_SCRIPTS / "datahub_annotate.py"
    spec = importlib.util.spec_from_file_location("t6_ann_map", ann)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert e2.NODE_NAME_MAP == mod.NAME_MAP and e2.NODE_NAME_MAP["双11"] == "双11大促"


def test_every_c3_vocab_node_resolvable_by_e2_or_known_gap():
    """C3 打标词表(节点标注最新版)里每个节点名，经 NAME_MAP 后都应能在 E2 日历查到。
    KNOWN_GAPS 是日历本身没有的节点(由 E2 兜底归入上周);新增缺口要么补映射/日历，
    要么显式加入这里——防止词表/日历改名后再次静默失配。"""
    import csv
    import io

    KNOWN_GAPS = {"腊八节", "世界羽毛球日", "跨年"}
    e2 = _load("t6_e2_vocab", "e2_nodes.py")
    names = {r["name"].strip() for r in csv.DictReader(io.StringIO(MARKETING_CALENDAR.read_text(encoding="utf-8-sig")))}
    vocab_md = _latest(ANNOTATION_PROMPTS / "节点标注").read_text(encoding="utf-8")
    vocab = re.findall(r"^\|\s*([^|]+?)\s*\|\s*(?:大众节日|电商大促|二十四节气|小众节点)\s*\|", vocab_md, re.M)
    assert len(vocab) > 50
    missing = {n for n in vocab if e2.NODE_NAME_MAP.get(n, n) not in names}
    assert missing == KNOWN_GAPS


def _self_check_module():
    spec = importlib.util.spec_from_file_location("t6_selfcheck_e2", INSIGHT / "scripts" / "report_self_check.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("nodes,expect_warn", [(["开学季"], True), (None, False)])
def test_self_check_surfaces_e2_fallback_nodes_as_warn(tmp_path, nodes, expect_warn):
    """兜底节点不进正文标注，但必须以 warn 进自校验结果(→ HC3 issues_detected)。"""
    import json

    sc = _self_check_module()
    round_dir = tmp_path / "06_洞察" / "v2"
    round_dir.mkdir(parents=True)
    lw = {"has_data": True, "data_md": "x", "empty_text": "上周无节点。"}
    if nodes:
        lw["unverified_nodes"] = nodes
    (round_dir / "e2_flags.json").write_text(json.dumps({"last_week": lw}, ensure_ascii=False), encoding="utf-8")
    result = {"status": "pass", "failed_sections": [], "issues": []}
    sc._add_e2_unverified_nodes(result, tmp_path, tmp_path / "07_报告" / "热点报告_W46_v2.md", None)
    if expect_warn:
        assert result["status"] == "warn" and "开学季" in result["issues"][0]["detail"]
        assert result["issues"][0]["severity"] == "warn"
    else:
        assert result == {"status": "pass", "failed_sections": [], "issues": []}


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
