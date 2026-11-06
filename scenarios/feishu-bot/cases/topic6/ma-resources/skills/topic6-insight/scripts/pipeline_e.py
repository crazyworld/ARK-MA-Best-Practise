#!/usr/bin/env python3
"""
E 阶段洞察生成 pipeline（MA 版）
有限并发调用 LLM，生成 4 个可断点恢复的报告洞察版块。

MA 适配要点：
  - 删掉 PROJECT_ROOT.parents[1] 上溯（MA 沙箱下 skill 路径 /mnt/skills/topic6-insight/，
    项目路径独立在 /workspace/Projects/ 下）
  - anthropic SDK → openai 兼容 SDK（AsyncOpenAI），统一走 bmc-llm-relay（blueai 模型网关）
    * 只读 LLM_RELAY_BASE_URL / LLM_RELAY_API_KEY，缺失直接报错，不回退 ARK_* / OPENAI_*
      （ARK_* 只用于部署脚本调方舟 Managed Agent 控制面）。
  - cost-tracker 从 topic6-annotation skill 挂载路径调用（annotation/insight 共用同一份）

设计原则：
  - skip_sampling 数据阶段用于正式交付；demo 模式允许基于 50 条抽样数据生成演示报告
  - full 模式先做 500 条标注校准，不直接生成洞察
  - 同一项目洞察按版本迭代（v1、v2...），每轮单独一个子目录

用法：
    python pipeline_e.py \\
        --project-dir "/workspace/Projects/W35_20260824-20260830" \\
        [--publish-date 2026-08-31] [--model ep-xxx] [--version 2] \\
        [--skip-data-prep] [--sections e1,e2]

输出（{project_dir}/06_洞察/v{n}/）：
    e1_v{n}.md ~ e4_v{n}.md   4版块洞察
    e{1,3,4}_data.md          数据快照
    e2_flags.json             E2 has_data 判断+数据/固定话术
    e4_candidates.json        E4 打标候选
    e4_tagging_audit.md       E4 打标原始输出（审计向）
    pipeline_e_report_v{n}.json  执行记录+Token 用量
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openai import AsyncOpenAI

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent  # scripts/ 上一级即 skill 根
PROMPTS_DIR = SKILL_DIR / "02_洞察"
SHARED_ROLE_STYLE = PROMPTS_DIR / "_shared" / "role_style.md"
E_DATA_SCRIPT = SKILL_DIR / "01_统计" / "run_stats.py"
COST_TRACKER = Path("/mnt/skills/topic6-annotation/tool/cost-tracker/cost_tracker.py")

# 复用 01_统计 的打分公式与平台顺序(E3 热点词打标后重新打分),不在本文件重复实现
STATS_DIR = SKILL_DIR / "01_统计"
if str(STATS_DIR) not in sys.path:
    sys.path.insert(0, str(STATS_DIR))
from e3_platforms import EXPECTED_PLATFORMS, _score_word_candidates  # noqa: E402
from _common import fmt_score, md_link_title  # noqa: E402
WORKSPACE_ROOT = Path("/workspace")
CHECKPOINT_VERSION = 1
# E 阶段峰值 6 路(E1/E2 + E3 打标∥初稿 + E4 打标∥枚举);CC V2.0 不设上限,这里设为峰值,
# 不排队又保留护栏。2026-11-04 bmc-llm-relay 6 路并发实测全部成功,无 429。
DEFAULT_MAX_WORKERS = 6
DEFAULT_MAX_ATTEMPTS = 5
# finish_reason=length 时 max_tokens 自动翻倍重试(对齐 CC V2 需求5),截断稿不落 checkpoint
MAX_TOKENS_RETRIES = 2
MAX_TOKENS_CEILING = 32000
# 完整性闸未通过(空内容/表格列数错乱)时,原参数重跑该版块的次数
SECTION_REVALIDATE_RETRIES = 1


def _rel_to_proj(p: Path, proj: Path) -> str:
    """产物里只记相对项目根的路径（避免把 MA 沙箱绝对路径写进交付文件）。"""
    try:
        return p.resolve().relative_to(proj.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


SECTIONS = [
    {
        "id": "e1",
        "name": "行业及热门话题",
        "prompt_dir": "E1_行业话题",
        "data_file": "e1_data.md",
        "section_title": "# 一、行业及热门话题",
        # 起始预算对齐 CC V2.0(实测输出约 1200 + thinking);撞线会自动升档,贴近用量是为省一次重试往返
        "max_tokens": 4000,
    },
    {
        "id": "e2",
        "name": "营销节点",
        "prompt_dir": "E2_营销节点",
        "data_file": "e2_flags.json",
        "section_title": "# 二、营销节点",
        "max_tokens": 5000,  # 对齐 CC V2.0(实测输出约 1700~2200)
    },
    {
        "id": "e3",
        "name": "平台新鲜事",
        "prompt_dir": "E3_平台新鲜事",
        "data_file": "e3_data.md",
        "section_title": "# 三、平台新鲜事",
        "max_tokens": 18000,
    },
    {
        "id": "e4",
        "name": "营销发现",
        "prompt_dir": "E4_营销发现",
        "data_file": "e4_data.md",
        "section_title": "# 四、营销发现",
        "max_tokens": 16000,
    },
]


# ---------------------------------------------------------------------------
# 版本管理
# ---------------------------------------------------------------------------

def next_version(insight_dir: Path) -> int:
    versions = []
    for d in insight_dir.glob("v*"):
        if d.is_dir():
            m = re.match(r"v(\d+)$", d.name)
            if m:
                versions.append(int(m.group(1)))
    return (max(versions) + 1) if versions else 1


def latest_round_dir(insight_dir: Path) -> Path | None:
    candidates = []
    for d in insight_dir.glob("v*"):
        if d.is_dir():
            m = re.match(r"v(\d+)$", d.name)
            if m:
                candidates.append((int(m.group(1)), d))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def resolve_round_version(insight_dir: Path, version: int | None, sections: str | None) -> int:
    """--sections 定点重跑未传 --version 时落回最新一轮:新开一轮只含部分版块,
    Phase F 按轮次合并四个版块会缺文件,status.e_insights.e*_file 也会指向不存在的文件。"""
    if version:
        return version
    latest = latest_round_dir(insight_dir)
    if sections and latest is not None:
        print(f"[pipeline_e] --sections 未指定 --version,复用最新轮次 {latest.name}")
        return int(latest.name[1:])
    return next_version(insight_dir)


def latest_data_run_id(merge_dir: Path, mode: str) -> int | None:
    nums = []
    for f in merge_dir.glob(f"wide_table_{mode}_r*.xlsx"):
        m = re.search(r"_r(\d+)\.xlsx$", f.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


# ---------------------------------------------------------------------------
# Prompt 加载
# ---------------------------------------------------------------------------

PROMPT_START_MARKER = "<!-- PROMPT_START -->"


def _prompt_version_key(path: Path) -> tuple:
    """v10 必须排在 v9 之后:按数字排序,非 v{数字} 命名排最前。"""
    m = re.fullmatch(r"v(\d+)", path.stem)
    return (1, int(m.group(1))) if m else (0, path.stem)


def load_prompt_template(prompt_dir: str) -> str:
    skill_prompt_dir = PROMPTS_DIR / prompt_dir
    if not skill_prompt_dir.exists():
        raise FileNotFoundError(f"Prompt 目录不存在：{skill_prompt_dir}")

    versions = sorted(skill_prompt_dir.glob("v*.md"), key=_prompt_version_key)
    if not versions:
        raise FileNotFoundError(f"Prompt 目录下无 v*.md 文件：{skill_prompt_dir}")

    latest = versions[-1]
    print(f"  [prompt] 使用：{latest.name}")
    text = latest.read_text(encoding="utf-8")

    if PROMPT_START_MARKER in text:
        text = text.split(PROMPT_START_MARKER, 1)[1].lstrip("\n")

    if SHARED_ROLE_STYLE.exists():
        shared = SHARED_ROLE_STYLE.read_text(encoding="utf-8")
        text = shared.rstrip("\n") + "\n\n" + text
    else:
        print(f"  [prompt] ⚠️ 共享角色/风格文件不存在：{SHARED_ROLE_STYLE}，跳过拼接", file=sys.stderr)

    return text


def fill_prompt(template: str, data: str) -> str:
    if "{{data}}" not in template:
        raise ValueError("Prompt 模板中未找到 {{data}} 占位符")
    return template.replace("{{data}}", data)


# ---------------------------------------------------------------------------
# LLM 调用（OpenAI 兼容 endpoint，走 bmc-llm-relay / blueai 模型网关）
# ---------------------------------------------------------------------------

async def _stream_completion(
    api_key: str,
    base_url: str,
    section: dict,
    prompt: str,
    model: str,
) -> dict:
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        timeout=600.0,
    )
    content_parts = []
    input_tokens = output_tokens = 0
    stop_reason = None
    try:
        stream = await client.chat.completions.create(
            model=model,
            max_tokens=section["max_tokens"],
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if content:
                content_parts.append(content)
            if getattr(choice, "finish_reason", None):
                stop_reason = choice.finish_reason
    finally:
        await client.close()
    content = "".join(content_parts)
    # 截断且正文为空(推理模型把预算全花在思考上)交给上层升档,不当作网络错误重试
    if not content.strip() and stop_reason != "length":
        raise ValueError("LLM stream returned empty content")
    return {
        "content": content,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "stop_reason": stop_reason,
    }


async def call_llm_async(
    api_key: str,
    base_url: str,
    section: dict,
    prompt: str,
    model: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    sid = section["id"]
    name = section["name"]
    t0 = time.time()

    print(f"[{sid}] 开始流式调用 LLM({name})...")
    last_error = None
    # 升档状态跨网络重试保留:断流重连时不回退到初始预算
    cur_max = int(section["max_tokens"])
    upgrades = 0
    discarded_in = discarded_out = 0
    for attempt in range(max_attempts):
        try:
            while True:
                response = await _stream_completion(
                    api_key, base_url, dict(section, max_tokens=cur_max), prompt, model
                )
                if (
                    response["stop_reason"] != "length"
                    or upgrades >= MAX_TOKENS_RETRIES
                    or cur_max >= MAX_TOKENS_CEILING
                ):
                    break
                discarded_in += response["input_tokens"]
                discarded_out += response["output_tokens"]
                nxt = min(cur_max * 2, MAX_TOKENS_CEILING)
                print(
                    f"[{sid}] ⚠️ finish_reason=length,max_tokens {cur_max}→{nxt} "
                    f"自动升档重试({upgrades + 1}/{MAX_TOKENS_RETRIES})",
                    file=sys.stderr,
                )
                cur_max = nxt
                upgrades += 1
            elapsed = time.time() - t0
            print(
                f"[{sid}] ✅ 完成，耗时 {elapsed:.1f}s，"
                f"input={response['input_tokens']} / "
                f"output={response['output_tokens']}"
            )
            if response["stop_reason"] == "length":
                print(
                    f"[{sid}] ⚠️ 升档至 max_tokens={cur_max} 后仍 finish_reason=length,"
                    "该版块按截断失败处理",
                    file=sys.stderr,
                )
            return {
                "id": sid,
                "name": name,
                "status": "ok",
                **response,
                # token 计入被丢弃的截断尝试(真实成本),effective 只记最终采纳那次
                "input_tokens": response["input_tokens"] + discarded_in,
                "output_tokens": response["output_tokens"] + discarded_out,
                "effective_output_tokens": response["output_tokens"],
                "max_tokens_used": cur_max,
                "max_tokens_upgrades": upgrades,
                "elapsed_s": round(elapsed, 1),
                "model": model,
            }
        except Exception as error:
            last_error = error
            status_code = getattr(error, "status_code", None)
            if (
                status_code is not None
                and status_code != 429
                and not 500 <= status_code < 600
            ):
                break
            if attempt < max_attempts - 1:
                delay = min(30.0, 2 ** attempt) + random.uniform(0.0, 0.5)
                print(
                    f"[{sid}] 第 {attempt + 1}/{max_attempts} 次调用失败："
                    f"{error}；{delay:.1f}s 后重试",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)

    elapsed = time.time() - t0
    print(
        f"[{sid}] ❌ LLM 调用失败({elapsed:.1f}s)：{last_error}",
        file=sys.stderr,
    )
    return {
        "id": sid,
        "name": name,
        "status": "error",
        "error": str(last_error),
        "content": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_s": round(elapsed, 1),
        "model": model,
        "stop_reason": None,
    }


# ---------------------------------------------------------------------------
# 完整性闸(对齐 CC V2 D 项):只判客观硬指标,不判洞察质量
# ---------------------------------------------------------------------------

_MD_TABLE_SEP_LINE_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _count_md_row_cells(line: str) -> int:
    """数一行 markdown 表格的单元格数,转义过的 \\| 不算列分隔符。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return len(s.replace("\\|", "\x00").split("|"))


