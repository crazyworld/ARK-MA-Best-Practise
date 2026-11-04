# sub-agents Specification

## Purpose
Coordinator 通过 Multi Agent 委派的子 Agent 契约：`topic6-annotator`（单路 DataHub 标注启动）、`topic6-validator`（HC 前质量审计）、`topic6-insighter`（单版块洞察，当前主流程不再委派）。

源码：`ma-resources/agents/{annotator,validator,insighter}.{json,system.md}`；Skill：`skills/topic6-annotation`、`skills/validator`、`skills/topic6-insight`。

## Requirements

### Requirement: 子 Agent 资源定义
三个子 Agent SHALL 各自只挂载一个 Skill（annotator→`topic6-annotation`，validator→`validator`，insighter→`topic6-insight`），工具仅 `agent_toolset_20260701`，模型与 Coordinator 保持一致（当前 bmc-llm-relay `DeepSeek-V4-Flash-0731`，完整 `model` 块与 coordinator.json 相同）。子 Agent 的 skill_id/version 以占位符由 `create_all.sh` 渲染。子 Agent 每次只输出一条结构化 JSON 后 end_turn，MUST NOT 输出长文本回显。

#### Scenario: 新增子 Agent
- **WHEN** 新增一个子 Agent
- **THEN** MUST 同时新增 `<name>.json` + `<name>.system.md`、在 `coordinator.json` 的 `multiagent.agents` 中登记、并更新 `create_all.sh` 的创建顺序（子 Agent 先于 Coordinator 创建）

### Requirement: Annotator 输入契约
Coordinator 委派消息 SHALL 为 JSON：`task ∈ {c0,c3,r1,r2,r3,r4,r5}`、`mode ∈ {demo,full,skip_sampling}`（只影响回显）、`project_dir`（目录名）、`input_path`（绝对路径，只读）、`output_path`（兼容字段，不传给 CLI）、`prompt_version`（Coordinator 从 `_版本状态.md` 解析）。task 到 Prompt 目录映射：`c0→C0_基础事实`、`c3→节点标注`、`r1→R1_平台借势`、`r2→R2_商业合作`、`r3→R3_风险预警`、`r4→R4_创意借鉴`、`r5→R5_消费者行为`。

#### Scenario: R 路输入
- **WHEN** task=r3
- **THEN** `input_path` 为筛选后的可用子集，Annotator 不自行判断输入

### Requirement: Annotator 执行与边界
Annotator SHALL 只执行一次短时 bash：

```bash
python /mnt/skills/topic6-annotation/scripts/datahub_annotate.py \
  --task {task} --prompt-file /mnt/skills/topic6-annotation/prompts/{task_dir}/{prompt_version}.md \
  --input {input_path} --project-dir /workspace/Projects/{project_dir} \
  --model-id "$DATAHUB_MODEL_ID" --run-id {整数} --mode {mode} --launch-background
```

返回 `submitted|running|done` 后立即输出 `{"status":"submitted","task":…,"run_id":…}`；启动失败输出 `{"status":"failed","task":…,"reason":…}`。Annotator MUST NOT：先 cat Prompt 到上下文、自加 `nohup/&/sleep/while/tail --pid/ps`、读 `.bash_bg`、等待或轮询 worker、读 completion_meta、调用 cost_tracker、做 filter/merge/retry、修改输入、写项目目录外路径（`/tmp` 除外）、用 edit/write 改 `run_config.yaml`、启动第二个 worker。唯一允许的重试：`invalid model_id` 时从脚本打印的 `/api/v1/model/list` 中选大小写完全一致的同名候选，最多重试 1 次。MUST NOT 改用 `$C2_CHAT_MODEL_ID`。

#### Scenario: 模型 ID 大小写错误
- **WHEN** DataHub 返回 invalid model_id，候选列表中有 `DeepSeek-V4-Flash`
- **THEN** 修正 `--model-id` 重试 1 次；仍失败则回报两次 stderr

#### Scenario: 普通启动错误
- **WHEN** 输入文件不存在
- **THEN** 立即返回 failed JSON，不重试不降级

