#!/usr/bin/env python3
"""
Phase F Step 0 · 合并四版块洞察为完整报告(MA 版)

读取 {project_dir}/06_洞察/v{N}/e1_v{N}.md ~ e4_v{N}.md,拼接成一份完整周报。
只做 Step 0(合并),不含自校验/飞书发布步骤。

MA 适配:删掉 PROJECT_ROOT.parents[1] 上溯,--project-dir 支持绝对/相对(相对以 /workspace 为根)。

用法:
    python pipeline_f.py --project-dir "/workspace/Projects/W35_20260824-20260830" \\
        [--version N] [--period-label 2026-W35] [--date-start 2026-08-24] [--date-end 2026-08-30]

输出:{project_dir}/07_报告/热点报告_{period_label}_v{N}.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path("/workspace")

SECTION_IDS = ["e1", "e2", "e3", "e4"]
DEMO_DISCLAIMER = "> 演示说明：本报告基于 50 条分层样本生成，仅供流程演示，不可作为正式全量结论。"
SMOKE_DISCLAIMER = "> 冒烟测试说明：本报告基于 {rows} 条分层样本生成（mode=smoke），仅供流程验证，不可作为正式全量结论。"
# 页尾说明卡固定文案,不交给 LLM 生成。build-report.mjs 按「热度值说明」前缀匹配引用块渲染
# footer-note,缺失时用同文案兜底(HEAT_NOTE_DEFAULT,两处逐字一致,有测试锁定)
HEAT_NOTE = (
    "> 热度值说明：报告展示热度值为经过标准化后的热度分值（跨平台、跨期均可比），非平台原始热度值。"
    "平台新鲜事中热度值为热点数量及热度值综合评分。"
)


def load_run_config(proj: Path) -> dict:
    cfg_path = proj / "run_config.yaml"
    if not cfg_path.exists():
        return {}
    import yaml

    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def smoke_disclaimer(cfg: dict) -> str | None:
    """smoke 的脚本侧 --mode 是 skip_sampling,真实模式只在 run_config 顶层 mode。"""
    if str(cfg.get("mode", "")).strip().lower() != "smoke":
        return None
    rows = ((cfg.get("status") or {}).get("sample") or {}).get("sample_rows") or 30
    return SMOKE_DISCLAIMER.format(rows=rows)


def latest_insight_round(insight_dir: Path) -> int | None:
    versions = []
    for d in insight_dir.glob("v*"):
        if d.is_dir():
            m = re.match(r"v(\d+)$", d.name)
            if m:
                versions.append(int(m.group(1)))
    return max(versions) if versions else None


def resolve_period_info(proj: Path, args: argparse.Namespace) -> tuple[str, str, str]:
    """解析 period_label / date_start / date_end。优先级:
    1. CLI 显式传参
    2. run_config.yaml 的 period 块
    3. 项目目录名解析(W{N}_{YYYYMMDD}-{YYYYMMDD})
    三者都取不到时报错退出(不编造)。"""
    period_label, date_start, date_end = args.period_label, args.date_start, args.date_end
    if period_label and date_start and date_end:
        return period_label, date_start, date_end

    cfg_path = proj / "run_config.yaml"
    if cfg_path.exists():
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        period = cfg.get("period", {})
        period_label = period_label or period.get("period_label")
        date_start = date_start or period.get("date_start")
        date_end = date_end or period.get("date_end")
        if period_label and date_start and date_end:
            return period_label, date_start, date_end

    m = re.match(r"(W\d+)[^\d]*(\d{8})-(\d{8})", proj.name)
    if m:
        week_id, start_raw, end_raw = m.groups()
        date_start = date_start or f"{start_raw[:4]}-{start_raw[4:6]}-{start_raw[6:]}"
        date_end = date_end or f"{end_raw[:4]}-{end_raw[4:6]}-{end_raw[6:]}"
        period_label = period_label or f"{date_start[:4]}-{week_id}"
        return period_label, date_start, date_end

    print(
        f"[pipeline_f] 无法解析周期信息:{proj.name} 不匹配 W{{N}}_{{YYYYMMDD}}-{{YYYYMMDD}},"
        "且未提供 run_config.yaml 或 --period-label/--date-start/--date-end",
        file=sys.stderr,
    )
    sys.exit(1)


CANONICAL_PERIOD_RE = re.compile(r"^\d{4}-W\d{2}$")


def canonical_period_label(period_label: str, date_start: str) -> str:
    """报告展示用周期标签统一为 `YYYY-Www`(ISO 周,取起始日)。

    period_label 由 Agent 手写进 run_config,曾出现 `W43 (2026-10-19~2026-10-25)` 这类格式,
    导致页头只剩 `W43`、数据周期日期重复。已规范时原样返回;否则由 date_start 确定性推导。
    只用于报告正文展示,输出文件名仍沿用原 period_label(Agent 后续阶段按它拼路径)。"""
    label = (period_label or "").strip()
    if CANONICAL_PERIOD_RE.match(label):
        return label
    from datetime import date

    iso_year, iso_week, _ = date.fromisoformat(date_start).isocalendar()
    canonical = f"{iso_year}-W{iso_week:02d}"
    print(
        f"[pipeline_f] WARN period_label={period_label!r} 不符合 YYYY-Www,报告展示改用 {canonical}",
        file=sys.stderr,
    )
    return canonical


def format_period_display(period_label: str, date_start: str, date_end: str) -> str:
    """避免 period_label 已含日期时再次拼接同一日期范围。"""
    date_range = f"{date_start} ~ {date_end}"
    if date_range in period_label:
        return period_label
    return f"{period_label}（{date_range}）"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="合并四版块洞察为完整报告(Phase F Step 0)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-dir", required=True, help="项目目录(绝对路径,或相对 /workspace)")
    parser.add_argument(
        "--mode",
        choices=["demo", "skip_sampling"],
        default="skip_sampling",
    )
    parser.add_argument("--version", type=int, default=None, help="要合并的洞察轮次;不传时自动取06_洞察/下最新一轮")
    parser.add_argument("--period-label", default=None, help="覆盖周期标签,如 2026-W35")
    parser.add_argument("--date-start", default=None, help="覆盖起始日期,如 2026-08-24")
    parser.add_argument("--date-end", default=None, help="覆盖结束日期,如 2026-08-30")
    args = parser.parse_args()

    proj = Path(args.project_dir)
    if not proj.is_absolute():
        proj = WORKSPACE_ROOT / proj
    if not proj.exists():
        print(f"[pipeline_f] 项目目录不存在:{proj}", file=sys.stderr)
        return 1

    insight_dir = proj / "06_洞察"
    version = args.version if args.version is not None else latest_insight_round(insight_dir)
    if version is None:
        print(f"[pipeline_f] {insight_dir} 下没有任何 v{{N}}/ 子目录,先跑 pipeline_e.py", file=sys.stderr)
        return 1

    round_dir = insight_dir / f"v{version}"
    section_files = {sid: round_dir / f"{sid}_v{version}.md" for sid in SECTION_IDS}
    missing = [str(p) for p in section_files.values() if not p.exists() or p.stat().st_size == 0]
    if missing:
        print(f"[pipeline_f] 以下文件缺失或为空:{missing}", file=sys.stderr)
        return 1

    print(f"[pipeline_f] 合并轮次:v{version}({round_dir})")

    period_label, date_start, date_end = resolve_period_info(proj, args)
    display_label = canonical_period_label(period_label, date_start)
    period_display = format_period_display(display_label, date_start, date_end)
    print(f"[pipeline_f] 周期:{period_display}")

    sections = [section_files[sid].read_text(encoding="utf-8").strip() for sid in SECTION_IDS]
    header_lines = [
        f"# 社媒热点周刊 · {display_label}",
        f"> 数据范围：微博、知乎、抖音、B站\n> 数据周期：{period_display}",
    ]
    if args.mode == "demo":
        header_lines.append(DEMO_DISCLAIMER)
    smoke_note = smoke_disclaimer(load_run_config(proj))
    if smoke_note:
        header_lines.append(smoke_note)

    report = "\n\n".join([
        *header_lines,
        "\n\n---\n\n".join(sections),
        "---",
        HEAT_NOTE,
    ])

    out_dir = proj / "07_报告"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"热点报告_{period_label}_v{version}.md"
    out_path.write_text(report + "\n", encoding="utf-8")

    print(f"[pipeline_f] 已写入:{out_path}")
    print(f"[pipeline_f] 完成,共 {len(report)} 字符")
    _write_f_status(proj, {"f": {
        "merged_report_file": str(out_path.relative_to(proj)),
        "round_version": version,
        "period_label": display_label,
    }})
    return 0


def _write_f_status(proj: Path, patch: dict) -> None:
    """Phase G 读 status.f.merged_report_file;原先无人写入,由 Agent 临场拼路径。"""
    sys.path.insert(0, str(SCRIPT_DIR.parents[1] / "topic6-annotation" / "scripts"))
    try:
        from run_config_state import update_run_config
    except ImportError as error:  # pragma: no cover
        print(f"[pipeline_f] WARN 无法写回 run_config: {error}", file=sys.stderr)
        return
    update_run_config(proj, patch, current_phase="f")


if __name__ == "__main__":
    sys.exit(main())