def validate_section_content(sid: str, content: str, stop_reason: str | None = None) -> list[str]:
    """返回问题列表(空=通过):正文为空 / 升档后仍截断 / 表格行列数与表头不一致。"""
    if not content or not content.strip():
        return [f"[{sid}] 正文为空"]
    issues = []
    if stop_reason == "length":
        issues.append(f"[{sid}] finish_reason=length,升档至上限后仍被截断")
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if (
            line.startswith("|")
            and i + 1 < len(lines)
            and _MD_TABLE_SEP_LINE_RE.match(lines[i + 1].strip())
        ):
            header_cols = _count_md_row_cells(line)
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                row_cols = _count_md_row_cells(lines[j])
                if row_cols != header_cols:
                    issues.append(
                        f"[{sid}] 表格行单元格数({row_cols})≠表头({header_cols}):"
                        f"{lines[j].strip()[:80]}"
                    )
                j += 1
            i = j
            continue
        i += 1
    return issues


# ---------------------------------------------------------------------------
# E2 专属路由
# ---------------------------------------------------------------------------

def build_e2_prompt_data(flags: dict) -> str:
    parts = []
    if flags["last_week"]["has_data"]:
        parts.append("## 上周节点\n\n" + flags["last_week"]["data_md"])
    if flags["next_week"]["has_data"]:
        parts.append("## 本周节点预告\n\n" + flags["next_week"]["data_md"])
    return "\n\n---\n\n".join(parts)


async def build_e2_task(llm_call, sec: dict, insight_dir: Path) -> dict:
    sid, name = sec["id"], sec["name"]
    flags_path = insight_dir / sec["data_file"]
    if not flags_path.exists():
        return await _error_result(sec, f"数据文件不存在：{flags_path}")

    flags = json.loads(flags_path.read_text(encoding="utf-8"))
    lw, nw = flags["last_week"], flags["next_week"]

    if not lw["has_data"] and not nw["has_data"]:
        print(f"[{sid}] 上周/下周均无节点，跳过 LLM，直接输出固定话术")
        content = (
            f"## 1.1 上周节点\n\n{lw['empty_text']}\n\n"
            f"## 1.2 本周节点预告\n\n{nw['empty_text']}"
        )
        return {
            "id": sid, "name": name, "status": "ok", "content": content,
            "input_tokens": 0, "output_tokens": 0, "elapsed_s": 0.0,
            "model": "none(无节点，跳过LLM)", "stop_reason": None,
            # 确定性固定话术,不受 MIN_SECTION_BYTES 门槛约束(该门槛只防 LLM 空/截断输出)
            "fixed_text": True,
        }

    try:
        template = load_prompt_template(sec["prompt_dir"])
    except FileNotFoundError as e:
        return await _error_result(sec, str(e))

    try:
        prompt = fill_prompt(template, build_e2_prompt_data(flags))
    except ValueError as e:
        return await _error_result(sec, str(e))

    result = await llm_call(sec, prompt)
    if result["status"] != "ok":
        return result

    llm_body = _strip_markdown_fence(result["content"]).rstrip()
    if lw["has_data"] and nw["has_data"]:
        pass
    elif lw["has_data"]:
        llm_body = f"{llm_body}\n\n## 1.2 本周节点预告\n\n{nw['empty_text']}"
    else:
        llm_body = f"## 1.1 上周节点\n\n{lw['empty_text']}\n\n{llm_body}"

    result["content"] = llm_body
    return result


# ---------------------------------------------------------------------------
# 子步骤公共工具(E3/E4 打标、初稿、仲裁共用)
# ---------------------------------------------------------------------------
# 子步骤是内部判断任务:不拼共享角色/风格文件,产出不进最终报告,只写审计文件。

PSEUDO_SEP = " || "
# 子 Prompt 允许的"空表"占位行(如"无候选""无重复事件"),不是数据行
_PSEUDO_EMPTY_MARKERS = ("无候选", "无重复事件", "无")


def _escape_md_cell(v) -> str:
    return str(v).replace("|", "\\|")


def _render_multi_value(v) -> str:
    """多值字段(行业/商业实体)原始用"|"分隔;改成顿号,撰写 LLM 转抄时清理掉转义符也不会撑破表格列。"""
    return str(v).replace("|", "、")


def _render_pseudo_rows(header: list, rows: list) -> str:
    lines = [PSEUDO_SEP.join(header)]
    for row in rows:
        lines.append(PSEUDO_SEP.join(str(c) for c in row))
    return "\n".join(lines)


def _parse_pseudo_rows(text: str, expected_header: list | None = None) -> list:
    """解析" || "伪表格。

    expected_header:首行与之一致才当表头跳过;LLM 漏写表头时首行按数据行处理,不会把
    第一条数据误当表头导致整表静默解析为 0 条。多列表格中不含分隔符的行(如"无重复事件"
    占位句、代码块围栏)不是数据行,跳过。
    """
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("```")
    ]
    if not lines:
        return []
    if expected_header is not None:
        header = expected_header
        first = [c.strip() for c in lines[0].split(PSEUDO_SEP)]
        data_lines = lines[1:] if first == header else lines
    else:
        header = [c.strip() for c in lines[0].split(PSEUDO_SEP)]
        data_lines = lines[1:]
    rows = []
    for ln in data_lines:
        if len(header) > 1 and PSEUDO_SEP.strip() not in ln:
            continue
        cells = [c.strip() for c in ln.split(PSEUDO_SEP)]
        if len(cells) == 1 and cells[0] in _PSEUDO_EMPTY_MARKERS:
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _split_headed_sections(text: str, prefix: str) -> dict:
    sections: dict = {}
    title = None
    buf: list = []
    for line in text.split("\n"):
        if line.startswith(prefix):
            if title is not None:
                sections[title] = "\n".join(buf).strip()
            title = line[len(prefix):].strip()
            buf = []
        else:
            buf.append(line)
    if title is not None:
        sections[title] = "\n".join(buf).strip()
    return sections


_PSEUDO_SPLIT_RE = re.compile(r"\s*\|\|\s*")


def _header_cells(line: str) -> list:
    return [c.strip() for c in _PSEUDO_SPLIT_RE.split(line.strip())]


def _locate_pseudo_table(text: str, expected_header: list, known_headers: list) -> str | None:
    """按期望表头行在全文中定位一张" || "伪表格,返回「表头行 + 数据行」,找不到返回 None。

    截止于下一个已知表头行或下一个 `### ` 标题。不依赖 `### 标题`:模型漏写标题(W44 E3 打标/
    初稿直接从表头开始输出)时照样能取到;同一输出里有多张表(E4)时各取各的,不会串表。"""
    lines = text.splitlines()
    others = [h for h in known_headers if h != expected_header]
    for i, line in enumerate(lines):
        if _header_cells(line) != expected_header:
            continue
        block = [line]
        for nxt in lines[i + 1:]:
            if nxt.startswith("### ") or _header_cells(nxt) in others:
                break
            block.append(nxt)
        return "\n".join(block)
    return None


def _parse_table(text: str, sections: dict, title: str, expected_header: list,
                 known_headers: list) -> tuple[list, bool]:
    """先按表头定位,定位不到才退回按 `### 标题` 取段(兼容模型漏写表头行)。

    返回 (rows, located);located=False 表示表头与标题都没找到。"""
    block = _locate_pseudo_table(text, expected_header, known_headers)
    if block is None:
        if title not in sections:
            return [], False
        block = sections[title]
    return _parse_pseudo_rows(block, expected_header=expected_header), True


def _warn_unparsed(label: str, text: str, located: bool, warnings: list | None) -> None:
    """非空输出里既找不到表头也找不到标题 → 明确告警(此前静默解析为 0 行)。"""
    if located or not text.strip():
        return
    msg = f"{label}输出未找到期望表头或标题,解析为 0 行"
    print(f"[pipeline_e] ⚠️ {msg}", file=sys.stderr)
    if warnings is not None:
        warnings.append(msg)


def _strip_trailing_rule(body: str) -> str:
    """_split_headed_sections 切出的非末节会带上原分隔符"---",重新拼接前剥掉。"""
    body = body.rstrip()
    if body.endswith("---"):
        body = body[:-3].rstrip()
    return body


def _latest_sub_prompt_path(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"子步骤 Prompt 目录不存在：{directory}")
    versions = sorted(directory.glob("v*.md"), key=_prompt_version_key)
    if not versions:
        raise FileNotFoundError(f"子步骤 Prompt 目录下无 v*.md 文件：{directory}")
    return versions[-1]


async def _run_sub_step(llm_call, step_id: str, name: str, prompt_dir: Path,
                        data: str, max_tokens: int) -> dict:
    """跑一次子步骤 LLM 调用。Prompt 缺失按调用失败返回,由调用方走各自的降级路径。"""
    try:
        path = _latest_sub_prompt_path(prompt_dir)
    except FileNotFoundError as error:
        return {"id": step_id, "name": name, "status": "error", "error": str(error),
                "content": "", "input_tokens": 0, "output_tokens": 0, "elapsed_s": 0.0}
    print(f"  [{step_id}] 使用：{path.parent.name}/{path.name}")
    template = path.read_text(encoding="utf-8")
    if PROMPT_START_MARKER in template:
        template = template.split(PROMPT_START_MARKER, 1)[1].lstrip("\n")
    sec = {"id": step_id, "name": name, "max_tokens": max_tokens}
    return await llm_call(sec, fill_prompt(template, data))


