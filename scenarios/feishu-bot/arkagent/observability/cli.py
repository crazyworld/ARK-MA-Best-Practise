"""``arkagent trace`` 子命令:show / list。

    arkagent trace list [--status failed] [--limit 20] [--scenario topic6]
    arkagent trace show <task_id | job_id | session_id> [--json] [--raw] [--no-events]

库路径取自 TOPICX_CONTROL_DB_PATH / TOPIC5_PIPELINE_DB_PATH / TOPIC6_PIPELINE_DB_PATH,
也可用 ``--db topic6=/path/x.db`` 显式指定(可重复)。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from ..config import DEFAULT_ARK_BASE_URL, load_config_file
from ..paths import get_case_paths
from .adapters.base import TraceRef, TraceSource
from .adapters.sqlite_sources import PipelineJobSource, TopicxSource
from .diagnose import diagnose
from .render import render_findings, render_list, render_text
from .service import build_trace

_CASES_DIR = Path(__file__).resolve().parents[2] / "cases"
_DB_ENV = {
    "topicx": "TOPICX_CONTROL_DB_PATH",
    "topic5": "TOPIC5_PIPELINE_DB_PATH",
    "topic6": "TOPIC6_PIPELINE_DB_PATH",
}
_DB_DEFAULT = {
    "topicx": _CASES_DIR / "topicx" / "data" / "topicx_control.db",
    "topic5": _CASES_DIR / "topic5" / "data" / "topic5_pipeline.db",
    "topic6": _CASES_DIR / "topic6" / "data" / "topic6_pipeline.db",
}
_USAGE = (
    "arkagent trace list [--status S] [--limit N] [--scenario topicx|topic5|topic6]\n"
    "arkagent trace show <id> [--json] [--raw] [--no-events]\n"
    "公共参数: --db <scenario>=<path>(可重复)  --env-file <config.env>"
)


def _parse(args: list[str]) -> tuple[list[str], dict[str, str], dict[str, str], set[str]]:
    positional, options, dbs, flags = [], {}, {}, set()
    value_opts = {"--status", "--limit", "--scenario", "--env-file", "--db"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_opts:
            if i + 1 >= len(args):
                raise RuntimeError(f"{arg} 需要一个参数")
            if arg == "--db":
                name, _, path = args[i + 1].partition("=")
                if name not in _DB_ENV or not path:
                    raise RuntimeError("--db 格式:<topicx|topic5|topic6>=<path>")
                dbs[name] = path
            else:
                options[arg] = args[i + 1]
            i += 2
        elif arg.startswith("--"):
            flags.add(arg)
            i += 1
        else:
            positional.append(arg)
            i += 1
    return positional, options, dbs, flags


def _load_env(env_file: Optional[str]) -> None:
    candidates = [env_file] if env_file else [
        get_case_paths(case).config_path for case in ("topicx", "topic6", "topic5")
    ]
    for path in candidates:
        if path and os.path.exists(path):
            load_config_file(path)
            return


def _db_path(scenario: str, overrides: dict[str, str]) -> Optional[str]:
    path = overrides.get(scenario) or os.environ.get(_DB_ENV[scenario]) or str(_DB_DEFAULT[scenario])
    return path if os.path.exists(path) else None


def build_sources(overrides: dict[str, str]) -> dict[str, TraceSource]:
    jobs: dict[str, PipelineJobSource] = {}
    for scenario in ("topic5", "topic6"):
        path = _db_path(scenario, overrides)
        if path:
            jobs[scenario] = PipelineJobSource(scenario, path)
    sources: dict[str, TraceSource] = {}
    control = _db_path("topicx", overrides)
    if control:
        sources["topicx"] = TopicxSource(control, jobs)
    sources.update(jobs)
    return sources


def _find(sources: dict[str, TraceSource], ident: str) -> Optional[TraceRef]:
    # topicx 在前:task 级视图包含 router session,是信息最全的根。
    for source in sources.values():
        ref = source.resolve(ident)
        if ref:
            return ref
    return None


async def run(args: list[str]) -> int:
    positional, options, dbs, flags = _parse(args)
    if not positional or positional[0] not in ("show", "list"):
        print(_USAGE)
        return 2
    _load_env(options.get("--env-file"))
    sources = build_sources(dbs)
    if not sources:
        raise RuntimeError("找不到任何数据库。请用 --db 或环境变量指定(见 arkagent trace)。")

    if positional[0] == "list":
        scenario = options.get("--scenario")
        if scenario and scenario not in sources:
            raise RuntimeError(f"未配置或找不到 {scenario} 的数据库")
        picked = {scenario: sources[scenario]} if scenario else sources
        limit = int(options.get("--limit", "20"))
        refs = [
            r
            for src in picked.values()
            for r in src.list_refs(limit=limit, status=options.get("--status"))
        ]
        refs.sort(key=lambda r: r.started_at_ms, reverse=True)
        print(render_list(refs[:limit]))
        return 0

    if len(positional) < 2:
        print(_USAGE)
        return 2
    ref = _find(sources, positional[1])
    if ref is None:
        print(f"没有找到 {positional[1]}(已在 {', '.join(sources)} 中查找)", flush=True)
        return 1

    ark = None
    if "--no-events" not in flags:
        api_key = (os.environ.get("ARK_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("缺少 ARK_API_KEY:请用 --env-file 指定 config.env,或加 --no-events 只看数据库侧")
        from ..ark import ArkClient

        ark = ArkClient(api_key, (os.environ.get("ARK_BASE_URL") or DEFAULT_ARK_BASE_URL).rstrip("/"))
    try:
        trace = await build_trace(ref, ark, redact="--raw" not in flags)
    finally:
        if ark is not None:
            await ark.aclose()
    findings = diagnose(trace, now_ms=int(time.time() * 1000))
    if "--json" in flags:
        payload = trace.to_dict()
        payload["findings"] = [f.to_dict() for f in findings]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_findings(findings) + "\n\n" + render_text(trace))
    return 0
