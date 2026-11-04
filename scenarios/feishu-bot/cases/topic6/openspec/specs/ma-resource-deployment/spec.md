# ma-resource-deployment Specification

## Purpose
Topic6 在方舟 MA 上的资源（7 个 Skill、Environment、Memory Store、4 个 Agent）的打包、上传、创建/更新与 ID 回写流程，以及多环境隔离约定。

源码：`update_ma.sh`、`tools/pack_skills.sh`、`tools/upload_skills.py`、`ma-resources/create_all.sh`、`ma-resources/environment.json`、`ma-resources/memory-store.json`、`ma-resources/memory/topic6/*.md`、`ma-resources/skill_ids.json(.example)`、`ma-resources/created_ids.json`、`ma-resources/version_mapping.md`。

## Requirements

### Requirement: 唯一源码目录
`ma-resources/` SHALL 是部署资源的唯一源码目录。`tools/out/*.zip|*.sha256`、`skill_ids.json`、`created_ids.json` 为生成物（不入库），MUST NOT 手工修改。客户原始文件（`assets_from_customer/`）只作为迁移基线。

#### Scenario: 直接改 zip
- **WHEN** 开发者修改 `tools/out/topic6-insight.zip`
- **THEN** 下次 `pack_skills.sh` 会覆盖该修改；正确做法是改 `ma-resources/skills/topic6-insight/`