class _Usage:
    """汇总一个版块内所有子调用的真实 token 花费(含失败/被丢弃的尝试)。"""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, result: dict | None) -> dict | None:
        if result:
            self.input_tokens += result.get("input_tokens", 0) or 0
            self.output_tokens += result.get("output_tokens", 0) or 0
        return result


def _sub_ok(result: dict | None, sid: str, label: str, fallback: str) -> bool:
    if result is not None and result.get("status") == "ok" and result.get("content", "").strip():
        return True
    err = (result or {}).get("error", "空输出")
    print(f"[{sid}] ⚠️ {label}失败,{fallback}：{err}", file=sys.stderr)
    return False


def _section_result(sec: dict, content: str, usage: _Usage, t0: float, model: str,
                    stop_reason: str | None, **extra) -> dict:
    return {
        "id": sec["id"], "name": sec["name"], "status": "ok", "content": content,
        "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
        "elapsed_s": round(time.time() - t0, 1), "model": model,
        "stop_reason": stop_reason, **extra,
    }


# ---------------------------------------------------------------------------
# E3 专属路由(对齐 CC V2.0):1a 热点词打标 ∥ 1b 事件洞察初稿 → 本地合并 → 四平台并发撰写
# ---------------------------------------------------------------------------
# 1a:跨平台去重后的热点驱动词判"主体是否明确"(过滤)+"是否可借势"(打分加成),本地复用
#     e3_platforms._score_word_candidates 重新打分取 TOP3,追加到各平台块末尾。
#     失败时本轮不展示「平台热点词」(不回退未过滤候选,避免主体不明词混入)。
# 1b:每平台 TOP5 事件簇读全量标题判信号类型,作为「洞察初稿参考」插到平台块开头。
#     失败时撰写阶段退回只看 3 条代表标题自行判断。
# 撰写:Prompt 本就逐平台独立(禁止跨平台比较),按平台切片并发,墙钟=最慢单平台,
#     单次输出与平台数解耦;任一平台失败则整个 E3 失败(缺平台的报告不完整)。

E3_WORD_CANDIDATES_FILE = "e3_word_candidates.json"
E3_EVENT_FULLTEXT_FILE = "e3_event_fulltext.json"
E3_TAGGING_AUDIT_FILE = "e3_tagging_audit.md"
E3_INSIGHT_DRAFT_AUDIT_FILE = "e3_insight_draft_audit.md"
E3_TAGGING_PROMPT_DIR = PROMPTS_DIR / "E3_平台新鲜事" / "_tagging"
E3_INSIGHT_DRAFT_PROMPT_DIR = PROMPTS_DIR / "E3_平台新鲜事" / "_insight_draft"
# 起始预算(CC V2 实测值),撞线由 call_llm_async 自动升档
E3_TAGGING_MAX_TOKENS = 20000
E3_INSIGHT_DRAFT_MAX_TOKENS = 5000
E3_PLATFORM_WRITE_MAX_TOKENS = 8000
E3_KEEP_TAG = "具体实体/热梗"
E3_FILTER_TAG = "主体不明的动作/结果词"
E3_SIGNAL_TYPES = {"品牌/商业动作型", "风险/舆情型", "消费态度/情绪信号型", "无信号"}


def _build_e3_tagging_input(candidates: dict) -> str | None:
    seen: dict = {}
    for rows in candidates.values():
        for r in rows:
            seen.setdefault(r["热点驱动词"], (r["词频"], r["均分"]))
    if not seen:
        return None
    rows = [[w, freq, fmt_score(score, suffix="")] for w, (freq, score) in seen.items()]
    return _render_pseudo_rows(["热点驱动词", "词频", "均分"], rows)


E3_TAGGING_HEADER = ["热点驱动词", "归类", "可借势", "理由"]
E3_DRAFT_HEADER = ["平台", "事件簇名", "信号类型", "判断依据"]


def _parse_e3_tagging_output(text: str, warnings: list | None = None) -> dict:
    sections = _split_headed_sections(text, prefix="### ")
    rows, located = _parse_table(text, sections, "热点词打标", E3_TAGGING_HEADER, [E3_TAGGING_HEADER])
    _warn_unparsed("E3热点词打标", text, located, warnings)
    return {
        r["热点驱动词"]: {"归类": r.get("归类", ""), "可借势": r.get("可借势", "")}
        for r in rows if r.get("热点驱动词")
    }


def _build_e3_insight_draft_input(fulltext: dict) -> str | None:
    blocks = []
    for pname, events in fulltext.items():
        if not events:
            continue
        lines = [f"## {pname}"]
        for ev in events:
            lines.append(f"### {ev['事件簇名']}")
            lines.extend(f"- {t}" for t in ev["标题列表"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else None


def _parse_e3_insight_draft_output(text: str, warnings: list | None = None) -> dict:
    sections = _split_headed_sections(text, prefix="### ")
    rows, located = _parse_table(text, sections, "洞察初稿", E3_DRAFT_HEADER, [E3_DRAFT_HEADER])
    _warn_unparsed("E3洞察初稿", text, located, warnings)
    result = {}
    for r in rows:
        pname, event = r.get("平台"), r.get("事件簇名")
        if not pname or not event:
            continue
        signal = r.get("信号类型", "")
        result[(pname, event)] = {
            "信号类型": signal if signal in E3_SIGNAL_TYPES else "无信号",
            "判断依据": r.get("判断依据", ""),
        }
    return result


def _render_e3_word_table(pname: str, word_rows: list, word_tags: dict) -> str:
    """过滤主体不明词 + 可借势加成后重新打分,渲染该平台「平台热点词 TOP N」。
    漏打标的词保守按保留处理;可借势漏打按"否"(只少一次加成,不丢数据)。"""
    kept = []
    for r in word_rows:
        tag = word_tags.get(r["热点驱动词"], {})
        if tag.get("归类", E3_KEEP_TAG) == E3_FILTER_TAG:
            continue
        kept.append({**r, "可借势": tag.get("可借势") == "是"})
    wdf = _score_word_candidates(kept)
    if len(wdf) < 2:
        return f"> {pname} 上周平台热点词候选(过滤主体不明词后)<2 个,不输出此子版块。"
    top3 = wdf.head(3)
    lines = [f"### {pname} · 平台热点词 TOP {len(top3)}", "",
             "| 平台热点词 | 热度值 | 具体热搜 |", "|---|---|---|"]
    for _, r in top3.iterrows():
        lines.append(
            f"| {r['热点驱动词']} | {fmt_score(r['综合热度指数'], suffix='')} | {r['具体热搜']} |"
        )
    return "\n".join(lines)


def _compose_e3_platform_blocks(raw_e3_data: str, candidates: dict | None,
                                word_tags: dict | None, draft: dict | None) -> dict:
    """按平台组装撰写输入:{平台: body}。draft 插在开头,热点词表追加在末尾。"""
    blocks = {}
    for pname, body in _split_headed_sections(raw_e3_data, prefix="## ").items():
        parts = []
        if draft:
            rows = [
                f"- {event}：{info['信号类型']}（{info['判断依据']}）"
                for (p, event), info in draft.items() if p == pname
            ]
            if rows:
                parts.append("### 洞察初稿参考（内部判断依据，不是展示内容）\n\n" + "\n".join(rows))
        parts.append(_strip_trailing_rule(body))
        if candidates is not None and word_tags is not None and candidates.get(pname):
            parts.append(_render_e3_word_table(pname, candidates[pname], word_tags))
        blocks[pname] = "\n\n".join(parts)
    return blocks


# 平台小节标题:`### 微博`、`### **抖音**`、`### 微博热点`、`### B站 · 平台热点词 TOP 3`。
# 平台名后必须是结尾/分隔符/固定后缀,避免把「#### 微博之夜…」这类事件小标题误当成平台切换。
_E3_PLATFORM_HEADING_RE = re.compile(
    r"^\s*#{2,3}\s*\**\s*(微博|抖音|B站|知乎)\s*(?:热点|新鲜事|平台)?\s*\**\s*(?:$|[·:：（(|\-—])")


def _isolate_e3_platform(pname: str, body: str, warnings: list | None = None) -> str:
    """单平台撰写结果只保留本平台内容(W44:微博段里写出「上周抖音/B站/知乎无营销可用热点」,
    拼接后三个平台标题各出现两次)。

    - 以其他平台名开头的标题(`### 抖音`、`### 抖音热点`、`## **B站**`…)起、到下一个本平台标题前的内容丢弃;
    - 删除「上周{其他平台}无营销可用热点」占位行(模型可能不写标题直接写这句);本平台的这句保留;
    - 找不到本平台标题时原样返回并告警:宁可留给 report_self_check 判 fail 定点重跑,也不丢正文。"""
    lines = body.split("\n")
    if not any((m := _E3_PLATFORM_HEADING_RE.match(l)) and m.group(1) == pname for l in lines):
        msg = f"E3·{pname} 撰写结果未找到「### {pname}」标题,未做跨平台过滤"
        print(f"[pipeline_e] ⚠️ {msg}", file=sys.stderr)
        if warnings is not None:
            warnings.append(msg)
        return body
    others = [p for p in EXPECTED_PLATFORMS if p != pname]
    placeholder = re.compile(rf"^\s*上周\s*(?:{'|'.join(map(re.escape, others))})\s*无营销可用热点")
    kept, owner, dropped = [], None, 0
    for line in lines:
        m = _E3_PLATFORM_HEADING_RE.match(line)
        if m:
            owner = m.group(1)
        if (owner not in (None, pname)) or placeholder.match(line):
            dropped += bool(line.strip())
            continue
        kept.append(line)
    if dropped:
        msg = f"E3·{pname} 撰写结果混入其他平台内容,已剔除 {dropped} 行"
        print(f"[pipeline_e] ⚠️ {msg}", file=sys.stderr)
        if warnings is not None:
            warnings.append(msg)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))


