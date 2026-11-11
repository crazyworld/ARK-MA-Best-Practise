#!/usr/bin/env python3
"""Phase G:读取 HC3 定稿飞书文档正文 → 还原为 md2source.py 可解析的 Markdown。

HC3 期间用户可以直接在飞书文档里改文字、表格和配图;网页必须以飞书定稿为准(客户 V2
G_ui_publish.md「输入源=HC3 后飞书文档实际内容」)。本脚本只负责"飞书文档 → Markdown",
后续 md2source → build → validate 链不变。

身份与取块复用 fetch_case_images.py(tenant token、/docx/v1/documents/{id}/blocks 全量分页)。

还原规则(与 publish_report_to_feishu 导入方向一一对应,保证 md2source 解析结果一致):
- heading1..9      → `#`..`#########` 标题
- text             → 段落;整段加粗 → `**…**`(md2source 视为小标题)
- bullet / todo    → `- …`;ordered → `N. …`
- quote / quote_container 子块 → `> …`
- table            → Markdown 表格(单元格多段用 `<br>` 连接,`|` 转义为 `\\|`)
- divider          → `---`
- 链接             → `[文本](url)`(飞书 link.url 为 URL 编码,解码一次还原)
- image            → 跳过(案例配图由 fetch_case_images.py 单独下载)
- grid / callout 等容器 → 递归输出子块

兜底(不阻断 Phase G):读取失败、还原后无「一、/二、…」章节、或章节数少于本地报告时,
改用 --fallback 指定的本地 md,stdout 标明 source=local_fallback 与原因。

用法:python fetch_report_md.py --doc <docx URL|document_id> --out <report.md> --fallback <本地报告.md>
stdout:JSON {"status":"success","source":"feishu"|"local_fallback","out":…,"reason":…}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from fetch_case_images import FEISHU, document_id, list_blocks, tenant_token  # noqa: E402

PAGE, TEXT, BULLET, ORDERED, CODE, QUOTE, TODO = 1, 2, 12, 13, 14, 15, 17
DIVIDER, IMAGE, TABLE, TABLE_CELL, QUOTE_CONTAINER = 22, 27, 31, 32, 34
HEADING_LEVEL = {3 + i: i + 1 for i in range(9)}  # block_type 3..11 → heading1..9
SECTION_RE = re.compile(r"^#\s+[一二三四五六七八九十]+、", re.M)


def _elements(block: dict) -> list[dict]:
    for value in block.values():
        if isinstance(value, dict) and isinstance(value.get("elements"), list):
            return value["elements"]
    return []


def _render_inline(elements: list[dict], *, in_table: bool = False) -> tuple[str, bool]:
    """返回 (Markdown 文本, 是否整段加粗)。"""
    parts: list[str] = []
    bold_flags: list[bool] = []
    for el in elements:
        if "text_run" in el:
            run = el["text_run"]
            text = run.get("content", "")
            style = run.get("text_element_style") or {}
            link = (style.get("link") or {}).get("url")
        elif "mention_doc" in el:
            text = el["mention_doc"].get("title", "")
            style, link = {}, el["mention_doc"].get("url")
        elif "equation" in el:
            text, style, link = el["equation"].get("content", ""), {}, None
        else:
            continue
        if not text:
            continue
        if in_table:
            text = text.replace("|", "\\|").replace("\n", "<br>")
        if link:
            text = f"[{text}]({unquote(link)})"
        if text.strip():
            bold_flags.append(bool(style.get("bold")))
        parts.append(text)
    rendered = "".join(parts)
    whole_bold = bool(bold_flags) and all(bold_flags)
    return rendered, whole_bold


def _cell_text(cell: dict, by_id: dict) -> str:
    lines = []
    for child_id in cell.get("children", []):
        child = by_id.get(child_id)
        if child is None:
            continue
        text, _ = _render_inline(_elements(child), in_table=True)
        if text.strip():
            lines.append(text.strip())
    return "<br>".join(lines)


def _render_table(block: dict, by_id: dict) -> list[str]:
    table = block.get("table") or {}
    prop = table.get("property") or {}
    cols = int(prop.get("column_size") or 0)
    cell_ids = table.get("cells") or block.get("children") or []
    if cols <= 0 or not cell_ids:
        return []
    rows = [cell_ids[i:i + cols] for i in range(0, len(cell_ids), cols)]
    out = []
    for r, row in enumerate(rows):
        cells = [_cell_text(by_id.get(cid, {}), by_id) for cid in row]
        out.append("| " + " | ".join(cells) + " |")
        if r == 0:
            out.append("|" + "|".join(["---"] * cols) + "|")
    return out


def blocks_to_markdown(blocks: list[dict]) -> str:
    by_id = {b["block_id"]: b for b in blocks}
    root = next((b for b in blocks if b.get("block_type") == PAGE), None)
    top = root.get("children", []) if root else [b["block_id"] for b in blocks if not b.get("parent_id")]
    lines: list[str] = []
    ordered_no = 0

    def emit(block_id: str, quote: bool = False) -> None:
        nonlocal ordered_no
        blk = by_id.get(block_id)
        if blk is None:
            return
        btype = blk.get("block_type")
        prefix = "> " if quote else ""
        if btype != ORDERED:
            ordered_no = 0

        if btype in HEADING_LEVEL:
            text, _ = _render_inline(_elements(blk))
            lines.extend(["", "#" * HEADING_LEVEL[btype] + " " + text.strip(), ""])
        elif btype in (TEXT, CODE):
            text, whole_bold = _render_inline(_elements(blk))
            if not text.strip():
                lines.append("" if not quote else ">")
            else:
                body = f"**{text.strip()}**" if whole_bold and btype == TEXT else text
                lines.extend(prefix + ln for ln in body.split("\n"))
                if not quote:
                    lines.append("")
        elif btype in (BULLET, TODO):
            text, _ = _render_inline(_elements(blk))
            lines.append(f"{prefix}- {text.strip()}")
        elif btype == ORDERED:
            ordered_no += 1
            text, _ = _render_inline(_elements(blk))
            lines.append(f"{prefix}{ordered_no}. {text.strip()}")
        elif btype == QUOTE:
            text, _ = _render_inline(_elements(blk))
            lines.extend("> " + ln for ln in text.split("\n"))
            lines.append("")
        elif btype == QUOTE_CONTAINER:
            for child in blk.get("children", []):
                emit(child, quote=True)
            lines.append("")
        elif btype == DIVIDER:
            lines.extend(["", "---", ""])
        elif btype == TABLE:
            lines.extend(["", *_render_table(blk, by_id), ""])
        elif btype in (IMAGE, TABLE_CELL):
            return  # 配图另行下载;单元格由 table 统一渲染
        else:
            for child in blk.get("children", []):  # grid / grid_column / callout 等容器
                emit(child, quote=quote)

    for block_id in top:
        emit(block_id)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def _section_count(md: str) -> int:
    return len(SECTION_RE.findall(md))


def _structure(md: str) -> tuple[int, int]:
    """(表格数, 链接数):用 md2source 同一解析器统计,判断飞书还原是否完整。"""
    from md2source import convert  # 同目录确定性归一化脚本

    blocks = convert(md)
    tables = sum(1 for b in blocks if "docx-table-block" in b["className"])
    links = sum(len(b["links"]) for b in blocks)
    return tables, links


def choose_source(feishu_md: str | None, local_md: str | None, error: str = "") -> tuple[str, str, str]:
    """返回 (采用的 Markdown, source, reason)。

    以飞书定稿为准;只有还原结果明显不完整(像是读取/转换故障而不是用户编辑)时才回退本地:
    无章节、章节数少于本地、本地有表格/链接而飞书还原后一个都没有。
    """
    if feishu_md is not None:
        sections = _section_count(feishu_md)
        local_sections = _section_count(local_md or "")
        if sections == 0:
            error = "飞书文档还原后没有「一、/二、…」章节标题"
        elif sections < local_sections:
            error = f"飞书文档章节数 {sections} 少于本地报告 {local_sections},疑似读取不完整"
        else:
            try:
                f_tables, f_links = _structure(feishu_md)
                l_tables, l_links = _structure(local_md) if local_md else (0, 0)
            except Exception as exc:  # noqa: BLE001
                error = f"飞书文档还原结果无法被 md2source 解析: {exc}"
            else:
                if l_tables and not f_tables:
                    error = f"飞书文档还原后表格数为 0(本地 {l_tables}),疑似转换故障"
                elif l_links and not f_links:
                    error = f"飞书文档还原后链接数为 0(本地 {l_links}),疑似转换故障"
                else:
                    return feishu_md, "feishu", ""
    if local_md is None:
        raise RuntimeError(f"飞书正文不可用且无本地兜底: {error}")
    return local_md, "local_fallback", error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HC3 定稿飞书文档 → Markdown")
    parser.add_argument("--doc", required=True, help="飞书 docx URL 或 document_id")
    parser.add_argument("--out", required=True, help="输出 Markdown 路径")
    parser.add_argument("--fallback", required=True, help="飞书读取失败时使用的本地报告 md")
    args = parser.parse_args(argv)

    fallback = Path(args.fallback)
    local_md = fallback.read_text(encoding="utf-8") if fallback.is_file() else None
    feishu_md, error = None, ""
    try:
        import httpx

        headers = {"Authorization": f"Bearer {tenant_token()}"}
        with httpx.Client(base_url=FEISHU, headers=headers, timeout=60) as client:
            feishu_md = blocks_to_markdown(list_blocks(client, document_id(args.doc)))
    except Exception as exc:  # noqa: BLE001 - 读取失败走本地兜底,不阻断 Phase G
        error = f"读取飞书文档失败: {exc}"

    try:
        md, source, reason = choose_source(feishu_md, local_md, error)
    except RuntimeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(json.dumps({"status": "success", "source": source, "out": str(out), "reason": reason},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
