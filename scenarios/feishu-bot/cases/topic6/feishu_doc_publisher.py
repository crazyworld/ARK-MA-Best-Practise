"""HC 阶段产物 → 飞书可编辑文档的发布器。

所有文档创建在 job 子文件夹中，继承父级权限（白名单用户 full_access）。
不上传到 OSS/TOS（只能看不能编辑），全部使用飞书在线文档。

职责：
1. HC1: 将宽表 xlsx → 上传到飞书在线表格，用户可直接编辑
2. HC2: 同上 + distribution_summary → 飞书在线文档(docx)
3. HC3: 报告已在飞书文档中（Phase F 创建），只校验 URL
4. Phase H: 最终报告文档链接归档到子文件夹
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pipeline_store import PipelineJob

log = logging.getLogger("arkagent.topic6.doc_publisher")

# 飞书文档 URL 前缀
FEISHU_SHEETS_URL_PREFIX = "https://bluefocus.feishu.cn/sheets/"
FEISHU_FILE_URL_PREFIX = "https://bluefocus.feishu.cn/drive/folder/"


class FeishuDocPublisher:
    """HC 阶段产物 → 飞书可编辑文档。

    依赖：
    - feishu: FeishuSender 实例（已有 add_permission / create_folder / import_spreadsheet）
    - docs_folder_token: 飞书共享根文件夹 token
    """

    def __init__(self, feishu, docs_folder_token: str):
        self._feishu = feishu
        self._root_folder_token = docs_folder_token

    @property
    def enabled(self) -> bool:
        """是否已配置飞书文档发布（有根文件夹 token 才启用）。"""
        return bool(self._root_folder_token)

    def init_folder_permissions(self, authorized_open_ids: tuple[str, ...]) -> None:
        """Gateway 启动时：确保所有白名单用户对根文件夹有 full_access（幂等）。

        同步调用，由上层在启动阶段用 run_in_executor 包装。
        """
        if not self._root_folder_token or not authorized_open_ids:
            return
        for open_id in authorized_open_ids:
            try:
                self._feishu.add_permission(
                    token=self._root_folder_token,
                    token_type="folder",
                    member_open_id=open_id,
                    perm="full_access",
                )
            except Exception as err:  # noqa: BLE001
                if "already" not in str(err).lower():
                    log.warning(
                        "topic6 init folder permission failed user=%s: %s",
                        open_id, err,
                    )

    def create_job_folder(self, job: PipelineJob) -> str:
        """在根文件夹下创建 job 专属子文件夹，返回子文件夹 token。

        子文件夹自动继承父级权限，白名单用户无需额外授权。
        同步调用。
        """
        if not self._root_folder_token:
            return ""
        # 命名格式: {project_dir最后一段}_{job_id前12位}
        project_name = job.project_dir.rstrip("/").rsplit("/", 1)[-1] if job.project_dir else "unknown"
        folder_name = f"{project_name}_{job.job_id[:12]}"
        try:
            sub_folder_token = self._feishu.create_folder(
                parent_token=self._root_folder_token,
                name=folder_name,
            )
            log.info(
                "topic6 job docs folder created job=%s folder=%s token=%s",
                job.job_id, folder_name, sub_folder_token,
            )
            return sub_folder_token
        except Exception as err:  # noqa: BLE001
            log.error(
                "topic6 create job docs folder failed job=%s: %s",
                job.job_id, err,
            )
            return ""

    async def publish_hc_artifacts(
        self,
        job: PipelineJob,
        hc_kind: str,
        payload: dict,
        sync_root: Path,
        job_folder_token: str,
    ) -> dict:
        """发布 HC 产物到飞书，返回 {artifact_name: feishu_url} 映射。

        HC1: 上传宽表 → {"wide_table": "https://feishu.cn/sheets/xxx"}
        HC2: 上传宽表 + 分布摘要文档 → 两个链接
        HC3: 校验飞书文档 URL → {"report": url}
        """
        if not job_folder_token:
            log.warning("topic6 publish_hc_artifacts: no job_folder_token, skip")
            return {}

        if hc_kind == "HC1":
            return self._publish_wide_table(job, payload, sync_root, job_folder_token)
        elif hc_kind == "HC2":
            urls = self._publish_wide_table(job, payload, sync_root, job_folder_token)
            # HC2 可能有额外的分布摘要，但当前 payload 中 distribution_summary 是文本字段
            # 不需要单独创建文档，直接在卡片摘要中展示即可
            return urls
        elif hc_kind == "HC3":
            return self._verify_report_doc(payload)
        return {}

    def _publish_wide_table(
        self,
        job: PipelineJob,
        payload: dict,
        sync_root: Path,
        folder_token: str,
    ) -> dict:
        """将本地宽表 xlsx 上传到飞书文件夹。"""
        wide_table_path = payload.get("wide_table_path", "")
        if not wide_table_path:
            log.info("topic6 publish_wide_table: no wide_table_path in payload")
            return {}

        # 从 artifact_sync 的本地缓存找到对应文件
        local_path = self._resolve_local_artifact(sync_root, job.job_id, wide_table_path)
        if not local_path or not local_path.exists():
            log.warning(
                "topic6 publish_wide_table: local artifact not found job=%s path=%s resolved=%s",
                job.job_id, wide_table_path, local_path,
            )
            return {}

        try:
            title = f"{job.mode}_wide_table"
            file_token = self._feishu.import_spreadsheet(
                folder_token=folder_token,
                file_path=str(local_path),
                title=title,
            )
            url = f"{FEISHU_SHEETS_URL_PREFIX}{file_token}"
            log.info(
                "topic6 wide_table published job=%s token=%s url=%s",
                job.job_id, file_token, url,
            )
            return {"wide_table": url}
        except Exception as err:  # noqa: BLE001
            log.error(
                "topic6 publish_wide_table failed job=%s: %s",
                job.job_id, err,
            )
            return {}

    @staticmethod
    def _verify_report_doc(payload: dict) -> dict:
        """HC3: 校验飞书文档 URL 存在。"""
        doc_url = str(payload.get("feishu_doc_url") or "").strip()
        if not doc_url:
            return {}
        return {"report": doc_url}

    @staticmethod
    def _resolve_local_artifact(
        sync_root: Path, job_id: str, session_path: str,
    ) -> Optional[Path]:
        """从 artifact_sync 的本地缓存中解析出文件路径。

        artifact_sync 的目录结构:
            sync_root / {job_id} / .objects / {hash}__{safe_name}
            sync_root / {job_id} / {project} / {文件名}  (materialized)

        session_path 是 Agent 在沙箱中的路径（如 /workspace/Projects/xxx/wide_table.xlsx），
        我们需要找到对应的本地文件。策略：
        1. 优先在 job 目录下递归查找同名文件
        2. 在 .objects 目录下查找
        """
        # 安全化 job_id 目录名（与 artifact_sync._safe_component 一致）
        safe_job = "".join(
            c if c.isalnum() or c in "._-" else "_"
            for c in Path(job_id).name.strip()
        ) or "unknown-job"
        job_root = sync_root / safe_job
        if not job_root.is_dir():
            return None

        target_name = Path(session_path).name

        # 策略1: 递归查找（排除 .objects 目录，优先 materialized 路径）
        for path in job_root.rglob(target_name):
            if ".objects" not in path.parts:
                return path

        # 策略2: 在 .objects 目录查找（文件名是 {hash}__{safe_name}）
        objects_dir = job_root / ".objects"
        if objects_dir.is_dir():
            safe_name = "".join(
                c if c.isalnum() or c in "._-" else "_"
                for c in target_name
            )
            for path in objects_dir.iterdir():
                if path.name.endswith(f"__{safe_name}"):
                    return path

        return None
