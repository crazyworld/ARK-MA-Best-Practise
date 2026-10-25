# Changelog

All notable changes to the Topic6 热点周报 pipeline are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.1.1] - 2026-10-23

### Added

- **统一任务通知器 `job_notifier.py`**: 独立通知模块，所有场景（HC 审核/完成/失败/中断）统一 @ 任务发起人。由 gateway 装配注入 runner 和 hitl，消除跨层私有属性访问。
- **HC 审核主动 @ 通知**: HC1/HC2/HC3 卡片发出后自动 @ 发起人，确保长时间运行后审核人及时感知。
- **终态统一通知**: 任务完成/失败/中断时自动 @ 发起人，覆盖全部 14 个终态路径，通过 `_render_and_patch` 统一拦截，零遗漏。
- **`model_request_failed_error` 指数退避自动重试**: Provider 侧临时故障（速率限制/超载）不再直接终止 pipeline，而是 30s→60s→120s→240s→300s 退避重试，最多 5 次，期间卡片显示重试进度。
- **C2 等待脚本 `wait_c2_complete.py`**: 与 `wait_datahub_batch.py` 同模式（≤105s 短检查），替代 Agent 自行 `sleep` 裸轮询，从根本上避免方舟 120s bash 超时导致的 session 中断。
- **C2 后台启动脚本 `launch_c2_background.py`**: 封装 nohup/PID/日志重定向，Agent 无需自行拼复合 shell。幂等：C2 已在跑时返回 `already_running`。
- **Demo 第二批并发脚本 `run_demo_batch2.py`**: 封装 R1~R5 routes + C2 fast 两进程并发 + wait + 退出码检查，替代 18 行 bash 代码块。
- **Phase F 飞书发布脚本 `publish_report_to_feishu.py`**: 封装创建文件夹→导入文档→授权发起人→转移 owner→授权 admin→校验 JSON 全流程，Agent 一条命令搞定，无需自行拼 5-6 步 lark-cli。

### Changed

- **`coordinator.system.md` 脚本化改造**: C2 启动（nohup shell → `launch_c2_background.py`）、C2 等待（裸 sleep → `wait_c2_complete.py`）、demo 第二批（18 行 bash → `run_demo_batch2.py`）、Phase F 飞书发布（5-6 步 lark-cli → `publish_report_to_feishu.py`）。Agent 职责收窄为"按顺序调脚本 + 在 HC 点输出 JSON"。
- **`create_all.sh`**: 修复 coordinator Agent 创建时 jq 缺少 `--arg name` 导致的 `$name is not defined` 错误。
- **`resume_job` HC phase 脱离前缀**: HC 通过后 phase 从 "HC1" 改为 "post-HC1"，避免后续 idle 被误判为"等人审"而跳过 nudge。
- **`/log` 命令事件解析修复**: 从错误的 `event["data"]["content"]` / `event["tool"]["name"]` 改为正确的方舟事件结构（`event_text()` / `event["name"]`），加 80 条上限。
- **飞书 Drive API 全部改用 `requests` 直接调用**: `add_permission`、`create_folder`、`import_spreadsheet` 三个方法从 lark_channel SDK `BaseRequest`（导致 9499 Bad Request）改为 `requests` + 标准 HTTP（JSON/multipart）。自己管理 `tenant_access_token`。
- **`send_hc_card` 自动补建 `docs_folder_token`**: 旧 job 或 start_job 时创建失败的场景，HC 发卡时自动补建飞书子文件夹，不再静默跳过整个发布链路。
- **`DataHub 标注模型`**: `DeepSeek-V4-Flash-0731` → `DeepSeek-V4-Flash`（DataHub 尚未支持 0731 版本）。

### Fixed

