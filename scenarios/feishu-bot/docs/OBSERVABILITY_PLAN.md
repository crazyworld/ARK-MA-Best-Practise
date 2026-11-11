# ada 全链路可观测:现状、缺口与分期规划

> 状态:P0 已实现(未提交) · 创建:2026-11-02 · 分支:`feat/trace-agent`(基于 `origin/main` `552b055`)
> 对应需求(玉新 / ada FDE):分析师(内部运营)能通过看板看到每次任务的执行链路;也能通过 CLI / 接口取到观测数据,交给 Agent 分析。

## 1. 背景与目标

ada 现在有 topic5(复盘报告)、topic6(热点周报)两个 topic 能力,由 topicx 统一入口经 router 分发。任务出问题时(例如 BYOK 下 `model_request_failed_error`、HC 卡住、工具超时),排查靠人工翻 SQLite、日志和方舟后台,没有统一入口。

目标:

1. 一个任务的完整执行链路可还原:`飞书消息 → router → 业务 session → 每轮 user message → 模型调用 → 工具调用 → 工具内部的外部调用`。
2. 同一份数据同时服务三类使用者:分析师看板、CLI / HTTP 接口、群聊诊断 Agent(独立飞书应用)。三者口径一致。
3. 新增 topic 只需写一个 adapter,不改看板和接口。

约束:

- 服务需要支持分布式部署(用户全局规则)。观测层不得依赖本地文件或进程内状态;持久化通过 `TraceSink` 接口隔离,SQLite 只是默认实现。
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

**不存在的字段**(2026-11-02 复核,事件体字段与上次一致):

- `span.model_request_*` 没有模型名,没有 `session_thread_id`,**没有模型调用级 request_id**。
- 仅约 1/4 事件带 `session_thread_id`。
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
3. **缓存命中为 0**:需与模型侧确认。
4. **飞书文档同步**:本文档尚无对应飞书文档;若要对外评审,需另行创建。

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-02 | 初版:落盘现状、验证事实、14 项缺口、P0 设计与分期 |
| 2026-11-02 | P0 实现:`arkagent/observability/` + `arkagent trace list/show`;`tests/test_observability.py` 20 个用例,全量 582 通过;对真实 job 复验,数值与 §3 一致(40 次模型调用、245.5 万/1.4 万 token、39 次工具全配对、最慢 632s) |

## 10. P0 已知限制

- 工具归类是启发式:多行 shell(`for` 循环、嵌套 heredoc)可能归到 `for` / `mkdir` 等,不影响耗时与计数。
- 线程归属为推断(`[线程~]`),子 Agent 与主 Agent 同时活跃时置空。
- 旧版本目录(`~/.arkagent/cases/topic6/`)不会被自动发现,需 `--env-file` / `--db` 指定;新版统一到 `~/.arkagent/cases/topicX/`。
- topic5 / topicx 的 adapter 只有单测覆盖,尚未用真实库验证(本机没有这两个库)。
- 事件只拉取不落库,方舟清理后即不可恢复,见 P1。
