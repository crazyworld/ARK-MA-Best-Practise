# testing-and-verification Specification

## Purpose
Topic6 的测试组织、运行入口、spec ↔ 测试映射，以及本地/线上排查工具。保证每次变更都能用确定性测试证明契约未被破坏。

源码：`scenarios/feishu-bot/tests/test_topic6_*.py`（22 个文件）、`cases/topic6/run_tests.sh`、`cases/topic6/tests/test_phase_f_local.{py,sh}`、`cases/topic6/tools/{session_triage.py, verify_feishu_docs.py, verify_feishu_pipeline.py, mock_project.py, manual_artifact_sync.py}`、`cases/topic6/reset_pipeline_state.py`。

## Requirements

### Requirement: 测试位置与运行入口
Topic6 单元/回归测试 SHALL 放在 `scenarios/feishu-bot/tests/test_topic6_*.py`，在 `scenarios/feishu-bot/` 下以 conda 环境 `topic6`（Python ≥ 3.10）运行。`cases/topic6/run_tests.sh` SHALL 提供：`all`（默认，全部 topic6 测试）、`fix|quick`（`test_topic6_fix_regression.py`）、`full`（详细输出）、`-k …` 等自定义 pytest 参数。测试 MUST NOT 依赖真实方舟/飞书/DataHub 网络（用 fake client / mock 数据）。

#### Scenario: 常规回归
- **WHEN** 修改任一 Gateway 或 Skill 脚本
- **THEN** 运行 `./run_tests.sh` 全部通过后才可执行 `update_ma.sh`

#### Scenario: 缺少 conda 环境
- **WHEN** 找不到 `topic6` 环境
- **THEN** 脚本提示创建命令并退出 1

### Requirement: spec 与测试映射
每个 capability 的行为变更 SHALL 在对应测试文件中增加或修改用例：

| capability | 主要测试文件 |
|---|---|
| gateway-command-routing | `test_topic6_config.py`、`test_topic6_new_command.py`、`test_topic6_ma_resources.py` |
| session-runner | `test_topic6_fix_regression.py`、`test_topic6_hc_fallback.py`、`test_topic6_interrupt_recovery.py`、`test_topic6_new_command.py` |
| pipeline-job-store | `test_topic6_fix_regression.py`、`test_topic6_reset_pipeline_state.py` |
| hitl-review | `test_topic6_hitl_operator.py`、`test_topic6_hc_sheet_roundtrip.py`、`test_topic6_doc_publisher.py`、`test_topic6_hc_fallback.py` |
| credential-management | `test_topic6_user_oauth.py`、`test_topic6_interrupt_recovery.py` |
| artifact-sync | `test_topic6_artifact_sync.py`、`test_topic6_doc_publisher.py` |
| coordinator-orchestration / sub-agents | `test_topic6_parity_gaps.py`、`test_topic6_pipeline_optimizations.py`、`test_topic6_ma_resources.py` |
| skill-fetch-normalize | `test_topic6_mock_nodes.py`、`test_topic6_parity_gaps.py` |
| skill-annotation | `test_topic6_annotate_parse_retry.py`、`test_topic6_smoke_base_table.py`、`test_topic6_publish_node_artifacts.py`、`test_topic6_degraded_route.py`、`test_topic6_mock_nodes.py`、`test_topic6_parity_gaps.py` |
| skill-event-registry | `test_topic6_e2e_smoke.py`、`test_topic6_pipeline_optimizations.py`、`test_topic6_interrupt_recovery.py` |
| skill-insight | `test_topic6_ma1027_regression.py`、`test_topic6_pipeline_optimizations.py`、`test_topic6_e2e_smoke.py`、`cases/topic6/tests/test_phase_f_local.*` |
| skill-web-publish | `test_topic6_web_report_alignment.py`、`test_topic6_ma1027_regression.py` |
| ma-resource-deployment | `test_topic6_ma_resources.py` |

新增 capability MUST 在本表登记测试文件。Prompt 类契约（coordinator.system.md 的固定命令/禁止项）SHALL 以字符串断言测试覆盖关键片段，防止误删。

#### Scenario: 修改触发词
- **WHEN** 修改 `parse_trigger` 关键词
- **THEN** 同步更新 gateway-command-routing spec 与 `test_topic6_new_command.py`

### Requirement: 端到端验证
涉及 MA 运行时行为（Prompt、Skill 脚本、Session 交互）的变更 SHALL 在单测通过并 `update_ma.sh` 部署、重启 Gateway 后，用飞书发送 `热点周报 smoke`（覆盖 HC1+HC2+HC3 全路径，30 条）或 `热点周报 demo` 完成一次端到端验收；仅影响 Phase F 发布时可先用 `/test_phase_f`、`/test_feishu` 验证。验收后 MUST 用 `tools/session_triage.py <session_id>` 检查时间线、报错、HC 与成本摘要。

#### Scenario: 修改 Phase E Prompt
- **WHEN** 新增 E3 v3
- **THEN** 部署后跑一次 smoke，并用 session_triage 确认 E3 无失败与自校验 fail

### Requirement: 排查与恢复工具
系统 SHALL 提供：
- `tools/session_triage.py <session_id> [--events-file] [--full]`：拉取事件输出时间线/报错/HC/成本，原始事件写入 out-dir，不打印密钥；
- `tools/manual_artifact_sync.py`：手动同步 Session 产物（见 artifact-sync）；
- `tools/mock_project.py --mock-dir --out --round {sample|full}`：按 manifest sha256 把扁平 mock 产物还原为项目目录树，供本地节点测试；
- `tools/verify_feishu_docs.py`、`tools/verify_feishu_pipeline.py`：飞书权限/发布链路真实 API 验证；
- `reset_pipeline_state.py`：清理本地 `topic6_pipeline.db` 中因中断遗留的活跃任务，解除"已有任务运行中"阻塞。

工具 MUST 从 `~/.arkagent/cases/topic6/config.env`（可用 `TOPIC6_CONFIG_FILE` 覆盖）读取凭据且不输出密钥。

#### Scenario: 本地状态卡死
- **WHEN** Gateway 异常退出后新触发一直提示"已有任务运行中"，而 MA Session 已终止
- **THEN** 先 `/new` 或 `/continue`；仍无法恢复时运行 `reset_pipeline_state.py`

### Requirement: 变更记录
行为变更 SHALL 同步记录到 `TOPIC6_CHANGELOG.md`；部署/配置步骤变化同步 `TOPIC6_DEBUG_QUICKSTART.md`；文件新增/拆分/合并同步 `CUSTOMER_TO_MA_FILE_MAPPING.md` 与 `ma-resources/version_mapping.md`。使用 openspec 后，`TOPIC6_CHANGELOG.md` 条目 SHOULD 引用对应 change-id。

#### Scenario: 归档 change
- **WHEN** `openspec archive <id>` 完成
- **THEN** CHANGELOG 中有对应条目且引用 change-id