async def build_e3_task(llm_call, sec: dict, insight_dir: Path, model: str = "") -> dict:
    sid = sec["id"]
    t0 = time.time()
    data_path = insight_dir / sec["data_file"]
    if not data_path.exists():
        return await _error_result(sec, f"数据文件不存在：{data_path}")
    raw_data = data_path.read_text(encoding="utf-8")
    try:
        template = load_prompt_template(sec["prompt_dir"])
    except FileNotFoundError as error:
        return await _error_result(sec, str(error))
    usage = _Usage()

    def _load_json(name: str) -> dict | None:
        path = insight_dir / name
        if not path.exists():
            print(f"[{sid}] ⚠️ {path.name} 不存在(旧版 run_stats 产出?),跳过对应子步骤",
                  file=sys.stderr)
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    candidates = _load_json(E3_WORD_CANDIDATES_FILE)
    fulltext = _load_json(E3_EVENT_FULLTEXT_FILE)
    tagging_input = _build_e3_tagging_input(candidates) if candidates else None
    draft_input = _build_e3_insight_draft_input(fulltext) if fulltext else None

    async def _skip():
        return None

    tag_res, draft_res = await asyncio.gather(
        _run_sub_step(llm_call, "e3_tagging", "E3热点词打标", E3_TAGGING_PROMPT_DIR,
                      tagging_input, E3_TAGGING_MAX_TOKENS) if tagging_input else _skip(),
        _run_sub_step(llm_call, "e3_insight_draft", "E3洞察初稿", E3_INSIGHT_DRAFT_PROMPT_DIR,
                      draft_input, E3_INSIGHT_DRAFT_MAX_TOKENS) if draft_input else _skip(),
    )
    usage.add(tag_res)
    usage.add(draft_res)

    parse_warnings: list = []
    word_tags = None
    if tagging_input and _sub_ok(tag_res, sid, "热点词打标", "本轮不展示「平台热点词」"):
        word_tags = _parse_e3_tagging_output(tag_res["content"], parse_warnings)
        _atomic_text(tag_res["content"], insight_dir / E3_TAGGING_AUDIT_FILE)
        print(f"[{sid}] 打标完成({len(word_tags)} 个候选词)")
    draft = None
    if draft_input and _sub_ok(draft_res, sid, "洞察初稿", "撰写阶段自行判断信号"):
        draft = _parse_e3_insight_draft_output(draft_res["content"], parse_warnings)
        _atomic_text(draft_res["content"], insight_dir / E3_INSIGHT_DRAFT_AUDIT_FILE)
        print(f"[{sid}] 洞察初稿完成({len(draft)} 个事件)")

    blocks = _compose_e3_platform_blocks(raw_data, candidates, word_tags, draft)
    ordered = [p for p in EXPECTED_PLATFORMS if p in blocks]
    if not ordered:
        return {**await _error_result(sec, "e3_data.md 未切分出任何平台块(## 微博/抖音/B站/知乎)"),
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}

    async def _write(pname: str) -> dict:
        psec = {"id": f"{sid}:{pname}", "name": f"{sec['name']}·{pname}",
                "max_tokens": E3_PLATFORM_WRITE_MAX_TOKENS}
        return await llm_call(psec, fill_prompt(template, f"## {pname}\n\n{blocks[pname]}"))

    results = await asyncio.gather(*(_write(p) for p in ordered))
    for r in results:
        usage.add(r)
    failed = [r for r in results if r.get("status") != "ok" or not r.get("content", "").strip()]
    if failed:
        errs = "; ".join(f"{r['id']}:{r.get('error') or '空输出'}" for r in failed)
        return {**await _error_result(sec, f"E3 分平台撰写失败：{errs}"),
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}

    parts = []
    for pname, r in zip(ordered, results):
        body = _strip_leading_heading(_strip_markdown_fence(r["content"]), sec["section_title"])
        parts.append(_isolate_e3_platform(pname, body, parse_warnings).strip())
    content = "\n\n".join(parts)
    truncated = any(r.get("stop_reason") == "length" for r in results)
    return _section_result(sec, content, usage, t0, model,
                           "length" if truncated else "stop", split_platforms=ordered,
                           **({"parse_warnings": parse_warnings} if parse_warnings else {}))


# ---------------------------------------------------------------------------
# E4 专属路由(对齐 CC V2.0):打标 ∥ 草稿枚举 → 跨板块仲裁 → A/B 两组并发撰写
# ---------------------------------------------------------------------------
# 打标:舆情风险(风险类型+乙方可接性)/合作动态(变动类型+参考价值)过滤;失败保守保留全部。
# 草稿枚举:营销观察/消费洞察客观枚举角度(≤10 条);失败则两部分本轮为空(不回退全量,
#     避免绕开价值前置过滤)。
# 仲裁:跨板块同一事件只留一处 + 同池内部重复合并链接;失败则不移除/不合并。
# 分组/组间/组内排序由脚本确定(不让模型在长表格里心算),A 组=合作动态+舆情风险(表格),
# B 组=营销观察+消费洞察(卡片)并发撰写;每组过格式+链接溯源校验,不过则该组重写 1 次。

E4_CANDIDATES_FILE = "e4_candidates.json"
E4_TAGGING_AUDIT_FILE = "e4_tagging_audit.md"
E4_CURATION_AUDIT_FILE = "e4_curation_audit.md"
E4_ARBITRATION_AUDIT_FILE = "e4_arbitration_audit.md"
E4_TAGGING_PROMPT_DIR = PROMPTS_DIR / "E4_营销发现" / "_tagging"
E4_CURATION_PROMPT_DIR = PROMPTS_DIR / "E4_营销发现" / "_curation"
E4_ARBITRATION_PROMPT_DIR = PROMPTS_DIR / "E4_营销发现" / "_arbitration"
E4_WRITING_A_PROMPT_DIR = "E4_营销发现/_writing_coop_risk"
E4_WRITING_B_PROMPT_DIR = "E4_营销发现/_writing_marketing_consumer"
E4_TAGGING_MAX_TOKENS = 4000
E4_CURATION_MAX_TOKENS = 4000
E4_ARBITRATION_MAX_TOKENS = 4000
E4_WRITING_MAX_TOKENS = 10000
E4_DRAFT_LIMIT = 10
E4_GROUP_REWRITE_RETRIES = 1

# 合作动态组间固定优先级(按合作对象商业稀缺度),打标兜底类型排正式类型后、合作争议前
_COOP_TYPE_PRIORITY = {"品牌联名/资源合作": 0, "品牌×IP合作": 1, "品牌×明星代言合作": 2, "合作争议": 100}
_COOP_FALLBACK_PRIORITY = 50
# 舆情风险全部写完整「风险影响」(终稿不留"—",精简与否由 HC3 人工决定)
_RISK_FULL_ANALYSIS_LIMIT = 10_000


def _score_of(item: dict) -> float:
    try:
        return float(str(item.get("热度分", "0")).replace("分", "").strip())
    except ValueError:
        return 0.0


def _render_risk_candidate_links(item: dict) -> str:
    lines = []
    for t in item.get("具体热搜", []):
        title = md_link_title(t["标题"])
        link = t.get("链接", "")
        text = f"[{title}]({link})" if (link and link != "—") else title
        lines.append(f"- {text} · {t.get('平台', '—')} · {t.get('热度分', '—')}")
    return "<br>".join(lines)


def _render_coop_candidate_link(item: dict) -> str:
    title = md_link_title(item.get("代表热搜标题", ""))
    link = item.get("代表热搜链接", "")
    return f"[{title}]({link})" if (link and link != "—") else title


def _build_e4_tagging_input(candidates: dict) -> str | None:
    parts = []
    risk = candidates.get("舆情风险", [])
    if risk:
        rows = [[r["事件簇名"], r["行业"], r["热度分"], r["风险判断说明"]] for r in risk]
        parts.append("### 舆情风险候选（待打标）\n\n"
                     + _render_pseudo_rows(["事件簇名", "行业", "热度分", "风险判断说明"], rows))
    coop = candidates.get("合作动态", [])
    if coop:
        rows = [[c["事件簇名"], c["商业实体"], c["行业"], c["热度分"], c["合作动态说明"]] for c in coop]
        parts.append("### 合作动态候选（待打标）\n\n"
                     + _render_pseudo_rows(["事件簇名", "商业实体", "行业", "热度分", "合作动态说明"], rows))
    return "\n\n".join(parts) if parts else None


E4_RISK_TAG_HEADER = ["事件簇名", "风险类型", "可接性", "理由"]
E4_COOP_TAG_HEADER = ["事件簇名", "变动类型", "参考价值", "理由"]
E4_MARKETING_DRAFT_HEADER = ["事件簇名", "现象概括"]
E4_CONSUMER_DRAFT_HEADER = ["候选组合", "现象概括"]
E4_CROSS_ARB_HEADER = ["涉及候选", "最终归属", "理由"]
E4_SAME_ARB_HEADER = ["涉及候选", "保留候选", "理由"]


def _parse_e4_tagging_output(text: str, warnings: list | None = None) -> tuple:
    sections = _split_headed_sections(text, prefix="### ")
    known = [E4_RISK_TAG_HEADER, E4_COOP_TAG_HEADER]
    risk_rows, risk_loc = _parse_table(text, sections, "舆情风险打标", E4_RISK_TAG_HEADER, known)
    coop_rows, coop_loc = _parse_table(text, sections, "合作动态打标", E4_COOP_TAG_HEADER, known)
    _warn_unparsed("E4候选打标", text, risk_loc or coop_loc, warnings)
    risk_tags = {r["事件簇名"]: r for r in risk_rows if r.get("事件簇名")}
    coop_tags = {r["事件簇名"]: r for r in coop_rows if r.get("事件簇名")}
    return risk_tags, coop_tags


def _filter_e4_tagged(candidates: dict, risk_tags: dict | None, coop_tags: dict | None) -> tuple:
    """按打标过滤,返回 (risk_kept, coop_kept),元素为 [类型, 候选]。tags=None 表示打标不可用,
    保守保留全部;单条漏打标同样保守保留。"""
    def _keep(pool, tags, type_key, verdict_key, ok_value):
        kept = []
        for item in candidates.get(pool, []):
            if tags is None:
                kept.append(["未打标（调用失败，保守保留）", item])
                continue
            tag = tags.get(item["事件簇名"])
            if tag is None:
                kept.append(["未分类（打标遗漏，保守保留）", item])
            elif tag.get(verdict_key, ok_value) == ok_value:
                kept.append([tag.get(type_key) or "未分类", item])
        return kept

    return (_keep("舆情风险", risk_tags, "风险类型", "可接性", "接"),
            _keep("合作动态", coop_tags, "变动类型", "参考价值", "有用"))


def _build_e4_curation_input(candidates: dict) -> str | None:
    parts = []
    creative = candidates.get("营销观察", [])
    if creative:
        rows = [[c["事件簇名"], c["行业"], c["热度分"], c["营销发现说明"]] for c in creative]
        parts.append("### 营销观察候选（待枚举）\n\n"
                     + _render_pseudo_rows(["事件簇名", "行业", "热度分", "营销发现说明"], rows))
    cons = candidates.get("消费洞察", [])
    if cons:
        rows = [[c["事件簇名"], c["行业"], c["热度分"], c["消费者行为说明"]] for c in cons]
        parts.append("### 消费洞察候选（待枚举）\n\n"
                     + _render_pseudo_rows(["事件簇名", "行业", "热度分", "消费者行为说明"], rows))
    return "\n\n".join(parts) if parts else None


def _parse_e4_curation_output(text: str, warnings: list | None = None) -> tuple:
    sections = _split_headed_sections(text, prefix="### ")
    known = [E4_MARKETING_DRAFT_HEADER, E4_CONSUMER_DRAFT_HEADER]
    mk_rows, mk_loc = _parse_table(text, sections, "营销观察草稿", E4_MARKETING_DRAFT_HEADER, known)
    cs_rows, cs_loc = _parse_table(text, sections, "消费洞察草稿", E4_CONSUMER_DRAFT_HEADER, known)
    _warn_unparsed("E4草稿枚举", text, mk_loc or cs_loc, warnings)
    marketing = [
        {"事件簇名": r["事件簇名"], "现象概括": r.get("现象概括", "")}
        for r in mk_rows if r.get("事件簇名")
    ]
    consumer = []
    for r in cs_rows:
        names = [n.strip() for n in r.get("候选组合", "").split("+") if n.strip()]
        if names:
            consumer.append({"候选组合": names, "现象概括": r.get("现象概括", "")})
    return marketing, consumer


