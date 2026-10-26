"""HC 阶段产物 → 飞书可编辑文档的发布器。

所有文档创建在 job 子文件夹中；配置了共享根文件夹时继承父级权限（白名单用户
full_access），未配置时建在机器人云空间根目录。无论哪种，宽表创建后都单独给任务
发起人授予 edit 并回查校验。不上传到 OSS/TOS（只能看不能编辑），全部使用飞书在线文档。

职责：
1. HC1: 将宽表 xlsx → 上传到飞书在线表格，用户可直接编辑
2. HC2: 同上 + distribution_summary → 飞书在线文档(docx)
3. HC3: 报告已在飞书文档中（Phase F 创建），只校验 URL
4. Phase H: 最终报告文档链接归档到子文件夹
5. HC 审核通过后回读：用户在线修改过的表格导出为 xlsx，交给 Gateway 挂回 Session
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


def artifact_urls(artifacts: Optional[dict]) -> dict:
    """{name: {"url",...}} → {name: url}，供 HC 卡片渲染"打开编辑"按钮。兼容旧的 {name: url}。"""
    urls: dict = {}
    for name, item in (artifacts or {}).items():
        url = item.get("url") if isinstance(item, dict) else item
        if url:
            urls[name] = str(url)
    return urls


class FeishuDocPublisher:
    """HC 阶段产物 → 飞书可编辑文档。

    依赖：
    - feishu: FeishuSender 实例（add_permission / list_permission_members / create_folder /
      create_spreadsheet_from_xlsx / get_doc_meta / export_sheet_xlsx）
    - docs_folder_token: 飞书共享根文件夹 token（可空：建在机器人云空间根目录）
    """

    def __init__(self, feishu, docs_folder_token: str):
        self._feishu = feishu
        self._root_folder_token = docs_folder_token

    @property
    def enabled(self) -> bool:
        """有飞书 sender 即启用；根文件夹 token 缺省时建在机器人云空间根目录。"""
        return self._feishu is not None

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
        未配置根文件夹时 parent_token=""，即建在机器人云空间根目录（只有机器人可见，
        宽表另行授权给发起人）。同步调用。
        """
        # 命名格式: {project_dir最后一段}_{job_id前12位}
        project_name = job.project_dir.rstrip("/").rsplit("/", 1)[-1] if job.project_dir else "unknown"
        folder_name = f"{project_name}_{job.job_id[:12]}"
        try:
            sub_folder_token = self._feishu.create_folder(
                parent_token=self._root_folder_token or "",
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
        """发布 HC 产物到飞书，返回 {artifact_name: {"url","token","type",...}} 映射。

        HC1: 上传宽表 → {"wide_table": {"url":..., "token":..., "type":"sheet", "granted":bool}}
        HC2: 同 HC1（分布摘要是文本字段，直接在卡片展示）
        HC3: 校验飞书文档 URL → {"report": {"url": url, "token": "", "type": "docx"}}
        卡片渲染用 :func:`artifact_urls` 转成 {name: url}。
        """
        if not job_folder_token:
            log.warning("topic6 publish_hc_artifacts: no job_folder_token, skip")
            return {}

        if hc_kind == "HC1":
            return self._publish_wide_table(job, hc_kind, payload, sync_root, job_folder_token)
        elif hc_kind == "HC2":
            urls = self._publish_wide_table(job, hc_kind, payload, sync_root, job_folder_token)
            # HC2 可能有额外的分布摘要，但当前 payload 中 distribution_summary 是文本字段
            # 不需要单独创建文档，直接在卡片摘要中展示即可
            return urls
        elif hc_kind == "HC3":
            return self._verify_report_doc(payload)
        return {}

    def _publish_wide_table(
        self,
        job: PipelineJob,
        hc_kind: str,
        payload: dict,
        sync_root: Path,
        folder_token: str,
    ) -> dict:
        """将本地宽表 xlsx 导入为飞书在线表格，并授权任务发起人编辑。"""
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
            title = self._wide_table_title(job, hc_kind)
            sheet_token, sheet_url = self._feishu.create_spreadsheet_from_xlsx(
                folder_token=folder_token,
                file_path=str(local_path),
                title=title,
            )
        except Exception as err:  # noqa: BLE001
            log.error(
                "topic6 publish_wide_table failed job=%s: %s",
                job.job_id, err,
            )
            return {}
        # 优先用接口返回的 url（租户域名不一定是 bluefocus），缺失时才拼前缀兜底
        url = sheet_url or f"{FEISHU_SHEETS_URL_PREFIX}{sheet_token}"
        granted = self._grant_editor(sheet_token, "sheet", job.user_open_id, job.job_id)
        log.info(
            "topic6 wide_table published job=%s token=%s url=%s granted=%s",
            job.job_id, sheet_token, url, granted,
        )
        return {"wide_table": {"url": url, "token": sheet_token, "type": "sheet", "granted": granted}}

    def _grant_editor(self, token: str, token_type: str, open_id: str, job_id: str) -> bool:
        """给任务发起人授予 edit 并回查协作者列表确认；失败只记日志，返回 False。

        机器人仍是 owner；授权失败时卡片照样给链接（发起人可找机器人管理员申请）。
        """
        if not open_id:
            log.error("topic6 grant editor skipped: job=%s 无 user_open_id token=%s", job_id, token)
            return False
        try:
            self._feishu.add_permission(
                token=token, token_type=token_type, member_open_id=open_id, perm="edit",
            )
            members = self._feishu.list_permission_members(token, token_type)
        except Exception as err:  # noqa: BLE001
            log.error(
                "topic6 grant editor failed job=%s token=%s user=%s: %s",
                job_id, token, open_id, err,
            )
            return False
        for member in members:
            if member.get("member_id") == open_id and member.get("perm") in ("edit", "full_access"):
                return True
        log.error(
            "topic6 grant editor not verified job=%s token=%s user=%s members=%s",
            job_id, token, open_id, members,
        )
        return False

    def fetch_user_edited_sheet(self, token: str, since_ts: int = 0) -> Optional[bytes]:
        """回读用户在线修改过的表格：未被他人改过返回 None，改过则导出 xlsx 字节。

        判定（metas/batch_query）：latest_modify_user == owner_id（机器人自己写入）
        或 latest_modify_time <= since_ts（HC 发卡时间之前）视为未修改。
        同步调用；异常向上抛，由调用方降级为"使用沙箱版本"。
        """
        meta = self._feishu.get_doc_meta(token, "sheet")
        owner = str(meta.get("owner_id") or "")
        modifier = str(meta.get("latest_modify_user") or "")
        try:
            modify_ts = int(str(meta.get("latest_modify_time") or "0"))
        except ValueError:
            modify_ts = 0
        if not modifier or modifier == owner or modify_ts <= int(since_ts or 0):
            log.info(
                "topic6 sheet unchanged token=%s modifier=%s owner=%s modify_ts=%s since=%s",
                token, modifier, owner, modify_ts, since_ts,
            )
            return None
        log.info(
            "topic6 sheet edited by user token=%s modifier=%s modify_ts=%s, exporting",
            token, modifier, modify_ts,
        )
        return self._feishu.export_sheet_xlsx(token)

    @staticmethod
    def _wide_table_title(job: PipelineJob, hc_kind: str) -> str:
        """宽表标题：{项目名}_{HC1|HC2}_wide_table，区分同一 job 的两轮表格。"""
        project = job.project_dir.rstrip("/").rsplit("/", 1)[-1] if job.project_dir else ""
        parts = [project or job.mode, hc_kind.upper(), "wide_table"]
        return "_".join(p for p in parts if p)

    @staticmethod
    def _verify_report_doc(payload: dict) -> dict:
        """HC3: 校验飞书文档 URL 存在。"""
        doc_url = str(payload.get("feishu_doc_url") or "").strip()
        if not doc_url:
            return {}
        return {"report": {"url": doc_url, "token": "", "type": "docx"}}

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
