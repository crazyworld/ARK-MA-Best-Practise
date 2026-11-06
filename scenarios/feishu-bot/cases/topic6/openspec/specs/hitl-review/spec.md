# hitl-review Specification

## Purpose
HC1/HC2/HC3 人工审核闭环：审核卡片渲染、HC 产物发布到飞书、卡片回调处理、在线修改回读、决策注入 MA Session，以及进度卡片与 @通知。

源码：`topic6_hitl.py`（`Topic6Hitl`、`build_hc_card`、`build_hc_resolved_card`、`_build_decision_message`）、`feishu_doc_publisher.py`（`FeishuDocPublisher`）、`topic6_progress_card.py`（`build_progress_card`）、`job_notifier.py`（`JobNotifier`）。

## Requirements

### Requirement: 单卡原则
每个任务在会话中 SHALL 只有一张交互卡片：启动时的进度卡在 HC 时被 patch 成审核卡，处理后 patch 成"已处理"卡，续跑后再 patch 回运行卡。仅当主卡不存在（初始化失败）时，`send_hc_card` 才 MAY 新发一张并回写 `progress_card_message_id`。

#### Scenario: 主卡初始化失败后遇到 HC
- **WHEN** 任务没有 `progress_card_message_id`，进入 HC1
- **THEN** 新发一张审核卡并保存其 message_id 作为后续主卡

### Requirement: HC 卡片内容
`build_hc_card` SHALL 生成飞书 schema 2.0 卡片：头部颜色 HC1 蓝 / HC2 绿 / HC3 紫，Validator 结论为 `BLOCK` 时改红色且通过按钮文案改为"⚠️ 忽略阻断并通过"。正文包含：任务 ID、Session、模式、阶段；`__fallback__` 载荷只显示中性提示（MUST NOT 展示 Agent 尾部原文）；载荷摘要（模式、项目目录、宽表、分布摘要表、发现问题、飞书文档、妙搭页面）；已发布产物的"📝 打开编辑 {name}"按钮；Validator 区块（`SKIP` 时只显示未执行原因，issues 最多 5 条）。交互区 MUST 含三项：通过按钮、`reject_reason` 必填表单（≤500 字）+"打回并继续"、`remark_note` 必填表单 +"📝 发送补充说明"。按钮 value 为 `{job_id, hc, event_id, decision}`。

分布摘要指标与标签：`rows` 总行数、`c0_valid_rate`、`r1_r5_valid_rates`、`c3_valid_rate`、`c0_null_rate`、`marketing_hit_rate`、`r_subset_rows`、`c2_coverage`、`c3_coverage`、`max_cluster_rows`；比例类按百分比一位小数展示。

#### Scenario: Validator 阻断
- **WHEN** 载荷 `validator_verdict.conclusion == "BLOCK"`
- **THEN** 卡头为红色，审核人仍可点击"⚠️ 忽略阻断并通过"

#### Scenario: Validator 未执行
- **WHEN** `conclusion == "SKIP"` 且有 `audit_summary`
- **THEN** 只显示"🔍 Validator: ⏭️ 未执行 — <原因>"

### Requirement: HC 前产物发布
`send_hc_card` 在发布器可用时 SHALL：
1. 任务无 `docs_folder_token` 时创建 job 子文件夹（名称 `{project_dir 末段 | unknown}_{job_id[:12]}`，父目录为 `TOPIC6_DOCS_FOLDER_TOKEN` 或机器人云空间根）并落库；失败只 warning。
2. 最多 3 次（间隔 5s）执行"强制 `artifact_sync.sync_once` → `publish_hc_artifacts`"，直到拿到发布结果。只有本地文件未同步（返回空）才重试；飞书写入失败返回 `{wide_table: {error}}`，MUST NOT 重试建表，卡片显示"宽表推送飞书失败，请在任务 outputs 中下载"及原因，且不落库。
3. HC1/HC2：在本地同步目录中按文件名查找 `wide_table_path` 对应文件（优先非 `.objects` 的物化路径，其次 `.objects/{hash}__{safe_name}`），用 `create_spreadsheet_from_xlsx` 导入为飞书表格，标题 `{项目名|mode}_{HC1|HC2}_wide_table`；写入按"行数 ≤ 5000 且单批 JSON ≤ `FEISHU_SHEET_WRITE_MAX_CHARS`（默认 300000）字符"切批；建表后任何失败 MUST 删除该表（`DELETE /drive/v1/files/{token}?type=sheet`）并抛 `SpreadsheetWriteError`；URL 优先使用接口返回值；给任务发起人授予 `edit` 并回查协作者确认（`granted`）。HC3：仅校验 `feishu_doc_url` 存在并返回 `{report: {url, type: docx}}`。
4. 发布结果的 `{token, type, url}` MUST 通过 `set_hc_artifacts` 落库，供审核通过后回读。

任一步失败 MUST NOT 阻止发卡。

#### Scenario: Files API 传播延迟
- **WHEN** 第一次同步时宽表尚未出现在 outputs
- **THEN** 5s 后重试，最多 3 次；仍失败则发不带编辑按钮的卡片

#### Scenario: 授权失败
- **WHEN** `add_permission` 抛异常
- **THEN** `granted=False`，卡片仍给出表格链接

### Requirement: 启动时共享文件夹授权
Gateway 启动时 `init_folder_permissions` SHALL 为 `AUTHORIZED_OPEN_IDS` 中每个用户在根文件夹授予 `full_access`（幂等）；根 token 或白名单为空时跳过；单个用户失败只 warning。

