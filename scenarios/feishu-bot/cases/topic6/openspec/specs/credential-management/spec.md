# credential-management Specification

## Purpose
管理 Topic6 运行所需的两类飞书凭据，并以方舟 Vault 注入 MA 沙箱：
- **Bot 身份**（tenant_access_token）：Phase F 用 `lark-cli --as bot` 发布飞书文档；
- **用户身份**（user_access_token，每用户独立 Vault）：Phase H 妙搭发布必须使用用户 OAuth。

App Secret 只保留在 Gateway 主机；沙箱只拿短期 token。

源码：`gateway.py`（token 刷新协程、`_ensure_user_authorized`）、`topic6_user_oauth.py`（`FeishuOAuth`、`Topic6UserAuthorization`）、`authorize_miaoda_user.py`、`../../arkagent/lark_vault.py`（共享库，不属于本 case）。

## Requirements

### Requirement: Bot tenant token Vault
Gateway SHALL 维护一个只含环境变量凭据 `LARKSUITE_CLI_TENANT_ACCESS_TOKEN` 的 Bot Vault：
- 配置了 `TOPIC6_LARK_BOT_VAULT_ID` 时原地更新该 Vault；否则用 `ensure_lark_cli_vault(name_hint="topic6-bot")` 按名字 `ark-topic6-bot-tenant-token-v3-<app_id>` 幂等查找或创建。
- 首次就绪后 MUST 调用 `runner.add_vault_id`（放在 vault 列表最前），并设置就绪事件。
- token 剩余有效期 > `BOT_TOKEN_REFRESH_MARGIN_S=1800s` 时不重复申请（飞书在最后 30 分钟才签发新 token，旧 token 仍有效，故无空窗）。
- 新任务启动前 MUST 等待 Bot Vault 就绪最多 `BOT_VAULT_READY_TIMEOUT_S=30s`；超时只 warning 并继续（该 Session 不含 bot token，Phase F 走 `feishu_api_fallback`），因为 Session 创建后无法追加 Vault。

#### Scenario: 首次启动自动置备
- **WHEN** 未配置 `TOPIC6_LARK_BOT_VAULT_ID`，Gateway 启动
- **THEN** 按名字查找或创建 Bot Vault，写入 tenant token，并挂到之后每个新 Session

#### Scenario: 置备超时
- **WHEN** 用户触发时 Bot Vault 30s 内仍未就绪
- **THEN** 任务照常启动并记录 warning

### Requirement: 常驻 token 刷新协程
Gateway SHALL 在启动恢复时开启唯一的常驻协程（幂等），每 `TOKEN_REFRESH_INTERVAL_S=60s` 执行一次：刷新 Bot token；`TOPIC6_SKIP_MIAODA_OAUTH` 未开启时遍历 `user_oauth` 全部用户调用 `refresh_in_background`，跳过 `refresh_token == ""` 的用户。单项失败只记日志，协程 MUST NOT 因异常退出（`CancelledError` 除外）。

#### Scenario: 一个用户刷新失败
- **WHEN** 用户 A refresh 失败，用户 B 正常
- **THEN** B 仍被刷新，协程继续下一轮

### Requirement: 用户 OAuth 授权（Device Flow）
`Topic6UserAuthorization.authorize` SHALL 以 scopes `offline_access`、`auth:user.id:read`、`spark:app:read`、`spark:app:write` 发起飞书 Device Flow，轮询拿到 token 后读取扫码者 `open_id`；指定 `expected_open_id` 且不一致时 MUST 拒绝且不写任何凭据。access_token 超过 `MAX_VAULT_SECRET_BYTES=4096` 字节 MUST 报错。成功后写入用户 Vault 并 `save_user_oauth`。

