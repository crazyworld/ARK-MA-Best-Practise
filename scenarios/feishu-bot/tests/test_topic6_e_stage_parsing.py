"""W44 Phase E 输出解析与报告质量修复回归(Review B2/B3/B5/B7)。

- B2:子步骤 LLM 输出漏写 `### 标题` 时按表头定位,不再静默解析为 0 行;多表输出不串表
- B3:E3 单平台撰写结果混入其他平台段落/空占位时剔除,找不到本平台标题保留原文
- B5:热搜标题含换行 / 半角方括号时链接仍可被 md2source 识别
- B7:report_self_check 拦截 E3 平台重复与串平台
干净性:标题、表头都规范时结果与改动前一致。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

TOPIC6 = Path(__file__).resolve().parents[1] / "cases" / "topic6"
INSIGHT = TOPIC6 / "ma-resources" / "skills" / "topic6-insight"
STATS = INSIGHT / "01_统计"
WEB = TOPIC6 / "ma-resources" / "skills" / "topic6-web-report"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pe():
    old = sys.modules.get("openai")
    old_out, old_err = sys.stdout, sys.stderr
    stub = types.ModuleType("openai")
    stub.AsyncOpenAI = object
    sys.modules["openai"] = stub
    try:
        sys.stdout = types.SimpleNamespace(buffer=io.BytesIO())
        sys.stderr = types.SimpleNamespace(buffer=io.BytesIO())
        return _load(INSIGHT / "scripts" / "pipeline_e.py", "t6_pe_parsing")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if old is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old


@pytest.fixture(scope="module")
def stats():
    if str(STATS) not in sys.path:
        sys.path.insert(0, str(STATS))
    import _common  # noqa: E402
    import e4_marketing  # noqa: E402
    return types.SimpleNamespace(common=_common, e4=e4_marketing)


@pytest.fixture(scope="module")
def selfcheck():
    return _load(INSIGHT / "scripts" / "report_self_check.py", "t6_selfcheck_parsing")


@pytest.fixture(scope="module")
def md2source():
    return _load(WEB / "scripts" / "md2source.py", "t6_md2source_parsing")


# ------------------------------------------------------------------ B2

E3_TAG_NO_TITLE = """热点驱动词 || 归类 || 可借势 || 理由
双11 || 主体明确 || 是 || 电商节点
上热搜 || 主体不明的动作/结果词 || 否 || 无主体
"""


def test_e3_tagging_without_title_is_parsed(pe):
    """W44:模型直接从表头输出、没写「### 热点词打标」,原逻辑解析为 0 个候选词。"""
    warnings: list = []
    tags = pe._parse_e3_tagging_output(E3_TAG_NO_TITLE, warnings)
    assert set(tags) == {"双11", "上热搜"} and tags["双11"]["可借势"] == "是"
    assert warnings == []


def test_e3_draft_without_title_is_parsed(pe):
    text = "平台 || 事件簇名 || 信号类型 || 判断依据\n微博 || 某事件 || 风险/舆情型 || 依据\n"
    assert pe._parse_e3_insight_draft_output(text) == {
        ("微博", "某事件"): {"信号类型": "风险/舆情型", "判断依据": "依据"}}


def test_e3_regular_output_unchanged(pe):
    text = "### 热点词打标\n\n" + E3_TAG_NO_TITLE
    assert pe._parse_e3_tagging_output(text) == pe._parse_e3_tagging_output(E3_TAG_NO_TITLE)


E4_TAG_BOTH = """### 舆情风险打标
事件簇名 || 风险类型 || 可接性 || 理由
风险A || 品牌危机公关 || 接 || r

### 合作动态打标
事件簇名 || 变动类型 || 参考价值 || 理由
合作B || 新代言官宣 || 有用 || c
"""


def test_e4_tagging_regular_output(pe):
    risk, coop = pe._parse_e4_tagging_output(E4_TAG_BOTH)
    assert set(risk) == {"风险A"} and set(coop) == {"合作B"}


