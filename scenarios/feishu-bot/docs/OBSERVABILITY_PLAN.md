# ada 全链路可观测:现状、缺口与分期规划

> 状态:P0 已实现并提交 · 创建:2026-11-02 · 分支:`feat/trace-p0`(基于 `origin/main` `cc74a03`) · 排障 Bot 方案见 §11
> 对应需求(玉新 / ada FDE):分析师(内部运营)能通过看板看到每次任务的执行链路;也能通过 CLI / 接口取到观测数据,交给 Agent 分析。

## 1. 背景与目标

ada 现在有 topic5(复盘报告)、topic6(热点周报)两个 topic 能力,由 topicx 统一入口经 router 分发。任务出问题时(例如 BYOK 下 `model_request_failed_error`、HC 卡住、工具超时),排查靠人工翻 SQLite、日志和方舟后台,没有统一入口。

目标:

1. 一个任务的完整执行链路可还原:`飞书消息 → router → 业务 session → 每轮 user message → 模型调用 → 工具调用 → 工具内部的外部调用`。
2. 同一份数据同时服务三类使用者:分析师看板、CLI / HTTP 接口、群聊诊断 Agent(独立飞书应用)。三者口径一致。
3. 新增 topic 只需写一个 adapter,不改看板和接口。

约束:

- **部署形态(2026-11-12 决策,取代此前"必须支持分布式"的约束)**:当前阶段不考虑分布式。排障 Bot 与被排查的 bot 同机部署,直接读本机的库和日志。这是用户对全局规则的明确例外,仅限本需求;持久化仍通过 `TraceSink` 接口隔离,以便日后迁移,但不为多机场景付出额外设计成本。详见 §11。
- 分析师是内部运营,不是所有人都应看到原文;token、凭据必须脱敏,原文分级授权。
- 观测层放在通用层(`arkagent/`),不反向依赖 `cases/*`。

## 2. 现状:已有的关联手段

```
飞书 event_id
  └─ task_runs.task_id                      (topicx/control_store.py)
       ├─ router_session_id
       └─ business_session_id
            └─ pipeline_jobs.job_id ── ma_session_id   (topic5 / topic6)
                 ├─ pipeline_hc_events
                 └─ pipeline_receipts       (仅 topic5)
```

`task_id` 的传递方式:

| 方式 | 位置 | 作用 |
|---|---|---|
| session `title = "topicx/{task_id}/{scenario}"` | `topic5_runner.py`、`topic6_runner.py` | 方舟侧可按 title 反查;`_find_unique_session` 用它在超时后找回 session |
| 环境变量 `TOPICX_TASK_ID` | 同上,经 `create_session(env_overrides=)` | 沙箱内脚本可读到(目前未确认有人使用) |
| `control_events.correlation_id / causation_id` | `topicx/control_store.py` | 控制面事件因果 |

事件消费有两条路径,都**不保存原始事件**:

- `ArkClient.run()`(router 使用):只收集 `agent.message` 文本,处理 `agent.custom_tool_use`。
- 各 runner 的 `_consume_events`(topic5/6 业务 session):`event_progress` 把工具事件压成一行进度文字塞进卡片,刻意丢弃参数。

## 3. 验证事实(2026-10-30 / 2026-11-02 实测,只读)

样本:`scenarios/data/topic6_pipeline.db` 中的真实 job,经 `GET /sessions/{id}/events` 拉取原文。

| 层 | 事件流里有什么 | 实测 |
|---|---|---|
| 模型调用 | `span.model_request_start/end`;end 带 `model_request_start_id`、`model_usage{input,output,cache_*}`、`is_error` | 正常 session:40 次调用,输入约 245 万 / 输出约 1.4 万 token,单次平均 5.9s、最长 14.9s |
| 工具调用 | `agent.tool_use` ↔ `agent.tool_result`,靠 `tool_use_id` 配对,无耗时字段 | 39 对全部配对;工具名仅 `bash`(33)/`bash_wait`(2)/`memory_*`(4);最长一次 632s |
| 子 Agent | `session.thread_created`、`agent.thread_message_sent/received`、`session.thread_status_*` | 该 session 有子线程 `topic6-annotator-lp` |
| 轮次 | `user.message` → `session.status_idle`(`stop_reason`:`end_turn` / `retries_exhausted`) | 2 轮 |
| 失败 | `session.error{error.type,message,retry_status}` | 失败样本:3 次模型调用全部 `is_error`,usage 全 0,最终 `retries_exhausted` |
| 时间 | 每个事件有 `processed_at`(纳秒),整体单调 | 可直接算耗时 |

**字段随方舟版本变化(2026-11-12 修正)**:

- 模型名、模型调用级 request_id:**始终没有**(2026-11-12 复核仍无)。
- `session_thread_id` 在 `span.model_request_*` 上**因时间而异**:10-16 的 topic6 样本为 0/40,11-11 的 `ada-pm` / 分析师样本全部带(如 18/18),且 `agent.message`、`agent.thinking`、`agent.tool_*` 也都带。**方舟在 10-16 与 11-11 之间改过**,解析不能写死"span 不带线程 id",要按事件里实际有无字段处理。
- 新增事件 `session.usage`:每轮 `session.status_idle` 之前一条,含 `active_seconds`、input / output / cache token、`server_tool_use`。**是 session 累计值,不是本轮增量**(`ada-pm` 样本第 2 轮 usage=186410,而本轮 span 汇总仅 64089,差额 122321 恰为第 1 轮);统计 token 不得跨轮相加。
- 响应头 `x-request-id` 只对应"拉事件"这次 HTTP 请求。

**方舟保留期**:官方口径为"session 在 14 天无新事件后被清理"。2026-11-02 复核时 10-14、10-16 的 session 仍可读(已 17~19 天),清理看起来是惰性的,**不可依赖**,事后拉取要尽早落库(P1)。

方舟侧已答复"可以提供模型调用级 request_id",目前尚未在事件里出现,待方舟上线后补(见 §8)。

## 4. 链路分层

```
L1 飞书消息 → L2 task / router → L3 业务 job + session → L4 每轮 user message
   → L5 模型调用 → L6 工具调用 → L7 工具内部的外部调用(沙箱里的 lark-cli / DataHub / MCP)
```

## 5. 缺口清单与建议

| # | 层 | 缺失 | 建议 | 难度 | 期 |
|---|---|---|---|---|---|
| 1 | L5 | 事件流不落库,只能事后向方舟拉(保留期有限) | `ArkClient` 事件循环加统一 `on_event` 钩子,写 `session_events` | 低 | P1 |
| 2 | L5 | 事件无模型名,BYOK 与托管模型无法区分 | `create_session` 时记录 agent 的 model 配置到 trace 的 session 记录 | 低 | P1 |
| 3 | L5 | 模型 span 不归属子 Agent | 按 `session.thread_status_*` 区间推断,**标注为推断**;并发歧义时置空 | 中 | **P0** |
| 4 | L5 | 无模型调用级 request_id;`_capture_ma_request_id` 默认关闭 | 常态记录 POST `/sessions`、`/events` 的 `x-request-id` 并绑定 session / turn;模型级待方舟上线 | 低 + 外部依赖 | P1 |
| 5 | L4 | 无 turn 概念(首次 / `/continue` / HC 回复 / `retry_failed` 都是 `user.message`) | 本地生成 `turn_id`,`send_message` 时记录原因 | 低 | P1;P0 先按事件边界切分 |
| 6 | L6 | 工具无耗时、参数不留存 | 耗时由配对事件时间差得出;参数脱敏后存;卡片展示口径不变 | 中 | **P0**(耗时 / 脱敏) |
| 7 | L6 | 工具名过粗(全是 `bash`) | 解析 `command` 生成 `tool_category`(如 `lark-cli docs`),规则可配置 | 中 | **P0** |
| 8 | L7 | 沙箱内外部调用是黑盒,`TOPICX_TASK_ID` 已注入但未见使用 | skill 脚本公共入口读该变量,外部调用带 trace 头,调用摘要以约定标记行输出,由 Gateway 从 `tool_result` 收取 | 高 | P2 |
| 9 | L3 | 阶段靠解析 `agent.message` 里的 `[phase] X` | 短期写 `phase_transitions`(带事件 id);长期改结构化 | 短期低 | P0 解析 / P1 落库 |
| 10 | L2/L3 | topic5 / topic6 表结构不一致 | 通用层统一模型,case 写 adapter,不改原表 | 中 | **P0** |
| 11 | L1 | 飞书发送 / patch 出口未入库 | `FeishuSender` 出口记录 `message_id`、类型、成败 | 低 | P1 |
| 12 | 跨层 | 无 `task_id` 时(旧单 topic 入口)挂不上根 | 兜底以 `job_id` 为根 | 低 | **P0** |
| 13 | 部署 | 所有 store 都是本地 SQLite | `TraceSink` 抽象;P0 不持久化,天然无状态 | 中 | P0 设计 / P1 实现 |
| 14 | 安全 | 脱敏与分级未定义 | 元数据(全员)/ 参数摘要(脱敏)/ 原文(限权限);看板按角色展示 | 中 | P0 脱敏 / P3 分级 |

附带发现(非观测问题,但接上看板会暴露):

