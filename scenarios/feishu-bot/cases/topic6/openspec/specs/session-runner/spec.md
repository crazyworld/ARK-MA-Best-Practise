# session-runner Specification

## Purpose
桥接飞书与方舟 MA Session 的长任务运行器：创建 Session、发送首条消息、消费 SSE 事件、识别阶段/项目目录/HC/最终发布结果，并在 Agent 偶发中断、Provider 故障、网络断线时自动恢复。

源码：`topic6_runner.py`（`Topic6Runner` 及模块级解析函数）。依赖：`arkagent.ark`（`ArkClient`、`event_text/event_progress/event_error/event_requires_action`）。

## Requirements

### Requirement: 全局单活跃任务
`start_job` SHALL 在 `asyncio.Lock` 内串行执行"检查活跃任务 → 创建 Session → 落库"。只要全局存在任一 `running|wait_hc` 任务，MUST 抛 `Topic6RunnerError("当前已有热点周报任务{运行中|等待审核}（mode=…，phase=…）。请等待前一个任务结束后再发起。")`。

#### Scenario: 并发触发
- **WHEN** 两条触发消息几乎同时到达
- **THEN** 只有一条创建 Session，另一条收到已有任务提示

### Requirement: Session 创建契约
系统 SHALL 以 `coordinator_agent_id`、`environment_id` 创建 Session，并：
- 配置了 `memory_store_id` 时挂载 `{"type":"memory_store","memory_store_id":…,"access":"read_only"}`，并注入环境变量 `TOPIC6_MEMORY_STORE_ID`；
- 始终注入 `FEISHU_USER_OPEN_ID=<触发者 open_id>`；
- `vault_ids` = Runner 已登记 Vault（bot Vault 在前）+ 用户 Vault，去空去重保序。Session 创建后无法追加 Vault。

落库时 `project_dir` MUST 为空，由 Coordinator 的 `[project_dir]` 标记回写。首条 user.message MUST 为：

```
[topic6] 触发词=热点报告 mode={mode} workspace_root=/workspace/Projects
用户原始消息:{原始文本}
```

#### Scenario: 未配置 Memory Store
- **WHEN** `TOPIC6_MEMORY_STORE_ID` 为空
- **THEN** `resources` 为空列表，且不注入 `TOPIC6_MEMORY_STORE_ID`

### Requirement: 单进度卡片
启动时 SHALL 先发送一张进度卡片并保存 `progress_card_message_id`，后续所有运行/HC/终态都 patch 同一张卡片，MUST NOT 为每个工具事件单独发消息。发送失败 MUST NOT 阻塞主流程。patch 规则：非 `force` 时最小间隔 `PROGRESS_MIN_INTERVAL_SEC=4s`；渲染签名（`json.dumps(sort_keys=True)`）不变则跳过；同一 job 的 patch MUST 经 per-job `asyncio.Lock` 串行；patch 失败只记 warning。终态（done/failed/stopped）patch 成功后 SHALL 调用 `JobNotifier` @发起人。

#### Scenario: 卡片初始化失败
- **WHEN** `send_interactive_card` 抛异常
- **THEN** 任务照常运行，后续 `_render_and_patch` 因无 message_id 直接跳过

#### Scenario: 阶段切换
- **WHEN** Agent 输出新的 `[phase] X`
- **THEN** 立即 force patch，不受 4s 节流

### Requirement: SSE 消费与断线重连
`_drive_session` SHALL 先打开事件流再发送首条消息（仅发送一次，携带 BYOK `extra_headers`）。Runner 向 Session 注入的每一条 user.message（首条、HC 续跑、/continue、/republish、idle 催促、provider 重试、重启恢复）MUST 经 `Topic6Runner._send_message` 发送；BYOK `extra_headers` 按该 Session 创建时的 agent 快照决定：`agent.model.provider` 非空才携带（方舟原生模型的旧 Session 携带会 400 InvalidModelHeader），结果按 Session 缓存，查询失败时按当前配置携带；Header 值 MUST NOT 写入日志、数据库或消息正文。事件流在未观察到终态前结束 MUST 视为断线，最多重连 `SSE_RECONNECT_MAX_ATTEMPTS=5` 次，延迟 `min(1·2^n, 16)s × U(0.8,1.2)`；重连 MUST NOT 重发 user.message，并以事件 `id` 去重。重连耗尽 MUST `mark_failed("sse_stream_error: …"[:400])`。同一 Session 同时只允许一个消费任务（`_active_streams`），续跑前 MUST cancel 旧任务；退出时仅当自己仍是登记任务才移除引用。

