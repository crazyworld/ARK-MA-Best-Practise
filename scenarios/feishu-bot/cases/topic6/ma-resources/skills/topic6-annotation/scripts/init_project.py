#!/usr/bin/env python3
"""init_project.py — 启动阶段确定性脚本:解析周期 → 推导取数窗口 → 建/续项目目录 + run_config

对齐原版 prompt/00_角色与触发.md「日期解析」+ Projects/_项目模板/run_config.yaml,
把原本由 Agent 临场推断的部分全部脚本化(Agent 只负责把用户原话传进来)。

周期解析(--text 为用户原始消息):当前只支持单周(weekly)
  无日期            → 最近一个完整自然周(周一~周日,今天所在周之前)
  W35 / 第35周      → ISO 周 W35 的起止(年份取 --today 所在 ISO 年)
  继续W36          → 查 /workspace/Projects/W36*/run_config.yaml 断点续跑
双周 / 日期区间(09-01到09-14)暂不支持,返回 ambiguous(Phase A 只按单个 ISO 周取数)。
新建周期必须落在最近 4 个完整自然周内(RECENT_WEEKS),更早的返回 ambiguous;
续跑已有项目不受此限。
命中不支持的写法、周次不存在、超出近 4 周、尚未结束或续跑目标不唯一时返回
status=ambiguous,附 A/B/C 选项,Coordinator 必须按 R15 输出 [need_decision] 让用户裁决,不得自行猜。

取数窗口:fetch_start = date_start − 7 天,fetch_end = date_end + 1 天
(fetch_hot_topics.py 需要余量才能识别完整 ISO 周)。

stdout:单行 JSON
  {"status":"created|resumed|ambiguous", "project_dir":..., "period":{...}, "fetch":{...},
   "current_phase":..., "options":[...]}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

WORKSPACE_PROJECTS = Path(os.environ.get("MA_WORKSPACE", "/workspace")) / "Projects"
MODES = ("demo", "smoke", "full", "skip_sampling")
FETCH_LEAD_DAYS = 7
FETCH_TAIL_DAYS = 1
# 新建周期只接受最近 N 个完整自然周:起始日 ≥ 最近完整周周一 − (N−1)×7 天。
# 只约束新建,不约束「继续W..」/--resume-dir 续跑已有项目。
RECENT_WEEKS = 4

_WEEK_RE = re.compile(r"(?:(\d{4})\s*[-年]?\s*)?(?:W|w|第)\s*(\d{1,2})\s*周?")
_RANGE_RE = re.compile(
    r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?\s*(?:到|至|~|～|-|—)\s*"
    r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?"
)
_RESUME_RE = re.compile(r"继续\s*(?:W|w|第)?\s*(\d{1,2})")
# 双周 / 日期区间只用于识别并拒绝(暂不支持),不参与周期计算
_BIWEEKLY_RE = re.compile(r"双周|连续两周|两周")
DIR_SUFFIX = {"weekly": "热点周报"}


class Ambiguous(Exception):
    """options: [(展示文案, 重调追加参数 | None)];None=停止或需用户补充信息,不得重调。

    用户裁决后 Coordinator 用「原 --text/--mode + resolutions[字母]」重新调用,
    不往 --text 里拼选项文字(脚本不解析选项文字,拼了会原样再次 ambiguous)。
    """

    def __init__(self, reason: str, options: list[tuple[str, list[str] | None]]):
        super().__init__(reason)
        self.reason = reason
        self.options = [label for label, _ in options]
        self.resolutions = {label.split(")")[0].strip(): args for label, args in options}


def iso_week_start(year: int, week: int) -> date:
    return date.fromisocalendar(year, week, 1)


def last_complete_week(today: date) -> date:
    """今天所在周之前的最近一个完整周的周一。"""
    return today - timedelta(days=today.weekday() + 7)


def earliest_allowed_start(today: date) -> date:
    """最近 RECENT_WEEKS 个完整周里最早那周的周一;新建周期的起始日不得早于它。"""
    return last_complete_week(today) - timedelta(days=7 * (RECENT_WEEKS - 1))


def parse_period(text: str, today: date, *, prefer: str | None = None,
                 allow_incomplete: bool = False) -> dict:
    """只解析单周。prefer=latest:用户在 ambiguous 后选「最近完整周」;allow_incomplete:接受未结束周。"""
    text = text or ""
    if prefer == "latest":
        start = last_complete_week(today)
    else:
        range_m = _RANGE_RE.search(text)
        # 日期区间里的 "09-01" 不应被当成周号:先把区间文本挖掉再找周号
        rest = text.replace(range_m.group(0), " ") if range_m else text
        unsupported = range_m.group(0) if range_m else None
        if unsupported is None:
            biweekly_m = _BIWEEKLY_RE.search(text)
            unsupported = biweekly_m.group(0) if biweekly_m else None
        if unsupported:
            raise Ambiguous(
                f"暂不支持「{unsupported}」,当前只支持单周(如 W45 或不写周期取最近完整周)",
                [("A) 改为最近一个完整周", ["--prefer", "latest"]),
                 ("B) 重新给出周次", None),
                 ("C) 停止", None)],
            )
        week_m = _WEEK_RE.search(rest)
        if week_m:
            year = int(week_m.group(1) or today.isocalendar()[0])
            week = int(week_m.group(2))
            try:
                start = iso_week_start(year, week)
            except ValueError as error:
                raise Ambiguous(f"周次不存在: {year}-W{week}",
                                [("A) 重新给出周次", None),
                                 ("B) 改为最近一个完整周", ["--prefer", "latest"]),
                                 ("C) 停止", None)]) from error
        else:
            start = last_complete_week(today)
    end = start + timedelta(days=6)

    earliest = earliest_allowed_start(today)
    if start < earliest:
        raise Ambiguous(
            f"周期 {start}~{end} 超出可选范围:仅支持最近 {RECENT_WEEKS} 个完整周"
            f"({earliest} 起)",
            [("A) 改为最近一个完整周", ["--prefer", "latest"]),
             (f"B) 重新给出 {earliest} 之后的周次", None),
             ("C) 停止", None)],
        )
    if end >= today and not allow_incomplete:
        raise Ambiguous(
            f"周期 {start}~{end} 尚未结束(今天 {today}),数据不完整",
            [("A) 改为最近一个完整周", ["--prefer", "latest"]),
             ("B) 仍按该周期取已有数据", ["--allow-incomplete"]),
             ("C) 重新给出周次", None)],
        )
    iso_year, iso_week, _ = start.isocalendar()
    return {
        "period_id": f"W{iso_week}",
        "period_label": f"{iso_year}-W{iso_week:02d}",
        "date_start": start.isoformat(),
        "date_end": end.isoformat(),
        "period_type": "weekly",
    }


def project_dir_name(period: dict) -> str:
    start = period["date_start"].replace("-", "")
    end = period["date_end"].replace("-", "")
    return f"{period['period_id']}{DIR_SUFFIX[period['period_type']]}_{start}-{end}"


def fetch_window(period: dict) -> dict:
    start = date.fromisoformat(period["date_start"]) - timedelta(days=FETCH_LEAD_DAYS)
    end = date.fromisoformat(period["date_end"]) + timedelta(days=FETCH_TAIL_DAYS)
    return {"fetch_start": start.isoformat(), "fetch_end": end.isoformat()}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resume(projects: Path, week: int) -> Path:
    hits = sorted(p for p in projects.glob(f"W{week}*")
                  if re.match(rf"W{week}(?!\d)", p.name) and (p / "run_config.yaml").exists())
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Ambiguous(f"未找到可续跑的 W{week} 项目(/workspace/Projects/W{week}*/run_config.yaml)",
                        [(f"A) 新建 W{week} 项目从头跑", ["--new-week", str(week)]),
                         ("B) 停止", None), ("C) 指定其他周次", None)])
    raise Ambiguous(f"W{week} 有多个项目: {[p.name for p in hits]}",
                    [(f"{chr(65 + i)}) 续跑 {p.name}", ["--resume-dir", p.name])
                     for i, p in enumerate(hits[:3])])


def _resumed(project: Path, mode: str, fallback_period: dict | None = None) -> dict:
    cfg = _load(project / "run_config.yaml")
    period = cfg.get("period") or fallback_period or {}
    return {
        "status": "resumed",
        "project_dir": str(project),
        "mode": cfg.get("mode", mode),
        "period": period,
        "fetch": cfg.get("fetch") or fetch_window(period),
        "current_phase": (cfg.get("status") or {}).get("current_phase", "init"),
    }


def build_run_config(period: dict, mode: str) -> dict:
    """对齐原版 _项目模板/run_config.yaml 的顶层块;status 由各脚本原子写入。"""
    return {
        "mode": mode,
        "period": period,
        "fetch": fetch_window(period),
        "cost": {"usd_to_cny": 7.2},
        "status": {
            "created_at": datetime.now().astimezone().isoformat(),
            "current_phase": "init",
        },
    }


def run(text: str, mode: str, today: date, projects: Path, *,
        prefer: str | None = None, allow_incomplete: bool = False, overwrite: bool = False,
        resume_dir: str | None = None, new_week: int | None = None) -> dict:
    if resume_dir:
        project = projects / Path(resume_dir).name
        if not (project / "run_config.yaml").exists():
            raise Ambiguous(f"续跑目标不存在: {project}", [("A) 停止", None)])
        return _resumed(project, mode)

    resume_m = None if new_week else _RESUME_RE.search(text or "")
    if resume_m:
        return _resumed(_resume(projects, int(resume_m.group(1))), mode)

    # 「继续W36」找不到项目后用户选新建:按周次文本解析(_WEEK_RE 会命中 W36)
    period = parse_period(f"W{new_week}" if new_week else text, today,
                          prefer=prefer, allow_incomplete=allow_incomplete)
    project = projects / project_dir_name(period)
    config_path = project / "run_config.yaml"
    if config_path.exists() and not overwrite:
        cfg = _load(config_path)
        if cfg.get("mode") and cfg.get("mode") != mode:
            raise Ambiguous(
                f"{project.name} 已存在且 mode={cfg.get('mode')},本次请求 mode={mode}",
                [(f"A) 按原 mode={cfg.get('mode')} 断点续跑", ["--resume-dir", project.name]),
                 (f"B) 以 mode={mode} 从头重跑(覆盖 run_config,已有产物保留)", ["--overwrite"]),
                 ("C) 停止", None)],
            )
        return _resumed(project, mode, period)

    project.mkdir(parents=True, exist_ok=True)
    cfg = build_run_config(period, mode)
    tmp = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, config_path)
    return {
        "status": "created",
        "project_dir": str(project),
        "mode": mode,
        "period": period,
        "fetch": cfg["fetch"],
        "current_phase": "init",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--text", required=True, help="用户原始触发消息(原样传入)")
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--today", default=None, help="测试用,默认当天 YYYY-MM-DD")
    p.add_argument("--projects-root", default=str(WORKSPACE_PROJECTS))
    # 以下参数只用于 ambiguous 裁决后的重调,取值原样来自上一次 stdout 的 resolutions
    p.add_argument("--prefer", choices=("latest",), default=None)
    p.add_argument("--allow-incomplete", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--resume-dir", default=None)
    p.add_argument("--new-week", type=int, default=None)
    args = p.parse_args()
    today = date.fromisoformat(args.today) if args.today else date.today()
    try:
        result = run(args.text, args.mode, today, Path(args.projects_root),
                     prefer=args.prefer, allow_incomplete=args.allow_incomplete,
                     overwrite=args.overwrite, resume_dir=args.resume_dir,
                     new_week=args.new_week)
    except Ambiguous as error:
        result = {"status": "ambiguous", "reason": error.reason, "options": error.options,
                  "resolutions": error.resolutions}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
