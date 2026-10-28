"""Topic6 网页报告对齐分析师版(W39)的回归测试。

正确性:复现 W43 smoke 网页的缺陷 → 修复后不再出现
  - P0-1 Phase G 手拼 source.json 丢链接/丢「行业洞察」小标题 → md2source.py 确定性归一化
  - P0-2 period_label 写成 `W43 (2026-10-19~2026-10-25)` → 页头 `W43`、周期日期重复
  - P0-3 E3 无热门事件表格却输出「热门事件 TOP 5/TOP 1」
  - P1-4 页尾「热度值说明」缺失 / smoke 声明由 LLM 临场写
  - P2-6 风险热搜 `· 平台 · 81.7分` 带"分"时链接不渲染
干净性:规范输入保持原行为(规范 period 不改写、有表格的 TOP N 不动、非 smoke 不加声明、模板示例仍通过校验)。
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

TOPIC6_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
SKILLS = TOPIC6_DIR / "ma-resources" / "skills"
WEB = SKILLS / "topic6-web-report"
BUILDER = WEB / "assets" / "source" / "build-report.mjs"
EXAMPLE = WEB / "assets" / "source" / "source.example.json"
MD2SOURCE = WEB / "scripts" / "md2source.py"
VALIDATE_RENDERED = WEB / "scripts" / "validate-rendered-content.mjs"
PIPELINE_F = SKILLS / "topic6-insight" / "scripts" / "pipeline_f.py"
PIPELINE_E = SKILLS / "topic6-insight" / "scripts" / "pipeline_e.py"
E1_DIR = SKILLS / "topic6-insight" / "02_洞察" / "E1_行业话题"
E3_DIR = SKILLS / "topic6-insight" / "02_洞察" / "E3_平台新鲜事"
COORDINATOR = TOPIC6_DIR / "ma-resources" / "agents" / "coordinator.system.md"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipeline_f():
    return _load(PIPELINE_F, "t6_pipeline_f_align")


@pytest.fixture(scope="module")
def pipeline_e():
    # pipeline_e 导入时会重绑 sys.stdout/stderr(utf-8 包装),与 pytest 捕获冲突;同既有用例做法临时替换
    old = sys.modules.get("openai")
    old_out, old_err = sys.stdout, sys.stderr
    stub = types.ModuleType("openai")
    stub.AsyncOpenAI = object
    sys.modules["openai"] = stub
    try:
        sys.stdout = types.SimpleNamespace(buffer=io.BytesIO())
        sys.stderr = types.SimpleNamespace(buffer=io.BytesIO())
        return _load(PIPELINE_E, "t6_pipeline_e_align")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if old is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old


@pytest.fixture(scope="module")
def md2source():
    return _load(MD2SOURCE, "t6_md2source_align")


W43_SECTIONS = {
    "e1": """# 一、行业及热门话题

**行业洞察：**
- 上周营销可用热搜 18 条，共覆盖 11 个行业，TOP3 行业（影视综艺+体育赛事+服饰鞋包）热点数量合计占总数的 41.2%，行业热搜数量较多；
- 「服饰鞋包」热点数量与热度值双双位居前列，主要由侯明昊代言官宣引发，行业话题讨论较热。

| TOP10行业 | 热点总计 | 热度值 | 主要驱动话题 |
| --- | --- | --- | --- |
| 影视综艺 | 3 | 45.1 | - 谢画楼发现魏娆是假的了<br> <br>- 《小城良方》开播倒计时1天 |
| 服饰鞋包 | 2 | 89.4 | - 侯明昊出任雪中飞全球品牌代言人 |
""",
    "e2": """# 二、营销节点

## 1.1 上周节点

上周无节点。

## 1.2 本周节点预告

1个节点覆盖1/7天。

| 节点日期 | 节点名称 | 节点类型 | 品类机会 |
|---|---|---|---|
| 2026-10-31 | 万圣节 | 大众节日 | 适配美妆服饰、潮玩手办等领域 |
""",
    "e3": """# 三、平台新鲜事

### 微博

**热门事件 TOP 5**

本期热门话题均为单条独立内容，未形成跨热搜的聚合讨论效应。

**平台热点词 TOP 3**

### 抖音

**热门事件 TOP 1**

该平台本期热门话题集中在单起独立话题。
""",
    "e4": """# 四、营销发现