def _build_e4_arbitration_input(risk_kept: list, coop_kept: list, marketing: list) -> str | None:
    parts = []
    if coop_kept:
        rows = [[it["事件簇名"], typ, it.get("合作动态说明", "")] for typ, it in coop_kept]
        parts.append("### 合作动态候选（已打标，待仲裁）\n\n"
                     + _render_pseudo_rows(["事件簇名", "变动类型", "合作动态说明"], rows))
    if risk_kept:
        rows = [[it["事件簇名"], typ, it.get("风险判断说明", "")] for typ, it in risk_kept]
        parts.append("### 舆情风险候选（已打标，待仲裁）\n\n"
                     + _render_pseudo_rows(["事件簇名", "风险类型", "风险判断说明"], rows))
    if marketing:
        rows = [[d["事件簇名"], d.get("现象概括", "")] for d in marketing]
        parts.append("### 营销观察候选（已枚举，待仲裁）\n\n"
                     + _render_pseudo_rows(["事件簇名", "草稿概括角度"], rows))
    return "\n\n".join(parts) if parts else None


def _split_pool_refs(raw: str) -> list:
    refs = []
    for part in raw.split(" + "):
        part = part.strip()
        if ":" in part:
            pool, name = part.split(":", 1)
            refs.append((pool.strip(), name.strip()))
    return refs


def _parse_e4_arbitration_output(text: str, warnings: list | None = None) -> tuple:
    """返回 (跨板块重复[{涉及候选, 最终归属}], 同池重复[{pool, names, keep}])。"""
    sections = _split_headed_sections(text, prefix="### ")
    known = [E4_CROSS_ARB_HEADER, E4_SAME_ARB_HEADER]
    cross_rows, cross_loc = _parse_table(text, sections, "跨板块重复事件", E4_CROSS_ARB_HEADER, known)
    same_rows, same_loc = _parse_table(text, sections, "同池内部重复事件", E4_SAME_ARB_HEADER, known)
    _warn_unparsed("E4跨板块仲裁", text, cross_loc or same_loc, warnings)
    cross = []
    for r in cross_rows:
        refs = _split_pool_refs(r.get("涉及候选", ""))
        if refs and r.get("最终归属"):
            cross.append({"涉及候选": refs, "最终归属": r["最终归属"].strip()})
    same = []
    for r in same_rows:
        refs = _split_pool_refs(r.get("涉及候选", ""))
        pools = {p for p, _ in refs}
        keep = r.get("保留候选", "").strip()
        if keep and len(pools) == 1 and refs:  # 只接受同一池子内部的分组
            same.append({"pool": next(iter(pools)), "names": [n for _, n in refs], "keep": keep})
    return cross, same


def _merge_links_into(rep: dict, other: dict, kind: str) -> None:
    """把 other 的热搜链接并入 rep(原地)。合作动态单链接字段挪进 _merged_links 列表。"""
    if kind == "risk":
        rep.setdefault("具体热搜", [])
        seen = {t.get("链接") for t in rep["具体热搜"]}
        for t in other.get("具体热搜", []):
            if t.get("链接") not in seen:
                rep["具体热搜"].append(t)
                seen.add(t.get("链接"))
        rep["具体热搜"].sort(key=_score_of, reverse=True)
        return
    merged = rep.setdefault("_merged_links", [{
        "标题": rep.get("代表热搜标题", ""), "链接": rep.get("代表热搜链接", ""),
        "热度分": rep.get("热度分", ""),
    }])
    if other.get("代表热搜链接", "") not in {m.get("链接") for m in merged}:
        merged.append({"标题": other.get("代表热搜标题", ""), "链接": other.get("代表热搜链接", ""),
                       "热度分": other.get("热度分", "")})


def _merge_same_pool(kept: list, groups: list, kind: str) -> tuple:
    """按同池重复分组合并:保留 keep(不在 kept 里则取组内热度最高),其余并入链接后删除。"""
    by_name = {it["事件簇名"]: it for _, it in kept}
    removed: set = set()
    for g in groups:
        present = [n for n in g["names"] if n in by_name and n not in removed]
        if len(present) < 2:
            continue
        rep = g["keep"] if g["keep"] in present else max(present, key=lambda n: _score_of(by_name[n]))
        for n in present:
            if n != rep:
                _merge_links_into(by_name[rep], by_name[n], kind)
                removed.add(n)
    return [[t, it] for t, it in kept if it["事件簇名"] not in removed], len(removed)


def _apply_e4_arbitration(risk_kept: list, coop_kept: list, marketing: list,
                          cross: list, same: list) -> tuple:
    removed = {"合作动态": set(), "舆情风险": set(), "营销观察": set()}
    for ruling in cross:
        for pool, name in ruling["涉及候选"]:
            if pool != ruling["最终归属"] and pool in removed:
                removed[pool].add(name)
    coop_kept = [[t, c] for t, c in coop_kept if c["事件簇名"] not in removed["合作动态"]]
    risk_kept = [[t, c] for t, c in risk_kept if c["事件簇名"] not in removed["舆情风险"]]
    marketing = [d for d in marketing if d["事件簇名"] not in removed["营销观察"]]
    coop_kept, n_coop = _merge_same_pool(coop_kept, [g for g in same if g["pool"] == "合作动态"], "coop")
    risk_kept, n_risk = _merge_same_pool(risk_kept, [g for g in same if g["pool"] == "舆情风险"], "risk")
    return risk_kept, coop_kept, marketing, sum(len(v) for v in removed.values()), n_coop + n_risk


def _top_hot_links(item: dict, limit: int = 3) -> dict:
    links = item.get("具体热搜", [])
    if len(links) <= limit:
        return item
    return {**item, "具体热搜": sorted(links, key=_score_of, reverse=True)[:limit]}


def _group_and_order_risk(kept: list) -> list:
    """舆情风险:组内按具体热搜条数(合并体量)、热度分降序;组间按组大小、组内最高热度降序。"""
    groups: dict = {}
    for risk_type, item in kept:
        groups.setdefault(risk_type, []).append(item)
    for items in groups.values():
        items.sort(key=lambda it: (-len(it.get("具体热搜", [])), -_score_of(it)))
    ordered = sorted(groups, key=lambda t: (-len(groups[t]), -max(_score_of(i) for i in groups[t])))
    return [(t, it, idx < _RISK_FULL_ANALYSIS_LIMIT)
            for t in ordered for idx, it in enumerate(groups[t])]


def _render_e4_risk_section(kept: list) -> str:
    if not kept:
        return ("## 舆情风险数据\n\n上周无风险预警热点，或候选均判定为乙方公关不接主案"
                "（财务/法律/政治/安全类硬事实问题，普通品牌公关改变不了，不适合出现在"
                "本报告的「品牌公关能怎么应对」框架里）。")
    lines = [
        f"## 舆情风险数据\n\n共 {len(kept)} 个事件簇（已按乙方公关可接性打标过滤+跨板块归属"
        "仲裁+同池内部去重，原候选池TOP20；**以下数据已按分组排序规则预先分好组、排好序**，"
        "组间/组内顺序不需要再自行判断，照抄即可）\n",
        "| 风险类型 | 事件簇名 | 行业 | 热度分 | 具体热搜 | 风险判断说明 | 展开分析 |",
        "|---|---|---|---|---|---|---|",
    ]
    for risk_type, item, expand in _group_and_order_risk(kept):
        lines.append(
            f"| {_escape_md_cell(risk_type)} | {_escape_md_cell(item['事件簇名'])} "
            f"| {_escape_md_cell(_render_multi_value(item['行业']))} | {item['热度分']} "
            f"| {_render_risk_candidate_links(_top_hot_links(item))} "
            f"| {_escape_md_cell(item['风险判断说明'])} | {'是' if expand else '否'} |"
        )
    return "\n".join(lines)


def _render_e4_coop_section(kept: list) -> str:
    if not kept:
        return "## 合作动态数据\n\n上周无对营销人有参考价值的商业合作动态。"
    ordered = sorted(kept, key=lambda p: (_COOP_TYPE_PRIORITY.get(p[0], _COOP_FALLBACK_PRIORITY),
                                          -_score_of(p[1])))
    lines = [
        f"## 合作动态数据\n\n共 {len(kept)} 条（已按对营销人参考价值打标过滤+跨板块归属"
        "仲裁+同池内部去重，原候选池TOP20；**以下数据已按分组排序规则预先分好组、排好序**，"
        "顺序不需要再自行判断，照抄即可）\n",
        "| 变动类型 | 事件簇名 | 商业实体 | 行业 | 热度分 | 代表热搜 | 合作动态说明 |",
        "|---|---|---|---|---|---|---|",
    ]
    for coop_type, item in ordered:
        if item.get("_merged_links"):
            link_cell = "<br>".join(
                f"- {_render_coop_candidate_link({'代表热搜标题': m.get('标题', ''), '代表热搜链接': m.get('链接', '')})}"
                f" · {m.get('热度分', '—')}"
                for m in item["_merged_links"]
            )
        else:
            link_cell = _render_coop_candidate_link(item)
        lines.append(
            f"| {_escape_md_cell(coop_type)} | {_escape_md_cell(item['事件簇名'])} "
            f"| {_escape_md_cell(_render_multi_value(item['商业实体']))} "
            f"| {_escape_md_cell(_render_multi_value(item['行业']))} "
            f"| {item['热度分']} | {link_cell} | {_escape_md_cell(item['合作动态说明'])} |"
        )
    return "\n".join(lines)