### Requirement: Validator 输入与路由
Validator 输入 SHALL 为 `{task, project_dir, audit_files[], context, previous_issues[]}`。`task` 路由（路径相对 `/mnt/skills/validator/`）：`hc1`/`hc2` → `KS/KS_Checklists/Topic6_热点报告检查清单.md` 对应章节 + 数据审计、脚本审计；`hc3` → 同清单 §HC3 + 格式审计、逻辑审计。其余 task（`qc_plan`、`intent_audit`、`industry_rpt`、`brand_profile`、`biz_question`）保留但 topic6 主流程不使用。xlsx 待审文件 MUST 先运行 `scripts/xlsx_checker.py --file … --task …` 获取确定性统计。

#### Scenario: HC2 回归检查
- **WHEN** task=hc2 且 `previous_issues` 含 HC1 的 P0 问题
- **THEN** 全量数据中未改善则仍判 P0

### Requirement: Validator 规则与结论
问题分级 SHALL 为 P0（影响核心结论/数据错误/逻辑矛盾）、P1（可信度/来源/格式）、P2（表述/排版）。结论：存在 P0 → `BLOCK`；无 P0 有 P1 → `WARN`；否则 `PASS`。规则：
- HC1.1/HC2.1 数据质量：任一列空值率 >50% 或乱码率 >5% → P0；空值率 >20% → P1；
- HC1.2 常识校验：抽样 ≤10 行，明显违反常识 >10% → P1；HC2.3 按平台分层抽 20 行（每平台 ≥3）；
- HC1.3/HC2.4 异常内容：系统性异常率 >5% → P1（建议改提示词），个例 → P2；
- HC1.4 未知分类：出现枚举外值 → P1；
- HC2.2 HC1 问题回归：未改善则保持原级别；
- HC3.1 E1~E4 版块缺失 → P0；HC3.2 Markdown 结构异常 → P1；HC3.3 常识错误 → P0、可信度问题 → P1；HC3.4 营销案例截图、HC3.5 案例归类 MUST NOT 判断。

Validator MUST NOT 修改被审文件、给出修改方案（只说问题）、跳过验证放行、对无法验证的问题做确定性判断、隐瞒问题。

#### Scenario: 空值率超阈
- **WHEN** `xlsx_checker` 报 `any_column_null_above_50pct=true`
- **THEN** conclusion=BLOCK

### Requirement: Validator 输出与日志
Validator SHALL 输出一条 JSON 后 end_turn：`{task, conclusion: PASS|WARN|BLOCK|SKIP, issues:[{severity, rule_id, category, description, location, suggestion}], suggestion, coverage_note, audit_summary}`。HC3 的 `coverage_note` MUST 固定为"本次检查仅覆盖第1-3条（版块完整性/格式/内容质量），第4-5条（营销案例截图、案例归类准确性）需人工核实"。不可恢复错误 MUST 返回 `conclusion=SKIP` 与 `audit_summary="审计失败: …"`。输出前 SHALL 写 `{project_dir}/validator_logs/{task}_{timestamp}.md`，写入失败不阻塞结论。只允许写 `validator_logs/` 与 `/tmp`。

Gateway 渲染规则见 hitl-review（BLOCK 红色卡头、SKIP 仅提示未执行）；Coordinator 将 verdict 原样放入 HC 载荷 `validator_verdict`。

#### Scenario: 读取文件失败
- **WHEN** audit_files 中的 xlsx 不存在
- **THEN** 返回 SKIP，HC 流程照常继续

### Requirement: Insighter 保留状态
`topic6-insighter` SHALL 作为已注册但主流程不委派的子 Agent 保留（Phase E 由 `pipeline_e.py` 单入口内部并发完成）。其契约（输入 `section ∈ e1..e4`、`mode ∈ demo|skip_sampling`、`wide_table_path`、`publish_date`、`insight_version`、`prompt_version`；full 模式直接返回 failed；输出 `{status, section, insight_path, data_snapshot, cost_yuan}`）仅供手工单版块调试。重新启用委派 MUST 先通过 openspec change 修改 coordinator-orchestration 的 Phase E 要求，并修正 `insighter.system.md` 中已失效的脚本名引用（`e2_marketing_node.py`→`e2_nodes.py`、`e3_platform.py`→`e3_platforms.py`）。

#### Scenario: Coordinator 委派 insighter
- **WHEN** 在当前契约下 Coordinator 为 Phase E 委派 4 个 insighter
- **THEN** 违反 coordinator-orchestration 的 Phase E 要求
