#!/usr/bin/env python3
"""手动拉取指定 job 的 Session 输出文件到本地 artifacts 目录。

用法:
    # 在 topic6 virtualenv 下，从 cases/topic6/ 目录运行
    python tools/manual_artifact_sync.py job_e295dbb74cf6

    # 指定 session_id（跳过 pipeline DB 查询）
    python tools/manual_artifact_sync.py --session-id <ma_session_id>

    # 指定输出目录（默认 data/artifacts）
    python tools/manual_artifact_sync.py job_e295dbb74cf6 --output-dir /tmp/artifacts

自动加载 ~/.arkagent/cases/topic6/config.env（与 `arkagent run --case topic6` 完全一致）。

脚本复用 artifact_sync.py 的 Topic6ArtifactSync.sync_once，行为与线上轮询完全一致：
增量下载（fingerprint 未变 + 本地文件存在则跳过）、原子写入、manifest 物化。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 确保 cases/topic6/ 和项目根都在 sys.path 中
TOPIC6_DIR = Path(__file__).resolve().parent.parent
FEISHU_BOT_DIR = TOPIC6_DIR.parent.parent
for p in (str(TOPIC6_DIR), str(FEISHU_BOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from arkagent.ark import ArkClient  # noqa: E402
from artifact_sync import Topic6ArtifactSync  # noqa: E402


def resolve_session_id(job_id: str, db_path: str = "") -> str:
    """从 pipeline DB 查 job_id 对应的 ma_session_id。"""
    from pipeline_store import PipelineStore  # noqa: E402

    # db_path 为空则让 PipelineStore 自己解析（env → DEFAULT_PIPELINE_DB_PATH）
    store = PipelineStore(db_path if db_path else None)
    print(f"  pipeline DB:  {store._path}")
    job = store.get_job(job_id)
    if job is None:
        print(f"错误: pipeline DB 中找不到 job_id={job_id}", file=sys.stderr)
        sys.exit(1)
    print(f"Job 信息:")
    print(f"  job_id:         {job.job_id}")
    print(f"  ma_session_id:  {job.ma_session_id}")
    print(f"  mode:           {job.mode}")
    print(f"  status:         {job.status}")
    print(f"  current_phase:  {job.current_phase}")
    print(f"  project_dir:    {job.project_dir}")
    return job.ma_session_id


async def run_sync(
    ark: ArkClient,
    job_id: str,
    session_id: str,
    sync_root: Path,
) -> int:
    syncer = Topic6ArtifactSync(
        ark,
        sync_root,
        get_job=lambda _: None,  # 手动模式不检查 job 状态
    )
    downloaded = await syncer.sync_once(job_id, session_id)
    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(
        description="手动拉取 topic6 job 的 Session 输出文件"
    )
    parser.add_argument(
        "job_id",
        nargs="?",
        help="pipeline job_id（如 job_e295dbb74cf6）；与 --session-id 二选一",
    )
    parser.add_argument(
        "--session-id",
        help="直接指定 MA session_id（跳过 pipeline DB 查询）",
    )
    parser.add_argument(
        "--output-dir",
        default=str(TOPIC6_DIR / "data" / "artifacts"),
        help="输出根目录（默认 cases/topic6/data/artifacts）",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="pipeline DB 路径（默认自动检测）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="打印详细日志",
    )
    args = parser.parse_args()

    if not args.job_id and not args.session_id:
        parser.error("必须指定 job_id 或 --session-id")

    # ---- 加载 config.env（与 arkagent run --case topic6 完全一致）----
    from arkagent.config import load_config_file  # noqa: E402
    from arkagent.paths import get_case_paths  # noqa: E402

    case_paths = get_case_paths("topic6")
    config_env_path = case_paths.config_path
    if os.path.exists(config_env_path):
        load_config_file(config_env_path)
        print(f"已加载配置: {config_env_path}")
    else:
        print(f"警告: 配置文件不存在: {config_env_path}", file=sys.stderr)

    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        print("错误: 缺少 ARK_API_KEY（config.env 中也没有）", file=sys.stderr)
        sys.exit(1)
    base_url = (
        os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        .strip()
        .rstrip("/")
    )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    # 解析 session_id
    if args.session_id:
        session_id = args.session_id.strip()
        job_id = args.job_id or f"manual_{session_id[:16]}"
    else:
        job_id = args.job_id
        session_id = resolve_session_id(job_id, args.db_path)

    sync_root = Path(args.output_dir)
    print(f"\n开始同步:")
    print(f"  session_id:  {session_id}")
    print(f"  job_id:      {job_id}")
    print(f"  输出目录:    {sync_root / job_id}")
    print()

    ark = ArkClient(api_key, base_url)
    try:
        downloaded = asyncio.run(run_sync(ark, job_id, session_id, sync_root))
    finally:
        asyncio.run(ark.aclose())

    job_root = sync_root / job_id
    objects_dir = job_root / ".objects"
    file_count = sum(1 for _ in objects_dir.glob("*")) if objects_dir.exists() else 0

    print(f"\n同步完成:")
    print(f"  本次新下载:  {downloaded} 个文件")
    print(f"  .objects 总计: {file_count} 个文件")
    print(f"  index.json:  {job_root / 'index.json'}")

    if downloaded == 0 and file_count == 0:
        print(
            "\n⚠ 未拉取到任何文件。可能原因:\n"
            "  1. Session 已过期，Files API 返回空列表\n"
            "  2. Agent 未产出任何 purpose=agent 的文件\n"
            "  3. ARK_API_KEY 无权访问该 Session\n"
            "\n可加 -v 查看详细请求日志排查。"
        )


if __name__ == "__main__":
    main()
