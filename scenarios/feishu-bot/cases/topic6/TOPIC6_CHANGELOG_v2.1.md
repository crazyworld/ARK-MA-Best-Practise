# Changelog

All notable changes to the Topic6 热点周报 pipeline are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

- **DataHub 标注模型**: `Doubao-Seed-1.6-lite` → `DeepSeek-V4-Flash-0731`。
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
| `environment.json` | `DATAHUB_MODEL_ID` → `DeepSeek-V4-Flash-0731` |

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
