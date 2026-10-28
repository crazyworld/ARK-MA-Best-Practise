#!/usr/bin/env python3
"""report_self_check.py — Phase F 发布前报告自校验(确定性脚本)

对齐原版 prompt/07 Step 1 六项 + skill/insight/SKILL.md §五 HC3 前洞察质量检查中
可脚本化的条目。只扫正文(先剥掉 markdown 链接文字/URL、表格单元格里的热搜标题),
避免把「《万疆》」「经纪约合作」「10万游客」之类热搜标题误判。

严重度:
  fail  结构性问题或精确内部措辞 → 必须退回 Phase E 重跑对应版块(--sections)
  warn  启发式命中 → 不阻断,写入 HC3 issues_detected 交人工判断

人工项(不在本脚本):四版块风格一致、E4 消费洞察按覆盖人群排序、数字逐个回溯宽表。

用法:
  python report_self_check.py --project-dir {project_dir} --report-file 07_报告/xxx.md [--version N]
stdout:单行 JSON {"status":"pass|warn|fail","failed_sections":[...],"issues":[...]}
exit code:fail=1,其余=0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WORKSPACE_ROOT = Path("/workspace")
_ANNOTATION_SCRIPTS = Path(__file__).resolve().parents[2] / "topic6-annotation" / "scripts"

SECTION_HEADINGS = {
    "e1": "# 一、行业及热门话题",
    "e2": "# 二、营销节点",
    "e3": "# 三、平台新鲜事",
    "e4": "# 四、营销发现",
}
MIN_PROSE_CHARS = 20

# (检查项, 严重度, 正则, 适用版块 None=全部)
PHRASE_RULES: list[tuple[str, str, re.Pattern, set | None]] = [
    ("LLM 痕迹", "fail", re.compile(r"综上所述|样本量充足|已按判断规则过滤|作为\s*AI|以下是.{0,6}(洞察|分析|报告)"), None),
    ("版块意图泄露", "fail", re.compile(r"版块意图|本版块旨在|写作要求|输出要求|Prompt"), None),
    ("打标内部措辞", "fail", re.compile(r"乙方能不能接|乙方能否承接|对营销人有无参考价值|打标结果"), {"e4"}),
    ("越界推断", "fail", re.compile(r"年度趋势|密集月份"), None),
    ("模糊数字", "warn", re.compile(r"约\s*\d|大约|估计|大概|左右"), None),
    ("执行性建议", "warn", re.compile(r"建议品牌立即|应该布局|务必|立即行动|必须抓住"), None),
    ("热度单位", "fail", re.compile(r"热度[^\n，。|]{0,6}\d+(?:\.\d+)?\s*万|\d+(?:\.\d+)?\s*万\s*(?:热度|热值)"), None),
    ("跨平台比较", "warn", re.compile(
        r"(?:微博|抖音|B站|知乎).{0,8}(?:高于|低于|多于|少于|领先|不及|超过)(?:微博|抖音|B站|知乎)"
        r"|相比(?:于)?(?:微博|抖音|B站|知乎)"), {"e3"}),
]

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def _prose(line: str) -> str:
    """去掉链接与 URL 后的可读正文;热搜标题都在链接文字里,一并剥掉。"""
    line = _LINK_RE.sub(" ", line)
    return re.sub(r"https?://\S+", " ", line)


def split_sections(text: str) -> dict[str, list[tuple[int, str]]]:
    sections: dict[str, list[tuple[int, str]]] = {}
    current = None
    heading_to_id = {v: k for k, v in SECTION_HEADINGS.items()}
    for no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped in heading_to_id:
            current = heading_to_id[stripped]
            sections[current] = []
            continue
        if current:
            sections[current].append((no, line))
    return sections


def _is_prose_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("|", "#", ">", "---", "- 链接", "- [配图"))


def check(text: str) -> dict:
    issues: list[dict] = []

    def add(section, item, severity, detail, line=None):
        issues.append({"section": section, "check": item, "severity": severity,
                       "detail": detail, "line": line})

    sections = split_sections(text)
    for sid, heading in SECTION_HEADINGS.items():
        body = sections.get(sid)
        if body is None:
            add(sid, "章节完整性", "fail", f"缺少章节标题「{heading}」")
            continue
        prose = [l for _, l in body if _is_prose_line(l) and len(_prose(l).strip()) >= MIN_PROSE_CHARS]
        if not prose:
            add(sid, "洞察段落", "fail", "版块只有表格/标题,没有洞察正文")

    for sid, body in sections.items():
        for no, line in body:
            for url in (m.group(2).strip() for m in _LINK_RE.finditer(line)):
                if not re.match(r"^https?://", url):
                    add(sid, "超链接格式", "fail", f"非 http(s) 链接: {url[:60]}", no)
            if line.strip().startswith("- 链接") and not re.search(r"https?://", line):
                add(sid, "超链接格式", "fail", "「链接」行没有可点击 URL", no)
            if not _is_prose_line(line) and not line.strip().startswith("|"):
                continue
            prose = _prose(line)
            for item, severity, pattern, scope in PHRASE_RULES:
                if scope and sid not in scope:
                    continue
                m = pattern.search(prose)
                if m:
                    add(sid, item, severity, f"命中「{m.group(0)}」", no)

    e4 = sections.get("e4") or []
    in_consumer, insight_blocks, current_has = False, 0, None
    missing_finding = 0
    for _, line in e4:
        s = line.strip()
        if s.startswith("### "):
            in_consumer = s == "### 消费洞察"
            continue
        if in_consumer and s.startswith("#### 洞察"):
            if current_has is False:
                missing_finding += 1
            insight_blocks += 1
            current_has = False
        elif in_consumer and s.startswith("- 营销发现"):
            current_has = True
    if current_has is False:
        missing_finding += 1
    if missing_finding:
        add("e4", "消费洞察启示段", "fail", f"{missing_finding} 条消费洞察缺「营销发现」")

    failed = sorted({i["section"] for i in issues if i["severity"] == "fail"})
    status = "fail" if failed else ("warn" if issues else "pass")
    return {"status": status, "failed_sections": failed, "issues": issues}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", required=True)
    p.add_argument("--report-file", required=True, help="相对项目目录或绝对路径")
    p.add_argument("--version", type=int, default=None)
    args = p.parse_args()
    proj = Path(args.project_dir)
    if not proj.is_absolute():
        proj = WORKSPACE_ROOT / proj
    report = Path(args.report_file)
    if not report.is_absolute():
        report = proj / report
    if not report.exists():
        print(json.dumps({"status": "fail", "failed_sections": [],
                          "issues": [{"check": "报告文件", "severity": "fail",
                                      "detail": f"不存在: {report}"}]}, ensure_ascii=False))
        return 1
    result = check(report.read_text(encoding="utf-8"))
    result["report_file"] = str(report)
    out = report.with_name(f"self_check_{report.stem}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.path.insert(0, str(_ANNOTATION_SCRIPTS))
    try:
        from run_config_state import update_run_config

        update_run_config(proj, {"f": {"self_check": {
            "status": result["status"], "failed_sections": result["failed_sections"],
            "issue_count": len(result["issues"]), "file": str(out)}}})
    except ImportError:  # pragma: no cover
        pass
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