def _render_e4_marketing_drafts(candidates: dict, drafts: list) -> str:
    by_name = {c["事件簇名"]: c for c in candidates.get("营销观察", [])}
    rows = [(by_name[d["事件簇名"]], d["现象概括"])
            for d in drafts[:E4_DRAFT_LIMIT] if d["事件簇名"] in by_name]
    if not rows:
        return "## 营销观察数据\n\n上周未枚举出符合条件的营销观察草稿角度。"
    lines = [
        f"## 营销观察数据\n\n共 {len(rows)} 条草稿角度（Step B客观枚举，最终数量由"
        "撰写阶段按「营销观察筛选视角」挑选，可能进一步收窄）\n",
        "| 事件簇名 | 平台 | 行业 | 热度分 | 代表热搜 | 营销发现说明 | 草稿概括角度 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c, gist in rows:
        lines.append(
            f"| {_escape_md_cell(c['事件簇名'])} | {_escape_md_cell(c['平台'])} "
            f"| {_escape_md_cell(_render_multi_value(c['行业']))} | {c['热度分']} "
            f"| {_render_coop_candidate_link(c)} | {_escape_md_cell(c['营销发现说明'])} "
            f"| {_escape_md_cell(gist)} |"
        )
    return "\n".join(lines)


def _render_e4_consumer_drafts(candidates: dict, drafts: list) -> str:
    by_name = {c["事件簇名"]: c for c in candidates.get("消费洞察", [])}
    rows = []
    for d in drafts[:E4_DRAFT_LIMIT]:
        members = [by_name[n] for n in d["候选组合"] if n in by_name]
        if members:
            rows.append((d["候选组合"], members, d["现象概括"]))
    if not rows:
        return "## 消费洞察数据\n\n上周未枚举出符合条件的消费洞察草稿角度。"
    lines = [
        f"## 消费洞察数据\n\n共 {len(rows)} 条草稿组合（Step B客观枚举，单候选或同"
        "品类组合，最终数量由撰写阶段按「跨热点归纳规则」挑选，可能进一步收窄）\n",
        "| 候选组合 | 候选明细 | 草稿概括角度 |",
        "|---|---|---|",
    ]
    for names, members, gist in rows:
        detail = "<br>".join(
            f"- {_render_coop_candidate_link(m)} · {m.get('平台', '—')} · {m['热度分']}："
            f"{_escape_md_cell(m.get('消费者行为说明', ''))}"
            for m in members
        )
        lines.append(f"| {_escape_md_cell('+'.join(names))} | {detail} | {_escape_md_cell(gist)} |")
    return "\n".join(lines)


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^\s)]+)\)")
_E4_CARD_HEADING_RE = re.compile(r"^#### (案例|洞察) \d+：", re.MULTILINE)
_E4_CASE_CARD_FIELDS = ["- 原始热点：", "- 营销发现：", "- 链接：", "[配图：待补充]"]
_E4_INSIGHT_CARD_FIELDS = ["- 群体：", "- 触发事件：", "- 行为反应：", "- 营销发现："]
_E4_EMPTY_STATE_MAX_CHARS = 120


def _validate_e4_b_cards(text: str) -> list:
    """B 组卡片格式:至少 1 张 #### 案例/洞察 卡且字段齐全;无卡片时只允许短空状态说明。"""
    matches = list(_E4_CARD_HEADING_RE.finditer(text))
    if not matches:
        if len(text.strip()) > _E4_EMPTY_STATE_MAX_CHARS:
            return [f"未找到「#### 案例 N：」/「#### 洞察 N：」卡片,且正文 {len(text.strip())} 字"
                    "超出空状态说明长度,疑似未按卡片格式输出"]
        return []
    issues = []
    for i, m in enumerate(matches):
        block = text[m.start(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        required = _E4_CASE_CARD_FIELDS if m.group(1) == "案例" else _E4_INSIGHT_CARD_FIELDS
        missing = [f for f in required if f not in block]
        if missing:
            issues.append(f"卡片「{block.splitlines()[0].strip()}」缺失字段：{missing}")
    return issues


def _validate_e4_grounding(result_text: str, input_data: str) -> list:
    """链接溯源:撰写结果引用的 URL 必须全部出自本组输入数据(拦截脱离候选池编造)。"""
    allowed = {url for _, url in _MD_LINK_RE.findall(input_data)}
    used = {url for _, url in _MD_LINK_RE.findall(result_text)}
    stray = sorted(used - allowed)
    if not stray:
        return []
    return [f"引用了 {len(stray)} 个不在本组输入数据里的链接(疑似编造)：{stray[:5]}"]


def validate_e4_group(group: str, result_text: str, input_data: str) -> list:
    """A 组查表格列数(对齐 V2 validate-writing 的 _check_table_integrity),B 组查卡片字段;
    两组都查链接溯源。在组内校验,失败只重写该组,不拖累另一组。"""
    if group == "a":
        issues = [i for i in validate_section_content("e4:A", result_text) if "表格行单元格数" in i]
    else:
        issues = _validate_e4_b_cards(result_text)
    issues += _validate_e4_grounding(result_text, input_data)
    return issues


async def build_e4_task(llm_call, sec: dict, insight_dir: Path, model: str = "") -> dict:
    sid = sec["id"]
    t0 = time.time()
    candidates_path = insight_dir / E4_CANDIDATES_FILE
    if not candidates_path.exists():
        return await _error_result(sec, f"候选文件不存在：{candidates_path}(请重跑 run_stats)")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    try:
        template_a = load_prompt_template(E4_WRITING_A_PROMPT_DIR)
        template_b = load_prompt_template(E4_WRITING_B_PROMPT_DIR)
    except FileNotFoundError as error:
        return await _error_result(sec, str(error))
    usage = _Usage()

    async def _skip():
        return None

    # ---- Step 1:打标 ∥ 草稿枚举(数据源独立)----
    tagging_input = _build_e4_tagging_input(candidates)
    curation_input = _build_e4_curation_input(candidates)
    tag_res, cur_res = await asyncio.gather(
        _run_sub_step(llm_call, "e4_tagging", "E4候选打标", E4_TAGGING_PROMPT_DIR,
                      tagging_input, E4_TAGGING_MAX_TOKENS) if tagging_input else _skip(),
        _run_sub_step(llm_call, "e4_curation", "E4草稿枚举", E4_CURATION_PROMPT_DIR,
                      curation_input, E4_CURATION_MAX_TOKENS) if curation_input else _skip(),
    )
    usage.add(tag_res)
    usage.add(cur_res)

    parse_warnings: list = []
    risk_tags = coop_tags = None
    if tagging_input and _sub_ok(tag_res, sid, "打标", "保守保留全部舆情风险/合作动态候选"):
        risk_tags, coop_tags = _parse_e4_tagging_output(tag_res["content"], parse_warnings)
        _atomic_text(tag_res["content"], insight_dir / E4_TAGGING_AUDIT_FILE)
    risk_kept, coop_kept = _filter_e4_tagged(candidates, risk_tags, coop_tags)

    marketing, consumer = [], []
    if curation_input and _sub_ok(cur_res, sid, "草稿枚举", "营销观察/消费洞察本轮为空"):
        marketing, consumer = _parse_e4_curation_output(cur_res["content"], parse_warnings)
        _atomic_text(cur_res["content"], insight_dir / E4_CURATION_AUDIT_FILE)
    print(f"[{sid}] 打标/枚举完成:舆情风险{len(risk_kept)} 合作动态{len(coop_kept)} "
          f"营销观察{len(marketing)} 消费洞察{len(consumer)}")

    # ---- Step 2:跨板块归属仲裁 + 同池去重 ----
    arb_input = _build_e4_arbitration_input(risk_kept, coop_kept, marketing)
    if arb_input:
        arb_res = usage.add(await _run_sub_step(
            llm_call, "e4_arbitration", "E4跨板块仲裁", E4_ARBITRATION_PROMPT_DIR,
            arb_input, E4_ARBITRATION_MAX_TOKENS))
        if _sub_ok(arb_res, sid, "仲裁", "三板块候选不做移除/合并(人工核查时留意重复)"):
            cross, same = _parse_e4_arbitration_output(arb_res["content"], parse_warnings)
            risk_kept, coop_kept, marketing, n_removed, n_merged = _apply_e4_arbitration(
                risk_kept, coop_kept, marketing, cross, same)
            _atomic_text(arb_res["content"], insight_dir / E4_ARBITRATION_AUDIT_FILE)
            print(f"[{sid}] 仲裁完成:跨板块移除 {n_removed} 条,同池合并 {n_merged} 条")

    # ---- Step 3:A/B 两组并发撰写 + 每组校验 ----
    data_a = "\n\n---\n\n".join([_render_e4_coop_section(coop_kept), _render_e4_risk_section(risk_kept)])
    data_b = "\n\n---\n\n".join([_render_e4_marketing_drafts(candidates, marketing),
                                 _render_e4_consumer_drafts(candidates, consumer)])

    async def _write_group(group: str, template: str, data: str) -> dict:
        gsec = {"id": f"{sid}:{group.upper()}", "name": f"{sec['name']}·{group.upper()}组",
                "max_tokens": E4_WRITING_MAX_TOKENS}
        prompt = fill_prompt(template, data)
        result: dict = {}
        for attempt in range(E4_GROUP_REWRITE_RETRIES + 1):
            result = usage.add(await llm_call(gsec, prompt)) or {}
            if result.get("status") != "ok" or not result.get("content", "").strip():
                return {**result, "status": "error", "error": result.get("error") or "空输出"}
            body = _strip_markdown_fence(result["content"])
            issues = validate_e4_group(group, body, data)
            if not issues:
                return {**result, "content": body}
            print(f"[{gsec['id']}] ⚠️ 校验未通过({attempt + 1}/{E4_GROUP_REWRITE_RETRIES + 1}):{issues}",
                  file=sys.stderr)
            result = {**result, "status": "error", "error": "; ".join(issues), "validation_issues": issues}
        return result

    res_a, res_b = await asyncio.gather(_write_group("a", template_a, data_a),
                                        _write_group("b", template_b, data_b))
    failed = [r for r in (res_a, res_b) if r.get("status") != "ok"]
    if failed:
        errs = "; ".join(f"{r.get('id', sid)}:{r.get('error')}" for r in failed)
        return {**await _error_result(sec, f"E4 分组撰写失败：{errs}"),
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}

    body_a = _strip_leading_heading(res_a["content"], sec["section_title"]).strip()
    body_b = _strip_leading_heading(res_b["content"], sec["section_title"]).strip()
    truncated = any(r.get("stop_reason") == "length" for r in (res_a, res_b))
    return _section_result(sec, f"{body_a}\n\n{body_b}", usage, t0, model,
                           "length" if truncated else "stop", split_writing=True,
                           **({"parse_warnings": parse_warnings} if parse_warnings else {}))


def _atomic_text(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_json(value: dict, path: Path) -> None:
    _atomic_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        path,
    )


E3_TOP_HEADING_RE = re.compile(
    r"^(\s*(?:#{1,6}\s*)?\**\s*(?:🔥|✨)?\s*(热门事件|平台热点词)\s*TOP\s*)(\{?N\}?|\d+)(\s*\**\s*)$"
)
E3_BOUNDARY_RE = re.compile(r"^\s*(#{1,6}\s|\**\s*(?:🔥|✨)?\s*(?:热门事件|平台热点词)\s*TOP)")


def _fix_e3_top_headings(body: str) -> str:
    """E3「热门事件/平台热点词 TOP N」的 N 以其后表格的实际数据行数为准(脚本确定,不信 LLM)。

    表格为空(e3_platforms.py 判定无≥2条的事件簇 / 热点词候选<2)时删除该标题行,
    只保留说明句——避免出现 W43 那种 \"TOP 5\" 下面没有任何卡片的情况。"""
    lines = body.split("\n")
    out = []
    for i, line in enumerate(lines):
        m = E3_TOP_HEADING_RE.match(line)
        if not m:
            out.append(line)
            continue
        rows = 0
        seen_sep = False
        for nxt in lines[i + 1:]:
            if E3_BOUNDARY_RE.match(nxt):
                break
            s = nxt.strip()
            if not s.startswith("|"):
                if seen_sep:
                    break
                continue
            if re.match(r"^\|[\s:|-]+\|$", s):
                seen_sep = True
            elif seen_sep:
                rows += 1
        if rows == 0:
            continue
        out.append(f"{m.group(1)}{rows}{m.group(4)}")
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def _write_section_output(sec: dict, result: dict, output_path: Path) -> None:
    body = _strip_markdown_fence(result["content"])
    if sec["id"] != "e2":
        body = _strip_leading_heading(body, sec["section_title"])
    if sec["id"] == "e3":
        body = _fix_e3_top_headings(body)
    _atomic_text(f"{sec['section_title']}\n\n{body}\n", output_path)


def _pipeline_signature(
    insight_dir: Path,
    *,
    model: str,
    mode: str,
    data_run_id: int | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "checkpoint_version": CHECKPOINT_VERSION,
                "model": model,
                "mode": mode,
                "data_run_id": data_run_id,
            },
            sort_keys=True,
        ).encode()
    )
    input_paths = [SHARED_ROLE_STYLE]
    input_paths.extend(
        insight_dir / sec["data_file"]
        for sec in SECTIONS
    )
    input_paths.extend(
        insight_dir / name
        for name in (E4_CANDIDATES_FILE, E3_WORD_CANDIDATES_FILE, E3_EVENT_FULLTEXT_FILE)
    )
    # 版块主 Prompt 与 E3/E4 子步骤 Prompt(_tagging/_curation/_arbitration/_writing_*)
    for sec in SECTIONS:
        input_paths.extend(
            sorted((PROMPTS_DIR / sec["prompt_dir"]).rglob("v*.md"))
        )
    for path in input_paths:
        digest.update(str(path).encode())
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_checkpoint(
    path: Path,
    *,
    signature: str,
    model: str,
    mode: str,
    version_tag: str,
) -> dict:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        checkpoint = {}
    valid = (
        checkpoint.get("checkpoint_version") == CHECKPOINT_VERSION
        and checkpoint.get("run_signature") == signature
        and checkpoint.get("model") == model
        and checkpoint.get("mode") == mode
        and checkpoint.get("version") == version_tag
        and isinstance(checkpoint.get("sections"), dict)
    )
    if not valid:
        checkpoint = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "run_signature": signature,
            "model": model,
            "mode": mode,
            "version": version_tag,
            "sections": {},
        }
        _atomic_json(checkpoint, path)
    return checkpoint