- **缓存命中为 0**:样本输入约 245 万 token,`cache_read_input_tokens` 全为 0,需确认 BYOK / 模型侧是否支持 prompt cache。
- **长耗时工具**:`bash_wait` 轮询可达 632s,诊断时要区分"真在跑"与"卡住"。

## 6. P0 设计

### 6.1 范围

只读、事后拉取、不改线上代码、不新增持久化:

- 从 SQLite(只读打开)读取 task / job / HC 记录,按 session id 调 `list_session_events` 拉事件,还原为 trace。
- 提供 CLI:`arkagent trace show <id>`、`arkagent trace list`。
- 覆盖缺口 3、6(耗时 + 脱敏)、7、9(解析)、10、12。

非目标(明确不做):落库、HTTP 接口、看板、诊断 Agent、skill 脚本埋点、改任何 runner。

### 6.2 模块(`arkagent/observability/`,通用层,不 import `cases/*`)

| 文件 | 职责 |
|---|---|
| `model.py` | `Trace` / `SessionNode` / `Turn` / `Span` 数据类及 `to_dict()`;span 种类:`model_call` `tool_call` `thread_message` `error` `hc_wait` |
| `redact.py` | 敏感信息脱敏(Bearer / Authorization / `--token` / 名称含 token、secret、key、password 的赋值) |
| `tool_category.py` | 把工具调用归类(`bash` 的 command 首个可执行名及子命令、`memory_*` 等) |
| `events.py` | 纯函数:MA 事件列表 → turn / span(配对、耗时、线程归属推断、阶段切换) |
| `adapters/base.py` | `TraceSource` 协议:`resolve(id) -> TraceRef`、`list_refs(...)` |
| `adapters/sqlite_sources.py` | topicx(`task_runs`)、topic5、topic6(`pipeline_jobs` / `pipeline_hc_events`)三个只读 adapter,`sqlite3` 以 `mode=ro` 打开 |
| `service.py` | `build_trace(ref, event_source)`:组装 + 汇总(调用数、token、错误数、耗时);session 已被清理时降级为"仅 DB 侧数据" |
| `render.py` | 文本时间线与 JSON 输出 |

CLI 在 `arkagent/cli.py` 增加 `trace` 子命令,复用 `ArkClient.list_session_events`。

### 6.3 数据模型要点

- `trace_id`:有 `task_id` 用之,否则退化为 `job_id`(缺口 12)。
- 每个 `Span` 含 `source`(`event` / `db` / `inferred`),区分直接观测与推断(线程归属、阶段)。
- 时间统一为毫秒时间戳;耗时 = 配对事件 `processed_at` 之差。
- 所有出口(CLI 文本 / JSON)默认经 `redact`;`--raw` 才输出未脱敏参数,且 P0 仅限本地 CLI。

### 6.4 数据库定位

adapter 通过参数 / 环境变量取库路径:`TOPICX_CONTROL_DB_PATH`、`TOPIC5_PIPELINE_DB_PATH`、`TOPIC6_PIPELINE_DB_PATH`,或 CLI `--db` 显式指定。通用层不 import case 代码获取默认路径。

### 6.5 测试与验收

- 单测:用结构脱敏后的事件夹具覆盖配对、耗时、失败 span、线程归属推断(含并发歧义置空)、脱敏、工具归类、session 已清理的降级。
- 验收:对 `scenarios/data/topic6_pipeline.db` 中的真实 job(含正常 / 模型失败两类)执行 `arkagent trace show`,结果与 §3 实测数值一致。

## 7. 后续分期

| 期 | 内容 | 对应缺口 |
|---|---|---|
| P1 | `on_event` 落库、`TraceSink` 持久化实现、request_id / turn_id / 模型名 / 阶段切换入库、飞书出口记录 | 1 2 4 5 9 11 13 |
| P2 | skill 脚本埋点与沙箱 trace 透传 | 8 |
| P3 | 脱敏分级、HTTP 接口、分析师看板(可参考 `digital-employee/admin/`)、独立飞书应用诊断 Agent | 14 |

诊断 Agent 只依赖 P0 查询接口,可与 P1 并行。

## 8. 待确认 / 外部依赖

