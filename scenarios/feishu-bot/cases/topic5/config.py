"""topic5 case 的配置:飞书凭据 + topic5 专属的 coordinator/environment/memory。

与 topic6 的差异:topic5 复盘报告通过**飞书文档**交付,不走妙搭 Miaoda,因此:
  - 没有 Miaoda 用户 OAuth / artifact-sync 相关字段
  - 环境变量键前缀是 ``TOPIC5_*``
  - DB 默认落在 ``data/topic5_pipeline.db``

纯 stdlib(os / dataclasses / pathlib),可在沙箱内独立 import 测试。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


REQUIRED_KEYS = [
    "ARK_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "TOPIC5_COORDINATOR_AGENT_ID",
    "TOPIC5_ENVIRONMENT_ID",
]


@dataclass(frozen=True)
class Topic5Config:
    ark_api_key: str
    ark_base_url: str
    feishu_app_id: str
    feishu_app_secret: str
    coordinator_agent_id: str
    environment_id: str
    memory_store_id: str
    pipeline_db_path: str
    vault_id: str
    authorized_open_ids: tuple[str, ...]
    artifact_sync_dir: str = ""
    artifact_poll_interval_sec: float = 60.0
    replay_job_id: str = ""


DEFAULT_ARTIFACT_SYNC_DIR = Path(__file__).resolve().parent / "data" / "artifacts"

def load_case_config(env: Optional[Mapping[str, str]] = None) -> Topic5Config:
    environ = os.environ if env is None else env
    missing = [k for k in REQUIRED_KEYS if not (environ.get(k) or "").strip()]
    if missing:
        raise RuntimeError(f"缺少环境变量:{', '.join(missing)}")
    open_ids = tuple(
        i.strip()
        for i in (environ.get("AUTHORIZED_OPEN_IDS") or "").replace(",", " ").split()
        if i.strip()
    )
    return Topic5Config(
        ark_api_key=environ["ARK_API_KEY"],
        ark_base_url=(environ.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
        feishu_app_id=environ["FEISHU_APP_ID"],
        feishu_app_secret=environ["FEISHU_APP_SECRET"],
        coordinator_agent_id=environ["TOPIC5_COORDINATOR_AGENT_ID"].strip(),
        environment_id=environ["TOPIC5_ENVIRONMENT_ID"].strip(),
        memory_store_id=(environ.get("TOPIC5_MEMORY_STORE_ID") or "").strip(),
        pipeline_db_path=(environ.get("TOPIC5_PIPELINE_DB_PATH") or "").strip(),
        vault_id=(environ.get("ARK_VAULT_ID") or "").strip(),
        authorized_open_ids=open_ids,
        # 过程文件同步：默认落 cases/topic5/data/artifacts/<job_id>/；设为 off 关闭。
        artifact_sync_dir=(
            ""
            if (environ.get("TOPIC5_ARTIFACT_SYNC_DIR") or "").strip().lower() == "off"
            else (environ.get("TOPIC5_ARTIFACT_SYNC_DIR") or "").strip()
            or str(DEFAULT_ARTIFACT_SYNC_DIR)
        ),
        artifact_poll_interval_sec=float(
            (environ.get("TOPIC5_ARTIFACT_POLL_INTERVAL_SEC") or "60").strip()
        ),
        # 测试回放：设为历史 job_id 时，新任务复用其外部数据与标注缓存。
        replay_job_id=(environ.get("TOPIC5_REPLAY_JOB_ID") or "").strip(),
    )