### 合作动态
上周仅监测到1起新代言官宣类合作动作。

#### 新代言官宣
| 事件 | 代表热搜 | 行业 | 热度分 | 备注 |
| --- | --- | --- | --- | --- |
| 侯明昊雪中飞全球品牌代言人 | [侯明昊雪中飞全球品牌代言人](https://s.weibo.com/weibo?q=%E4%BE%AF%E6%98%8E%E6%98%8A) | 服饰鞋包 | 90.1分 | 全球代言 |

### 舆情风险
上周舆情风险均为品牌危机公关类。

**品牌危机公关**
| 风险事件 | 具体热搜 | 风险影响 |
| --- | --- | --- |
| 小天才封闭社交倒逼家长购买 | - [小天才封闭社交倒逼家长购买](https://s.weibo.com/weibo?q=%E5%B0%8F%E5%A4%A9%E6%89%8D) · 微博 · 81.7分 | 儿童智能硬件品类可参考 |

### 营销观察
#### 案例 1：侯明昊雪中飞代言（微博 · 90.1分）
- 原始热点：侯明昊雪中飞全球品牌代言人（微博）
- 营销发现：借代言人热度做冬季品类预热。
- 链接：https://s.weibo.com/weibo?q=%E4%BE%AF%E6%98%8E%E6%98%8A
- [配图：待补充]

### 消费洞察
#### 洞察 1：儿童智能产品生态绑定引发家长消费决策压力
- 群体：儿童智能硬件消费相关家长群体
- 触发事件：[小天才封闭社交倒逼家长购买](https://s.weibo.com/weibo?q=%E5%B0%8F%E5%A4%A9%E6%89%8D)（微博 · 81.7分）
- 行为反应：家长表达消费决策压力
- 营销发现：可作为产品规则与用户沟通的参考方向
""",
}


def _make_project(tmp_path: Path, *, mode: str, period_label: str) -> Path:
    proj = tmp_path / "W43热点周报_20261019-20261025"
    rd = proj / "06_洞察" / "v1"
    rd.mkdir(parents=True)
    for sid, body in W43_SECTIONS.items():
        (rd / f"{sid}_v1.md").write_text(body, encoding="utf-8")
    (proj / "run_config.yaml").write_text(
        f"mode: {mode}\n"
        "period:\n"
        f"  period_label: \"{period_label}\"\n"
        "  date_start: \"2026-10-19\"\n"
        "  date_end: \"2026-10-25\"\n"
        "status:\n  sample:\n    sample_rows: 30\n",
        encoding="utf-8",
    )
    return proj


def _run_pipeline_f(proj: Path) -> Path:
    result = subprocess.run(
        [sys.executable, str(PIPELINE_F), "--project-dir", str(proj)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    reports = list((proj / "07_报告").glob("热点报告_*_v1.md"))
    assert len(reports) == 1
    return reports[0]


def _build(tmp_path: Path, report: Path) -> tuple[list, str]:
    source = tmp_path / "source.json"
    html_path = tmp_path / "index.html"
    conv = subprocess.run([sys.executable, str(MD2SOURCE), str(report), str(source)],
                          capture_output=True, text=True, check=False)
    assert conv.returncode == 0, conv.stderr
    build = subprocess.run(["node", str(BUILDER), str(source), str(html_path)],
                           capture_output=True, text=True, check=False)
    assert build.returncode == 0, build.stderr
    check = subprocess.run(["node", str(VALIDATE_RENDERED), str(source), str(html_path)],
                           capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
    return json.loads(source.read_text(encoding="utf-8")), html_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 正确性

def test_bad_period_label_is_canonicalized_in_report(tmp_path):
    proj = _make_project(tmp_path, mode="smoke", period_label="W43 (2026-10-19~2026-10-25)")
    text = _run_pipeline_f(proj).read_text(encoding="utf-8")
    assert "# 社媒热点周刊 · 2026-W43" in text
    assert "> 数据周期：2026-W43（2026-10-19 ~ 2026-10-25）" in text
    assert text.count("2026-10-19") == 1


def test_end_to_end_w43_smoke_page_matches_analyst_structure(tmp_path):
    proj = _make_project(tmp_path, mode="smoke", period_label="W43 (2026-10-19~2026-10-25)")
    report = _run_pipeline_f(proj)
    text = report.read_text(encoding="utf-8")
    assert "冒烟测试说明：本报告基于 30 条分层样本" in text
    assert text.rstrip().endswith("平台新鲜事中热度值为热点数量及热度值综合评分。")

    blocks, html = _build(tmp_path, report)
    # 页头周期与分析师版同格式
    assert "<strong>2026·W43</strong>" in html
    # 链接全部进入 block.links 并渲染,无裸 markdown
    hrefs = {link["href"] for b in blocks for link in b["links"]}
    assert len(hrefs) >= 2
    for href in hrefs:
        assert f'href="{href}"' in html
    assert "](http" not in html
    # 风险热搜带"分"也能渲染链接,且文本保留"分"
    assert '小天才封闭社交倒逼家长购买</a> · 微博 · 81.7分' in html
    # 「行业洞察」小标题渲染为 minor-heading
    assert "行业洞察：</h3>" in html
    # 页尾热度值说明卡 + smoke 提示
    assert '<aside class="footer-note" aria-label="热度值说明">' in html
    assert "caution-note" in html and "冒烟测试说明" in html
    # 案例:左文右图卡片;HC3 未配图 → 占位(非阻断);占位文字行与裸链接行被吸收,原始热点挂链接
    assert html.count('class="case-card case-card--visual"') == 1
    assert 'class="case-visual case-visual--placeholder"' in html
    assert "[配图" not in html
    assert "链接：http" not in html
    assert re.search(r'原始热点：(?:</strong>)?<a class="source-link" href="https://s\.weibo\.com/weibo\?q=%E4%BE%AF', html)


def test_case_card_uses_hc3_manual_image(tmp_path):
    proj = _make_project(tmp_path, mode="full", period_label="2026-W43")
    report = _run_pipeline_f(proj)
    images = tmp_path / "case_images"
    images.mkdir()
    (images / "case-1.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                      "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    )
    source = tmp_path / "source.json"
    html_path = tmp_path / "index.html"
    conv = subprocess.run([sys.executable, str(MD2SOURCE), str(report), str(source),
                           "--case-images", str(images)], capture_output=True, text=True, check=False)
    assert conv.returncode == 0, conv.stderr
    assert '"case_images_missing": 0' in conv.stdout
    for script in (WEB / "scripts" / "validate-case-image-sources.mjs",):
        r = subprocess.run(["node", str(script), str(source)], capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stderr
    build = subprocess.run(["node", str(BUILDER), str(source), str(html_path)],
                           capture_output=True, text=True, check=False)
    assert build.returncode == 0, build.stderr
    html = html_path.read_text(encoding="utf-8")
    assert '<figure class="case-visual"><img src="data:image/png;base64,' in html
    assert 'class="case-visual case-visual--placeholder"' not in html


def test_fetch_case_images_maps_first_image_after_case_heading():
    module = _load(WEB / "scripts" / "fetch_case_images.py", "t6_fetch_case_images")

    def heading(bid, text):
        return {"block_id": bid, "block_type": 6, "heading4": {"elements": [{"text_run": {"content": text}}]}}

    blocks = [
        {"block_id": "root", "block_type": 1, "children": ["h0", "h1", "b1", "g1", "h2", "b2", "h3", "i3"]},
        heading("h0", "营销观察"),
        heading("h1", "案例 1：甲（微博 · 90.0分）"),
        {"block_id": "b1", "block_type": 12, "bullet": {"elements": []}},
        {"block_id": "g1", "block_type": 24, "children": ["gc"]},
        {"block_id": "gc", "block_type": 25, "children": ["i1", "i1b"]},
        {"block_id": "i1", "block_type": 27, "image": {"token": "tok1"}},
        {"block_id": "i1b", "block_type": 27, "image": {"token": "tok1-second"}},
        heading("h2", "案例 2：乙（B站 · 80.0分）"),
        {"block_id": "b2", "block_type": 12, "bullet": {"elements": []}},
        heading("h3", "消费洞察"),
        {"block_id": "i3", "block_type": 27, "image": {"token": "not-a-case"}},
    ]
    cases, tokens = module.case_image_tokens(blocks)
    assert cases == [1, 2]
    assert tokens == {1: "tok1"}


def test_md2source_handles_url_with_space_like_analyst_bug(md2source):
    blocks = md2source.convert(
        "# 一、行业及热门话题\n\n"
        "- 触发事件:[上京东买vivo X500系列演唱会神器](https://s.weibo.com/weibo?q=上京东买vivo X500系列演唱会神器)(微博 · 100.0分)\n"
    )
    bullet = [b for b in blocks if "bullet" in b["className"]][0]
    assert "](" not in bullet["text"]
    assert bullet["links"][0]["text"] == "上京东买vivo X500系列演唱会神器"
    assert " " not in bullet["links"][0]["href"]


def test_e3_top_heading_removed_when_no_table(pipeline_e):
    body = W43_SECTIONS["e3"].split("\n", 2)[2]
    fixed = pipeline_e._fix_e3_top_headings(body)
    assert "TOP 5" not in fixed and "TOP 1" not in fixed and "TOP 3" not in fixed
    assert "本期热门话题均为单条独立内容" in fixed


def test_e3_top_heading_n_corrected_to_row_count(pipeline_e):
    body = (
        "### 微博\n\n**热门事件 TOP 5**\n\n洞察句。\n\n"
        "| 排名 | 事件名称 | 行业 | 热点总计 | 热度值 | 具体热搜 |\n|---|---|---|---|---|---|\n"
        "| 1 | A | 游戏 | 3 | 90.0 | - a |\n| 2 | B | 游戏 | 2 | 80.0 | - b |\n\n"
        "**平台热点词 TOP {N}**\n\n"
        "| 平台热点词 | 热度值 | 具体热搜 | 适配领域 | 借势方向 |\n|---|---|---|---|---|\n"
        "| x | 90.0 | - a | 游戏 | 可借势 |\n"
    )
    fixed = pipeline_e._fix_e3_top_headings(body)
    assert "**热门事件 TOP 2**" in fixed
    assert "**平台热点词 TOP 1**" in fixed


def test_prompts_versioned_and_contracts_documented():
    e3 = (E3_DIR / "v2.md").read_text(encoding="utf-8")
    assert "候选数=0" in e3 and "禁止：「TOP N」的 N 与其下表格实际行数不一致" in e3
    e1 = (E1_DIR / "v2.md").read_text(encoding="utf-8")
    assert "无上期数据：省略第3句" in e1
    assert (E3_DIR / "v1.md").exists() and (E1_DIR / "v1.md").exists(), "R06 Prompt 只增不改"
    phase_g = COORDINATOR.read_text(encoding="utf-8")
    phase_g = phase_g[phase_g.index("### Phase G"):phase_g.index("### Phase H")]
    assert "scripts/md2source.py" in phase_g and "禁止手工拼 source.json" in phase_g


# ---------------------------------------------------------------- 干净性

def test_canonical_period_label_untouched(pipeline_f, capsys):
    assert pipeline_f.canonical_period_label("2026-W43", "2026-10-19") == "2026-W43"
    assert "WARN" not in capsys.readouterr().err
    # ISO 跨年:2027-01-01 属于 2026-W53
    assert pipeline_f.canonical_period_label("W53", "2027-01-01") == "2026-W53"


def test_non_smoke_report_has_no_smoke_disclaimer(tmp_path):
    proj = _make_project(tmp_path, mode="full", period_label="2026-W43")
    text = _run_pipeline_f(proj).read_text(encoding="utf-8")
    assert "冒烟测试" not in text
    assert "# 社媒热点周刊 · 2026-W43" in text
    assert "热度值说明" in text


def test_e3_regular_output_unchanged(pipeline_e):
    body = (
        "### 微博\n\n**热门事件 TOP 1**\n\n洞察句。\n\n"
        "| 排名 | 事件名称 | 行业 | 热点总计 | 热度值 | 具体热搜 |\n|---|---|---|---|---|---|\n"
        "| 1 | A | 游戏 | 3 | 90.0 | - a |\n"
    )
    assert pipeline_e._fix_e3_top_headings(body) == body


def test_template_example_still_builds_and_validates(tmp_path):
    source = tmp_path / "source.example.json"
    source.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    html_path = tmp_path / "index.html"
    build = subprocess.run(["node", str(BUILDER), str(source), str(html_path)],
                           capture_output=True, text=True, check=False)
    assert build.returncode == 0, build.stderr
    check = subprocess.run(["node", str(VALIDATE_RENDERED), str(source), str(html_path)],
                           capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