async def run_all_sections(
    project_dir: Path,
    insight_dir: Path,
    model: str,
    sections: list,
    *,
    checkpoint: dict,
    checkpoint_path: Path,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reuse_completed: bool = True,
) -> list:
    # 模型统一走 bmc-llm-relay(blueai 模型网关);不回退 ARK_*,避免静默改走方舟计费
    api_key = os.environ.get("LLM_RELAY_API_KEY")
    if not api_key:
        raise EnvironmentError("LLM_RELAY_API_KEY 未设置(bmc-llm-relay 模型网关 Key)")

    base_url = os.environ.get("LLM_RELAY_BASE_URL")
    if not base_url:
        raise EnvironmentError("LLM_RELAY_BASE_URL 未设置(如 https://bmc-llm-relay.bluemediagroup.cn/v1)")

    semaphore = asyncio.Semaphore(max_workers)

    async def llm_call(sec: dict, prompt: str) -> dict:
        async with semaphore:
            return await call_llm_async(
                api_key,
                base_url,
                sec,
                prompt,
                model,
                max_attempts=max_attempts,
            )

    async def build_section(sec: dict) -> dict:
        if sec["id"] == "e2":
            return await build_e2_task(llm_call, sec, insight_dir)
        if sec["id"] == "e3":
            return await build_e3_task(llm_call, sec, insight_dir, model)
        if sec["id"] == "e4":
            return await build_e4_task(llm_call, sec, insight_dir, model)
        try:
            template = load_prompt_template(sec["prompt_dir"])
        except FileNotFoundError as error:
            print(f"[{sec['id']}] ❌ {error}", file=sys.stderr)
            return await _error_result(sec, str(error))

        data_path = insight_dir / sec["data_file"]
        if not data_path.exists():
            error = f"数据文件不存在：{data_path}"
            print(f"[{sec['id']}] ❌ {error}", file=sys.stderr)
            return await _error_result(sec, error)

        data = data_path.read_text(encoding="utf-8")
        try:
            prompt = fill_prompt(template, data)
        except ValueError as error:
            print(f"[{sec['id']}] ❌ {error}", file=sys.stderr)
            return await _error_result(sec, str(error))
        return await llm_call(sec, prompt)

    async def run_section(sec: dict) -> dict:
        cached = checkpoint["sections"].get(sec["id"])
        output_path = insight_dir / sec["output_file"]
        if (
            reuse_completed
            and isinstance(cached, dict)
            and cached.get("status") == "ok"
            and cached.get("content")
            and output_path.exists()
        ):
            print(f"[{sec['id']}] checkpoint 命中，跳过 LLM")
            return {**cached, "cached": True}

        if not reuse_completed and cached is not None:
            checkpoint["sections"].pop(sec["id"], None)
            _atomic_json(checkpoint, checkpoint_path)

        spent_in = spent_out = 0
        result: dict = {}
        for revalidate in range(SECTION_REVALIDATE_RETRIES + 1):
            try:
                result = await build_section(sec)
            except Exception as error:  # 单版块异常不拖垮其余版块
                print(f"[{sec['id']}] ❌ 版块内部异常：{error!r}", file=sys.stderr)
                result = await _error_result(sec, f"版块内部异常：{error!r}")
            if result["status"] != "ok":
                break
            issues = (
                [] if result.get("fixed_text")
                else validate_section_content(
                    sec["id"], result.get("content", ""), result.get("stop_reason")
                )
            )
            if not issues:
                break
            if revalidate < SECTION_REVALIDATE_RETRIES:
                # 被丢弃的这次也是真实花费,计入本版块 token
                spent_in += result.get("input_tokens", 0)
                spent_out += result.get("output_tokens", 0)
                print(
                    f"[{sec['id']}] ⚠️ 完整性校验未通过,重跑该版块"
                    f"({revalidate + 1}/{SECTION_REVALIDATE_RETRIES}):{issues}",
                    file=sys.stderr,
                )
                continue
            result = {
                **result,
                "status": "error",
                "error": f"完整性校验未通过(重跑{SECTION_REVALIDATE_RETRIES}次):{'; '.join(issues)}",
                "validation_issues": issues,
            }
        result["input_tokens"] = result.get("input_tokens", 0) + spent_in
        result["output_tokens"] = result.get("output_tokens", 0) + spent_out

        if result["status"] == "ok" and result.get("content"):
            _write_section_output(sec, result, output_path)
            checkpoint["sections"][sec["id"]] = result
            _atomic_json(checkpoint, checkpoint_path)
            print(f"[{sec['id']}] checkpoint 已写入")
        return result

    return list(
        await asyncio.gather(
            *(asyncio.create_task(run_section(sec)) for sec in sections)
        )
    )


async def _error_result(sec: dict, err: str) -> dict:
    return {
        "id": sec["id"],
        "name": sec["name"],
        "status": "error",
        "error": err,
        "content": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_s": 0.0,
        "model": "",
        "stop_reason": None,
    }


# ---------------------------------------------------------------------------
# 成本记录
# ---------------------------------------------------------------------------

# 每百万 token 单价(USD估算,实际计价以 bmc-llm-relay 账单为准,这里仅供 cost_tracker 台账估算)。
# doubao-seed-evolving 方舟原价 6/30 RMB per M token,按 7 汇率换算成 USD。
PRICING: dict[str, tuple[float, float]] = {
    "doubao-seed-evolving":         (0.857, 4.286),
    "DeepSeek-V4-Pro":              (2.0, 8.0),
    "DeepSeek-V4-Flash":            (0.5, 2.0),
    "DeepSeek-V4.1-Flash":          (0.5, 2.0),
    "claude-opus-4-5":              (5.0, 25.0),
    "claude-opus-4-7":              (5.0, 25.0),
    "claude-opus-4-8":              (5.0, 25.0),
    "claude-opus-5":                (5.0, 25.0),
    "claude-sonnet-4-5-20250929":   (3.0, 15.0),
    "claude-sonnet-4-6":            (3.0, 15.0),
    "claude-sonnet-5":              (2.0, 10.0),
    "claude-haiku-4-5-20251001":    (1.0, 5.0),
}


def _price_of(model: str) -> tuple[float, float]:
    # 模型 ID 大小写不统一（环境变量 Doubao-Seed-Evolving vs 表内 doubao-seed-evolving），按小写匹配
    name = model.lower()
    for key, value in PRICING.items():
        if key.lower() in name:
            return value
    print(f"[pipeline_e] ⚠️ 未知模型定价：{model},成本记为 0", file=sys.stderr)
    return (0.0, 0.0)


