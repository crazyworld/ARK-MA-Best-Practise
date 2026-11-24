# Topic5 当前执行流程与输出说明

> 口径日期：2026-11-24（同步 FIX_PLAN 与缺陷清单 V1.0）
> 适用范围：当前 MA 版 Topic5 营销活动复盘报告  
> 事实源：`ma-resources/io/contracts/pipeline.v0.2.yaml`、`pipeline-orchestrator/handlers.py`、
> `topic5_runner.py`。若本文与历史方案或客户原始资源包不一致，以当前代码和 DAG 为准。
>
> 交互式节点视图：[`topic5_pipeline_overview.html`](./topic5_pipeline_overview.html)

## 1. 先说明容易产生“出入”的地方

当前运行链路不是客户原始资源包的原样部署，而是适配 MA 后的
**薄 Agent + Durable Workflow** 实现。因此，旧文档中的角色数量、节点数量和人工关卡展示
与现网会有差异。

| 对比项 | 客户原始设计/旧文档 | 当前实际实现 |
| --- | --- | --- |
| 物理 Agent | ProjectAgent、Topic5Agent、多个 Subagent、Validator | 2 个 MA Agent：`topic5-coordinator`、`topic5-validator` |
| Subagent | Discovery、Opinion、Insight、Narrative 分别运行 | 保留为 DAG 逻辑 owner，由编排器 handler 执行，不单独创建 MA Agent |
| 节点数量 | 部分文档仍写 28/29 节点 | 当前权威 DAG 为 38 节点 |
| G3 | 容易被理解为用户审核卡 | 独立 Validator 自动质检；`p0_count == 0` 自动放行，不向用户发卡 |
| 活动数据 | 容易被理解为 G1/G2 表单附件 | 默认 G2 后由 n03b 自动采集活动原帖（只抓帖子，采集授权后才计费）；也可在话题上传 `.xlsx`；`TOPIC5_ACTIVITY_DATA_MODE=upload` 恢复「G2 后等待上传」 |
| 图表交付 | 旧方案提到 COS/CDN | 当前在沙箱生成本地 PNG，n20 发布时上传为飞书文档 media |
| 流程状态 | 依赖 Agent 对话上下文 | Run、节点状态、Artifact 版本和审核记录持久化，可局部重试和冷恢复 |

## 2. 系统执行边界

| 组件 | 责任 |
| --- | --- |
| 飞书 Gateway | 接收触发消息和文件、创建/恢复 MA Session、发送进度卡和审核卡、同步 Job 状态 |
| `topic5-coordinator` | 薄前台；只负责启动/续跑编排器、透传人工决定、输出关卡或终态 |
| `pipeline-orchestrator` | 读取 38 节点 DAG，执行数据处理、状态推进、Artifact 版本化、局部重试和 QC 循环 |
| `topic5-validator` | 独立读取冻结版本，执行机器规则扫描和全文语义检查，输出 `qc_report` |
| 用户 | 提交 G1、确认 G2、授权采集或上传活动明细 Excel、处理条件人工关卡、完成 G4 终验、必要时 `/fix` 交接后修正 |

Coordinator 不负责计算指标、撰写报告或判断质检是否通过；这些动作都由编排器和 Validator
按确定性契约执行。

## 3. 端到端流程与阶段输出