- **Session 中断根因**: Claude 模型在 C2 等待阶段用 `sleep 117` 裸轮询（违反 prompt 禁令），触发方舟 120s bash detach 阈值，叠加 `resume_job` 后 phase 仍为 "HC1" 导致 nudge 被跳过（0/3 直接 stopped）。修复：`wait_c2_complete.py` + `post-HC` 过渡态。
- **飞书文件夹创建 9499 Bad Request**: lark_channel SDK 的 `BaseRequest.body(json_string)` 未正确设置 Content-Type，飞书服务端解析失败。改用 `requests.post(json=body)` 解决。
- **`create_folder` 返回值可能为空**: 静默返回空字符串导致 `docs_folder_token` 写空、发布链路跳过。改为返回空时 `raise RuntimeError`。

### Not Yet Implemented

- **Session 分段（方案 A）**: 当前 session 上下文在超长运行（1190 事件 / 累计 2700 万 tokens）后，方舟 MA 在构建 BYOK 请求时出现间歇性内部失败（`model_request_failed_error`，`input=0 output=0`，请求未发出）。根治方案：Gateway 在 token 消耗接近阈值时主动创建新 session + 注入上下文摘要。当前通过指数退避重试止血。

---

### Files Changed (v2.1.1)

**Modified (7)**

| File | Summary |
|------|---------|
| `topic6_runner.py` | `resume_job` post-HC 过渡态、`_render_and_patch` 终态通知钩子、`model_request_failed_error` 指数退避重试、`_ProgressState` 新增 `provider_retry_count` |
| `topic6_hitl.py` | HC 审核通知→`JobNotifier`、`send_hc_card` 自动补建 `docs_folder_token`、P0-2 日志增强 |
| `gateway.py` | 装配 `JobNotifier`、`/log` 事件解析修复 |
| `arkagent/feishu.py` | `add_permission`/`create_folder`/`import_spreadsheet` 改用 requests、新增 `_get_tenant_access_token` |
| `coordinator.system.md` | C2 启动/等待/demo 第二批/Phase F 全部脚本化 |
| `create_all.sh` | coordinator jq 修复 `--arg name` |
| `test_topic6_pipeline_optimizations.py` | 断言同步更新（新脚本名 + Phase F 措辞） |

**Added (5)**

| File | Summary |
|------|---------|
| `job_notifier.py` | 统一通知器：@ 发起人通知 HC 审核/完成/失败/中断 |
| `wait_c2_complete.py` | C2 等待脚本（≤105s 短检查，同 `wait_datahub_batch.py` 模式） |
| `launch_c2_background.py` | C2 后台启动封装（nohup/PID/日志/幂等） |
| `run_demo_batch2.py` | Demo 第二批并发封装（routes + C2 fast） |
| `publish_report_to_feishu.py` | Phase F 飞书发布全流程封装（6 步 lark-cli → 1 个脚本） |

### Test Results (v2.1.1)

```
Total: 225 passed, 0 failed (2.83s)
  topic6_*: 208 passed
  gateway + config_store: 17 passed
  Cross-module (+ digital_employee): 255 passed, 0 failed
```

## [2.1.0] - 2026-10-22

### Added