1. **模型调用级 request_id**:方舟已答复可提供,2026-11-02 复核事件体尚未出现;上线后补 P1 缺口 4。
2. **方舟事件保留**:官方 14 天,实测偏宽松但不可依赖;P1 落库不宜推迟。
3. **缓存命中为 0**:仅 topic6 旧样本(10-16)成立;`ada-pm`(11-11)有大量命中,见 §11.11.4。是否因 BYOK 配置不同,需与模型侧确认。
4. **飞书文档同步**:本文档尚无对应飞书文档;若要对外评审,需另行创建。

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-02 | 初版:落盘现状、验证事实、14 项缺口、P0 设计与分期 |
| 2026-11-02 | P0 实现:`arkagent/observability/` + `arkagent trace list/show`;`tests/test_observability.py` 20 个用例,全量 582 通过;对真实 job 复验,数值与 §3 一致(40 次模型调用、245.5 万/1.4 万 token、39 次工具全配对、最慢 632s) |
| 2026-11-12 | 新增 §11.11:ada-pm(通用 Gateway、多智能体)与多智能体观测限制;**修正 §3**——`span` 是否带 `session_thread_id` 随方舟版本变化,新版全部带;发现 `session.usage` 为累计值、模型调用失败会被方舟自动重试;子线程窗口内看不到子 Agent 内部事件。同日新增 §11.10:读代码核实四类网关的会话键与命令分流(分析师复用通用 `Gateway`;数字员工为独立 `TopicSessionBot`,会话按话题共享);发现 `/new` 删除映射致旧 session 失联、`/new` 三种语义、通用网关缺 `user_id`。同日新增 §11 排障 Bot 方案:权限(私聊本人 / 群聊本群共享)、正文可见、同机部署不考虑分布式、`resolve_target` 识别规则、分层下钻控制模型成本、日志存储取舍;§1 约束改为同机部署 |

| 2026-11-12 | MVP 之后补三项:**私聊**(租户级 user_id,Scope 收敛权限)、**/new 归档**(session_history,通用 Gateway 与数字员工)、**session 日志落飞书文档**(bot 身份、继承文件夹权限、失败不外溢) |

| 2026-11-12 | 补三项:**adapter**(通用 Gateway 的 conversations / session_history、数字员工的 sessions,含 `/new` 归档的旧会话);**启动权限自检**(读 user_id 需 `contact:user.employee_id:readonly`,实测纠正此前"需 `contact:user.id:readonly`"的判断);长会话的重复警告合并为一条 |

| 2026-11-12 | 修正三处:**自检改用 `application/v6/scopes`**(查应用身份权限,不再需要探测对象;`lark-cli auth check` 只查用户身份);**文档写入改走业务应用自己的 SDK**(lark-cli 只绑定一个应用);**撤销重复警告合并**,按原始场景逐条分析 |

## 10. P0 已知限制

- 工具归类是启发式:多行 shell(`for` 循环、嵌套 heredoc)可能归到 `for` / `mkdir` 等,不影响耗时与计数。
- 线程归属为推断(`[线程~]`),子 Agent 与主 Agent 同时活跃时置空。
- 旧版本目录(`~/.arkagent/cases/topic6/`)不会被自动发现,需 `--env-file` / `--db` 指定;新版统一到 `~/.arkagent/cases/topicX/`。
- topic5 / topicx 的 adapter 只有单测覆盖,尚未用真实库验证(本机没有这两个库)。
- 事件只拉取不落库,方舟清理后即不可恢复,见 P1。

## 11. 排障 Bot 方案(2026-11-12 讨论结论)

### 11.1 目标

提供一个独立飞书应用的"排障 Bot":用户在群里或私聊里用自然语言提问,Bot 自动判断问的是哪个 session、哪个事件出了问题,依据该 session 的方舟 session log、gateway log 以及相关记录给出原因和位置。

### 11.2 已定决策

| 项 | 决策 |
|---|---|
| 覆盖范围 | gateway 上所有 bot(topicx、分析师研发环境、数字员工,以及排障 Bot 自身) |
| 部署 | **不考虑分布式**;排障 Bot 与被排查 bot 同机部署,直接读本机资源 |
| 权限(私聊) | 只能排查**自己名下**私聊 session |
| 权限(群聊) | 可排查**当前群内全部** session,不区分发起人,群内共享 |
| 内容可见性 | session log 除密钥脱敏外,**正文均可查看和出现在回答中** |
| 排障 Bot 自身 | 默认不分析自己正在进行的 session(避免提问产生的日志反过来进入下一次分析),仅在明确询问时读取历史 |

### 11.3 分层架构

```
第 3 层  排障 Bot(自然语言入口,独立飞书应用)
            │  只通过确定性工具取数,不直接读库 / 读日志
第 2 层  查询工具:resolve_target / trace show / why / logs / 取原文
            │  权限校验在此层强制,以飞书消息的 chat_id、user_id 为准,不接受 LLM 传参决定查谁的
第 1 层  数据层:各 bot 的 adapter、gateway 日志(落盘 + 统一标识)、事件留存、脱敏
```

`arkagent trace` 是第 2 层的雏形,Bot 的工具是对它的封装。