def record_costs(project_dir: Path, results: list, model: str, mode: str) -> None:
    if not COST_TRACKER.exists():
        print(f"[pipeline_e] ⚠️ cost_tracker.py 不存在({COST_TRACKER}),跳过成本记录")
        return

    for r in results:
        if (
            r["status"] != "ok"
            or r["input_tokens"] == 0
            or r.get("cached")
        ):
            continue
        price_in, price_out = _price_of(r["model"])
        raw_cost = (
            r["input_tokens"] / 1_000_000 * price_in
            + r["output_tokens"] / 1_000_000 * price_out
        )
        cmd = [
            sys.executable, str(COST_TRACKER),
            "--project-dir", str(project_dir),
            "append",
            "--phase", "E",
            "--task", f"{r['id']}_{r['name']}",
            "--mode", mode,
            "--model-id", r["model"],
            "--platform", "bmc-llm-relay",
            "--input-tokens", str(r["input_tokens"]),
            "--output-tokens", str(r["output_tokens"]),
            "--raw-cost", str(raw_cost),
            "--currency", "USD",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            print(f"  [cost] {r['id']} 成本已记录")
        except subprocess.CalledProcessError as e:
            print(f"  [cost] {r['id']} 成本记录失败：{e.stderr.decode()}", file=sys.stderr)


def _strip_leading_heading(text: str, section_title: str) -> str:
    lines = text.lstrip("\n").split("\n")
    if not lines:
        return text
    m = re.match(r"^#{1,3}\s*(.+?)\s*$", lines[0])
    if not m:
        return text
    first_text = m.group(1)
    title_text = re.sub(r"^#+\s*", "", section_title)
    title_text = re.sub(r"^[一二三四五六七八九十]+[、.]\s*", "", title_text).strip()
    if title_text and title_text in first_text:
        lines.pop(0)
        while lines and lines[0].strip() == "":
            lines.pop(0)
    return "\n".join(lines)


def _strip_markdown_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1 and s.rstrip().endswith("```"):
            inner = s[first_nl + 1: s.rfind("```")]
            return inner.strip()
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MIN_SECTION_BYTES = 100  # 对齐原版 prompt/06 Step 2:四份文件 size > 100 bytes
_ANNOTATION_SCRIPTS = SKILL_DIR.parent / "topic6-annotation" / "scripts"


def _load_run_config(proj: Path) -> dict:
    path = proj / "run_config.yaml"
    if not path.exists():
        return {}
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def check_hc2_gate(proj: Path, mode: str) -> str | None:
    """原版 prompt/06:66 前置条件。demo 设计上跳过 HC2,豁免;无 run_config 时不拦(本地调试)。"""
    if mode == "demo":
        return None
    cfg = _load_run_config(proj)
    if not cfg:
        return None
    if (cfg.get("status") or {}).get("hc2_passed") is not True:
        return "status.hc2_passed 不为 true:HC2 未通过前禁止进入 Phase E"
    return None


def too_small_sections(round_dir: Path, sections: list) -> list[str]:
    small = []
    for sec in sections:
        path = round_dir / sec["output_file"]
        if not path.exists() or path.stat().st_size <= MIN_SECTION_BYTES:
            small.append(sec["id"])
    return small


def _write_e_status(proj: Path, patch: dict, phase: str | None = None) -> None:
    sys.path.insert(0, str(_ANNOTATION_SCRIPTS))
    try:
        from run_config_state import update_run_config
    except ImportError as error:  # pragma: no cover
        print(f"[pipeline_e] WARN 无法写回 run_config: {error}", file=sys.stderr)
        return
    update_run_config(proj, patch, current_phase=phase)


def main():
    parser = argparse.ArgumentParser(
        description="E 阶段洞察生成 pipeline(skip_sampling 正式全量 / demo 抽样演示)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-dir", required=True,
                        help="项目目录(绝对路径,或相对 /workspace)")
    parser.add_argument(
        "--mode",
        choices=["demo", "full", "skip_sampling"],
        default="skip_sampling",
    )
    parser.add_argument("--publish-date", default=None)
    parser.add_argument("--model", default="DeepSeek-V4.1-Flash",
                        help="bmc-llm-relay 模型 ID(大小写需与网关 /v1/models 完全一致)")
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument("--skip-data-prep", action="store_true")
    parser.add_argument("--data-run-id", type=int, default=None)
    parser.add_argument("--sections", default=None)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    args = parser.parse_args()

    if args.max_workers < 1 or args.max_attempts < 1:
        parser.error("--max-workers 和 --max-attempts 必须为正整数")

    if args.mode == "full":
        print(
            "[pipeline_e] ❌ 不允许直接基于 full 模式的 500 条校准宽表生成洞察。\n"
            "  请先完成全量阶段并使用 --mode skip_sampling；如需 50 条演示报告,请使用 --mode demo。",
            file=sys.stderr,
        )
        sys.exit(1)

    proj = Path(args.project_dir)
    if not proj.is_absolute():
        proj = WORKSPACE_ROOT / proj
    if not proj.exists():
        print(f"[pipeline_e] ❌ 项目目录不存在：{proj}", file=sys.stderr)
        sys.exit(1)

    gate_error = check_hc2_gate(proj, args.mode)
    if gate_error:
        print(f"[pipeline_e] ❌ {gate_error}", file=sys.stderr)
        sys.exit(1)

    merge_dir = proj / "05_合并"
    data_run_id = args.data_run_id
    if data_run_id is None:
        data_run_id = latest_data_run_id(merge_dir, args.mode)

    if data_run_id is not None:
        wide_full = merge_dir / f"wide_table_{args.mode}_r{data_run_id}.xlsx"
        print(f"[pipeline_e] 标注数据 run_id = r{data_run_id}")
    else:
        wide_full = merge_dir / f"wide_table_{args.mode}.xlsx"
        print(f"[pipeline_e] 未找到版本化宽表,回退旧命名")

    if not wide_full.exists() and not args.skip_data_prep:
        print(
            f"[pipeline_e] ❌ {args.mode} 宽表不存在：{wide_full}\n"
            f"  请先运行 merge_annotations.py --mode {args.mode} --run-id {{N}}。",
            file=sys.stderr,
        )
        sys.exit(1)

    insight_dir = proj / "06_洞察"
    insight_dir.mkdir(parents=True, exist_ok=True)

    prev_round_dir = latest_round_dir(insight_dir)
    version = resolve_round_version(insight_dir, args.version, args.sections)
    version_tag = f"v{version}"
    round_dir = insight_dir / version_tag
    round_dir.mkdir(parents=True, exist_ok=True)
    print(f"[pipeline_e] 洞察版本：{version_tag}(目录：{round_dir.relative_to(proj)})")

    if (round_dir / f"e1_{version_tag}.md").exists():
        print(
            f"[pipeline_e] {version_tag} 已存在，将按 checkpoint "
            "复用已完成版块。"
        )

    sections = [
        dict(sec, output_file=f"{sec['id']}_{version_tag}.md")
        for sec in SECTIONS
    ]

    if args.sections:
        wanted = {s.strip() for s in args.sections.split(",") if s.strip()}
        known_ids = {sec["id"] for sec in SECTIONS}
        unknown = wanted - known_ids
        if unknown:
            print(f"[pipeline_e] ❌ 未知版块ID：{unknown}(可选：{sorted(known_ids)})", file=sys.stderr)
            sys.exit(1)
        sections = [sec for sec in sections if sec["id"] in wanted]
        print(f"[pipeline_e] --sections 限定：本轮只跑 {[s['id'] for s in sections]}")

    if args.skip_data_prep:
        if prev_round_dir is None:
            print(
                "[pipeline_e] ❌ --skip-data-prep 但没有任何历史轮次可复制数据文件,"
                "请去掉该参数让本轮正常跑一次统计脚本。",
                file=sys.stderr,
            )
            sys.exit(1)
        if prev_round_dir.resolve() == round_dir.resolve():
            print(
                f"[pipeline_e] --skip-data-prep：直接复用 {version_tag} "
                "已有数据文件。"
            )
        else:
            print(
                f"[pipeline_e] --skip-data-prep：从 "
                f"{prev_round_dir.name} 复制数据文件..."
            )
            for sec in SECTIONS:
                src = prev_round_dir / sec["data_file"]
                if src.exists():
                    shutil.copy(src, round_dir / sec["data_file"])
                else:
                    print(
                        f"[pipeline_e] ⚠️ {prev_round_dir.name} 下缺 "
                        f"{sec['data_file']},跳过复制",
                        file=sys.stderr,
                    )
            audit_src = prev_round_dir / "e3_word_freq_audit.md"
            if audit_src.exists():
                shutil.copy(audit_src, round_dir / "e3_word_freq_audit.md")
            candidates_src = prev_round_dir / E4_CANDIDATES_FILE
            if candidates_src.exists():
                shutil.copy(candidates_src, round_dir / E4_CANDIDATES_FILE)
    else:
        print("[pipeline_e] Step 1：运行 01_统计/run_stats.py 预处理数据...")
        cmd = [
            sys.executable, str(E_DATA_SCRIPT),
            "--project-dir", str(proj),
            "--mode", args.mode,
            "--out-dir", str(round_dir),
        ]
        if args.publish_date:
            cmd += ["--publish-date", args.publish_date]
        if data_run_id is not None:
            cmd += ["--run-id", str(data_run_id)]

        ret = subprocess.run(cmd)
        if ret.returncode != 0:
            print("[pipeline_e] ❌ run_stats.py 失败,终止", file=sys.stderr)
            sys.exit(1)

    signature = _pipeline_signature(
        round_dir,
        model=args.model,
        mode=args.mode,
        data_run_id=data_run_id,
    )
    checkpoint_path = round_dir / f"pipeline_e_checkpoint_{version_tag}.json"
    checkpoint = _load_checkpoint(
        checkpoint_path,
        signature=signature,
        model=args.model,
        mode=args.mode,
        version_tag=version_tag,
    )

    print(
        f"\n[pipeline_e] Step 2：流式调用 4 路 LLM"
        f"(模型：{args.model}，最大并发：{args.max_workers})..."
    )
    t_start = time.time()

    results = asyncio.run(
        run_all_sections(
            proj,
            round_dir,
            args.model,
            sections,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            max_workers=args.max_workers,
            max_attempts=args.max_attempts,
            reuse_completed=not bool(args.sections),
        )
    )

    total_elapsed = time.time() - t_start
    total_input = sum(r["input_tokens"] for r in results)
    total_output = sum(r["output_tokens"] for r in results)

    print(f"\n[pipeline_e] Step 3：保存洞察文件({version_tag})...")
    small = set(too_small_sections(round_dir, sections))
    for sec, result in zip(sections, results):
        if result["status"] == "ok" and sec["id"] in small and not result.get("fixed_text"):
            # 输出过短视为失败,并清掉 checkpoint 让原参数重跑时重新生成该版块
            result["status"] = "error"
            result["error"] = f"输出文件 <= {MIN_SECTION_BYTES} bytes"
            checkpoint["sections"].pop(sec["id"], None)
    if small:
        _atomic_json(checkpoint, checkpoint_path)
    ok_count = 0
    for sec, result in zip(sections, results):
        if result["status"] == "ok" and result["content"]:
            out_path = round_dir / sec["output_file"]
            print(f"  ✅ {sec['id']}：{out_path.name}")
            ok_count += 1
        else:
            print(f"  ❌ {sec['id']}：{result.get('error', '空输出')}")

    report = {
        "pipeline": "pipeline_e",
        "version": version_tag,
        "project_dir": _rel_to_proj(proj, proj),
        "mode": args.mode,
        "data_run_id": data_run_id,
        "model": args.model,
        "total_elapsed_s": round(total_elapsed, 1),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "ok_count": ok_count,
        "total_count": len(sections),
        "sections": results,
    }
    report_path = round_dir / f"pipeline_e_report_{version_tag}.json"
    _atomic_json(report, report_path)
    print(f"\n[pipeline_e] 执行报告：{report_path.relative_to(proj)}")

    print("[pipeline_e] Step 5：记录成本...")
    record_costs(proj, results, args.model, args.mode)

    truncated = [r["id"] for r in results if r.get("stop_reason") == "length"]

    print(f"\n{'='*60}")
    print(f"[pipeline_e] {'✅ 全部完成' if ok_count == len(sections) else '⚠️ 部分失败'}")
    print(f"  版本：{version_tag}")
    print(f"  成功：{ok_count}/{len(sections)} 版块")
    print(f"  总耗时：{total_elapsed:.1f}s")
    print(f"  Token 用量：input={total_input:,} / output={total_output:,}")
    print(f"  洞察文件：{round_dir}")
    if truncated:
        print(f"  ⚠️ 升档至上限后仍被截断(finish_reason=length):{truncated}"
              f"——需精简该版块输入或拆分输出")

    all_ok = ok_count == len(sections)
    # --sections 只重跑部分版块时，整体状态仍按四个版块文件判断
    all_sections = [dict(sec, output_file=f"{sec['id']}_{version_tag}.md") for sec in SECTIONS]
    missing_overall = too_small_sections(round_dir, all_sections)
    _write_e_status(proj, {"e_insights": {
        "status": "done" if all_ok and not missing_overall else "failed",
        "round_version": version,
        "mode": args.mode,
        "data_run_id": data_run_id,
        "failed_sections": sorted(
            {r["id"] for r in results if r["status"] != "ok"} | set(missing_overall)),
        **{f"{sec['id']}_file": str(round_dir / sec["output_file"]) for sec in all_sections},
        "report_file": str(report_path),
    }}, phase="e")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