- **HC 卡片补充说明按钮**: 审核人可以在 HC 卡片中向 Agent 提问或要求补充信息，Agent 回复后会再次发审核卡。([T2-Q1])
- **HC 审核通知**: HC 通过/打回/补充说明后自动在群里发通知消息，包含审核结果和操作人。([T1-Q4])
- **HC1/HC2 回读飞书表格**: 审核人在飞书在线表格中修改标注数据后点"通过"，Agent 自动下载最新版本再继续后续阶段。
- **`/log` 命令**: 在群里发送 `/log` 查看当前任务的 Agent 执行日志，支持 `/log <session_id>` 和 `/log <job_id>`。
- **进度卡片展示 Session ID**: 所有通知卡片（进度、HC 审核、错误）现在都显示 `session=xxx`。
- **Phase H 妙搭发布专项诊断**: 发布失败时不再显示通用"中断"信息，而是明确告知"妙搭发布失败"并给出 `/continue` 重试建议。([T2-Q5], [T2-Q6])
- **流程中断结构化诊断**: 启动阶段（<60s）无输出时识别为环境/授权问题；错误模式检测结合自动续跑次数判断是否需人工介入。([T1-Q1], [T1-Q2])
- **结构化错误信息**: 流程异常时显示任务 ID、阶段、Session ID 及可执行建议（`/continue` 或 `/new`）。
- **`run_tests.sh`**: 一键测试脚本，自动切换 conda env，支持 `fix`（回归）/ `all`（全量）/ `full`（详细）模式。
- **`tools/verify_feishu_docs.py`**: 飞书文档读写权限验证工具。
- **`tools/verify_feishu_pipeline.py`**: 模拟真实 Pipeline 各阶段向飞书写入宽表、报告、摘要，验证全链路。
- **`tests/test_topic6_fix_regression.py`**: 43 条回归测试，覆盖本次每个改动点的正确性及不破坏原有逻辑。
- **HC 产物自动发布到飞书可编辑文档 (P0-2)**: HC1/HC2 发卡前自动将宽表 xlsx 上传为飞书在线表格，HC 卡片嵌入「📝 打开编辑」按钮；HC3 校验飞书文档 URL。方案 B：App token 在统一文件夹下创建，文件夹级权限继承，白名单用户自动获得 full_access。([T1-Q3])
- **`feishu_doc_publisher.py`**: 新模块，HC 产物→飞书可编辑文档的发布器。职责：文件夹权限初始化、job 子文件夹创建、HC1/HC2 宽表上传、HC3 文档校验。
- **飞书 Drive API**: `FeishuSender` 新增 `add_permission`（权限管理，幂等）、`create_folder`（子文件夹创建）、`import_spreadsheet`（xlsx 上传）三个方法。
- **`TOPIC6_DOCS_FOLDER_TOKEN` 环境变量**: 飞书共享根文件夹 token，配置后自动启用 HC 产物发布。
- **`tests/test_topic6_doc_publisher.py`**: 27 条集成测试，覆盖文件解析、HC1/2/3 发布、artifact_sync→publisher→卡片端到端链路、pipeline_store 读写、降级保护。

### Changed

- **DataHub 标注模型**: `Doubao-Seed-1.6-lite` → `DeepSeek-V4-Flash`。
- **Validator Agent 模型**: `doubao-seed-2-1-pro-260628` → `claude-sonnet-5-5` via `bmc-llm-relay`，与 coordinator/annotator/insighter 统一。
- **`validate_hc_payload`**: HC1/HC2 新增 `wide_table_path` 存在性检查；`__fallback__` 兜底 payload 跳过校验以保持容错；HC3 的 Validator BLOCK 不再阻断卡片发送（保留已有的红色警示 + "忽略阻断并通过"按钮）。([T2-Q3])
- **`topic6_hitl.py` send_hc_card**: 发卡前强制触发 `artifact_sync.sync_once` + `publisher.publish_hc_artifacts`，产物链接通过 `published_urls` 渲染到卡片按钮中；publisher 不可用或异常时降级为无链接卡片。
- **`_payload_summary_elements`**: 新增 `published_urls` 可选参数，有值时渲染飞书 open_url 按钮；`build_hc_card` / `build_hc_resolved_card` 签名透传。
- **`gateway.py` build_gateway**: 装配 `FeishuDocPublisher`；启动时幂等初始化根文件夹权限；`start_job` 后创建 job 飞书子文件夹并写入 `docs_folder_token`。
- **`pipeline_store.py`**: `PipelineJob` 新增 `docs_folder_token` 字段；DB 幂等补列；新增 `update_docs_folder_token()` 方法。
- **`config.py`**: `Topic6Config` 新增 `docs_folder_token` 字段，读取 `TOPIC6_DOCS_FOLDER_TOKEN` 环境变量。

### Fixed

