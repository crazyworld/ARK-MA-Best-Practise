# Topic6 v2.1 上线步骤

> 本次改动涉及三层：方舟 MA 资源（Environment + Agent）、Gateway 本地代码、飞书应用配置。
> 依赖关系：飞书配置 → 方舟资源更新 → Gateway 重启，必须按序执行。

---

## 0. 前置条件确认

```bash
# 在线上机器（/home/ada/agents/ada-claw/workspace）确认当前环境
cd /home/ada/agents/ada-claw/workspace/scenarios/feishu-bot

# 确认没有正在运行的任务（有的话等完成或 /new 取消）
# 确认 .env 或 环境变量文件位置
cat ../config.toml   # 或对应的环境变量配置文件
```

---

## 1. 飞书应用权限与文件夹配置（一次性）

### 1.1 飞书应用新增权限

在 [飞书开放平台](https://open.feishu.cn/) 的应用管理后台，确认以下权限已开通：

| 权限 | 用途 | 操作 |
|------|------|------|
| `drive:file:upload` | 上传 xlsx 到飞书 Drive | 检查是否已有 |
| `drive:folder:create` | 创建子文件夹 | 检查是否已有 |
| `drive:permission:member:create` | 给文件夹/文档添加协作者 | **本次新增** |

> 如果应用已有 `docs:permission.member:create`（Phase F 创建报告时用），与 Drive 权限是不同的 scope，需要两个都开。

### 1.2 创建飞书共享根文件夹

1. 在飞书「云文档」中创建一个文件夹，例如 **「Topic6 热点周报产物」**
2. 从浏览器地址栏获取 folder token：
   ```
   https://bluefocus.feishu.cn/drive/folder/fldcnXXXXXXXXX
                                            ^^^^^^^^^^^^^^^^
                                            即为 TOPIC6_DOCS_FOLDER_TOKEN
   ```
3. 在该文件夹右上角「分享」→ 将飞书应用（FEISHU_APP_ID 对应的机器人）加为 **「管理者」**
   - 这使 app token 有权在文件夹下创建子文件夹和文档
   - 白名单用户的权限由代码在启动时自动添加（文件夹级继承）

### 1.3 记录 token

```
TOPIC6_DOCS_FOLDER_TOKEN=fldcnXXXXXXXXX
```

---

## 2. 环境变量配置

本次涉及三组环境变量，按作用域区分：

### 2.1 Gateway 本地环境变量（config.toml / .env / systemd）

| 变量 | 值 | 说明 | 必填 |
|------|-----|------|------|
| `TOPIC6_DOCS_FOLDER_TOKEN` | `fldcnXXXXXXX` | 步骤 1.2 获取的飞书文件夹 token | **本次新增**，不填则 HC 产物发布功能不启用（其他功能不受影响） |
| `BYOK_MODEL_AUTH_HEADER_NAME` | `Authorization` | BYOK 模式鉴权 header 名 | 已有（commit 533d74a） |
| `BYOK_MODEL_AUTH_HEADER_VALUE` | `Bearer sk-xxxx` | bmc-llm-relay 的 API Key | 已有（commit 533d74a） |

> `BYOK_MODEL_AUTH_HEADER_*` 是之前 BYOK 合入时已配置的，本次不需要改动。
> 它的作用是让 Gateway 在每次调 MA Session API 时附加 `X-User-Model-Header-Authorization: Bearer sk-xxxx`，
> 方舟运行时会把这个 header 透传给 Agent 的 BYOK 模型端点。

### 2.2 方舟 Environment 沙箱环境变量（environment.json → create_all.sh 展开）

这些变量在 `create_all.sh` 执行时通过 `${VAR}` 展开注入方舟 Environment，进入 Agent 沙箱：

| 变量 | 当前值 | 本次改动 | 说明 |
|------|--------|----------|------|
| `LLM_RELAY_API_KEY` | `sk-xxxx` | **无改动** | bmc-llm-relay API Key，沙箱内的 C2 relay 脚本使用 |
| `LLM_RELAY_BASE_URL` | `https://bmc-llm-relay.bluemediagroup.cn/v1` | **无改动** | bmc-llm-relay 端点 |
| `DATAHUB_MODEL_ID` | ~~`Doubao-Seed-1.6-lite`~~ → `DeepSeek-V4-Flash` | **本次改动** | DataHub 标注任务的模型 ID |
| `C2_CHAT_MODEL_ID` | `Doubao-Seed-Evolving` | 无改动 | C2 事件归并的 Chat 模型 |
| `EMBEDDING_MODEL_ID` | `Doubao-embedding` | 无改动 | 向量嵌入模型 |

> `DATAHUB_MODEL_ID` 已在 environment.json 中硬编码为 `DeepSeek-V4-Flash`，
> create_all.sh 不会用 `${DATAHUB_MODEL_ID}` 环境变量展开（它是固定值不是占位符），
> 所以**本地不需要 export 这个变量**。

### 2.3 方舟 Agent 模型配置（agent JSON → create_all.sh 创建）

4 个 Agent 全部走 bmc-llm-relay：

| Agent | 模型 | provider | 本次改动 |
|-------|------|----------|----------|
| coordinator | claude-sonnet-5-5 | bmc-llm-relay | 无改动（commit 533d74a 已切） |
| annotator | claude-sonnet-5-5 | bmc-llm-relay | 无改动 |
| insighter | claude-sonnet-5-5 | bmc-llm-relay | 无改动 |
| validator | claude-sonnet-5-5 | bmc-llm-relay | **本次改动**（从 doubao-seed-2-1-pro 切换） |

> Agent JSON 中的 `base_url` 和 `provider` 是方舟 MA 侧的配置，在 `create_all.sh` 时写入。
> Gateway 本地不需要配这些——但 Gateway 需要 `BYOK_MODEL_AUTH_HEADER_*` 来在每次
> send_message 时附加鉴权 header（方舟透传给 bmc-llm-relay）。

---

## 3. 确认本地 export 的环境变量

在**执行 create_all.sh 的 shell** 中，以下变量必须 export（create_all.sh 会用 `${VAR}` 展开写入 Environment）：

```bash
# 已有，确认仍然 export
export ARK_API_KEY="***"
export FEISHU_APP_ID="***"
export FEISHU_APP_SECRET="***"
export HOT_TOPICS_MCP_URL="***"
export DATAHUB_ENDPOINT="***"
export DATAHUB_API_KEY="***"
export BLUEAI_API_KEY="***"
export ARK_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"

# BYOK 相关（commit 533d74a 已有）
export LLM_RELAY_API_KEY="***"
export LLM_RELAY_BASE_URL="https://bmc-llm-relay.bluemediagroup.cn/v1"

# 方舟资源 ID（复用已有）
export TOPIC6_ENVIRONMENT_ID="***"
export TOPIC6_COORDINATOR_AGENT_ID="***"

# 本次新增（可选）
export TOPIC6_DOCS_FOLDER_TOKEN="fldcnXXXXXXXXX"
```

---

## 4. 拉取代码

```bash
cd /home/ada/agents/ada-claw/workspace
git pull origin main    # 或你的部署分支
```

---

## 5. 更新方舟 MA 资源

```bash
cd scenarios/feishu-bot/cases/topic6

# 一条命令完成：全量测试 → Skill 打包上传 → Environment 更新 → Agent 重建 → 资源 ID 校验
./update_ma.sh
```

`update_ma.sh` 内部依次执行 5 步：
1. `pytest -q` 全量测试（失败则中止）
2. `pack_skills.sh` 打包 7 个 Skills
3. `upload_skills.py --force` 强制上传
4. `create_all.sh --update-env --update-memory` 更新 Environment（`DATAHUB_MODEL_ID` 值变更）+ Memory + 重建 4 个 Agent（validator 模型切换生效）
5. 校验 `created_ids.json` 完整性 + 回写 Coordinator 新 ID 到 `config.env`

> 注意：`update_ma.sh` 不重启 Gateway，脚本结束后需手动重启（见步骤 8）。

**验证资源更新成功：**

```bash
# 检查 Environment 的 DATAHUB_MODEL_ID
curl -sS "$ARK_BASE_URL/environments/$TOPIC6_ENVIRONMENT_ID" \
  -H "Authorization: Bearer $ARK_API_KEY" \
  -H "X-Ark-Beta: agentic-2026-06-01" | jq '.data.config.env.DATAHUB_MODEL_ID'
# 期望输出: "DeepSeek-V4-Flash"

# 检查 Coordinator Agent 的模型
curl -sS "$ARK_BASE_URL/agents/$TOPIC6_COORDINATOR_AGENT_ID" \
  -H "Authorization: Bearer $ARK_API_KEY" \
  -H "X-Ark-Beta: agentic-2026-06-01" | jq '.data.model'
# 期望输出: {"id": "claude-sonnet-5-5", "provider": "bmc-llm-relay", ...}
```

---

## 6. 配置 Gateway 环境变量

在 Gateway 的环境变量配置文件中新增：

```bash
# config.toml 或 .env 或 systemd unit 的 Environment= 行
TOPIC6_DOCS_FOLDER_TOKEN=fldcnXXXXXXXXX
```

> 不加这个变量 = HC 产物发布功能不启用，其他所有功能正常运行。
> 可以先不加、部署验证其他功能后再加。

---

## 7. 运行测试（update_ma.sh 已自动执行，此处为手动补验）

> `update_ma.sh` 的第 1 步已经运行了 `pytest -q` 全量测试，通过才会继续后续步骤。
> 如果需要单独验证，可以手动执行：

```bash
cd /home/ada/agents/ada-claw/workspace

PYTHONPATH=scenarios/feishu-bot:scenarios/feishu-bot/cases/topic6 \
  python -m pytest scenarios/feishu-bot/tests/test_topic6*.py \
                   scenarios/feishu-bot/tests/test_gateway.py \
                   scenarios/feishu-bot/tests/test_config_store.py \
                   -q

# 期望: 200+ passed, 0 failed
```

---

## 8. 重启 Gateway

```bash
# 停止当前 Gateway
# （具体方式取决于部署方式：systemctl / pm2 / 手动进程）

# 启动 Gateway
# 日志中应该出现：
#   topic6 init docs folder permissions: 正在初始化... (如果配了 TOPIC6_DOCS_FOLDER_TOKEN)
#   或无此日志（如果没配，正常）
```

---

## 9. 线上验证

### 9.1 基本功能验证（不需要 TOPIC6_DOCS_FOLDER_TOKEN）

```
群里发: 热点周报 demo
```

确认：
- [ ] 任务启动正常，进度卡片显示 session_id
- [ ] HC1 卡片出现（含"通过""打回""补充说明"三个按钮）
- [ ] 点"通过"后群里收到通知消息（`HC1 审核结果：通过，Pipeline 继续运行中`）
- [ ] `/log` 命令返回 Session 事件日志
- [ ] 如果 Pipeline 中断，错误信息包含 job_id、phase、session_id

### 9.2 HC 产物发布验证（需要 TOPIC6_DOCS_FOLDER_TOKEN）

HC1 卡片中应出现 **「📝 打开编辑 wide_table」** 按钮：
- [ ] 按钮链接格式为 `https://bluefocus.feishu.cn/sheets/shtcnXXX`
- [ ] 点击后飞书在线表格可打开、白名单用户有编辑权
- [ ] HC3 卡片中出现 **「📝 打开编辑 report」** 按钮

如果 HC 卡片没有"打开编辑"按钮但其他功能正常 → 检查：
1. `TOPIC6_DOCS_FOLDER_TOKEN` 是否配置
2. Gateway 日志是否有 `topic6 create job docs folder` 日志
3. Gateway 日志是否有 `topic6 publish_hc_artifacts failed` 错误

### 9.3 飞书 API 验证工具（可选，详细诊断用）

```bash
cd scenarios/feishu-bot/cases/topic6

# 验证飞书权限可达
python tools/verify_feishu_docs.py

# 模拟完整 Pipeline 写入
python tools/verify_feishu_pipeline.py
```

---

## 10. 回滚方案

本次改动全部向后兼容，回滚分两层：

**只回滚 HC 产物发布**（保留其他改动）：
```bash
# 删除或置空环境变量
unset TOPIC6_DOCS_FOLDER_TOKEN
# 重启 Gateway
```

**完全回滚代码**：
```bash
git revert HEAD   # 或 git checkout <旧 commit>
./create_all.sh --update-env   # 恢复方舟资源
# 重启 Gateway
```

---

## 附录：环境变量完整清单

| 变量 | 作用域 | 本次状态 | 不配会怎样 |
|------|--------|----------|-----------|
| `TOPIC6_DOCS_FOLDER_TOKEN` | Gateway 本地 | **新增** | HC 产物发布不启用，其他功能正常 |
| `BYOK_MODEL_AUTH_HEADER_NAME` | Gateway 本地 | 已有 | BYOK 鉴权不附加，Agent 会用方舟默认模型（可能报错） |
| `BYOK_MODEL_AUTH_HEADER_VALUE` | Gateway 本地 | 已有 | 同上 |
| `LLM_RELAY_API_KEY` | 方舟沙箱 | 已有 | C2 relay 脚本无法调用 bmc-llm-relay |
| `LLM_RELAY_BASE_URL` | 方舟沙箱 | 已有 | 同上 |
| `DATAHUB_MODEL_ID` | 方舟沙箱(硬编码) | **值变更** | N/A（硬编码在 environment.json 中） |
