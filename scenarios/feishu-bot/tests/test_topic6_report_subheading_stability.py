"""报告小标题样式(W45/W46):「💡 行业洞察：」缺失、营销发现小标题变普通文字。

fixture 为 212 上 W46(job_7cc2cd7322c5)的真实 E1 原稿、合并报告和 HC3 飞书文档 blocks。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOPIC6 = Path(__file__).resolve().parents[1] / "cases" / "topic6"
SKILLS = TOPIC6 / "ma-resources" / "skills"
FIX = Path(__file__).resolve().parent / "fixtures" / "topic6"
GLUED = ("品牌联名/资源合作", "品牌×明星代言合作", "合作争议")


def _load(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pe():
    # 与 test_topic6_e_stage_parsing 相同:pipeline_e 顶层依赖 openai,测试只用写文件函数
    import io
    import types
    old_mod, old_out, old_err = sys.modules.get("openai"), sys.stdout, sys.stderr
    stub = types.ModuleType("openai")
    stub.AsyncOpenAI = object
    sys.modules["openai"] = stub
    try:
        sys.stdout = types.SimpleNamespace(buffer=io.BytesIO())
        sys.stderr = types.SimpleNamespace(buffer=io.BytesIO())
        return _load(SKILLS / "topic6-insight" / "scripts" / "pipeline_e.py", "t6_pe_subheading")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if old_mod is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old_mod


@pytest.fixture(scope="module")
def frm():
    return _load(SKILLS / "topic6-web-report" / "scripts" / "fetch_report_md.py", "t6_frm_subheading")


@pytest.fixture(scope="module")
def m2s():
    return _load(SKILLS / "topic6-web-report" / "scripts" / "md2source.py", "t6_m2s_subheading")


def _sig(m2s, md: str) -> list[tuple[str, str]]:
    return [(b["className"], b["text"]) for b in m2s.convert(md)]


def _write(pe, tmp_path, sid: str, content: str) -> str:
    sec = next(s for s in pe.SECTIONS if s["id"] == sid)
    out = tmp_path / f"{sid}_v1.md"
    pe._write_section_output(sec, {"content": content}, out)
    return out.read_text("utf-8")


# ------------------------------------------------------------ E1 行业洞察

def test_e1_lead_added_when_missing(pe, tmp_path):
    raw = (FIX / "w46_e1_v1_1116.md").read_text("utf-8")
    body = raw.split("\n", 2)[2].strip()  # 去掉章节标题,即模型原始输出
    got = _write(pe, tmp_path, "e1", body)
    assert got == raw.replace("# 一、行业及热门话题\n\n", "# 一、行业及热门话题\n\n**行业洞察：**\n", 1)


@pytest.mark.parametrize("content", ["**行业洞察：**\n- 第一句；\n- 第二句。", "行业洞察：\n- 第一句。"])
def test_e1_lead_not_duplicated(pe, tmp_path, content):
    assert _write(pe, tmp_path, "e1", content).count("行业洞察") == 1


def test_other_sections_untouched(pe, tmp_path):
    assert "行业洞察" not in _write(pe, tmp_path, "e2", "- 本期节点")


# ------------------------------------------------------------ 飞书回读拆小标题

def test_feishu_readback_matches_local_report(frm, m2s):
    blocks = json.loads((FIX / "w46_feishu_blocks_1116.json").read_text("utf-8"))
    local = (FIX / "w46_report_v1_1116.md").read_text("utf-8")
    md = frm.blocks_to_markdown(blocks)
    assert _sig(m2s, md) == _sig(m2s, local)
    heads = [t for c, t in _sig(m2s, md) if "heading3" in c]
    assert all(h in heads for h in GLUED)


def _run(content: str, bold: bool) -> dict:
    return {"text_run": {"content": content, "text_element_style": {"bold": bold}}}


@pytest.mark.parametrize("els", [
    [_run("行业洞察：", True)],                                  # 整段加粗:走原逻辑
    [_run("双11", True), _run("：正文", False)],                  # 行内加粗,无换行
    [_run("正文", False), _run("\n加粗", True)],                  # 开头不是加粗
    [_run("标题", True), {"mention_doc": {"title": "d", "url": "u"}}],
])
def test_split_not_triggered(frm, els):
    assert frm._split_bold_title(els) is None


def test_split_only_for_plain_text_blocks(frm):
    els = [_run("双11", True), _run("\n：正文", False)]
    blocks = [
        {"block_id": "root", "block_type": 1, "children": ["l", "q"]},
        {"block_id": "l", "block_type": 12, "bullet": {"elements": els}},
        {"block_id": "q", "block_type": 34, "quote_container": {}, "children": ["q1"]},
        {"block_id": "q1", "block_type": 2, "text": {"elements": els}},
    ]
    assert "**双11**" not in frm.blocks_to_markdown(blocks)