#### Scenario: 瞬时断线
- **WHEN** 事件流在 Phase C 中途断开一次
- **THEN** 1s 左右后重连，已处理事件不重复处理，任务不失败

#### Scenario: 重连耗尽
- **WHEN** 连续 6 次打开/消费失败
- **THEN** 任务标记 `failed`，卡片显示 `sse_stream_error`

### Requirement: 状态标记解析
系统 SHALL 从每条 `agent.message` 中解析：
- 阶段：正则 `\[phase\]\s+([A-Z0-9]+)`，变化时 `update_phase`；
- 项目目录：行首 `[project_dir] <path>`，`normalize_project_dir` 只接受 `/workspace/Projects` 的**直属**子目录（拒绝 `.`、`..`、多级路径），变化时覆盖并 warning。

#### Scenario: 越界项目目录
- **WHEN** Agent 输出 `[project_dir] /workspace/Projects/a/b`
- **THEN** 忽略，不更新 `project_dir`

### Requirement: 终态事件处理
`_consume_events` SHALL 按以下规则处理终态类事件：
- `user.interrupt` → `mark_stopped("用户在方舟后台手动停止了 Session")`。
- `session.error` 且错误含 `model_request_failed`，且 `provider_retry_count < 5` → 计数 +1，等待 `min(30·2^(n-1), 300)s`，卡片显示"模型服务暂时不可用，{d}s 后自动重试（n/5）"，然后发送 `"[系统] 模型服务已恢复，请继续执行当前阶段。"` 并置 `provider_retry_pending=True` 继续消费；发送失败 → `mark_failed("自动重试失败：…")`。
- 其他 `session.error` / `session.status_failed` / `session.status_terminated` → `mark_failed(error[:400])`。
- `span.model_request_end` 且非错误 → 重置 `provider_retry_count`。
- `session.status_idle`（非 requires_action）：若 `provider_retry_pending` 且 `stop_reason == retries_exhausted`，MUST 吸收该 idle（与上一个 error 属同一次故障，不消耗 nudge 额度）；否则交给 idle 处理。

#### Scenario: Provider 限流
- **WHEN** 收到 `session.error: model_request_failed_error`，且为第 1 次
- **THEN** 30s 后注入恢复消息，任务保持 `running`

#### Scenario: Provider 重试耗尽
- **WHEN** 第 6 次 `model_request_failed`
- **THEN** 任务 `failed`

### Requirement: Idle 判定优先级
`_handle_idle(job_id, collected, recovering)` SHALL 以最后一条 agent.message 为依据，按以下顺序判定，命中即停止：

1. **HC 结构化 JSON**：`extract_hc_payload` 逐个 `{` 起点 `raw_decode`，取第一个 `hc ∈ HC_KINDS` 的对象。未命中时，若当前 phase 为空/`A`/`B`/`C*`/`D*`/`HC*`/`post-HC*` 且文本不含 `[need_decision]`，用 `detect_hc_intent`（文本含 HCx 且含"等用户/请审核/点击卡片/输出 HC…"等意图词）兜底生成 `build_hc_fallback_payload`。
2. **Phase H 发布结果**：`extract_final_online_url(last)`；为空且 phase=`H` 时倒序扫描 Session 历史。命中 → `mark_done(online_url)`。
3. **Phase H 无链接**：`mark_failed("Phase H 妙搭发布失败：Agent idle 但未输出有效 online_url。…")` 并提示 `/continue`。
4. **`[need_decision]`**：`mark_stopped("Phase X 等待用户裁决…")`，把去掉标记的正文（≤3000 字）发群里，提示 `/continue <选择>`；MUST NOT 催促。
5. **结构化诊断** `_diagnose_interruption`：Session 启动 <60s 且无任何输出，或最后输出含错误词（error/failed/超时/timeout/quota/rate limit/异常）且 nudge 已用满 → `mark_stopped` + `⚠️ 原因`。
6. **自动续跑（nudge）**：`idle_nudge_count < MAX_IDLE_NUDGES(3)` 且 phase 不以 HC 开头 → 计数 +1，卡片追加"⚡ Agent 暂停，正在自动恢复（n/3）"，返回催促文本"你在 Phase {phase} 中途停止了，请从当前阶段继续执行。不要重复已完成的步骤，从上次输出之后的下一步继续。"
7. **超限**：`mark_stopped("Pipeline 在 Phase X 中断（自动续跑 n/3 次后仍未恢复）。…")`。