### 11.4 自动识别"问的是哪个 session / 事件"

识别由确定性的 `resolve_target` 完成,不交给 LLM 自由猜测,按优先级匹配:

| 优先级 | 提问方式 | 匹配依据 | 可靠性 |
|---|---|---|---|
| 1 | 引用 / 回复了 bot 的进度卡片或回复 | `reply_to_message_id` → `progress_card_message_id` / `card_message_id` → job | 确定 |
| 2 | 在同一话题里追问 | `thread_id` → `topic_thread_id` → task | 确定 |
| 3 | 直接给出 ID | task_id / job_id / session_id 精确匹配 | 确定 |
| 4 | "我刚才那个任务" | 提问人在当前会话内最近的任务;多个时必须反问 | 有歧义 |
| 5 | "上周三那个周报" | 时间 + 描述模糊匹配 | 不稳定,只给候选,不直接下结论 |

约束:返回"唯一命中 / 多个候选 / 未找到"三态;Bot 必须先复述识别结果("我理解你问的是 topic6 的 job_xxx,10-16 14:38 开始的热点周报")再给结论。选错 session 比答不出更糟。

"哪个事件出问题"由确定性逻辑(拟新增 `trace why`)先给出首个异常点,再由 LLM 解释,不让 LLM 在整段事件里自己找。

### 11.5 分析依据

| 依据 | 状态 |
|---|---|
| 方舟 session 事件流 | 已有(P0) |
| 数据库状态:`task_runs` / `pipeline_jobs` / HC 事件 / `user_oauth` 是否过期 | 已有(P0 覆盖前两类) |
| gateway 日志 | **缺**:当前基线日志只输出到终端不落盘;日志行仅约 24%(127 处中 30 处)带 job / session / task 标识 |
| 飞书出口记录(卡片是否发出、patch 是否成功、message_id 映射) | 缺 |
| 出问题时的 Agent / Environment 版本与模型(是否 BYOK) | 缺,创建 session 时未记录 |
| 方舟 `x-request-id` | 缺,待方舟提供模型级 ID |
| 历史相似故障库 | 可沉淀,参考 `错误案例库.md` |
| `conversations` 表(通用 bot 的 conversation_key → session) | 缺 adapter,P0 只覆盖 topicx / topic5 / topic6 |

### 11.6 控制模型成本:分层下钻,不全量灌入

一个正常 session 约 200 个事件、输入约 245 万 token,整段交给模型不可行。工具层按三步提供数据:

1. **汇总**:turn 数、模型调用数与失败数、最慢工具、首个异常点(几百字节)。
2. **异常点附近**:按首个异常点的时间取前后若干事件。
3. **按需取正文**:用户追问某一步的输入输出时,按事件 id 只取那一条。

### 11.7 日志存储:同机前提下的取舍

| 方案 | 说明 | 结论 |
|---|---|---|
| 本机文件 + SQLite | gateway 日志落盘(滚动),事件写本机库;排障 Bot 同机直读 | **当前阶段采用**,最简单 |
| 飞书文档(每 session 一份) | 从本机数据渲染,作为可交付视图 | 作为**交付物**,不作为分析输入:无法过滤聚合、整篇读取成本高、写入可能缺段 |
| SLS(`logstore-tracing`) | 已有 span 写入基础(`obs_pilot`),具备索引查询 | 当前不考虑;若日后需要多机或长期留存再评估。已知 span 内工具结果被截断到 2000 字符,不满足"正文均可查"时需另设完整事件存储 |

同机前提下,飞书文档的并发写入、跨实例去重问题也随之消失(同一 session 只由本机一个 gateway 进程持有)。

### 11.8 风险与未决项

1. **私聊身份**:飞书 `open_id` 按应用隔离,同一人在业务 Bot 与排障 Bot 下的 `open_id` 不同,私聊"自己名下"无法用 `open_id` 判断。需用跨应用稳定的 `user_id`(`IncomingMessage.user_id`,依赖应用开通"获取用户 user ID"权限)。**待核实**:各业务库是否记录了 `user_id`,没有则要补记录。
2. **群聊共享的边界**:"群聊内皆共享"意味着群成员能看到本群所有 session 的完整正文(含他人上传的业务数据);已按决策接受,但脱敏规则需覆盖密钥之外的常见凭据形态,且漏脱敏无法事后撤回。
3. **同机的代价**:排障 Bot 依赖被排查 bot 所在机器的文件系统和库,机器宕机或迁移时两者同时不可用;本机磁盘是日志的唯一副本。
4. **LLM 误判**:根因结论必须附证据位置(事件 id / 日志行),区分"观测到"与"推断"。
5. **文档限制未核实**:飞书文档单篇大小与写入频率限制,若要做交付视图需先确认。