| 阶段 | 责任主体 | 输入 | 执行节点/动作 | 持久化输出 | 用户可见结果 |
| --- | --- | --- | --- | --- | --- |
| 0. 任务接入 | Gateway | 飞书消息：`复盘报告`、`营销复盘` 或 `活动复盘` | 创建 Topicx Control Task、Topic5 Job 和 MA Session；启动 Coordinator | `task_id`、`job_id`、`session_id` | 同一话题中的进度卡 |
| 1. 需求确认 | 用户 + 编排器 | 触发消息、G1 表单 | `n00_intake` 生成问卷；`n01_hc_requirement` 等待 G1 | `requirement_snapshot`、`requirement_confirmed` | G1 需求确认卡；通过后进入方案编制 |
| 2. 方案编制与确认 | 编排器 + 用户 | `requirement_confirmed` | `n02_plan` 编制分析/执行方案；`n03_hc_plan` 等待 G2 | `analysis_plan`、`execution_plan`、`selected_modules` | G2 方案确认卡 |
| 3. 活动原帖获取 | 编排器 + 用户 | 已通过的 G2 | `n03b_activity_pull`：已上传 Excel 直接沿用；否则按活动调用 `ssjh-social-task` 数量预检 → HC_COLLECT 采集授权（数量上限）→ 创建/恢复原任务 → 下载核验（唯一ID、内容类型取值、日期）→ 19 列适配工作簿；授权卡等待期间也可改为上传 `.xlsx` | `activity_detail`（交接记录）、适配工作簿、`confirmed_plan.source_file` | 采集授权卡；核验通过后自动进入生产 Run |
| 4. 冻结生产输入 | 编排器 | G2 方案、冻结附件引用 | G2 resume 生成 `confirmed_plan`；沙箱直读附件并校验 SHA256，`n04_run_init` 校验 Excel schema 并冻结资源 | `confirmed_plan`、本地缓存、`run_record`、`resource_lock`、`context_snapshot` | 进度卡进入生产 Run |
| 5. 并行取数、清洗与去噪 | 工具 + 编排器 + 语义模型 | `confirmed_plan`、活动明细 | `n05_brand_pull`、`n06_brand_baseline`（读取需求确认单已确认的基准期起止）、`n13_wordcloud_pull` 并行；`n07_clean` 清洗；`n07b_noise_review` 去噪（选样与窗口由程序，判定由模型；噪音默认剔除，不确定保留） | `brand_stats`、`brand_stats_baseline`、`wordcloud_data`、`cleaned_data`、`denoised_data`、`noise_review` | 数据异常严重时先确认；进度卡显示取数/清洗进度 |
| 6. 两轮 AI 标注 | 编排器 + 标注工具 + Validator | `cleaned_data` | `n09_annotate_r1` → `n09r_review_r1`/`n09r_gate`（抽检，不达标经 `n09r_rework` 重标）→ `n09b_opinion_candidates`（逐活动×正/负）∥ `n08_ugc_discovery`（相关 UGC）→ `n10_hc_opinion`（策略 + Validator 来源审核）→ `n11_annotate_r2` → `n11r_review_r2`/`n11r_gate` | `annotated_round1`、`r1_review`、`opinion_candidates`、`ugc_category_list`、`confirmed_opinion_labels`、`annotated_data`、`r2_review` | 数据量 ≥ 阈值且 DataHub 不可用时先问用户；无歧义且来源审核无 P0 时自动通过；抽检轮次/额度用尽转 HC7 |
| 7. 统计、图表与洞察 | 编排器 + 工具 + 语义模型 | 标注数据、品牌数据、词云数据 | `n12/n13 → n14` 生成双版词云 → `n15` 全词表议题、可选原帖全文阅读/补读及 compose | `stats_bundle`、`chart_urls`、`wordcloud_insight`，双 PNG、`render.json`、`analysis.md`、`preview.html`、`analysis-evidence.json` | 进度卡显示聚合、图表和洞察状态 |
| 8. 报告撰写 | 编排器 | 统计、图表、词云洞察、REPORT_CONTRACT | `n16_report` 模型写作后装配数据说明、Top10、观点/UGC证据及正确图片 | `report_md` | 暂不直接交付；先进入独立质检 |
| 9. 独立质检与返修 | Validator + 编排器 | `report_md`、`chart_urls`、`stats_bundle` | `n17_validate` → `n18_qc_gate`；有 P0 时 `n19_fix` 后回到 n17；高点核查发现明确噪音时 `n18b_noise_rollback` 回到去噪增量重判（≤2 次） | `qc_report`；返修后生成新版 `report_md` | G3 自动执行；超过最大轮次或外部权限异常时展示 HC7（放宽留痕 / 补数据重做 / 终止） |
| 10. 飞书草稿与发布后检查 | 编排器 + 飞书 API | 已通过质检的报告和图片 | `n20_delivery_draft`：配置根目录时先查找/创建项目文件夹；首次创建文档，返工时在原文档原位修正（章节增删则同文档重建）；上传图片、先转文件夹再转文档所有权（失败降级可管理协作者）并回读；`n20_delivery_gate` 路由 | `delivery_draft`、`external_receipts`、`feishu_doc`、更新后的 `qc_report` | G4 卡片展示飞书文档链接供用户终验 |
| 11. 用户终验 | 用户 + 编排器 | `delivery_draft` | `n21_hc_user` → `n22_user_gate` | `user_confirmation` | 通过则提交；打回区分版式（直达 n20）与内容（n19 返修后重新质检）；超 3 轮或超时进入升级 |
| 12. 交付闭环 | 编排器 + Gateway | 飞书文档、用户确认 | `n23_delivery_commit` 做发布后最终检查；`n24_handoff` 校验闭环 | `delivery_record`、`task_result`、`handoff` | 最终进度卡、飞书文档 URL 和完成状态 |
| 13. 交接后修正（可选） | 用户 + 编排器 | 已交付报告、话题内 `/fix <修改内容>` | `run_pipeline.py post-handoff-fix`：n19 → n17 → n20（原位修正）→ G4 → n23 → n24 | `handoff.amendments[]`（修正前后 revision、报告版本） | 原文档链接不变，修正后再次终验 |