phase 缺省按 `A` 处理。

#### Scenario: HC 期间 idle
- **WHEN** 任务 `current_phase=HC1` 时 Session idle 且无新 HC JSON
- **THEN** 不 nudge，走超限分支之前的判定

#### Scenario: 裁决报告提到 HC2
- **WHEN** 最后输出含 `[need_decision]` 及"本轮不产出 HC2，请等待确认"
- **THEN** 不触发 HC 意图兜底，任务 `stopped` 并发出裁决请求

#### Scenario: Agent 偶发 end_turn
- **WHEN** Phase E 中 Agent idle，未输出 HC/URL/need_decision
- **THEN** 注入第 1 次催促，任务保持 `running`

### Requirement: HC 载荷处理
识别到 HC 载荷后 SHALL：重置 `idle_nudge_count`；载荷 `project_dir` 合法时回写；`validate_hc_payload` 校验（HC3 MUST 含 `feishu_doc_url`；HC1/HC2 非 `__fallback__` MUST 含 `wide_table_path`），失败 → `mark_failed` + 失败卡 + `❌ <错误>`。通过后 `append_hc_event` → `mark_wait_hc` → `card_sender.send_hc_card(job, kind, event_id, payload)` 并把返回的 message_id 写回 `pipeline_hc_events.card_message_id`。发卡异常时任务仍为 `wait_hc` 并提示手工处理。

`recovering=True` 且已存在同类未 resolve 的 HC 事件时，MUST 跳过校验并复用该事件重发卡片，MUST NOT 追加新事件。

兜底载荷（`__fallback__=True`）只恢复文本中有证据的指标：`rows/cols`（"N 行 × M 列"）、`c0_valid_rate`、`r1_r5_valid_rates`、HC2 的 `marketing_hit_rate`；`wide_table_path` 按契约生成 `/mnt/session/outputs/<project>/05_合并/wide_table_{mode}_r1.xlsx`（HC1 用任务 mode，HC2 用 `skip_sampling`，`smoke` 映射为 `full`）。MUST NOT 猜测缺失值。

#### Scenario: HC3 缺文档链接
- **WHEN** Agent 输出 `{"hc":"HC3"}` 但无 `feishu_doc_url`
- **THEN** 任务 `failed`，提示"HC3 缺少 feishu_doc_url：Phase F 飞书发布未完成"

#### Scenario: 重启后重建审核卡
- **WHEN** 恢复路径遇到已记录但未 resolve 的 HC1
- **THEN** 以原 event_id 重发卡片，`pipeline_hc_events` 行数不变

### Requirement: 最终发布链接提取
`extract_final_online_url(text)` SHALL 只接受 JSON 对象中显式的 `online_url` 字段，MUST NOT 从自由文本抓 URL。规则：`release_status` 存在且不为 `finished` → 拒绝；先 `unquote` 再剔除尾部 `"'})]` 与空白（仅当清洗结果仍是合法 http(s) URL）；必须为 http(s) 且有 host；拒绝 `feishu.cn`/`larkoffice.com`/`larksuite.com` 下 `/docx|docs|doc|wiki|sheets|sheet|base|bitable|file|drive|mindnotes|slides` 路径的云文档链接；不做妙搭域名白名单（`/page/<token>` 等放行）。