### 11.9 路径

| 步 | 内容 | 说明 |
|---|---|---|
| 1 | 数据层:gateway 日志落盘 + 统一 `task= job= session=` 标识;`conversations` adapter;核实并补记录 `user_id`;`trace why` | 不依赖 Bot,CLI 同样受益 |
| 2 | `resolve_target` + 权限校验,先在 CLI 里验证识别准确率 | 第 4 点的核心 |
| 3 | 排障 Bot 骨架:独立飞书应用 + custom tool 调第 2 层,先只读 | 小范围试用 |
| 4 | 覆盖自身、历史故障库、飞书文档交付视图 | 最后做 |

### 11.10 各 bot 的会话 / 任务 / 命令分流(2026-11-12 读代码核实)

排障 Bot 要覆盖全部 bot,而它们的"会话 → session"关系差别很大,`resolve_target` 和权限校验必须按 bot 区分实现。

#### 11.10.1 四类网关

`arkagent run --case <名>` 加载 `cases/<名>/gateway.py`;同一飞书应用只允许一个 gateway 进程(`GatewayLock`)。不同 bot 走**不同的网关类**,不是同一个网关里运行时分流。

| 网关 | 用它的 case | 会话键(判定"同一个会话") | 库 | 有无 task / job |
|---|---|---|---|---|
| 通用 `Gateway`(`arkagent/gateway/orchestrator.py`) | **分析师**(`cases/analyst/gateway.py`)、**ada-pm**(`cases/ada-pm/gateway.py`),两者都直接 `from arkagent.gateway.orchestrator import Gateway` | `chat_id : thread_id : user_open_id` | **各 case 一个库**:`~/.arkagent/cases/analyst/gateway.db`、`~/.arkagent/cases/ada-pm/gateway.db` 的 `conversations`(`ada-pm` 不匹配 `topic\d+`,不并入 `topicX` 共享目录) | 无,只有会话 → session;**ada-pm 是多智能体**(1 主 + 4 子,见 §11.11) |
| `TopicSessionBot`(`cases/digital-employee/digital_employee.py`,1273 行,独立实现) | 数字员工 | 群聊:`tenant : chat_id : (thread_id 或 message_id)`,**不含用户**;私聊:`tenant : chat_id : ""` | `{case_dir}/topic_bot_sessions.db` 的 `sessions` | 无 |
| `Topic6Gateway` | topic6 单入口 | job 按 `(chat_id, thread_id, user_open_id)` 找活跃任务 | `data/topic6_pipeline.db` 的 `pipeline_jobs` | 无 task,直接 job |
| `TopicxGateway` | topicx(统一入口,topic5 / topic6) | task 按 `(chat_id, topic_thread_id)` 或 `(chat_id, root_message_id)` 找 | `topicx_control.db` 的 `task_runs` + 各 topic 的 `pipeline_jobs` | **有**:每条受理的消息一条 task;意图命中才有 job |

注:topic5 / topic6 / topicx 的 `gateway.py` 里**没有**使用通用 `Gateway`,它们各自实现了网关类(`Topic5Gateway` / `Topic6Gateway` / `TopicxGateway`)。

#### 11.10.2 一条消息怎么被分流

共同前置:`should_handle`(文本非空;私聊,或群聊中 @ 了 bot)→ 用 `event_id` 去重 → 白名单校验。

**通用 `Gateway._process`**(分析师)——纯字符串匹配,不经过模型:

| 输入 | 行为 | 是否产生 session 事件 |
|---|---|---|
| `/new` | `DELETE conversations` 中该键的行,下条消息建新 session | 否 |
| `/whoami` | 回复当前岗位 | 否 |
| `/remember <内容>` | 写用户专属 Memory Store | 否 |
| `/role <描述>` | 更新岗位缓存 | 否 |
| `/stop`(分析师分支新增) | **不进串行队列**,向正在运行的 session 发中断,等待确认 | 有(`user.interrupt`) |
| 其它文本 | 查 `conversations`:无则建 session 并回"已收到";有则复用;`send_message` 并等到终态,回复最后一条 `agent.message` | 是 |

**`TopicSessionBot`**(数字员工):`is_reset_command` 判 `/new`(会先剥掉开头连续的 `@名字` 再比对),命中则 `sessions` 表删除该键并**立即新建 session**;其它走正常对话,另有 `serial` / `native-queue` 两种执行模式,群聊话题创建后会把 `thread_id` 作为别名绑定到同一 session(`_bind_thread_session`)。

**`Topic6Gateway`**:

| 输入 | 行为 |
|---|---|
| `/new` | **取消**当前活跃 job(置 `failed:cancelled_by_user`),停 token 保活与产物同步。语义与通用网关的 `/new` **不同** |
| `/continue [补充]` | 找会话内最近可续跑的 job,注入"继续"或补充,重订阅进度 |
| 命中触发词 `parse_trigger`(热点周报 demo / full / skip_sampling) | 创建新 job |
| 其它 | 回提示,不创建任何记录 |

**`TopicxGateway`**:不判命令。

- 能按 `(chat_id, 话题 / 根消息)` 找到已有 task 且消息在话题内 → 追问,续用原 task;
- 否则新建 `task_runs` 与 **router session**,由 router 模型判意图并**必须**调用一次 `submit_business_intent`:`topic5`(营销复盘)/ `topic6`(热点周报)/ `unsupported`;
- `unsupported` 直接成为终态,回复"未创建业务任务",**没有 job、没有业务 session**;
- 路由工具禁止模型提交 `task_id` / `session_id` / `job_id` / `open_id` / `chat_id` / `lease_token`,身份字段由网关自己填;
- 停止是卡片按钮(`stop_handler`),不是文本命令(实现未读,仅知状态流转 `stopping → stopped`);
- topic6 全局单例租约 `topic6_lease`,冲突时 task 为 `busy`。

task 状态:活跃 `routing / routed / authorizing / acquiring / starting / running / wait_hc / paused / stopping / reconcile_required`;终态 `unsupported / stopped / done / failed / busy`。

#### 11.10.3 对排障设计的影响

| # | 发现 | 影响与处理 |
|---|---|---|
| 1 | **通用 `Gateway` 与数字员工的 `/new` 都直接删除映射行,无历史表**(`reset_session` / `sessions.reset`) | 用户常见路径是"结果不对 → `/new` → 问刚才为什么错",此时旧 session 在方舟侧还在,**本机已查不到它属于谁**。**必须在排障 Bot 上线前**给两处 reset 加归档(删除前把 `key → session_id` 写入历史表并带归档时间),这是对线上代码的小改 |
| 2 | 同样叫 `/new`,语义三种:通用网关 = 重置会话;数字员工 = 重置并新建;topic6 = **取消任务** | 排障 Bot 解释用户行为时必须按 bot 区分,不能一概而论 |
| 3 | `/whoami` `/remember` `/role` 不产生任何 session 事件 | 用户说"我刚才发的命令没反应",**在方舟事件里查不到**,只能查 gateway 日志。这再次说明日志落盘与统一标识是必须项 |
| 4 | **会话键是否含用户,各 bot 不一致**:通用 `Gateway` 含 `user_open_id`(同群不同人是不同 session);数字员工群聊按话题共享、不含用户;topicx 按话题 / 根消息 | 与你定的"群聊内共享"规则对照:数字员工天然吻合;**通用 `Gateway`(分析师)在同一个群里每个人是独立 session**,按"当前 chat_id 查"就要列出该群全部用户的 session。数据上能做到,但群成员因此能看到同群他人与分析师 Bot 的完整对话——**已按你的决策接受,需在文档中明示这一后果** |
| 5 | 数字员工的鉴权优先用 `user_id`(`is_authorized`),并已有 `session_user_tokens` / `session_vaults` 等表;通用 `Gateway` 只用 `user_open_id` 白名单 | 私聊"自己名下"的判断,数字员工有条件用 `user_id`;通用 `Gateway` 的 `conversations` 键里只有 `user_open_id`,**没有 `user_id`**。排障 Bot 是独立应用,`open_id` 不通用,所以通用 `Gateway` 的私聊鉴权现在**做不了**,需要补记录 `user_id` |
| 6 | 数字员工是独立实现,有自己的库(`topic_bot_sessions.db`)与表结构 | `resolve_target` 需要为它**单独写 adapter**,不能复用通用 `conversations` 的读法 |
| 7 | 通用 `Gateway` 里 `/stop` 登记在内存集合 `_active_runs` / `_stopped_runs` | 中断状态**只存在于进程内存**,重启即丢;排障时无法从库里判断"这轮是被用户主动中断还是失败",只能看方舟事件里的 `user.interrupt` |

#### 11.10.4 adapter 覆盖现状与待办

| bot / 存储 | P0 是否覆盖 | 待办 |
|---|---|---|
| topicx `task_runs` | 是 | — |
| topic5 / topic6 `pipeline_jobs` | 是 | — |
| 通用 `Gateway` `conversations`(分析师、ada-pm) | **否** | 新增 adapter,**支持多个库路径**(每个 case 一个库,不能假定只有一个);配合归档历史表读历史 session |
| 数字员工 `sessions` | **否** | 新增 adapter |
| 数字员工 / 通用 `Gateway` 的 `/new` 归档 | 不存在 | 改 `reset_session` / `sessions.reset`,删除前归档 |
| 通用 `Gateway` 记录 `user_id` | 不存在 | `conversations` 补列并在 `save_session` 写入 |