#### Scenario: 未配置根文件夹
- **WHEN** `TOPIC6_DOCS_FOLDER_TOKEN` 为空
- **THEN** 跳过授权，job 子文件夹建在机器人云空间根目录，宽表单独授权给发起人

### Requirement: 卡片回调处理
`handle_card_action(action)` SHALL：
1. 从 `action.action.value`（dict 或 JSON 字符串）取 `decision/event_id/job_id/hc`；value 缺失时按表单按钮 `name`（`reject_submit`/`remark_submit`）推断 decision；都没有则忽略。
2. 先按 `event_id` 反查 HC 事件，失败再按 `open_message_id`/`message_id` 查未决事件；找不到则忽略。
3. 已 resolve 的事件 MUST 视为重复回调：只再 patch 一次已处理卡，不重复续跑。
4. 从 `form_value.reject_reason | remark_note | note` 或 `input_value` 取说明。`remark` 且无说明时 MUST 只 `append_remark(PENDING_REMARK_NOTE="等待补充说明")` 并提示"请再 @bot 一条说明"，不 resolve、不续跑。
5. 解析操作人标签：`resolve_operator_name` 真名 → `操作人 ...<open_id 后6位>` → `未知操作人`。
6. **先** `resolve_hc_event` 落库，**再** patch 已处理卡（失败不阻塞）。
7. 构造决策消息：`HCx pass` / `HCx reject: <原因|无说明>` / `HCx remark: <补充|无补充>`；HC1/HC2 的 pass 追加宽表回读提示。
8. `runner.resume_job(job, message)`，成功后回复"{HC} 审核结果：{通过|打回|补充说明}，Pipeline 继续运行中。（操作人：…）"；失败回复"⚠️ {HC} 续跑失败:…"。

`reject` MUST 注入 Session 由 Coordinator 决定回退或调整，Gateway MUST NOT 直接终止任务。

#### Scenario: 打回
- **WHEN** 审核人填写"C0 营销判定过宽"并点"打回并继续"
- **THEN** 注入 `HC1 reject: C0 营销判定过宽`，任务回到 `running`

#### Scenario: 重复点击
- **WHEN** 已通过的 HC 卡片再次收到回调
- **THEN** 不再调用 `resume_job`

### Requirement: 备注正文接续
`handle_remark_message` SHALL 仅当会话键存在活跃任务、其 `current_phase` 对应的最新 HC 事件未 resolve 且 `user_note == "等待补充说明"` 时接管文本：以 `remark` 决策 resolve、patch 已处理卡并以 `HCx remark: <文本>` 续跑，返回 `True`；否则返回 `False` 交给后续路由。

#### Scenario: 无待补充备注
- **WHEN** 当前 HC 没有点过"备注"
- **THEN** 返回 `False`，文本继续按命令/触发词处理

### Requirement: 在线修改宽表回读
HC1/HC2 通过时，若该事件的 `artifacts.wide_table.token` 存在，系统 SHALL 调用 `fetch_user_edited_sheet(token, hc_created_at)`：`latest_modify_user` 为空或等于 owner（机器人），或 `latest_modify_time <= HC 创建时间` 视为未修改，返回空；否则导出 xlsx，上传方舟 Files 并挂载到 Session `uploads/hc_edits/{HC}/{文件名}`，决策消息追加："用户在飞书在线表格中修改了 {HC} 宽表，最新版本已挂载到 /mnt/session/uploads/hc_edits/…，请先覆盖 {wide_table_path}（outputs 下同时覆盖 05_合并/ 源文件）并核对行数与列名"。任何异常 MUST 降级为追加"注意：飞书表格回读失败，使用沙箱版本继续。"，MUST NOT 阻塞续跑。

#### Scenario: 用户未修改
- **WHEN** 表格最后修改人是机器人
- **THEN** 决策消息仅为 `HC1 pass`

#### Scenario: 用户修改了表格
- **WHEN** 发起人在 HC1 发卡后编辑了表格
- **THEN** 新 xlsx 挂载到 Session，决策消息包含覆盖提示

### Requirement: 进度卡片渲染
`build_progress_card` SHALL 只根据传入快照渲染（不推断状态）：头部按状态——运行中蓝"🚀 运行中"、`wait_hc` 青"⏸️ 等待审核 {phase}"、完成绿、失败红、停止灰；正文含元信息行、最近 3 条审核记录、最近 `MAX_TOOL_LINES=10` 条工具行（有淘汰时提示省略条数）；`wait_hc` 提示在当前卡片完成审核；失败/停止显示"错误摘要/停止原因"（≤400 字）；完成且有 `online_url` 时显示"打开报告"按钮；终态（完成/失败/停止）且传入 `report_doc_url` 时追加"打开飞书报告文档"按钮。Runner 只在终态时从最新一条 HC3 事件的 `feishu_doc_url` 取该链接（报告建在机器人云空间，HC3 卡被覆盖后这是用户找到报告的入口），无 HC3 记录则不渲染。

#### Scenario: 完成态
- **WHEN** `status=done` 且 `online_url` 非空
- **THEN** 卡片显示"打开报告"按钮指向该 URL

### Requirement: @通知
`JobNotifier` SHALL 在 HC 卡片发出后发送 `notify_hc_ready`，在终态卡片 patch 成功后发送 `notify_done/failed/stopped`（错误/原因截断 200 字），有 HC3 报告链接时追加一行"飞书报告：{url}"。@ 文本仅在发起人真名出现在群名册中时生成，否则省略 @；所有通知失败 MUST 只记日志不抛异常。

#### Scenario: 名册查询失败
- **WHEN** `chat_roster` 抛异常
- **THEN** 通知照常发送但不含 @
