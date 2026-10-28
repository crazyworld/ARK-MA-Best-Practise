#!/usr/bin/env python3
"""Topic6 报告 Markdown → topic6-web-report source.json(确定性归一化,不经 LLM)。

输入:pipeline_f.py 产出的 `07_报告/热点报告_{period_label}_v{N}.md`
输出:与 `assets/source/source.example.json` 同构的 block 数组,供 build-report.mjs 构建。

映射规则(与 build-report.mjs 的解析约定一一对应):
- 报告标题 `# 社媒热点周刊 · ...`             → 丢弃(页头标题由模板按周期生成)
- 首个章节前的 `>` 引用                       → docx-quote_container-block(数据范围/数据周期声明)
  其中 `⚠️`/演示说明/冒烟测试 行              → 拆出为首章节首个 docx-text-block(渲染为 caution-note)
- `# 一、...` 章节标题                        → docx-heading1-block
- 章节内最浅一级子标题(## 或 ###)            → docx-heading2-block(平台 tab / 子版块卡片)
- 更深子标题、整行加粗 `**X**`                → docx-heading3-block(小标题)
- 普通段落                                     → docx-text-block
- `- ` 列表项                                  → docx-bullet-block(`[配图：待补充]` 占位行跳过)
- Markdown 表格                                → docx-table-block(`<br>` → 换行,`\\|` 还原;
                                                 热度列 `81.7分` → `81.7` 以渲染热度条,其余文本原样保留)
- `[标题](url)`                                → 正文只留标题,`{text,href}` 写入 block.links
- `---`                                        → docx-divider-block
- 末章节之后的 `>` 引用(热度值说明)          → docx-quote_container-block(页尾说明卡)

- `#### 案例 N：…` + 其后 bullet               → docx-grid-block(营销观察左文右图卡片;
                                                 有 --case-images/case-N.* 时挂图,否则模板渲染占位)

用法:python md2source.py <report.md> <source.json> [--case-images <dir>]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

SECTION_H1_RE = re.compile(r"^[一二三四五六七八九十]+、")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
LINK_RE = re.compile(r"\[([^\[\]]+)\]\((https?://[^()]*(?:\([^()]*\)[^()]*)*)\)")
BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
CAUTION_RE = re.compile(r"^(⚠️|演示说明|冒烟测试)")
PLACEHOLDER_RE = re.compile(r"^\[?配图[：:]\s*待补充\]?$")
CASE_LINK_RE = re.compile(r"^链接[：:]\s*(?:\[[^\]]*\]\()?(https?://[^\s)]+)\)?\s*$")
HEAT_HEADER_RE = re.compile(r"^(?:标准化.*分|热度分|热度值|热点最高标准化分)$")
HEAT_VALUE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*分$")


def _href(url: str) -> str:
    """URL 原样保留;仅把空格等不安全字符编码(W39 分析师版即因含空格的 URL 未被识别而露出原文)。"""
    return quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~-._")


def extract_links(text: str, links: list[dict]) -> str:
    """把 `[标题](url)` 替换为标题,并把 {text,href} 追加到 links(按出现顺序去重)。"""

    def repl(m: re.Match) -> str:
        title = m.group(1).strip()
        item = {"href": _href(m.group(2)), "text": title}
        if item not in links:
            links.append(item)
        return title

    return LINK_RE.sub(repl, text)


def strip_inline_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.replace("\\|", "|").strip() for c in cells]


def normalize_cell(cell: str, header: str, links: list[dict]) -> str:
    text = re.sub(r"<br\s*/?>", "\n", cell, flags=re.I)
    text = extract_links(text, links)
    text = strip_inline_md(text)
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    m = HEAT_VALUE_RE.match(text)
    if m and HEAT_HEADER_RE.match(header):
        text = m.group(1)  # 热度列统一裸数字,模板才渲染热度条
    return text


class Builder:
    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.pending_caution: list[str] = []
        self.seen_section = False

    def add(self, kind: str, text: str = "", links: list | None = None, tables: list | None = None) -> None:
        idx = len(self.blocks)
        self.blocks.append({
            "className": f"block docx-{kind}-block",
            "id": str(idx + 1),
            "images": [],
            "links": links or [],
            "tables": tables or [],
            "text": text,
            "index": idx,
        })

    def text_block(self, kind: str, raw: str) -> None:
        links: list[dict] = []
        text = strip_inline_md(extract_links(raw, links))
        if text:
            self.add(kind, text, links)


def section_heading_levels(lines: list[str]) -> dict[int, int]:
    """每个 H1 章节内:最浅子标题级别 → heading2,其余 → heading3。返回 {行号: 2|3}。"""
    result: dict[int, int] = {}
    section: list[tuple[int, int]] = []

    def flush() -> None:
        if not section:
            return
        top = min(level for _, level in section)
        for line_no, level in section:
            result[line_no] = 2 if level == top else 3
        section.clear()

    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        if level == 1:
            flush()
        else:
            section.append((i, level))
    flush()
    return result


def convert(md: str) -> list[dict]:
    lines = md.replace("\r\n", "\n").split("\n")
    sub_levels = section_heading_levels(lines)
    b = Builder()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()

        if not line:
            i += 1
            continue

        m = HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), strip_inline_md(m.group(2))
            if level == 1:
                if SECTION_H1_RE.match(title):
                    b.add("heading1", title)
                    b.seen_section = True
                    for note in b.pending_caution:
                        b.text_block("text", note if note.startswith("⚠️") else f"⚠️ {note}")
                    b.pending_caution.clear()
                # 非章节 H1(报告大标题)丢弃
            else:
                b.text_block(f"heading{sub_levels.get(i, 3)}", title)
            i += 1
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            if b.seen_section:
                b.add("divider", "")
            i += 1
            continue

        if line.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            links: list[dict] = []
            kept = []
            for ql in quote_lines:
                ql = strip_inline_md(extract_links(ql, links))
                if not ql:
                    continue
                if not b.seen_section and CAUTION_RE.match(ql):
                    b.pending_caution.append(ql)
                else:
                    kept.append(ql)
            if kept:
                b.add("quote_container", "\n".join(kept), links)
            continue

        if line.startswith("|") and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1].strip()):
            header = [strip_inline_md(c) for c in split_row(line)]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            links: list[dict] = []
            body = []
            for r in rows:
                r = (r + [""] * len(header))[: len(header)]
                body.append([normalize_cell(c, header[k], links) for k, c in enumerate(r)])
            table = [header, *body]
            text = "\n\n".join("\t".join(row) for row in table)
            b.add("table", text, links, [table])
            continue

        bold = BOLD_LINE_RE.match(line)
        if bold:
            b.text_block("heading3", bold.group(1))
            i += 1
            continue

        bullet = re.match(r"^[-*+•]\s+(.*)$", line)
        if bullet:
            item = bullet.group(1).strip()
            link_only = CASE_LINK_RE.match(item)
            if link_only:
                # 案例卡「- 链接：URL」并入本案例「原始热点」行的标题链接,不单独渲染裸 URL
                target = None
                for blk in reversed(b.blocks):
                    if "docx-bullet-block" not in blk["className"]:
                        break  # 越过案例标题即停止,不跨案例
                    if re.match(r"^原始热点[：:]", blk["text"]):
                        target = blk
                        break
                if target is not None:
                    topic = re.sub(r"^原始热点[：:]\s*", "", target["text"])
                    topic = re.sub(r"\s*[（(](?:微博|抖音|B站|知乎|快手|头条|百度|多平台)[^）)]*[）)]\s*$", "", topic)
                    if topic:
                        target["links"].append({"href": _href(link_only.group(1)), "text": topic})
                        i += 1
                        continue
            if not PLACEHOLDER_RE.match(item):
                b.text_block("bullet", item)
            i += 1
            continue

        para = [line]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or HEADING_RE.match(nxt) or nxt.startswith(("|", ">", "- ", "* ", "• "))
                    or BOLD_LINE_RE.match(nxt) or re.fullmatch(r"-{3,}", nxt)):
                break
            para.append(nxt)
            i += 1
        b.text_block("text", "\n".join(para))

    if not b.seen_section:
        raise ValueError("报告中没有任何「一、/二、…」章节标题,无法归一化")
    return _group_cases(b.blocks)


CASE_TITLE_RE = re.compile(r"^案例\s*(\d+)\s*[：:]")


def _group_cases(blocks: list[dict]) -> list[dict]:
    """营销观察:`案例 N：` 小标题 + 其后 bullet 合并成一个 docx-grid-block(左文右图卡片)。

    images 先留空;有 HC3 人工配图时由 attach_case_images 填入,缺图由模板渲染占位,非阻断。"""
    out: list[dict] = []
    i = 0
    while i < len(blocks):
        blk = blocks[i]
        m = CASE_TITLE_RE.match(blk["text"]) if "docx-heading3-block" in blk["className"] else None
        if not m:
            out.append(blk)
            i += 1
            continue
        lines, links = [blk["text"]], list(blk["links"])
        i += 1
        while i < len(blocks) and "docx-bullet-block" in blocks[i]["className"]:
            lines.append(blocks[i]["text"])
            links.extend(lk for lk in blocks[i]["links"] if lk not in links)
            i += 1
        out.append({**blk, "className": "block docx-grid-block", "text": "\n".join(lines),
                    "links": links, "images": [], "case_number": int(m.group(1))})
    for idx, blk in enumerate(out):
        blk["index"] = idx
        blk["id"] = str(idx + 1)
    return out


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def attach_case_images(blocks: list[dict], image_dir: Path, source_dir: Path) -> list[int]:
    """把 `case-<N>.<ext>`(HC3 人工配图,由 Phase G 从飞书文档下载)挂到案例 N。返回缺图案例号。"""
    missing = []
    for blk in blocks:
        n = blk.get("case_number")
        if n is None:
            continue
        found = next((image_dir / f"case-{n}{ext}" for ext in IMAGE_EXTS
                      if (image_dir / f"case-{n}{ext}").is_file()), None)
        if found is None:
            missing.append(n)
            continue
        try:
            src = found.resolve().relative_to(source_dir.resolve()).as_posix()
        except ValueError:
            src = str(found.resolve())
        blk["images"] = [{"alt": blk["text"].split("\n", 1)[0] + "配图", "src": src}]
    return missing


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Topic6 报告 md → source.json")
    parser.add_argument("report")
    parser.add_argument("source_json")
    parser.add_argument("--case-images", default=None,
                        help="HC3 人工配图目录,文件名 case-<N>.<png|jpg|...>;缺图案例渲染占位")
    args = parser.parse_args(argv[1:])
    src, dst = Path(args.report), Path(args.source_json)
    blocks = convert(src.read_text(encoding="utf-8"))
    cases = [blk["case_number"] for blk in blocks if "case_number" in blk]
    missing = cases
    if args.case_images:
        missing = attach_case_images(blocks, Path(args.case_images), dst.parent)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(blocks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n_links = sum(len(blk["links"]) for blk in blocks)
    print(f"[md2source] {len(blocks)} blocks, {n_links} links -> {dst}")
    print(json.dumps({"cases": len(cases), "case_images_missing": len(missing),
                      "missing_case_numbers": missing}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