#### 11.10.5 仍未核实

- `stop_handler.py`(534 行)的实现细节,仅确认状态名;
- topic5 单入口 `Topic5Gateway` 的命令分流(只确认类存在,未读 `_process`);
- 分析师的会话键是否因 `scope_output_by_user_topic=True` 而改变:读到该参数只影响**产物存储路径**(按分析师 / Topic 拆分),不影响 `conversation_key`,但"Topic"的来源没有读。

### 11.11 ada-pm 与多智能体的观测限制(2026-11-12 实测)

#### 11.11.1 ada-pm 是什么

- 网关:通用 `Gateway`(与分析师同类),`agent_id` 指向主 Agent `pm`,`pm` 通过 `multiagent` 字段引用 4 个子 Agent(t / g / s / validator),另挂 5 个只读知识库;BYOK(请求头经 `extra_headers` 传入)。
- 代码位于 `feat/analyst-bot` 工作区(**未提交**),不在 `feat/trace-p0` 基线上。

#### 11.11.2 实测样本与结论

样本:`ada-pm` 4 个 session(11-11)、分析师 2 个 session、topic6 旧样本 1 个(10-16,含 2 个并发子线程)。

| 结论 | 依据 |
|---|---|
| **`ada-pm` 现存 4 个 session 里没有真正的多线程**:`session.thread_created` 共 0 次,每个 session 只有主线程 `topic-lab-ada-pm-pm` | 事件原文统计 |
| 记忆里"冒烟时 `thread_created` 出现 3 次"对应的 session 已查不到,**并发子线程在 ada-pm 上没有可验证样本** | 4 个 session 均无 |
| 唯一的多线程样本(topic6 10-16):2 个 `annotator` 子线程在同一时刻(+169.6s)被拉起,各运行约 13~14 秒,**与主线程运行窗口重叠** | 事件时间线 |
| **子线程窗口内没有任何模型调用或工具事件**:主 session 只看得到"派发 `agent.thread_message_sent`"与"回传 `agent.thread_message_received`" | 子线程窗口内模型 / 工具事件 0 个 |

第 4 条是**方舟事件流本身的粒度限制**,不是解析问题:**多智能体下,主 session 的事件流看不到子 Agent 内部的模型调用、工具调用与报错**。排障 Bot 对子 Agent 内部只能给出"派发了什么、回传了什么、耗时多久",无法下钻。

> 待验证:子 Agent 自己的 session 是否可单独拉取。目前没有证据表明子线程对应独立的 session id(`session_thread_id` 形如 `sthr-…`,与 `sesn-…` 不同),**未验证**,不要假定能拉到。

#### 11.11.3 线程归属规则(取代此前"一律推断")

1. 事件自带 `session_thread_id` 时直接使用,标为**直接观测**。
2. 事件没有该字段(旧版方舟数据)时,才按 `session.thread_status_*` 的活跃区间推断,标为**推断**;同时存在多个活跃线程则留空——topic6 样本证明并发子线程真实存在。
3. 线程显示名取自同 `session_thread_id` 的 `session.thread_*` 事件的 `agent_name`。

#### 11.11.4 统计与判定口径

| 项 | 口径 |
|---|---|
| token 汇总 | 以 `span.model_request_end` 的 `model_usage` 汇总为准;`session.usage` 是累计值,**只用于对账,不可跨轮相加** |
| 模型调用失败 | **不等于会话失败**。`ada-pm` 样本出现 4 次 `is_error=true` 且 input=0 的调用(耗时 0.9~2.3 秒),会话仍正常结束,说明方舟会自动重试。判定:失败后重试成功 = 警告;`retry_status=exhausted` / `stop_reason=retries_exhausted` = 异常 |
| 缓存命中 | 此前"缓存命中为 0"只对 topic6 旧样本成立;`ada-pm` 样本 `cache_read_input_tokens` 有大量命中(如 8610、13896),**§8 第 3 条结论需收窄为"topic6 BYOK 旧样本"** |

#### 11.11.5 对已提交 P0 的影响(待决,未改代码)

`arkagent/observability/events.py` 现在对 `span.model_request_*` 一律推断线程;在新版数据上应优先读事件自带的 `session_thread_id`。另外 `session.usage` 事件目前被忽略,失败调用也未区分"已重试成功"。这些会改变已提交行为,单独评估后再改。
