# diagnose · 排障 Bot(MVP)

在飞书群里 @ 它,问"这个任务为什么失败 / 卡在哪 / 这一步的输入输出是什么"。

## 它能做什么

- 找到**当前群**最近的任务,给出首个异常点和原因(确定性诊断,不让模型自己翻事件找原因)
- 追问某一步时,展示那一步的输入输出正文(密钥已脱敏,过长截断并标注原长度)
- 同样的诊断结论也可以用命令行得到:`python -m arkagent trace show <id>`

## 范围(MVP)

| 项 | 规则 |
|---|---|
| 群聊 | 可排查**本群全部**任务,不区分发起人 |
| 私聊 | 只能查**自己发起、且发生在私聊里**的任务,用租户级 `user_id` 判断(`open_id` 按应用隔离,不能用)。应用须开通 `contact:user.id:readonly`,否则取不到 `user_id`,私聊会回复无法确认身份。**升级前创建的任务没有 `user_id`,私聊查不到**(不回填,没有可靠依据) |
| 覆盖的 bot | topicx、topic5、topic6(`task_runs` / `pipeline_jobs`);**分析师、ada-pm**(通用 `Gateway` 的 `conversations` + `session_history`,各 case 一个库);**数字员工**(`sessions` + `session_history`) |
| 对象类型 | **任务**(topicx 系)有状态和 `last_error`;**会话**(分析师 / ada-pm / 数字员工)跨多轮、没有成败状态,`status` 只有 `active`(在用)/ `archived`(被 `/new` 归档),`archived` 不代表出错 |
| 部署 | 与被排查的 bot **同机**,直接读本机的库;不考虑分布式 |

权限在工具层强制:chat_id 取自飞书消息,工具参数里没有这个字段,传了会被拒;越权的任务一律回"没找到",不暴露其存在。

## 启动

```bash
conda activate topic6
cd scenarios/feishu-bot

# 1. 创建飞书应用并写入 ~/.arkagent/cases/diagnose/config.env(前四项)
python -m arkagent init --case diagnose

# 2. 创建 MA 资源(Environment + Agent),把打印出的两个 ID 追加到 config.env
export ARK_API_KEY=...
bash cases/diagnose/ma-resources/create_all.sh

# 3. 参考 cases/diagnose/env.example 填各 bot 的库路径(至少一个;未配置的 bot 不参与排查)

# 4. 启动
python -m arkagent run --case diagnose
```

## 已知限制

- **不读 gateway 日志**:当前基线日志不落盘,也没有统一的任务标识,读不了。"我发的命令没反应"这类没有 session 事件的问题查不到。
- **多智能体看不到子 Agent 内部**:主 session 事件流里只有"派发"和"回传"。
- **升级前创建的数据**没有 `chat_type` / `user_id`:群范围按 `chat_id` 精确匹配放行(依赖"私聊 chat_id 不外泄"这一假设,不是强校验),私聊范围一律不放行。
- 数字员工的私聊归属从 `session_memory_scopes`(单聊 `scope_type=user`,`scope_id` 即 user_id)反推;没有该记录的私聊会话查不到。
- 方舟在 session 14 天无新事件后会清理,之后只能看数据库侧状态。
- 事件只在提问时实时拉取,不落库。

完整规划见 `docs/OBSERVABILITY_PLAN.md`。

## session 日志落飞书文档(可选)

每个 session 一份文档,每轮结束后追加新增部分,可直接转发给用户。这是**交付视图**,不是分析依据:
写入可能失败或缺段,排障 Bot 的结论始终以事件流和数据库为准。

| 项 | 说明 |
|---|---|
| 开启 | 在**被排查 bot**(通用 `Gateway` 类)的 config.env 里设 `SESSION_DOC_PARENT_TOKEN=<父节点 token>`,并在其 `build_gateway` 里加 `after_turn=make_after_turn(ark, case_name=..., db_path=..., app_id=..., app_secret=...)`;不设则完全不启用 |
| 身份 | 以**业务应用自己的身份**写(用 gateway 进程里已有的 `app_id` / `app_secret`,经 SDK 直调 OpenAPI),**不依赖 `lark-cli`**:lark-cli 只绑定一个应用,它的 bot 身份不是业务应用 |
| 权限 | **继承父节点权限,请事先手动配好**:该应用须是 wiki 空间成员(可编辑),且应用身份有 `docx:document:create`、`docx:document:write_only`。程序不调用"加协作者"接口(高风险写操作) |
| 失败 | 永远不影响用户收到回复,只记 warning 日志;失败那次不算数,下一轮会补写 |
| 幂等 | 写入进度记在本机 SQLite(`SESSION_DOC_DB_PATH`),重启、重放不会重复追加 |
| 内容 | 用户消息、Agent 回复、工具调用与结果、错误、子 Agent 消息;不含 thinking;密钥已脱敏;单条超 3000 字截断并标注原长度 |

> 已用分析师以外的 ada-pm 应用真实写入并回读验证:创建、追加、脱敏、幂等均正确(2026-11-12)。
>
> 文档**已经写出去就无法收回**(会被索引、缓存)。脱敏规则是启发式的,漏掉的凭据形态会随文档外泄,
> 所以只对你能控制访问范围的文件夹启用。