## 4. 主流程图

```text
飞书触发
  ↓
G1 需求确认
  ↓
方案编制 → G2 方案确认
  ↓
n03b 活动原帖：预检 → 采集授权 → 采集核验（或用户上传 Excel）
  ↓
n04 冻结输入与校验
  ↓
品牌活动期取数 ─┐
品牌基准期取数 ─┼→ 清洗 → 去噪 → 两轮标注 → 统计聚合
词云取数 ──────┘                     ↓
                         图表 + 词云洞察
                                  ↓
                              报告撰写
                                  ↓
                    G3 独立 Validator 自动质检
                      ├─ P0 → 修复 → 重新质检
                      └─ 通过
                                  ↓
                    飞书草稿 + 发布后机器检查
                                  ↓
                             G4 用户终验
                      ├─ 打回 → 重新生成草稿
                      ├─ 超时 → 升级
                      └─ 通过
                                  ↓
                       Delivery Commit + Handoff
```

## 5. 人工关卡和自动关卡

| 关卡 | 放行主体 | 是否固定出现 | 确认对象 | 结果 |
| --- | --- | --- | --- | --- |
| G1 | 用户 | 是 | 结构化业务需求 | 冻结 `requirement_confirmed` |
| G2 | 用户 | 是 | 分析方案、执行方案、模块选择（活动类型未确认时标红「待解决」） | 冻结 `confirmed_plan`，进入 n03b |
| HC_COLLECT | 用户 | 自动采集时 | 各活动预检声量与数量上限 | 授权后创建计费任务；也可改为上传 Excel |
| N10 | 系统或用户 | 否 | 观点/UGC 标签策略 | 无歧义自动确认，有歧义才发卡 |
| G3 | 独立 Validator | 是 | 当前版本 `report_md` | `p0_count == 0` 自动放行，不向用户发卡 |
| HC7 | 用户/项目负责人 | 否 | QC 超限或外部权限异常 | 放宽标准后续（报告内留痕）、补数据重做（可附结构化变更按影响矩阵精确重算）或终止 |
| G4 | 用户 | 是 | 飞书文档草稿及其 revision | 通过、打回或超时升级 |
| USER_ESCALATE | Supervisor / 用户 | 否 | G4 超时（3/5 个工作日）或返工超 3 轮 | 升级处理；累计 5 个工作日无回应转归档暂停，用户回复即恢复 |

## 6. 关键输入

### 6.1 G1 业务需求

主要包括品牌、活动名称、活动周期、平台、品牌/活动/产品关键词、内容范围、活动类型、品牌基准期（可选）、业务问题和交付期望。
活动类型以用户/关键词 Skill 确认为准，不由数据推断；基准期未填时默认参照开始日前 30 天（多活动以合并窗口开始日为参照），实际起止写入需求确认单。
G1 不接收活动明细文件。
默认需求 JSON 随控制消息原样传入，不使用 Ark Files；Coordinator 仅透传。

### 6.2 活动明细（自动采集或 Excel）

默认由 n03b 自动采集原帖并生成 19 列适配工作簿；用户也可上传 Excel。生产 Run 只消费写入 `confirmed_plan.source_file` 的冻结引用。

- 初次上传成功后，Job 从 `wait_input` 进入生产 Run。
- 流程已经进入后续节点时，再上传其他文件会被拒绝，不会覆盖当前输入。
- 只有 `n04_run_init` 因 `excel_source` 格式或字段校验失败时，才允许上传修正版。
- 修正版使用新的唯一路径描述、附件引用和 SHA256，并生成新版 `confirmed_plan`，随后只重试 n04。
- 其他节点失败时禁止换源，避免输入和既有产物不一致。

默认 `feishu_resource`：Gateway 校验后冻结 `message_id/file_key/sha256`，
`excel_source.py` 在沙箱下载到 `/workspace/topic5_repro_inputs/` 并校验哈希；
续跑复用经校验的缓存。`TOPIC5_INPUT_TRANSPORT=ark_files` 为旧上传/挂载方式，
旧 Session 继续遵循其冻结契约。

### 6.3 受控外部数据

