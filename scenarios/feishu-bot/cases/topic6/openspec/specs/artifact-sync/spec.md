# artifact-sync Specification

## Purpose
把 MA Session 的 `/mnt/session/outputs/` 产物增量同步到 Gateway 本地磁盘，供 HC 发布、调试与归档使用。同步本身不发送任何飞书消息。

源码：`artifact_sync.py`（`Topic6ArtifactSync`）、`tools/manual_artifact_sync.py`。Agent 侧的产物发布契约见 `ma-resources/skills/topic6-annotation/scripts/publish_node_artifacts.py` 与 coordinator-orchestration。

## Requirements

### Requirement: 本地目录布局
同步根目录 SHALL 为 `TOPIC6_ARTIFACT_SYNC_DIR`（默认 `cases/topic6/data/artifacts`）。每个 job 使用 `<root>/<safe(job_id)>/`：
- `.objects/<sha256(file_id)[:16]>__<safe(filename)>`：原始对象；
- `index.json`：`{schema_version:1, job_id, session_id, updated_at, files:{file_id:{file_id, filename, status, fingerprint:{size, updated_at}, object_path, sha256, synced_at}}}`；
- `<project>/…`：按 manifest 物化后的可读路径，及 `<project>/manifest.json`。

`safe()` MUST 只保留字母数字与 `._-`，其余替换为 `_`；空、`.`、`..` 使用兜底名（`unknown-job` / `artifact`）。

#### Scenario: 非法文件名
- **WHEN** Session 文件名为 `../a b.xlsx`
- **THEN** 对象名为 `<hash>__a_b.xlsx`，不发生目录穿越

### Requirement: 增量下载与原子写入
`sync_once(job_id, session_id)` SHALL 调用 `list_agent_files(session_id)`，跳过缺 `id` 或 `download_url` 的条目；当 index 中同 `file_id` 的 `fingerprint`（`size` + `updated_at`）未变且本地对象存在时 MUST 跳过下载。所有写入（对象、index、物化文件）MUST 通过"临时文件 + fsync + `os.replace`"原子完成。损坏的 `index.json` MUST 被忽略并重建。返回本次下载数量。

#### Scenario: 无变化
- **WHEN** 连续两次同步之间 Session 文件未变
- **THEN** 第二次返回 0，不发起下载

#### Scenario: index 损坏
- **WHEN** `index.json` 不是合法 JSON
- **THEN** 记录 warning，以空 index 重新同步

### Requirement: manifest 物化
对每个文件名为 `manifest.json` 的对象，若其含字符串 `project` 与列表 `publications`，系统 SHALL 复制到 `<project>/manifest.json`，并对 `publications[].files[]` 中每个 `output`：要求为相对路径、首段等于 `project`、不含 `..`；按 `size` →（同名 `source` basename 优先）→ `sha256` 匹配已下载对象，匹配成功则复制到 `<job_root>/<output>`。不满足条件的条目 MUST 静默跳过。

#### Scenario: output 越界
- **WHEN** manifest 中 `output` 为 `/etc/passwd` 或 `other_project/x`
- **THEN** 不物化该条目

### Requirement: 轮询生命周期
`start(job, loop)` SHALL 先停止该 job 已有任务再启动轮询协程：每轮 `sync_once` 后若 job 不存在或状态不在 `{running, wait_hc}` 则退出，否则休眠 `TOPIC6_ARTIFACT_POLL_INTERVAL_SEC`。单轮异常只 warning 并在下一轮重试。`stop(job_id)` 取消轮询。Gateway 在 `/new` 取消任务时 SHALL 先 `stop` 再做一次最终 `sync_once`；HITL 在发 HC 卡前 SHALL 主动调用 `sync_once`（见 hitl-review）。

#### Scenario: 任务完成
- **WHEN** job 变为 `done`
- **THEN** 当轮同步结束后轮询自动退出

#### Scenario: Gateway 重启
- **WHEN** 启动恢复后 job 仍为 `running`
- **THEN** 重新启动该 job 的轮询

### Requirement: 手动同步工具
`tools/manual_artifact_sync.py <job_id> | --session-id <id> [--output-dir DIR]` SHALL 自动加载 `~/.arkagent/cases/topic6/config.env`，复用 `Topic6ArtifactSync.sync_once`，行为与线上轮询一致。

#### Scenario: 仅知道 session
- **WHEN** 运行 `python tools/manual_artifact_sync.py --session-id sess_x`
- **THEN** 不查询 pipeline DB，直接按 session 同步
