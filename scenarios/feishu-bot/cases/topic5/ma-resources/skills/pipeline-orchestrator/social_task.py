"""活动原帖自动采集适配层（FIX-04，原版 KS_Activity_Data_Acquisition v0.5）。

调用打包的 ssjh-social-task v0.11.6（不改其默认值，差异全部写在任务配置里）：
  - 每个活动单独一份任务配置：profiler.name=活动名、keywords=活动关键词、filterwords=活动过滤词；
    search.keyword_fields=content,title；每个已选平台配置区显式 msg_type=1（只抓帖子，不抓评论/回帖）。
  - 首次 execute 只做数量预检并停在 AWAITING_QUANTITY_CONFIRMATION；拿到用户采集授权（数量上限+确认引用）
    后才创建计费任务；之后沿原任务有界推进（最短 120 秒查询间隔），同一 request_id/registry 不换 ID 重建。
  - 下载后核验：唯一 ID、内容类型取值（PGC/UGC/未知）、发表时间在活动期内；主帖范围由 msg_type=1 保证；适配为 19 列工作簿（列名与顺序固定），
    缺列补空并记 missing_fields，不补零不伪造；写 acquisition_handoff。
价格、私有配置、凭证不进入任何产物。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import platforms

_ORCH_DIR = Path(__file__).resolve().parent
SKILL_DIR = next((p for p in (_ORCH_DIR.parent / "ssjh-social-task",) if p.is_dir()),
                 _ORCH_DIR.parent / "ssjh-social-task")
VERSION = "0.11.6"
_NODE = "cmw0mbre6005e01msaa7m9543.c3NqaF9zb2NpYWxfdGFzay5weQ"
_OPS = {"preflight": "cHJlZmxpZ2h0", "precheck": "ZXhlY3V0ZS1wcmVjaGVjaw",
        "confirmed": "ZXhlY3V0ZS1jb25maXJtZWQ", "resume": "ZXhlY3V0ZS1yZXN1bWU"}
POLL_SECONDS = 120          # Skill 最短状态查询间隔
BUDGET_CODE = "BUDGET_LIMIT_EXCEEDED"

# Topic5 平台 slug → (search.source, 配置区, site)。依据 references/PLATFORMS.md。
PLATFORM_SOURCES: dict[str, tuple[str, str, str]] = {
    "weibo": ("weibo", "weibo", ""),
    "weixin": ("weixin", "weixin", ""),
    "xiaohongshu": ("xiaohongshu", "xiaohongshu", ""),
    "douyin": ("shortVideo", "short_video", "1003583"),
    "kuaishou": ("shortVideo", "short_video", "1334510"),
    "weixin_channels": ("shortVideo", "short_video", "25047925"),
    "bilibili": ("video", "video", "44"),
    "zhihu": ("qa", "qa", "101944"),
    "news": ("news", "news", ""),
    "forum": ("forum", "forum", ""),
}

# 适配工作簿 19 列（KS_Activity_Data_Acquisition「抓取后字段保留清单」，顺序固定）。
ADAPTED_COLUMNS = ("id", "分析对象", "站点名称", "标题", "内容", "视频封面内容识别", "音频内容识别",
                   "视频文本识别", "内容类型", "话题", "作者", "url", "发表时间", "总互动量",
                   "转发数", "评论数", "点赞数", "收藏数", "粉丝数")
# 原始记录字段 → 目标列（「来源字段映射」）。收藏类字段口径待核实（数据字典），不映射、记缺列。
FIELD_MAP = {"id": "id", "site_name": "站点名称", "title": "标题", "content": "内容",
             "cover_ocr_content": "视频封面内容识别", "audio_asr_content": "音频内容识别",
             "video_content": "视频文本识别", "data_type": "内容类型", "topics": "话题",
             "author": "作者", "url": "url", "publish_time": "发表时间", "interaction_cnt": "总互动量",
             "reposts_cnt": "转发数", "review_cnt": "评论数", "like_cnt": "点赞数", "fans_cnt": "粉丝数"}


# 缺陷清单 D10：取数表「内容类型」（data_type）为 PGC/UGC/未知（2026-11-03 用户更正），
# 与主帖/评论是两个独立维度。主帖范围只由任务配置 msg_type=1 保证（创建前 config_scope_check、
# 恢复时 scope_check 核对原任务条件），不再从该列推断评论；Skill 模板注释不作为语义依据。
CONTENT_TYPE_VALUES = ("PGC", "UGC", "未知")


def unavailable_reason() -> str:
    """不可用原因（空串=可用）：Skill 缺失、凭证缺失或 Python 低于 Skill 要求的 3.10。"""
    import sys
    if not (SKILL_DIR / "ssjh_social_task.py").is_file():
        return "未挂载 ssjh-social-task"
    missing = [k for k in ("SSJH_USERNAME", "SSJH_PASSWORD", "SSJH_FIXED_TOKEN") if not os.environ.get(k)]
    if missing:
        return "缺少采集凭证 " + "、".join(missing)
    if sys.version_info < (3, 10):
        return f"运行环境 Python {sys.version_info[0]}.{sys.version_info[1]} 低于 Skill 要求的 3.10"
    return ""


def api_available() -> bool:
    return not unavailable_reason()


def request_id(task_id: str, campaign_id: str, plan_version: str) -> str:
    """同一业务请求确定性复用 request_id（恢复/重跑不换 ID 重建）。"""
    return hashlib.sha256(f"{task_id}|{campaign_id}|{plan_version}".encode()).hexdigest()[:24]


def build_config(plan: dict, campaign: dict, *, rid: str, output_file: str) -> dict:
    """任务配置：只写本次需求的条件；所有已选平台配置区显式 msg_type=1。"""
    slugs = [platforms.to_slug(p) if p in platforms.CANON else str(p) for p in plan.get("platforms") or []]
    unsupported = [s for s in slugs if s not in PLATFORM_SOURCES]
    if unsupported:
        raise ValueError(f"social_task: 平台不在 ssjh-social-task 契约内：{unsupported}")
    search: dict[str, Any] = {
        "start_date": str(campaign.get("period_start") or plan.get("period_start")),
        "end_date": str(campaign.get("period_end") or plan.get("period_end")),
        "is_fast": 0, "sentiments": "0,1,-1", "keyword_fields": "content,title",
        "filters": {"ad": "all", "interaction_min": None, "interaction_max": None},
    }
    sources: list[str] = []
    for slug in slugs:
        source, section, site = PLATFORM_SOURCES[slug]
        if source not in sources:
            sources.append(source)
        area = search.setdefault(section, {"msg_type": 1})
        if site:
            area["site"] = ",".join(filter(None, [area.get("site", ""), site]))
    search["source"] = ",".join(sources)
    keywords = str(campaign.get("activity_query") or plan.get("activity_query") or "").strip()
    if not keywords:
        raise ValueError("social_task: 缺少活动关键词，不能创建原帖任务")
    return {
        "task": {"request_id": rid, "name": "", "email": ""},
        "search": search,
        "profiler": {"name": str(campaign.get("name") or campaign.get("id")), "keywords": keywords,
                     "filterwords": str(campaign.get("activity_filter") or plan.get("activity_filter") or "")},
        "precheck": {"uncertainty_acceptance_ref": ""},
        "output": {"file": output_file, "template": str(SKILL_DIR / "template.yaml")},
        "observability": {"delivery_mode": "intermediate", "sample_kind": "normal"},
    }


def _command(args: list[str]) -> list[str]:
    """调用命令。包内 `.blueai-skills-market/bsm-obs-manifest.yaml` 存在时经可观测包装调用；
    v0.11.6 原包缺该 manifest（包装器 fail-closed 无法运行），则直接调用其目标脚本
    `ssjh_social_task.py`（包装器仅做观测记录，业务行为等价）。不修改客户包。"""
    wrapper = SKILL_DIR / ".blueai-skills-market"
    if (wrapper / "bsm-obs-manifest.yaml").is_file():
        op = "confirmed" if "--max-records" in args else ("precheck" if "--action" in args else "resume")
        return ["node", str(wrapper / "bsm-obs-run.cjs"), "run", f"{_NODE}.{_OPS[op]}",
                "--interpreter", "python3", "--", *args]
    return ["python3", str(SKILL_DIR / "ssjh_social_task.py"), *args]


def _run(args: list[str], timeout: int = 600) -> dict:
    """返回 Skill 最后一行 JSON 信封。"""
    proc = subprocess.run(_command(args), capture_output=True, text=True, timeout=timeout,
                          cwd=str(SKILL_DIR))
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise RuntimeError(f"social_task: 无法解析 Skill 输出（exit={proc.returncode}）")


def execute(config_path: Path, cache_root: Path, registry: Path, *,
            max_records: Optional[int] = None, confirmation_ref: str = "") -> dict:
    args = ["--action", "execute", "--config", str(config_path),
            "--api-config", str(SKILL_DIR / "api_config.yaml"),
            "--cache-root", str(cache_root), "--registry", str(registry)]
    if max_records is not None:
        args += ["--max-records", str(max_records), "--confirmation-ref", confirmation_ref]
    return _run(args)


def is_budget_block(envelope: dict) -> bool:
    return envelope.get("code") == BUDGET_CODE or (envelope.get("error_detail") or {}).get("code") == BUDGET_CODE


def wait_completed(config_path: Path, cache_root: Path, registry: Path, deadline_s: int,
                   sleep=time.sleep) -> dict:
    """沿原任务有界推进直到 COMPLETED；错误或超时抛出（保留原任务，可恢复）。"""
    started = time.time()
    while True:
        envelope = execute(config_path, cache_root, registry)
        if envelope.get("state") == "COMPLETED" and envelope.get("ok"):
            return envelope
        if not envelope.get("ok"):
            raise RuntimeError(f"social_task: {envelope.get('code')} {envelope.get('message')}")
        if time.time() - started > deadline_s:
            raise RuntimeError(f"social_task: 等待供应商任务超时（state={envelope.get('state')}），"
                               "原任务已保留，可恢复")
        sleep(POLL_SECONDS)


def scope_check(run_dir: Path) -> dict:
    """原任务范围核对：读 run 的 state.json 中已绑定的规范化查询，每个配置区 msg_type 必须为 1。

    原任务包含评论/回帖或范围不明 → 停止，不修改原任务、不自动新建计费任务。
    """
    state_file = Path(run_dir) / "state.json"
    if not state_file.is_file():
        return {"queries": 0, "non_post_queries": [], "ok": False, "reason": "缺少 run state，范围不明"}
    state = json.loads(state_file.read_text(encoding="utf-8"))
    queries = ((state.get("contract") or {}).get("canonical_query")) or []
    bad = [q.get("platform") for q in queries if str(q.get("msg_type")) != "1"]
    return {"queries": len(queries), "non_post_queries": bad, "ok": bool(queries) and not bad,
            "reason": "" if queries and not bad else ("原任务含评论/回帖" if bad else "范围不明")}


def config_scope_check(config: dict) -> None:
    """创建前用 Skill 自身的规范化逻辑核对配置确实落实 msg_type=1。"""
    import sys
    if str(SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(SKILL_DIR))
    import query_contract  # noqa: E402  (Skill 内模块)
    queries = query_contract.normalize(config)
    bad = [q.get("platform") for q in queries if str(q.get("msg_type")) != "1"]
    if bad:
        raise ValueError(f"social_task: 配置未落实只抓帖子（msg_type≠1）：{bad}")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "|".join(str(v) for v in value if str(v).strip())
    return str(value)


def adapt(records: list[dict], campaign_name: str, period: tuple[str, str]) -> dict:
    """原始记录 → 19 列行（中文列名，供 excel_source.normalize_excel_row）+ 核验结果。"""
    rows, ids, issues = [], set(), []
    duplicates = out_of_period = 0
    invalid_types: dict[str, int] = {}
    present = {FIELD_MAP[k] for r in records for k in r if k in FIELD_MAP}
    missing = [c for c in ADAPTED_COLUMNS if c not in present and c != "分析对象"]
    start, end = (str(period[0]).replace("-", ""), str(period[1]).replace("-", ""))
    for record in records:
        rid = _text(record.get("id"))
        if rid in ids:
            duplicates += 1
            continue
        ids.add(rid)
        content_type = str(record.get("data_type") or "").strip()
        if content_type not in CONTENT_TYPE_VALUES:
            # 空值/非法值另行记录，不静默混同为供应商「未知」，不猜测枚举（KS 字段适配门禁）。
            key = content_type or "（空）"
            invalid_types[key] = invalid_types.get(key, 0) + 1
        row = {col: "" for col in ADAPTED_COLUMNS}
        for source, target in FIELD_MAP.items():
            if source in record and record[source] is not None:
                row[target] = record[source] if target in ("总互动量", "转发数", "评论数", "点赞数", "粉丝数") \
                    else _text(record[source])
        row["分析对象"] = campaign_name
        ymd = str(row["发表时间"])[:10].replace("-", "").replace("/", "")
        if ymd and start and end and not (start <= ymd <= end):
            out_of_period += 1
        rows.append(row)
    if duplicates:
        issues.append(f"重复 ID {duplicates} 条（已按 ID 去重）")
    if invalid_types:
        issues.append("内容类型取值不在 PGC/UGC/未知 内："
                      + "、".join(f"{k} {v} 条" for k, v in sorted(invalid_types.items()))
                      + "（待核验映射，不重新推断分类）")
    if out_of_period:
        issues.append(f"发表时间超出活动期 {out_of_period} 条")
    # 内容类型取值异常属于影响 PGC/UGC 分析的未解决缺口：适配状态不得记 ready。
    blocking = bool(invalid_types) or not rows
    return {"rows": rows, "missing_fields": missing, "retained_fields": list(ADAPTED_COLUMNS),
            "verification": {"records": len(records), "unique": len(ids), "adapted": len(rows),
                             "duplicates": duplicates, "invalid_content_types": invalid_types,
                             "out_of_period": out_of_period, "issues": issues, "ok": not blocking}}


def write_workbook(rows: list[dict], path: Path) -> str:
    from openpyxl import Workbook
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.create_sheet("数据", 0)
    for extra in [ws for ws in book.worksheets if ws is not sheet]:
        book.remove(extra)
    sheet.append(list(ADAPTED_COLUMNS))
    for row in rows:
        sheet.append([row.get(col, "") for col in ADAPTED_COLUMNS])
    book.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()