`n05`、`n06`、`n13` 通过 `SSJH-Social-Stat` 获取品牌活动期、基准期和词云数据。
这些数据与用户 Excel 责任不同：Excel 用于活动明细分析，SSJH 用于品牌及基准对比。

## 7. 最终输出

### 7.1 用户最终获得

1. 一份完成终验的飞书营销活动复盘文档；
2. 报告中的统计表、趋势图、情感/观点/UGC/KOL 等分析和词云议题；
3. 任务完成卡片及最终文档 URL。

### 7.2 系统保留的可审计输出

| 类别 | 主要 Artifact |
| --- | --- |
| 需求与方案 | `requirement_confirmed`、`analysis_plan`、`execution_plan`、`selected_modules`、`confirmed_plan` |
| 数据与标注 | `cleaned_data`、`annotated_round1`、`confirmed_opinion_labels`、`annotated_data` |
| 分析结果 | `stats_bundle`、`chart_urls`、`wordcloud_insight`、`report_md` |
| 质检 | `qc_report`，绑定被检查的 Report Artifact 版本 |
| 交付 | `delivery_draft`、`external_receipts`、`feishu_doc`、`delivery_record` |
| 闭环 | `task_result`、`handoff` |

Artifact 不原地覆盖。内容变化会生成新版本，因此返修前后的报告、质检结果和交付记录可以追溯。

## 8. 失败、重试与恢复

| 场景 | 当前处理 |
| --- | --- |
| 节点临时失败 | 按节点 retry 配置有限重试；失败后可从当前节点显式重试 |
| 外部副作用状态未知 | 先对账再重试，避免重复拉取、重复标注或重复创建文档 |
| Gateway 重启 | 从 SQLite Job 状态和沙箱 Run checkpoint 恢复，不从头执行 |
| n04 Excel 校验失败 | 允许上传修正版，更新冻结源引用，只重试 n04 |
| 采集超时/未知状态 | 保留原任务（同一 request_id），按最短 120 秒间隔恢复，不换 ID 重建计费任务 |
| 后续节点失败 | 保留原输入和既有 Artifact，禁止换 Excel |
| Validator 发现 P0 | 修复后生成新版报告，再次独立质检 |
| QC 超过最大轮次 | 进入 HC7 人工升级 |
| G4 打回 | 版式问题直达交付草稿，内容问题返修后重新质检；均在原文档原位修正并保持版本链 |
| 交付后发现问题 | 话题内 `/fix <修改内容>`，走交接后修正，handoff 追加 amendments |

## 9. 文档维护规则

词云当前由客户 `wordcloud-insight` v0.2.6 执行：n14 保留全词表，仅绘图展示前100词；
1400×880 全横排双版 PNG，报告引用透明 `wordcloud.png`，保留白底图与 `render.json`。
字体在部署前下载至仓库外缓存，经 SHA256 校验注入 Skill ZIP；运行时无需联网下载。
n15 通读完整词表，默认2–4议题、最多5个，低证据可仅1个，不强制行动建议。
原句使用模型选择 `quote_id`、代码复制原字段片段；原词必须属于当前范围且不重复，
证据帖子必须已读，未读需先补读。具体错误进入局部重试，有原帖的证据失败必须阻断。

n19 由代码重建核心 Top10 和代表证据，模型修复叙事；完整性校验兼容 Markdown
及加粗标题，并核对字段和实际行数。结构错误反馈到节点内下一次尝试，最多3次；
通过同源 Validator 扫描器预检后再交独立 G3。该次数与 DAG 的 `qc_max_rounds` 分开计。

Memory Store 当前仅包含 `MEMORY.md`、`_版本状态.md`、`错误案例库.md`。
Agent 通过 `memory_ls` / `memory_read` 读取；Python/JS 语义调用不自动继承 Memory。
客户 plan_pack/report_pack 统一迁移与节点知识注入尚未实施，见
[迁移审计](./KNOWLEDGE_MIGRATION_AUDIT.md)和[暂缓需求](./KNOWLEDGE_MEMORY_REQUIREMENT.md)。

1. 节点、输入、输出和路由以
   `ma-resources/io/contracts/pipeline.v0.2.yaml` 为准。
2. 节点实际业务行为以
   `ma-resources/skills/pipeline-orchestrator/handlers.py` 为准。
3. 飞书侧状态、卡片和文件上传行为以 `topic5_runner.py`、`topic5_hitl.py` 为准。
4. 修改 DAG、关卡、Artifact 或用户可见流程时，必须同步更新本文。
5. 客户原始 `assets_from_customer/` 只作为迁移历史，不作为当前运行口径。
