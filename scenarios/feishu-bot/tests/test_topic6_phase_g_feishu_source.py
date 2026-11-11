"""Phase G 网页以 HC3 定稿飞书文档正文为源(fetch_report_md.py)。

背景:HC3 期间用户可在飞书文档里直接改文字/表格/配图。此前 Phase G 只从飞书取配图,
正文仍读本地 merged_report_file,飞书里改的文字不会进网页(客户 V2 G_ui_publish.md 要求
以飞书定稿为输入源)。本组测试锁定:
  1. 飞书 blocks → Markdown 的还原与 md2source 解析等价(结构往返一致)
  2. 用户在飞书改的文字/表格单元格会进入 source.json
  3. 读取失败 / 还原不完整时回退本地 md 并给出原因,不阻断 Phase G
  4. Coordinator Phase G 与 /republish 指令都改为先取飞书正文
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

TOPIC6_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
WEB = TOPIC6_DIR / "ma-resources" / "skills" / "topic6-web-report"
SCRIPTS = WEB / "scripts"
FETCH_REPORT = SCRIPTS / "fetch_report_md.py"
MD2SOURCE = SCRIPTS / "md2source.py"
BUILDER = WEB / "assets" / "source" / "build-report.mjs"
VALIDATE_RENDERED = SCRIPTS / "validate-rendered-content.mjs"
COORDINATOR = TOPIC6_DIR / "ma-resources" / "agents" / "coordinator.system.md"
REPORT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "topic6" / "w43_report_online_1027.md"


def _load(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def frm():
    return _load(FETCH_REPORT, "t6_fetch_report_md")


@pytest.fixture(scope="module")
def md2source():
    return _load(MD2SOURCE, "t6_md2source_feishu")


# ---------------------------------------------------------------- md → 飞书 blocks(模拟导入)

_LINK_RE = re.compile(r"\[([^\[\]]+)\]\((https?://[^()\s]*(?:\([^()]*\)[^()\s]*)*)\)")


def _runs(text: str, bold: bool = False) -> list[dict]:
    """把一行 Markdown 内联文本转成飞书 text_run 元素(链接 URL 按飞书习惯做百分号编码)。"""
    from urllib.parse import quote

    els, pos = [], 0
    for m in _LINK_RE.finditer(text):
        if m.start() > pos:
            els.append({"text_run": {"content": text[pos:m.start()], "text_element_style": {"bold": bold}}})
        els.append({"text_run": {"content": m.group(1), "text_element_style": {
            "bold": bold, "link": {"url": quote(m.group(2), safe="")}}}})
        pos = m.end()
    if pos < len(text):
        els.append({"text_run": {"content": text[pos:], "text_element_style": {"bold": bold}}})
    return els


class _Doc:
    def __init__(self) -> None:
        self.blocks = [{"block_id": "root", "block_type": 1, "children": []}]
        self.n = 0

    def _id(self) -> str:
        self.n += 1
        return f"b{self.n}"

    def add(self, block: dict, parent: dict | None = None) -> dict:
        block["block_id"] = self._id()
        self.blocks.append(block)
        (parent or self.blocks[0]).setdefault("children", []).append(block["block_id"])
        return block


def md_to_feishu_blocks(md: str) -> list[dict]:
    """按飞书导入 Markdown 的方式生成 blocks(标题/段落/列表/引用/表格/分割线/图片)。"""
    doc = _Doc()
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            level = len(h.group(1))
            key = f"heading{level}"
            doc.add({"block_type": 2 + level, key: {"elements": _runs(h.group(2))}})
            i += 1
            continue
        if re.fullmatch(r"-{3,}", line):
            doc.add({"block_type": 22, "divider": {}})
            i += 1
            continue
        if line.startswith(">"):
            qc = doc.add({"block_type": 34, "quote_container": {}, "children": []})
            while i < len(lines) and lines[i].strip().startswith(">"):
                body = lines[i].strip()[1:].strip()
                doc.add({"block_type": 2, "text": {"elements": _runs(body)}}, qc)
                i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            rows = [line]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells_rows = [[c.strip() for c in re.split(r"(?<!\\)\|", r.strip("|"))] for r in rows]
            cols = len(cells_rows[0])
            table = doc.add({"block_type": 31, "table": {"property": {"row_size": len(rows), "column_size": cols},
                                                          "cells": []}, "children": []})
            for row in cells_rows:
                row = (row + [""] * cols)[:cols]
                for cell in row:
                    c = doc.add({"block_type": 32, "table_cell": {}, "children": []}, table)
                    table["table"]["cells"].append(c["block_id"])
                    for part in re.split(r"<br\s*/?>", cell.replace("\\|", "|")):
                        doc.add({"block_type": 2, "text": {"elements": _runs(part)}}, c)
            continue
        b = re.match(r"^[-*]\s+(.*)$", line)
        if b:
            doc.add({"block_type": 12, "bullet": {"elements": _runs(b.group(1))}})
            i += 1
            continue
        bold = re.fullmatch(r"\*\*(.+?)\*\*", line)
        if bold:
            doc.add({"block_type": 2, "text": {"elements": _runs(bold.group(1), bold=True)}})
            i += 1
            continue
        doc.add({"block_type": 2, "text": {"elements": _runs(line)}})
        i += 1
    return doc.blocks


def _shape(blocks: list[dict]) -> list[tuple]:
    return [(b["className"], b["text"], tuple((l["text"], l["href"]) for l in b["links"]),
             json.dumps(b["tables"], ensure_ascii=False)) for b in blocks]


# ---------------------------------------------------------------- 1. 往返一致

def test_feishu_roundtrip_matches_local_markdown_structure(frm, md2source):
    md = REPORT_FIXTURE.read_text(encoding="utf-8")
    restored = frm.blocks_to_markdown(md_to_feishu_blocks(md))
    want, got = md2source.convert(md), md2source.convert(restored)
    assert len(got) == len(want)
    assert _shape(got) == _shape(want)


# ---------------------------------------------------------------- 2. 飞书编辑进入网页

def test_user_edits_in_feishu_doc_reach_source_json(frm, md2source):
    md = REPORT_FIXTURE.read_text(encoding="utf-8")
    blocks = md_to_feishu_blocks(md)
    # 模拟 HC3 期间用户在飞书里改了一段正文 + 一个表格单元格 + 插入一张图(图片块应被忽略)
    para = next(b for b in blocks if b.get("block_type") == 2 and b.get("text", {}).get("elements")
                and len(b["text"]["elements"][0]["text_run"]["content"]) > 20
                and not b["text"]["elements"][0]["text_run"]["text_element_style"].get("bold"))
    para["text"]["elements"] = [{"text_run": {"content": "【HC3人工改写】这是用户在飞书里改过的洞察。"}}]
    cell_text = next(b for b in blocks if b.get("block_type") == 2
                     and any(p.get("block_type") == 32 for p in blocks if b["block_id"] in p.get("children", [])))
    cell_text["text"]["elements"] = [{"text_run": {"content": "人工改单元格"}}]
    blocks[0]["children"].insert(3, "img1")
    blocks.append({"block_id": "img1", "block_type": 27, "image": {"token": "tok"}})

    got = md2source.convert(frm.blocks_to_markdown(blocks))
    texts = "\n".join(b["text"] for b in got)
    assert "【HC3人工改写】这是用户在飞书里改过的洞察。" in texts
    assert any("人工改单元格" in json.dumps(b["tables"], ensure_ascii=False) for b in got)


def test_heading_link_bold_and_quote_rendering(frm):
    blocks = [
        {"block_id": "root", "block_type": 1, "children": ["h", "p", "q", "l"]},
        {"block_id": "h", "block_type": 3, "heading1": {"elements": [{"text_run": {"content": "一、行业话题"}}]}},
        {"block_id": "p", "block_type": 2, "text": {"elements": [{"text_run": {
            "content": "行业洞察：", "text_element_style": {"bold": True}}}]}},
        {"block_id": "q", "block_type": 34, "quote_container": {}, "children": ["q1"]},
        {"block_id": "q1", "block_type": 2, "text": {"elements": [{"text_run": {"content": "数据周期：W43"}}]}},
        {"block_id": "l", "block_type": 12, "bullet": {"elements": [{"text_run": {
            "content": "某热搜", "text_element_style": {"link": {"url": "https%3A%2F%2Fs.weibo.com%2Fweibo%3Fq%3D%25E4"}}}}]}},
    ]
    md = frm.blocks_to_markdown(blocks)
    assert "# 一、行业话题" in md
    assert "**行业洞察：**" in md
    assert "> 数据周期：W43" in md
    assert "- [某热搜](https://s.weibo.com/weibo?q=%E4)" in md


# ---------------------------------------------------------------- 3. 兜底

def test_choose_source_prefers_feishu_and_falls_back_on_incomplete(frm):
    local = REPORT_FIXTURE.read_text(encoding="utf-8")
    feishu = frm.blocks_to_markdown(md_to_feishu_blocks(local))

    md, source, reason = frm.choose_source(feishu, local)
    assert (source, reason) == ("feishu", "") and md == feishu

    md, source, reason = frm.choose_source("只有一段话\n", local)
    assert source == "local_fallback" and "章节" in reason and md == local

    no_tables = re.sub(r"^\|.*$", "", feishu, flags=re.M)
    md, source, reason = frm.choose_source(no_tables, local)
    assert source == "local_fallback" and "表格数为 0" in reason

    md, source, reason = frm.choose_source(None, local, "读取飞书文档失败: boom")
    assert source == "local_fallback" and "boom" in reason

    with pytest.raises(RuntimeError):
        frm.choose_source(None, None, "x")


def test_cli_falls_back_to_local_without_credentials(tmp_path):
    out = tmp_path / "report_feishu.md"
    env = {"PATH": "/usr/bin:/bin"}  # 无飞书凭据 → 读取失败 → 本地兜底,退出码 0
    r = subprocess.run([sys.executable, str(FETCH_REPORT), "--doc", "https://x.feishu.cn/docx/AbC",
                        "--out", str(out), "--fallback", str(REPORT_FIXTURE)],
                       capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert result["source"] == "local_fallback" and "LARK" in result["reason"]
    assert out.read_text(encoding="utf-8") == REPORT_FIXTURE.read_text(encoding="utf-8")


def test_restored_feishu_markdown_builds_and_validates(frm, tmp_path):
    md = frm.blocks_to_markdown(md_to_feishu_blocks(REPORT_FIXTURE.read_text(encoding="utf-8")))
    report = tmp_path / "report_feishu.md"
    report.write_text(md, encoding="utf-8")
    source, html = tmp_path / "source.json", tmp_path / "index.html"
    for cmd in ([sys.executable, str(MD2SOURCE), str(report), str(source)],
                ["node", str(BUILDER), str(source), str(html)],
                ["node", str(VALIDATE_RENDERED), str(source), str(html)]):
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert r.returncode == 0, (cmd[1], r.stdout, r.stderr)


# ---------------------------------------------------------------- 4. 编排指令

def test_coordinator_phase_g_reads_feishu_report_first():
    text = COORDINATOR.read_text(encoding="utf-8")
    phase_g = text[text.index("### Phase G"):text.index("### Phase H")]
    fetch = phase_g.index("fetch_report_md.py")
    convert = phase_g.index("md2source.py")
    assert fetch < convert
    assert "--fallback \"$project_dir/{status.f.merged_report_file}\"" in phase_g
    assert "md2source.py\" \"$OUT/report_feishu.md\"" in phase_g
    assert "status.g.report_source" in phase_g


def test_republish_message_rereads_feishu_text_and_images():
    runner = (TOPIC6_DIR / "topic6_runner.py").read_text(encoding="utf-8")
    msg = runner[runner.index("REPUBLISH_MESSAGE = ("):runner.index("NEED_DECISION_REPLY_LIMIT")]
    assert "正文" in msg and "配图" in msg and "status.f.feishu_doc_url" in msg
    assert "不要重跑 Phase A~F" in msg