def test_e4_tagging_missing_second_title_does_not_mix_tables(pe):
    """写了第一个标题、漏了第二个:原逻辑把合作表的表头+行并进舆情段。"""
    text = E4_TAG_BOTH.replace("### 合作动态打标\n", "")
    risk, coop = pe._parse_e4_tagging_output(text)
    assert set(risk) == {"风险A"}
    assert set(coop) == {"合作B"} and coop["合作B"]["变动类型"] == "新代言官宣"


def test_e4_tagging_no_titles_at_all(pe):
    text = E4_TAG_BOTH.replace("### 舆情风险打标\n", "").replace("### 合作动态打标\n", "")
    risk, coop = pe._parse_e4_tagging_output(text)
    assert set(risk) == {"风险A"} and set(coop) == {"合作B"}


def test_e4_curation_and_arbitration_without_titles(pe):
    cur = ("事件簇名 || 现象概括\n营销X || 概括\n\n"
           "候选组合 || 现象概括\n消费1 + 消费2 || 概括2\n")
    marketing, consumer = pe._parse_e4_curation_output(cur)
    assert marketing == [{"事件簇名": "营销X", "现象概括": "概括"}]
    assert consumer == [{"候选组合": ["消费1", "消费2"], "现象概括": "概括2"}]
    arb = ("涉及候选 || 最终归属 || 理由\n合作动态:A + 舆情风险:A || 合作动态 || r\n\n"
           "涉及候选 || 保留候选 || 理由\n无重复事件\n")
    cross, same = pe._parse_e4_arbitration_output(arb)
    assert cross == [{"涉及候选": [("合作动态", "A"), ("舆情风险", "A")], "最终归属": "合作动态"}]
    assert same == []


def test_header_missing_falls_back_to_title(pe):
    """模型写了标题但漏写表头行:仍按原逻辑从标题段解析(_parse_pseudo_rows 把首行当数据)。"""
    text = "### 热点词打标\n双11 || 主体明确 || 是 || 电商节点\n"
    assert set(pe._parse_e3_tagging_output(text)) == {"双11"}


def test_unparseable_output_warns(pe):
    warnings: list = []
    assert pe._parse_e3_tagging_output("模型输出了一段分析文字。", warnings) == {}
    assert warnings and "E3热点词打标" in warnings[0]
    empty: list = []
    pe._parse_e3_tagging_output("", empty)
    assert empty == []


# ------------------------------------------------------------------ B3

W44_WEIBO = """### 微博

**热门事件 TOP 2**

微博本期关注集中在节日消费。

| 事件 | 热度值 |
|---|---|
| A | 1 |

### 抖音

上周抖音无营销可用热点。

### B站

上周B站无营销可用热点。

### 知乎

上周知乎无营销可用热点。
"""


def test_e3_weibo_section_drops_other_platforms(pe):
    warnings: list = []
    out = pe._isolate_e3_platform("微博", W44_WEIBO, warnings)
    assert "### 抖音" not in out and "### 知乎" not in out and "上周B站" not in out
    assert "### 微博" in out and "| A | 1 |" in out
    assert warnings and "已剔除" in warnings[0]


def test_e3_placeholder_without_heading_is_dropped(pe):
    body = "### 微博\n\n正文。\n\n上周抖音无营销可用热点。\n"
    out = pe._isolate_e3_platform("微博", body)
    assert "上周抖音" not in out and "正文。" in out


def test_e3_own_empty_placeholder_kept(pe):
    body = "### 抖音\n\n上周抖音无营销可用热点。"
    assert pe._isolate_e3_platform("抖音", body) == body


def test_e3_heading_variants_normalized(pe):
    body = "### **微博热点**\n\n正文\n\n### 抖音：\n\n串入内容"
    out = pe._isolate_e3_platform("微博", body)
    assert "串入内容" not in out and "正文" in out


def test_e3_event_subheading_with_platform_word_not_cut(pe):
    """「#### 微博之夜…」这类事件小标题不是平台切换。"""
    body = "### 抖音\n\n#### 微博之夜红毯\n内容保留"
    assert pe._isolate_e3_platform("抖音", body) == body


def test_e3_missing_own_heading_keeps_body_and_warns(pe):
    warnings: list = []
    body = "**热门事件 TOP 1**\n\n正文"
    assert pe._isolate_e3_platform("知乎", body, warnings) == body
    assert warnings and "未找到" in warnings[0]


