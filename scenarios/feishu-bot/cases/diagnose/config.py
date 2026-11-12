"""diagnose case 的配置:飞书凭据 + 排障 Agent/Environment + 要排查的各 bot 的库路径。

排障 Bot 与被排查的 bot 同机部署(不考虑分布式),直接读本机的库;库路径不在这里写死,
由环境变量逐个指定,未配置的 bot 就不参与排查。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

REQUIRED_KEYS = [
    "ARK_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "DIAGNOSE_AGENT_ID",
    "DIAGNOSE_ENVIRONMENT_ID",
]

# 环境变量名 → 数据库。topicx 的 task_runs、topic5 / topic6 的 pipeline_jobs、通用 Gateway 的 conversations、数字员工的 sessions。
DB_ENV_KEYS = {
    "topicx": "TOPICX_CONTROL_DB_PATH",
    "topic5": "TOPIC5_PIPELINE_DB_PATH",
    "topic6": "TOPIC6_PIPELINE_DB_PATH",
    # 通用 Gateway 类 bot:每个 case 一个库,各配一个。
    "analyst": "ANALYST_GATEWAY_DB_PATH",
    "ada-pm": "ADA_PM_GATEWAY_DB_PATH",
    # 数字员工:独立的会话库。
    "digital-employee": "DIGITAL_EMPLOYEE_SESSION_DB_PATH",
}


@dataclass(frozen=True)
class DiagnoseConfig:
    ark_api_key: str
    ark_base_url: str
    feishu_app_id: str
    feishu_app_secret: str
    agent_id: str
    environment_id: str
    # 参与排查的库:{"topicx": path, "topic6": path, ...},只含已配置且文件存在的。
    db_paths: dict
    authorized_open_ids: tuple[str, ...]
    session_timeout_ms: int
    # session 日志落飞书文档(定时任务)。doc_parent_token 为空 = 不启用。
    doc_parent_token: str = ""
    doc_sync_interval_s: int = 120
    doc_lookback_days: int = 7
    doc_max_writes_per_cycle: int = 10
    doc_check_concurrency: int = 4
    doc_cycle_budget_s: int = 90
    doc_db_path: str = ""


def _positive(raw: Optional[str], default: int, name: str) -> int:
    if not (raw or "").strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} 必须是整数:{raw!r}") from None
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0:{value}")
    return value


def load_case_config(env: Optional[Mapping[str, str]] = None) -> DiagnoseConfig:
    environ = os.environ if env is None else env
    missing = [k for k in REQUIRED_KEYS if not (environ.get(k) or "").strip()]
    if missing:
        raise RuntimeError(f"缺少环境变量:{', '.join(missing)}")
    db_paths = {}
    for name, key in DB_ENV_KEYS.items():
        path = (environ.get(key) or "").strip()
        if not path:
            continue
        if not os.path.exists(path):
            raise RuntimeError(f"{key} 指向的文件不存在:{path}")
        db_paths[name] = path
    if not db_paths:
        raise RuntimeError("至少需要配置一个待排查的库:" + " / ".join(DB_ENV_KEYS.values()))
    # 两个 bot 指向同一个库文件会让数据串号(分析师和 ada-pm 的库都叫 gateway.db,只靠目录区分,
    # 配错一个路径就会把别的 bot 的会话当成自己的)。按真实路径判重,软链接也算。
    by_real: dict[str, str] = {}
    for name, path in db_paths.items():
        real = os.path.realpath(path)
        if real in by_real:
            raise RuntimeError(f"{DB_ENV_KEYS[by_real[real]]} 与 {DB_ENV_KEYS[name]} 指向同一个库文件:{real}")
        by_real[real] = name
    return DiagnoseConfig(
        ark_api_key=environ["ARK_API_KEY"].strip(),
        ark_base_url=(environ.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
        feishu_app_id=environ["FEISHU_APP_ID"].strip(),
        feishu_app_secret=environ["FEISHU_APP_SECRET"].strip(),
        agent_id=environ["DIAGNOSE_AGENT_ID"].strip(),
        environment_id=environ["DIAGNOSE_ENVIRONMENT_ID"].strip(),
        db_paths=db_paths,
        authorized_open_ids=tuple(
            x.strip() for x in (environ.get("AUTHORIZED_OPEN_IDS") or "").replace(",", " ").split() if x.strip()
        ),
        session_timeout_ms=int(environ.get("DIAGNOSE_SESSION_TIMEOUT_MS") or 180_000),
        doc_parent_token=(environ.get("SESSION_DOC_PARENT_TOKEN") or "").strip(),
        doc_sync_interval_s=_positive(environ.get("SESSION_DOC_INTERVAL_S"), 120, "SESSION_DOC_INTERVAL_S"),
        doc_lookback_days=_positive(environ.get("SESSION_DOC_LOOKBACK_DAYS"), 7, "SESSION_DOC_LOOKBACK_DAYS"),
        doc_max_writes_per_cycle=_positive(environ.get("SESSION_DOC_MAX_WRITES_PER_CYCLE"), 10, "SESSION_DOC_MAX_WRITES_PER_CYCLE"),
        doc_check_concurrency=_positive(environ.get("SESSION_DOC_CHECK_CONCURRENCY"), 4, "SESSION_DOC_CHECK_CONCURRENCY"),
        doc_cycle_budget_s=_positive(environ.get("SESSION_DOC_CYCLE_BUDGET_S"), 90, "SESSION_DOC_CYCLE_BUDGET_S"),
        doc_db_path=(environ.get("SESSION_DOC_DB_PATH") or "").strip(),
    )