授权入口有两个：
- 预授权脚本 `python cases/topic6/authorize_miaoda_user.py [--expected-open-id ou_xxx]`（在 `scenarios/feishu-bot/` 执行）；
- 聊天内授权：触发时用户无可用授权，Gateway 回复授权链接（5 分钟有效），同一 `open_id` 同时只有一个授权流程，后续触发复用并提示"正在等待你完成飞书授权"；超过 `IN_CHAT_AUTH_TIMEOUT_S=300s` 取消并要求重新发送触发词；成功回复"✅ 授权成功，正在继续执行任务…"。

#### Scenario: 扫错账号
- **WHEN** 聊天内授权时由另一人扫码
- **THEN** 拒绝写入，回复授权失败，需重新发送触发词

#### Scenario: 授权超时
- **WHEN** 用户 5 分钟内未扫码
- **THEN** 回复"授权链接已超时（5 分钟），请重新发送触发词获取新的授权链接。"

### Requirement: 用户 Vault 与凭据结构
每个用户 SHALL 拥有独立 Vault，名称 `ark-topic6-miaoda-user-<规范化open_id>`（截断 100 字符），优先复用已有同名 Vault。凭据类型 MUST 为 `environment_variable`，`secret_name=LARKSUITE_CLI_USER_ACCESS_TOKEN`，显示名 `topic6-miaoda-user-access-token`。凭据类型/secret_name 不可原地修改，重建时 MUST 先删除同 secret_name 的旧凭据再新建，Vault ID 保持不变。access_token MUST NOT 落本地 SQLite。

#### Scenario: 历史 env_oauth 凭据迁移
- **WHEN** 本地记录 `expires_at == 0`（历史平台托管凭据）
- **THEN** 下次刷新时删旧建新为 `environment_variable` 凭据

### Requirement: 用户 token 刷新
`refresh_if_needed(open_id, margin_ms)` SHALL：
- 无记录或 `refresh_token` 为空 → `UserAuthorizationRequired`；
- `expires_at - now > margin` 且非 `0` → 直接返回 Vault ID；
- 否则在 per-user `asyncio.Lock` 内双重检查后刷新。refresh_token 一次性：飞书刷新成功后 MUST **先**落库新 refresh_token 并把 `expires_at` 记为 `1`（待重试），**再**更新方舟凭据，成功后写真实 `expires_at`。写方舟失败时下一轮用新 refresh_token 重试，授权链不丢失。
- refresh_token 剩余 < 2 天时 warning。

`vault_id(open_id, force_refresh=True)`（任务触发时使用）SHALL 无视本地过期时间真实刷新一次，失败抛 `UserAuthorizationRequired` 以当场触发聊天内授权，避免拖到 Phase H 才暴露。常驻协程使用 `REFRESH_EARLY_MS=15min` 窗口。

#### Scenario: 写方舟失败
- **WHEN** 飞书刷新成功但 `update_environment_credential` 抛异常
- **THEN** 本地已保存新 refresh_token 且 `expires_at=1`，下一轮立即重试

#### Scenario: 触发时 refresh_token 已失效
- **WHEN** 用户发送触发词，强制刷新失败
- **THEN** Gateway 发起聊天内扫码，不启动 Session

### Requirement: 连续失败标记重新授权
`refresh_in_background` SHALL 统计每个用户连续失败次数，达到 `MAX_REFRESH_FAILURES=3` 时 `mark_reauthorization_required`：清空本地 refresh_token、保留 Vault/Credential ID，常驻协程不再重试。任一次成功清零计数。

#### Scenario: 连续 3 次失败
- **WHEN** 同一用户后台刷新连续失败 3 次
- **THEN** `refresh_token` 置空，之后刷新协程跳过该用户

### Requirement: 临时跳过妙搭授权
`TOPIC6_SKIP_MIAODA_OAUTH=true` 时，系统 SHALL 不获取用户 Vault 直接启动并记录 warning，且常驻协程不刷新用户 token。此时 Phase H 预期失败；该开关 MUST 只用于临时演示。

#### Scenario: 演示模式
- **WHEN** 开关开启后触发 demo
- **THEN** Session 只挂 Bot/共享 Vault，流程可走到 Phase H 并因缺少用户凭据失败