def test_e3_clean_output_unchanged(pe):
    body = "### 知乎\n\n**热门事件 TOP 1**\n\n正文\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    warnings: list = []
    assert pe._isolate_e3_platform("知乎", body, warnings) == body and warnings == []


# ------------------------------------------------------------------ B5

def test_md_link_title_sanitizes(stats):
    f = stats.common.md_link_title
    assert f("不烧心<br>第二行") == "不烧心 第二行"
    assert f("【[AI全民制作人】") == "【［AI全民制作人】"
    assert f("a|b\nc") == "a\\|b c"
    assert f("普通标题") == "普通标题"


def _rows():
    return pd.DataFrame([
        {"热点标题": "【[AI全民制作人】第一期", "链接": "https://b23.tv/x", "平台": "B站", "标准化热度分": 90},
        {"热点标题": "不烧心\n的秘诀", "链接": "https://weibo.com/y", "平台": "微博", "标准化热度分": 80},
    ])


def test_all_link_renderers_produce_parseable_links(stats, pe, md2source):
    rendered = [
        stats.common._render_titles_with_links(_rows(), show_platform=True),
        stats.e4._titles_platform_score(_rows(), n=2),
        stats.e4._title_with_link(_rows().iloc[0]),
        pe._render_risk_candidate_links({"具体热搜": [
            {"标题": "【[AI全民制作人】第一期", "链接": "https://b23.tv/x", "平台": "B站", "热度分": "90"}]}),
        pe._render_coop_candidate_link({"代表热搜标题": "不烧心\n的秘诀", "代表热搜链接": "https://weibo.com/y"}),
    ]
    for text in rendered:
        assert "\n" not in text
        links: list = []
        md2source.extract_links(text, links)
        assert links, text
        assert all(l["href"].startswith("https://") for l in links)


def test_escape_md_cell_unchanged_for_non_title_cells(pe):
    """非标题单元格(事件簇名/说明)仍只转义 |,半角方括号不改(不改正文字形)。"""
    assert pe._escape_md_cell("事件[A]|B") == "事件[A]\\|B"


# ------------------------------------------------------------------ B7

def _report(e3: str) -> str:
    return f"""# 一、行业及热门话题

行业洞察正文足够长，满足最少字数要求的一段文字内容。

# 二、营销节点

节点洞察正文足够长，满足最少字数要求的一段文字内容。

# 三、平台新鲜事

{e3}

# 四、营销发现

营销发现正文足够长，满足最少字数要求的一段文字内容。
"""


def test_selfcheck_flags_w44_duplicated_platforms(selfcheck):
    e3 = W44_WEIBO + "\n### 抖音\n\n抖音正文足够长，满足最少字数要求的一段文字内容。\n"
    result = selfcheck.check(_report(e3))
    checks = {i["check"] for i in result["issues"] if i["section"] == "e3"}
    assert "E3 平台重复" in checks and "e3" in result["failed_sections"]


def test_selfcheck_flags_foreign_placeholder(selfcheck):
    e3 = "### 微博\n\n微博正文足够长，满足最少字数要求的一段文字内容。\n上周抖音无营销可用热点。\n"
    result = selfcheck.check(_report(e3))
    assert any(i["check"] == "E3 串平台" for i in result["issues"])


def test_selfcheck_platform_subheading_not_duplicate(selfcheck):
    """pipeline_e 给撰写的输入含「### 微博 · 平台热点词 TOP 3」,模型原样转写不应判平台重复。"""
    e3 = ("### 微博\n\n微博正文足够长，满足最少字数要求的一段文字内容。\n\n"
          "### 微博 · 平台热点词 TOP 3\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
          "### 抖音\n\n上周抖音无营销可用热点。\n")
    result = selfcheck.check(_report(e3))
    assert not [i for i in result["issues"] if i["check"].startswith("E3 ")]


def test_selfcheck_clean_e3_passes(selfcheck):
    e3 = ("### 微博\n\n微博正文足够长，满足最少字数要求的一段文字内容。\n\n"
          "### 抖音\n\n上周抖音无营销可用热点。\n")
    result = selfcheck.check(_report(e3))
    assert not [i for i in result["issues"] if i["check"].startswith("E3 ")]