## 上线前权限检查

`arkagent run --case <任意 case>` 启动时自动检查**本应用的应用身份(tenant)权限**,缺什么明确告知,**不阻断启动**:

| 输出 | 含义 |
|---|---|
| `权限自检:… 均已开通 ✓` | 正常 |
| `[权限缺失] 应用 … 未开通(应用身份):…` | 缺权限,附影响说明、需发布新版本的提示、开通直达链接 |
| `[权限未确认] …` | 查不到本应用的权限(网络、接口报错、只开了用户身份权限),**不代表缺权限** |

检查项按该 bot 实际启用的能力决定:

| 能力 | 需要的应用身份权限 |
|---|---|
| 私聊鉴权(租户级 user_id) | `contact:user.employee_id:readonly` |
| session 日志写飞书文档(仅配置了 `SESSION_DOC_PARENT_TOKEN` 时检查) | `docx:document:create`、`docx:document:write_only` |

实现:`GET /open-apis/application/v6/scopes`,用**该应用自己的 tenant token** 调用,无需任何探测对象。

> **一个必须知道的坑**:权限分**用户身份(user)**和**应用身份(tenant)**两套,bot 只认 tenant。
> 同名权限只在 user 下开通、tenant 下没有时,调用仍会被拒(2026-11-12 实测分析师应用即是如此:
> `docx:document:create` 只开了 user 身份,写文档报 99991672)。
> `lark-cli auth check` / `auth scopes` 查的是**用户身份**(实测不支持 `--as bot`),**不能代替本检查**。
> 开通后需在开放平台**发布新版本**才生效。

读取 `user_id` 所需的权限是 `contact:user.employee_id:readonly`(实测:按 `user_id` 类型查通讯录时飞书服务端直接报缺这一项);`contact:user.id:readonly` 在这个场景下不是必需的。

另外排障 Bot 收到第一条没有 `user_id` 的私聊时,会在日志里再告警一次。

## session 日志落飞书文档(可选)

每个 session 一份文档,每轮结束后追加新增部分,可直接转发给用户。这是**交付视图**,不是分析依据:
写入可能失败或缺段,排障 Bot 的结论始终以事件流和数据库为准。

| 项 | 说明 |
|---|---|
| 开启 | 在**被排查 bot**(通用 `Gateway` 类)的 config.env 里设 `SESSION_DOC_PARENT_TOKEN=<父节点 token>`,并在其 `build_gateway` 里加 `after_turn=make_after_turn(ark, case_name=..., db_path=..., app_id=..., app_secret=...)`;不设则完全不启用 |
| 身份 | 以**业务应用自己的身份**写(用 gateway 进程里已有的 `app_id` / `app_secret`,经 SDK 直调 OpenAPI),**不依赖 `lark-cli`**:lark-cli 只绑定一个应用,它的 bot 身份不是业务应用 |
| 权限 | **继承父节点权限,请事先手动配好**:该应用须是 wiki 空间成员(可编辑),且应用身份有 `docx:document:create`、`docx:document:write_only`。程序不调用"加协作者"接口(高风险写操作) |
| 失败 | 永远不影响用户收到回复,只记 warning 日志;失败那次不算数,下一轮会补写 |
| 幂等 | 写入进度记在本机 SQLite(`SESSION_DOC_DB_PATH`),重启、重放不会重复追加 |
| 内容 | 用户消息、Agent 回复、工具调用与结果、错误、子 Agent 消息;不含 thinking;密钥已脱敏;单条超 3000 字截断并标注原长度 |

> 已用分析师以外的 ada-pm 应用真实写入并回读验证:创建、追加、脱敏、幂等均正确(2026-11-12)。
>
> 文档**已经写出去就无法收回**(会被索引、缓存)。脱敏规则是启发式的,漏掉的凭据形态会随文档外泄,
> 所以只对你能控制访问范围的文件夹启用。

## 上线前权限检查

`arkagent run --case <任意 case>` 启动时会自动检查当前应用能否读到租户级 `user_id`,并在启动输出里明确告知:

| 输出 | 含义 |
|---|---|
| `权限自检:contact:user.employee_id:readonly 已开通 ✓` | 正常 |
| `[权限缺失] 应用 … 未开通:…` | 缺权限,附影响说明和开通直达链接;**不阻断启动** |
| `[权限未确认] …` | 无法判断(没配 `AUTHORIZED_OPEN_IDS`、网络失败、探测对象不在通讯录范围内),**不代表缺权限** |

读取 `user_id` 所需的权限是 **`contact:user.employee_id:readonly`**(2026-11-12 对真实应用实测:按 `user_id` 类型查通讯录时,飞书服务端直接报缺这一项)。
`contact:user.id:readonly` 在这个场景下**不是**必需的。开通后需在开放平台**发布新版本**才生效。

另外排障 Bot 收到第一条没有 `user_id` 的私聊时,会在日志里再告警一次(启动探测可能是"未确认")。

## 诊断口径:按原始场景,不做汇总

`findings` 逐条列出每一次异常,**不合并、不截断**,每条带自己的轮次和事件 id,可单独追问。
(曾做过"重复警告合并为一条",已按用户要求撤销:长会话里每一轮的重试都可能对应不同原因,合并会丢掉逐轮的事件 id。)
