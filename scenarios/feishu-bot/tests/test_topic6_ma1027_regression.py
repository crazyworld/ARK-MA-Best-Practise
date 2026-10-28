"""1027 分析师测试(MA-TOPIC6测试记录-1027)6 个页面问题的回归锁定。

输入是线上实际产出的 W43 报告 md(fixtures/topic6/w43_report_online_1027.md,无「热度值说明」)
以及线上手拼形态的 source.json(多值单元格无 "- " 前缀、案例无 grid-block)。

| 编号 | 现象 | 锁定方式 |
|---|---|---|
| MA-01 | 行业话题「主要驱动话题」单列 | 多值字段必须是 ul.cell-list |
| MA-02 | 平台新鲜事「具体热搜」竖排 | 同上(热门事件卡) |
| MA-03 | 舆情风险「具体热搜」单列 | 同上 |
| MA-04 | 营销观察退化为标题+列表 | 案例数 == case-card--visual 数,缺图显示「配图待补充」 |
| MA-05 | 消费洞察触发事件无链接 | 每条「触发事件」都有 a.source-link |
| MA-06 | 尾注取到页头「数据范围」 | 尾注标签必须是「热度值说明」 |
另锁定 validate-rendered-content 能拦住线上退化形态(原先只查内容覆盖,布局退化可通过)。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOPIC6 = Path(__file__).resolve().parents[1] / "cases" / "topic6"
WEB = TOPIC6 / "ma-resources" / "skills" / "topic6-web-report"
TEMPLATE = WEB / "assets" / "source"
MD2SOURCE = WEB / "scripts" / "md2source.py"
VALIDATE = WEB / "scripts" / "validate-rendered-content.mjs"
PIPELINE_F = TOPIC6 / "ma-resources" / "skills" / "topic6-insight" / "scripts" / "pipeline_f.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "topic6"
ONLINE_MD = FIXTURES / "w43_report_online_1027.md"
HANDMADE_SOURCE = FIXTURES / "w43_source_handmade_noprefix_1027.json"
DEGRADED_PAGE = FIXTURES / "w43_online_degraded_page_1027.html"
HEAT_NOTE_PREFIX = "热度值说明：报告展示热度值为经过标准化后的热度分值"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


def _build(tmp_path: Path, *, md: Path | None = None, source: Path | None = None) -> tuple[str, Path]:
    out = tmp_path / "web"
    shutil.copytree(TEMPLATE, out)
    src = out / "source.json"
    if source is not None:
        shutil.copy(source, src)
    else:
        conv = subprocess.run([sys.executable, str(MD2SOURCE), str(md), str(src),
                               "--case-images", str(out / "case_images")],
                              capture_output=True, text=True, check=False)
        assert conv.returncode == 0, conv.stderr
    build = subprocess.run(["node", str(out / "build-report.mjs"), str(src), str(out / "index.html")],
                           capture_output=True, text=True, check=False)
    assert build.returncode == 0, build.stderr
    html = (out / "index.html").read_text(encoding="utf-8")
    return html[html.find("<main"):], src


def _validate(src: Path, html_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(VALIDATE), str(src), str(html_file)],
                          capture_output=True, text=True, check=False)


def _list_fields(html: str, label: str) -> list[str]:
    return re.findall(
        r'<div class="data-card-field[^"]*data-card-field--list-grid[^"]*"><dt>%s</dt><dd>(.*?)</dd></div>' % label,
        html, re.S)


def _section(html: str, section_id: str) -> str:
    m = re.search(r'<section class="report-section[^"]*" id="%s".*?</section>' % section_id, html, re.S)
    assert m, section_id
    return m.group(0)


def _assert_page(html: str) -> None:
    # MA-01
    drivers = _list_fields(_section(html, "industry-distribution"), "主要驱动话题")
    assert drivers and all('<ul class="cell-list">' in f for f in drivers), "MA-01"
    # MA-02:平台新鲜事「热门事件」卡的多值具体热搜
    platform = _section(html, "platform-hotspots")
    hot = [f for f in _list_fields(platform, "具体热搜") if "<br" in f or "<li" in f]
    assert hot and all('<ul class="cell-list">' in f for f in hot), "MA-02"
    # MA-03:营销发现(舆情风险)具体热搜
    findings = _section(html, "marketing-findings")
    risk = [f for f in _list_fields(findings, "具体热搜") if "<br" in f or "<li" in f]
    assert risk and all('<ul class="cell-list">' in f for f in risk), "MA-03"
    # MA-04
    titles = re.findall(r"^\s*案例\s*\d+\s*[：:]", re.sub(r"<[^>]+>", "\n", findings), re.M)
    cards = findings.count('class="case-card case-card--visual"')
    assert titles and cards == len(titles), f"MA-04 cards={cards} titles={len(titles)}"
    assert findings.count("配图待补充") >= cards, "MA-04 缺图应显示占位"
    assert not re.search(r"<li[^>]*>\s*https?://", findings), "MA-04 不应出现裸 URL 列表项"
    # MA-05
    triggers = re.findall(r"<li[^>]*>(?:(?!</li>).)*?触发事件.*?</li>", findings, re.S)
    assert triggers and all("source-link" in t for t in triggers), "MA-05"
    # MA-06
    foot = re.search(r'<aside class="footer-note"[^>]*><strong>([^<]*)</strong><span>([^<]*)</span>', html)
    assert foot and (foot.group(1) + foot.group(2)).startswith(HEAT_NOTE_PREFIX), \
        f"MA-06 尾注={foot.group(1) if foot else None}"


def test_online_report_md_rebuilt_without_rerunning_f(tmp_path):
    """线上已产出的 W43 md(无热度值说明)直接重跑 G,6 项全部正确。"""
    assert "热度值说明" not in ONLINE_MD.read_text(encoding="utf-8")
    html, src = _build(tmp_path, md=ONLINE_MD)
    _assert_page(html)
    result = _validate(src, tmp_path / "web" / "index.html")
    assert result.returncode == 0, result.stderr


def test_report_through_new_pipeline_f(tmp_path):
    """新 pipeline_f 会追加 HEAT_NOTE;模拟其输出后重建,6 项全部正确且尾注取自报告本身。"""
    text = ONLINE_MD.read_text(encoding="utf-8")
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("ma1027_pipeline_f", PIPELINE_F)
    mod = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    md = tmp_path / "report.md"
    md.write_text(text.rstrip() + "\n\n---\n\n" + mod.HEAT_NOTE + "\n", encoding="utf-8")
    html, _ = _build(tmp_path, md=md)
    _assert_page(html)


def test_template_fallback_heat_note_matches_pipeline_f():
    """模板兜底文案与 pipeline_f.HEAT_NOTE 逐字一致(只维护一份口径)。"""
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("ma1027_pipeline_f2", PIPELINE_F)
    mod = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    builder = (TEMPLATE / "build-report.mjs").read_text(encoding="utf-8")
    fallback = re.search(r'HEAT_NOTE_DEFAULT = "([^"]*)"', builder).group(1)
    assert fallback == mod.HEAT_NOTE.lstrip("> ")


def test_handmade_noprefix_cells_still_render_as_grid(tmp_path):
    """线上手拼形态(无 "- " 前缀)经新模板渲染,多值字段仍是网格(MA-01/02/03 渲染层兜底)。"""
    html, _ = _build(tmp_path, source=HANDMADE_SOURCE)
    for section, label in (("industry-distribution", "主要驱动话题"),
                           ("platform-hotspots", "具体热搜"),
                           ("marketing-findings", "具体热搜")):
        multi = [f for f in _list_fields(_section(html, section), label) if "<br" in f or "<li" in f]
        assert multi and all('<ul class="cell-list">' in f for f in multi), (section, label)


def test_validator_blocks_online_degraded_page():
    """线上退化页(1027 前模板 + 手拼 source 产出,固化为 fixture)必须被 validate-rendered-content 拦下并点名 MA-0x。

    fixture 只剥离了内联 <style>(嵌入字体),<main> 正文原样保留;不依赖 git 历史,rebase/squash 后仍有效。
    """
    result = _validate(HANDMADE_SOURCE, DEGRADED_PAGE)
    assert result.returncode != 0
    for code in ("MA-01", "MA-02/03", "MA-04", "MA-05", "MA-06"):
        assert code in result.stderr, (code, result.stderr[-800:])
