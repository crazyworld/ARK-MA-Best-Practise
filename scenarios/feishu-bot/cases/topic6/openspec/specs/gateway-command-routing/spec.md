# gateway-command-routing Specification

## Purpose
飞书入站消息与卡片回调的入口：过滤、去重、鉴权、命令分发、触发词解析与 Gateway 组件装配。

源码：`gateway.py`（`Topic6Gateway`、`build_gateway`、`_byok_model_headers`）、`config.py`（`load_case_config`）、`topic6_runner.py`（`normalize_user_text`、`parse_trigger`）、`env.example`。

## Requirements

### Requirement: 配置加载
`load_case_config` SHALL 校验必填环境变量 `ARK_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`TOPIC6_COORDINATOR_AGENT_ID`、`TOPIC6_ENVIRONMENT_ID`，缺失时 MUST 抛 `RuntimeError("缺少环境变量:<k1>, <k2>")`。可选项与默认值：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 去尾 `/` |
| `TOPIC6_MEMORY_STORE_ID` | 空 | 空则 Session 不挂 Memory |
| `TOPIC6_PIPELINE_DB_PATH` | 空 | 见 pipeline-job-store |
| `TOPIC6_ARTIFACT_SYNC_DIR` | `cases/topic6/data/artifacts` | |
| `TOPIC6_ARTIFACT_POLL_INTERVAL_SEC` | `60` | MUST > 0，否则 `RuntimeError` |
| `ARK_VAULT_ID` | 空 | 共享 Vault |
| `AUTHORIZED_OPEN_IDS` | 空=不限 | 逗号或空白分隔 |
| `TOPIC6_SKIP_MIAODA_OAUTH` | false | `1/true/yes` 为真 |
| `BYOK_MODEL_AUTH_HEADER_NAME/VALUE` | 空 | 两者都填才生效；coordinator 走 BYOK 时必填 |
| `TOPIC6_DOCS_FOLDER_TOKEN` | 空 | HC 产物发布根文件夹 |
| `TOPIC6_LARK_BOT_VAULT_ID` | 空 | 空则按名字自动置备 |

新增配置项 MUST 同步更新 `config.py` 的 `Topic6Config`、`env.example` 和 `TOPIC6_DEBUG_QUICKSTART.md`。

#### Scenario: 缺必填项
- **WHEN** 未设置 `TOPIC6_ENVIRONMENT_ID`
- **THEN** 启动失败并提示 `缺少环境变量:TOPIC6_ENVIRONMENT_ID`

#### Scenario: 轮询间隔非法
- **WHEN** `TOPIC6_ARTIFACT_POLL_INTERVAL_SEC=0`
- **THEN** 抛出 `RuntimeError`

### Requirement: 入站过滤与事件去重
`accept(message)` SHALL 仅处理文本非空且（单聊 `p2p` 或群聊 @机器人）的消息；MUST 通过 `GatewayStore.claim_event(event_id)` 去重，重复事件返回 `False`。处理 MUST 通过 `loop.call_soon_threadsafe` 投递到事件循环异步执行，不阻塞飞书回调线程。每个分支结束 MUST 调用 `complete_event(event_id, "completed")`，未捕获异常时 MUST 标记 `"failed"`。

#### Scenario: 群聊未 @
- **WHEN** 群聊消息未 @机器人
- **THEN** `accept` 返回 `False`，不做任何处理

#### Scenario: 飞书重投
- **WHEN** 同一 `event_id` 被投递两次
- **THEN** 第二次 `claim_event` 失败，不重复启动任务

### Requirement: 白名单鉴权
当 `AUTHORIZED_OPEN_IDS` 非空且发送者不在其中时，系统 SHALL 回复"当前用户未授权。请联系管理员把你的 open_id 加入白名单。"并结束处理，MUST NOT 进入任何命令分支。

#### Scenario: 非白名单用户触发
- **WHEN** 白名单为 `ou_a`，`ou_b` 发送"热点周报 demo"
- **THEN** 回复未授权提示，不创建 Session

### Requirement: 命令分发顺序
文本 MUST 先经 `normalize_user_text` 去除开头的 `@xxx`，再按以下固定顺序匹配，命中即返回：

1. `/new` → 取消当前会话键的活跃任务（见 session-runner）；取消后停止该 job 的产物同步并做一次最终 `sync_once`
2. `/continue` 或 `/resume` [补充消息]（正则 `^/(?:continue|resume)(?:\s+(.*))?$`，忽略大小写）→ 注入补充消息，缺省为"继续"；若任务已因历史 `online_url` 收口为 `done`，回复发布链接
3. `/log [session_id]` → 列出 Session 事件摘要（>4000 字截断）；无参数时取**全局**最新活跃任务，没有则取最近一条任务的 Session
4. `/test_feishu` → 用 tenant token 在 Drive 建文件夹/表格/上传文件后删除，返回各步结果
5. `/test_phase_f` → 以子进程运行 `publish_report_to_feishu.py` 的 mock 数据（超时 120s，剔除 `LARKSUITE_CLI_*` 环境变量）验证 fallback 链路
6. HC 备注正文（`hitl.handle_remark_message`）
7. 触发词（`parse_trigger`）
8. 以上都不命中 → 回复用法提示（列出 demo/smoke/full/skip_sampling、/continue、/new、/log）

新增命令 MUST 插入此顺序并更新用法提示文案。

#### Scenario: /continue 带补充
- **WHEN** 用户发送 `/continue A`
- **THEN** 注入的 user.message 为 `A`

#### Scenario: 备注优先于触发词
- **WHEN** 用户刚点了"备注"按钮，随后发送含"热点周报"的文字
- **THEN** 该文字作为 HC 备注处理，不启动新任务

### Requirement: 触发词与模式解析
`parse_trigger(text)` SHALL 在文本包含 `热点报告` 或 `热点周报` 时返回模式，否则返回 `None`。模式按以下优先级在小写文本中匹配：

| 优先级 | mode | 关键词 |
|---|---|---|
| 1 | `skip_sampling` | `skip_sampling` `skip-sampling` `跳过采样` `跳过抽样` |
| 2 | `demo` | `demo` `演示` |
| 3 | `smoke` | `smoke` `冒烟` `冒烟测试` `快速测试` `test` `小样本` `试跑` |
| 4 | `full` | `full` `完整` `正式` |
| 5 | `full` | 无模式词（默认） |

#### Scenario: 默认模式
- **WHEN** 文本为"热点周报"
- **THEN** 返回 `full`

#### Scenario: 兼容旧 test
- **WHEN** 文本为"热点周报 test"
- **THEN** 返回 `smoke`

### Requirement: 启动前凭据就绪
触发命中后，系统 SHALL：`TOPIC6_SKIP_MIAODA_OAUTH` 为真时以空用户 Vault 启动并记录 warning；否则调用 `_ensure_user_authorized` 获取用户 Vault（见 credential-management）。随后 MUST 调用 `_wait_bot_vault_ready`（最长 30s），再调用 `runner.start_job`，成功后启动该 job 的产物同步。`Topic6RunnerError` / `UserAuthorizationRequired` MUST 以原文回复用户。

#### Scenario: 已有活跃任务
- **WHEN** 全局已有 `running` 任务时再次触发
- **THEN** 回复"当前已有热点周报任务运行中（mode=…，phase=…）。请等待前一个任务结束后再发起。"

#### Scenario: 未预期异常
- **WHEN** 处理过程抛出其他异常
- **THEN** 回复 `❌ 执行异常（job=…, phase=…）：<原因前300字>`，附 session_id 与 `/log` 提示，并建议 `/continue` 或 `/new`

### Requirement: 启动恢复
`recover_active_jobs()` SHALL 在 Gateway 启动后依次：幂等初始化飞书共享文件夹权限（失败只 warning）→ 启动常驻 token 刷新协程 → 调用 `runner.recover_active_jobs()` → 为仍处于 `running|wait_hc` 的任务重启产物同步。恢复过程 MUST NOT 发送 `/continue` 或任何用户消息。

#### Scenario: 重启时任务在等审核
- **WHEN** Gateway 重启时某任务为 `wait_hc`
- **THEN** 重建同一张审核卡，状态保持 `wait_hc`，不向 Session 注入消息

### Requirement: BYOK 模型 Header
`_byok_model_headers` SHALL 仅当 `BYOK_MODEL_AUTH_HEADER_NAME` 与 `VALUE` 均非空、且 `ma-resources/agents/coordinator.json` 的 `model.provider` 非空时返回 `{"X-User-Model-Header-<name>": <value>}`；`provider` 为空（方舟原生模型）时 MUST 返回 `None`；`provider` 非空但 `BYOK_MODEL_AUTH_HEADER_*` 缺失时 MUST 抛错使 Gateway 启动失败（否则每次模型请求都被网关拒绝）；读取 JSON 失败时保留 Header 并记 warning。该 Header 只随 `send_message` 动态附加，MUST NOT 写入方舟侧持久配置。

#### Scenario: 切回方舟原生模型
- **WHEN** coordinator.json 的 `model` 无 `provider` 字段
- **THEN** 不附加 BYOK Header

#### Scenario: relay 模型缺鉴权配置
- **WHEN** coordinator.json 的 `model.provider=bmc-llm-relay` 且未配置 `BYOK_MODEL_AUTH_HEADER_VALUE`
- **THEN** Gateway 启动失败并提示缺少 BYOK Header 配置

### Requirement: 组件装配
`build_gateway` SHALL 装配：`GatewayStore`（case 级 event 去重库）、`PipelineStore`、`Topic6UserAuthorization(FeishuOAuth)`、`JobNotifier`、`Topic6Runner`（`vault_ids` = 非空的 `TOPIC6_LARK_BOT_VAULT_ID`、`ARK_VAULT_ID`）、`FeishuDocPublisher`、`Topic6ArtifactSync`、`Topic6Hitl`，并 MUST 调用 `runner.bind_card_sender(hitl)` 以避免 runner ↔ hitl 循环 import。

#### Scenario: HITL 未绑定
- **WHEN** runner 未绑定 card sender 且出现 HC
- **THEN** 任务仍进入 `wait_hc`，并回复文本提示用 `/continue` 推进