#### Scenario: 误把飞书文档当发布链接
- **WHEN** `{"online_url":"https://x.feishu.cn/docx/abc"}`
- **THEN** 返回空串

#### Scenario: 尾部误拼
- **WHEN** `online_url` 为 `https://a.aiforce.cloud/app%22%7D`
- **THEN** 返回 `https://a.aiforce.cloud/app`

### Requirement: 续跑、取消与审核恢复
Runner SHALL 提供以下三个入口并保持语义：
- `resume_job(job, msg)`：cancel 旧消费任务；若 `current_phase ∈ HC_KINDS` 则改为 `post-HCx`（避免后续 idle 被误判为等人审）；`resume_running`；立即 force patch 运行卡；以 `msg` 为首条消息启动新消费任务。
- `continue_job`：取会话键最近可续跑任务；非 `done` 时先倒序扫描 Session 历史，若已有合法 `online_url` 则直接 `mark_done` 返回，MUST NOT 再注入"继续"；否则重置 nudge 与 provider 重试额度后 `resume_job`。
- `cancel_active_job`：cancel 消费任务，`mark_failed("cancelled_by_user")`，force patch 失败卡。

#### Scenario: 已发布但被误判失败
- **WHEN** 任务 `failed`，但 Session 历史中已有 Phase H `online_url`，用户发送 `/continue`
- **THEN** 任务直接置 `done` 并回复发布链接，不向 Session 注入消息

### Requirement: 网页重新发布（/republish）
用于 HC3 通过后才在飞书文档补/换营销观察配图。Runner SHALL 提供：
- `find_republishable_job(chat_id, ref)`：`ref` 为 job_id 或 session_id 时定位该任务，MUST 属于当前会话且已过 HC3（`done`，或最近一次 HC3 决策为 `pass`）；无 `ref` 时取本会话按 `updated_at` 最近的已过 HC3 任务。不满足时抛 `Topic6RunnerError`。
- `republish_job(job)`：在启动锁内执行；全局已有 `running|wait_hc` 任务、或 Session 状态不是 `idle` 时 MUST 拒绝且不改本地状态。通过后 `resume_running(phase=G)`，发一张新的进度卡并落库其 message_id，在同一 Session 注入 `REPUBLISH_MESSAGE`（自包含：不重跑 A~F、按 Phase G 重新读取飞书文档配图并生成网页、按 Phase H 更新 `status.h.app_id` 同一应用、成功后只输出发布 JSON），旧 Session（切换模型/coordinator 版本前创建、已 `done`）同样适用。
- 判定本轮完成时，`_find_published_online_url` MUST 只扫描 `/republish` 之前事件数（`history_floor`）之后的消息，历史中的旧 `online_url` 不得把本轮发布失败误判为 `done`。

#### Scenario: 已完成任务补图后重新发布
- **WHEN** 任务 `done`，用户在飞书文档补图后发送 `/republish`
- **THEN** 同一 Session 只重跑 Phase G+H，本轮输出新的 `online_url` 后任务回到 `done`

#### Scenario: 本轮发布失败
- **WHEN** `/republish` 后 Phase H idle 且本轮没有新的发布 JSON
- **THEN** 任务置 `failed`，不因历史旧链接判为 `done`

### Requirement: Gateway 重启恢复
`recover_job` SHALL 只读取 `list_session_events`，回放 agent.message 更新 phase/project_dir，再看最后一个终态类事件（`user.interrupt`、`session.error`、`status_failed`、`status_terminated`、`status_idle`）：
- 无终态，或终态之后还有事件 → 仅恢复 SSE 订阅（返回 `subscribed`），不发送消息；
- `user.interrupt` → `stopped`；error/failed/terminated → `failed`；
- `idle` → `_handle_idle(recovering=True)`；返回 nudge 文本时发送并恢复订阅（`nudged`），发送失败 → `failed("恢复续跑失败：…")`。

单个任务恢复异常 MUST 只记录为 `recovery_error`，不阻塞其他任务。

#### Scenario: 旧 idle 后已有新事件
- **WHEN** 历史中最后一个 idle 之后还有 user.message
- **THEN** 只恢复订阅，不把旧 idle 当作当前状态