### Requirement: 全量更新入口
`update_ma.sh` SHALL 作为资源更新的标准入口（在 `cases/topic6/` 执行），步骤：加载 `${TOPIC6_CONFIG_FILE:-~/.arkagent/cases/topic6/config.env}` → 校验 `ARK_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`HOT_TOPICS_MCP_URL`、`BLUEAI_API_KEY`、`DATAHUB_ENDPOINT`、`DATAHUB_API_KEY`、`LLM_RELAY_BASE_URL`、`LLM_RELAY_API_KEY`、`BYOK_MODEL_AUTH_HEADER_NAME`、`BYOK_MODEL_AUTH_HEADER_VALUE` 与命令 `curl jq rsync zip shasum` → 使用 conda 环境 `${TOPIC6_CONDA_ENV:-topic6}` → `1/5` 在 `scenarios/feishu-bot` 运行 pytest 全量测试 → `2/5 pack_skills.sh` → `3/5 upload_skills.py --force` → `4/5 create_all.sh --update-env --update-memory` → `5/5` 校验 `created_ids.json` 全部非空且 config.env 中 `TOPIC6_COORDINATOR_AGENT_ID/ENVIRONMENT_ID/MEMORY_STORE_ID` 已回写。任一步失败 MUST 报告失败步骤并退出。脚本 MUST NOT 停止或重启 Gateway；结束后 Gateway MUST 手动重启以加载新 Coordinator ID。

ID 变化规则：Skills 与 4 个 Agent 每次重建（ID 变化）；Environment、Memory Store 原地更新（ID 不变）；飞书 App 与 Vault 不由本脚本管理。

#### Scenario: 测试失败
- **WHEN** 第 1 步 pytest 失败
- **THEN** 脚本退出，不上传 Skill

#### Scenario: 更新后未重启
- **WHEN** 运行 update_ma.sh 后未重启 Gateway
- **THEN** 新 Session 仍使用旧 Coordinator ID（可能已被删除），必须重启

### Requirement: Skill 打包约束
`tools/pack_skills.sh` SHALL 打包 7 个 Skill：`topic6-fetch-normalize`、`topic6-annotation`、`topic6-insight`、`topic6-event-registry`、`topic6-web-report`、`miaoda-web-publish`、`validator`。打包时 rsync 排除 `__pycache__`、`.DS_Store`、`*.pyc`、`node_modules`、`.git`，并 MUST 校验：zip ≤ 30 MiB、单文件 ≤ 30 MiB、解压总大小 ≤ 120 MiB、文件数 ≤ 500、所有文件位于同一顶层目录且其下直接有唯一 `SKILL.md`。产出 `tools/out/<name>.zip` 与 `.sha256`。新增 Skill MUST 同时登记到 `SKILLS` 数组、`upload_skills.py`、相应 Agent JSON 与 `version_mapping.md`。

#### Scenario: 文件超限
- **WHEN** 某 Skill 解压后超过 500 个文件
- **THEN** 打包失败

### Requirement: Skill 上传与版本
`tools/upload_skills.py [--force] [--only <key>]` SHALL 上传 zip 到 SkillHub，未变化（sha256 一致）时默认跳过，`--force` 强制重传；结果写入 `ma-resources/skill_ids.json`（`{skill_id, version, source_sha256}`）。Agent JSON 通过 `${skill_id.<name>}` / `${version.<name>}` 占位引用，MUST pin 明确 version，MUST NOT 使用 latest。上传后 SHALL 由维护人同步 `version_mapping.md`。

#### Scenario: 只改了一个 Skill
- **WHEN** 仅修改 topic6-insight 并运行 `upload_skills.py --only topic6-insight`
- **THEN** 只上传该 Skill，`skill_ids.json` 只更新该项

### Requirement: 资源创建脚本
`ma-resources/create_all.sh [--update-memory] [--update-env]` SHALL 使用方舟 MA beta header 调用 API，并按顺序：
1. Environment：优先复用 `TOPIC6_ENVIRONMENT_ID`，失效再按 name 查找（全部分页），否则创建；`--update-env` 时原地更新 config。`environment.json` 中 `${VAR}` 占位 MUST 由脚本用 jq 预先展开（方舟不做二次插值，未导出变量替换为空串）。
2. Memory Store：按 name 查找复用，首次创建时上传 `memory-store.json` 声明的 3 个文件（`MEMORY.md`、`_版本状态.md`、`错误案例库.md` → `/topic6/…`）；复用时默认不覆盖，`--update-memory` 才强制覆盖。
3. 校验 `skill_ids.json` 已填充。
4. 渲染 Agent JSON（替换 skill/version/`HOT_TOPICS_MCP_URL`/子 Agent ID），同名旧 Agent 先删后建，顺序：annotator → validator → insighter → coordinator。
5. 写 `created_ids.json`，并回写 `~/.arkagent/cases/topic6/config.env` 中的 3 个 Gateway 必需 ID。

沙箱侧使用 `LARK_APP_ID/SECRET`，Gateway 使用 `FEISHU_*`，默认复用同一套应用凭据。

#### Scenario: Environment ID 失效
- **WHEN** config.env 中的 `TOPIC6_ENVIRONMENT_ID` 已被删除
- **THEN** 按 name 查找或新建，并回写新 ID

#### Scenario: Memory 已存在
- **WHEN** 不带 `--update-memory` 重跑
- **THEN** 保留线上 Memory 内容

### Requirement: 多环境隔离
同一方舟账号运行多套 Topic6 时，SHALL 设置 `TOPIC6_RESOURCE_SUFFIX`（如 `-dev`），所有 Agent/Environment/MemoryStore 名称追加该后缀；留空保持原名称。按 name 删除/复用资源是全账号范围，未设置后缀的多套环境 MUST NOT 共用同一账号。

#### Scenario: 两人共用账号
- **WHEN** A 设置 `-alice`、B 设置 `-bob`
- **THEN** 两人的 `topic6-coordinator-alice` 与 `topic6-coordinator-bob` 互不删除

### Requirement: 沙箱环境定义
`environment.json` SHALL 声明：`cloud` 类型、网络 `unrestricted`；pip 依赖 `pandas>=2.0`、`openpyxl>=3.1`、`httpx>=0.27`、`requests>=2.31`、`pyyaml>=6.0`、`numpy>=1.24`、`openai>=1.0`、`python-dateutil>=2.8`、`lunardate>=0.2.2`；`setup_script` 按架构下载固定版本 lark-cli（v1.0.95，SHA-256 校验）到 `/usr/local/bin`；环境变量 `TZ=Asia/Shanghai`、`PYTHONUNBUFFERED=1`、`DATAHUB_ENDPOINT/API_KEY`、`LARK_APP_ID/SECRET`、`LARKSUITE_CLI_APP_ID=${LARK_APP_ID}`、通知关闭项、`BLUEAI_API_KEY`、`LLM_RELAY_API_KEY/BASE_URL`（沙箱模型统一走 bmc-llm-relay；MUST NOT 注入 `ARK_API_KEY/BASE_URL`，方舟凭据只留在 Gateway/部署侧）、`DATAHUB_MODEL_ID=DeepSeek-V4-Flash`、`C2_CHAT_MODEL_ID=Doubao-Seed-Evolving`、`EMBEDDING_MODEL_ID=Doubao-embedding`、`EMBEDDING_IS_MULTIMODAL=false`。短期 token（tenant/user access token）MUST NOT 写入 Environment，只能通过 Vault 注入。升级 lark-cli MUST 同步更新两个架构的 SHA-256。

#### Scenario: 新增沙箱变量
- **WHEN** 新增 `FOO_API_KEY`
- **THEN** 写入 `environment.json` 的 `config.env`（`${FOO_API_KEY}` 占位）、config.env 样例，并以 `--update-env` 更新

### Requirement: Memory 只读与权威源
Memory Store SHALL 只由 Gateway 侧部署脚本写入，Agent 只读（memory 工具）。`_版本状态.md` 是各 Skill `SKILL.md` 版本段的镜像，冲突时以 Skill 为准并同步 Memory。

#### Scenario: Prompt 升版
- **WHEN** C0 升到 v6
- **THEN** 更新 SKILL.md、`_版本状态.md`、`version_mapping.md`，并以 `--update-memory` 同步