- **URL 编码粘连**: Agent 将 JSON 结尾 `"}` 误拼进 online_url（`%22%7D`）时，自动解码并清理尾部非 URL 字符，不会误截正常 URL。([T2-Q5])
- **Recovery 路径误 reject**: 启动恢复时已有的 pending HC 事件不再被新增的 `wide_table_path` 校验误拒。

### Not Yet Implemented

以下问题已完成方案设计，待后续版本实施：

- 进度历史可回溯：双轨制——卡片实时更新 + 飞书时间线文档全过程记录。([T1-Q5])
- 飞书文档统一管理：文档注册表 + 命名规范，区分 draft/review/final。([T2-Q2], [T2-Q4b])
- HC3 飞书文档反向同步至 Session 沙箱（当前 Agent 已通过 lark-cli 自行回读，视 V2.0 排期决定是否 Gateway 侧增强）。([T2-Q4a])

---

### Files Changed

**Modified (11)**

| File | Summary |
|------|---------|
| `topic6_runner.py` | URL 清洗, Phase H 诊断, `_diagnose_interruption`, `validate_hc_payload` 扩展 |
| `topic6_hitl.py` | remark 表单, @通知, HC1/HC2 回读提示, session_id 展示, P0-2 publisher 集成 + `published_urls` 卡片渲染 |
| `topic6_progress_card.py` | session_id 展示, `build_error_diagnosis_card` |
| `gateway.py` | `/log` 命令, 结构化错误信息, session_id, P0-2 publisher 装配 + 启动权限初始化 + job 子文件夹创建 |
| `pipeline_store.py` | P0-2 `docs_folder_token` 字段 + DB 列 + `update_docs_folder_token()` |
| `config.py` | P0-2 `docs_folder_token` 字段 + `TOPIC6_DOCS_FOLDER_TOKEN` 环境变量 |
| `arkagent/feishu.py` | P0-2 Drive API: `add_permission`, `create_folder`, `import_spreadsheet` |
| `coordinator.system.md` | HC1/HC2 回读飞书表格指令, DataHub 模型名更新 |
| `annotator.system.md` | DataHub 模型名更新 |
| `validator.json` | 模型 → claude-sonnet-5-5 via bmc-llm-relay |
| `environment.json` | `DATAHUB_MODEL_ID` → `DeepSeek-V4-Flash` |

**Added (6)**

| File | Summary |
|------|---------|
| `feishu_doc_publisher.py` | P0-2 HC 产物→飞书可编辑文档发布器 |
| `tests/test_topic6_doc_publisher.py` | 27 条 P0-2 集成测试 |
| `tests/test_topic6_fix_regression.py` | 43 条回归测试 |
| `cases/topic6/run_tests.sh` | 一键测试脚本 |
| `cases/topic6/tools/verify_feishu_docs.py` | 飞书权限验证 |
| `cases/topic6/tools/verify_feishu_pipeline.py` | 飞书 Pipeline 全链路验证 |

### Test Results

```
Total: 255 passed, 0 failed (2.87s)
  Existing: 175 passed (no regressions, including original 121 + 43 fix regression + 11 feishu verify)
  New (P0-2): 27 passed (doc publisher integration suite)
  Digital employee + config + gateway: 80 passed (cross-module no regression)
```

[T1-Q1]: #t1-q1-主流程中断1
[T1-Q2]: #t1-q2-主流程中断2
[T1-Q3]: #t1-q3-hc产物不可见
[T1-Q4]: #t1-q4-审核后无通知
[T1-Q5]: #t1-q5-卡片覆盖历史
[T2-Q1]: #t2-q1-缺少询问机制
[T2-Q2]: #t2-q2-多个输出文档
[T2-Q3]: #t2-q3-输出格式不符
[T2-Q4a]: #t2-q4a-修改后不同步
[T2-Q4b]: #t2-q4b-命名不统一
[T2-Q5]: #t2-q5-报告ui无法输出
[T2-Q6]: #t2-q6-skill未调通
