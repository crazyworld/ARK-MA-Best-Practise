# coordinator-orchestration Specification

## Purpose
`topic6-coordinator`（MA 主 Agent）的流水线编排契约：启动序列、路径与产物发布、Phase A→H 时序、子 Agent 委派、等待/并发规范、HC 结构化输出、阶段标记与行为规则。Coordinator 是 Prompt 驱动，"代码"即 `ma-resources/agents/coordinator.system.md` 与 `coordinator.json`。

本 spec 是 Coordinator 与 Gateway（session-runner、hitl-review）、各 Skill 脚本之间的接口契约；修改 Prompt 时 MUST 保持这些契约，或通过 openspec change 同步修改对应 spec。

## Requirements

### Requirement: Agent 资源定义
`coordinator.json` SHALL 声明：名称 `topic6-coordinator`；模型（当前 bmc-llm-relay BYOK：`id=DeepSeek-V4.1-Flash`、`provider=bmc-llm-relay`、`base_url=https://bmc-llm-relay.bluemediagroup.cn/v1/chat/completions`、`protocol=openai_chat_completions`、`token_limits` 1024000/1024000/384000（按模型原生规格：上下文 1,024K 只计输入、输出上限 384K，不人为压低））；`system_file=coordinator.system.md`；挂载 6 个自定义 Skill（`topic6-fetch-normalize`、`topic6-annotation`、`topic6-insight`、`topic6-event-registry`、`topic6-web-report`、`miaoda-web-publish`），skill_id/version 以 `${skill_id.<name>}` / `${version.<name>}` 占位由 `create_all.sh` 渲染；工具 `agent_toolset_20260701` + MCP `hot-topics`（`${HOT_TOPICS_MCP_URL}`，`always_allow`）；`multiagent.type=coordinator`，子 Agent 为 `topic6-annotator`、`topic6-validator`、`topic6-insighter` 与 `self`。

#### Scenario: 切换模型
- **WHEN** 修改 `model.id` 或增加 `model.provider`
- **THEN** MUST 通过 `update_ma.sh`/`create_all.sh` 重建 Agent，且 `provider` 的有无决定 Gateway 是否附加 BYOK Header（见 gateway-command-routing）

### Requirement: 凭证安全
Coordinator 及子 Agent MUST NOT 使用 `env`、`printenv`、`set`、`export -p`、`echo "$…TOKEN/SECRET"` 或 `set -x` 探测/打印凭证；只允许 `[ -n "${VAR:-}" ]` 判断变量存在，传参时只引用变量名。

#### Scenario: 检查 lark-cli token
- **WHEN** 需要确认 `LARKSUITE_CLI_TENANT_ACCESS_TOKEN` 是否注入
- **THEN** 使用 `[ -n "${LARKSUITE_CLI_TENANT_ACCESS_TOKEN:-}" ]`，不输出变量值

### Requirement: 启动序列
每次触发 Coordinator SHALL 按序执行：
1. 用 memory 工具读取 `/{memstore_id}/topic6/MEMORY.md`、`错误案例库.md`、`_版本状态.md`（MUST NOT 用 bash `cat`；方舟不把 Memory Store 挂为目录）。
2. 从用户消息确定 mode（`demo|full|skip_sampling|smoke`），调用唯一启动脚本 `python /mnt/skills/topic6-annotation/scripts/init_project.py --text "<原始消息>" --mode {mode}`；周期解析、目录命名、取数窗口、`run_config` 初始化 MUST 全部由脚本完成，Agent MUST NOT 自行推算日期或手写 run_config。
3. 按 stdout JSON `status` 处理：`created` → 从 Phase A 开始；`resumed` → 从 `current_phase` 续跑且已 done 的分支不得重跑；`ambiguous` → 输出 `[need_decision]` 列出 `reason` 与 `options`，用户 `/continue <字母>` 后把 `resolutions[<字母>]` 参数数组原样追加到上一次命令（`null` 时停止或请用户补充），MUST NOT 把选项文字拼进 `--text`。
4. 立即输出一行 `[project_dir] /workspace/Projects/{PROJECT_DIR}`（绝对路径）。

支持的周期写法：无日期（最近完整周）、`W35`、`09-01到09-14`（custom）、双周、`继续W36`（断点续跑）。

#### Scenario: 周期歧义
- **WHEN** `init_project.py` 返回 `status=ambiguous`
- **THEN** Coordinator 输出 `[need_decision]` 并 end_turn，Gateway 将任务置 `stopped` 并转发选项

#### Scenario: 缺少 project_dir 标记
- **WHEN** Coordinator 未输出 `[project_dir]`
- **THEN** Gateway 的 `pipeline_jobs.project_dir` 保持为空，HC 兜底载荷无法生成宽表路径（视为契约违约）

### Requirement: 工作区与节点产物发布
`/workspace/Projects/{PROJECT_DIR}/` SHALL 是唯一运行工作区（中间文件、日志、PID、断点状态都在此）。每个节点通过质量关卡后，MUST 仅通过以下脚本把稳定输出复制到 `/mnt/session/outputs/{PROJECT_DIR}/` 同名相对路径：

```bash
python /mnt/skills/topic6-annotation/scripts/publish_node_artifacts.py \
  --project-dir "{project_dir}" --node "{node}" --run-id "{N}" \
  --include "{相对路径或 glob}" [--optional-include "..."]
```

脚本原子更新 `outputs/{PROJECT_DIR}/manifest.json`（节点、轮次、源/目标路径、大小、SHA-256），同节点同轮次重跑替换原记录。`--include` 未命中为发布错误；MUST NOT 发布临时文件、PID、日志、checkpoint、未完成分块或整个工作区。发布失败只记 warning，不得把已成功节点改判失败。

路径映射：`/mnt/skills/topic6-{annotation,insight,fetch-normalize,event-registry,web-report}/`、`/mnt/skills/miaoda-web-publish/`、成本台账 `/mnt/skills/topic6-annotation/tool/cost-tracker/`。

#### Scenario: 发布未命中
- **WHEN** `--include "05_合并/wide_table_full_r1.xlsx"` 文件不存在
- **THEN** 发布脚本报错，Coordinator 不得输出引用该文件的 HC 载荷

### Requirement: 节点发布清单
各节点关卡通过后 SHALL 发布以下内容（`{mode}` 为脚本侧 mode，`{N}` 为整数轮次）：

| node | 包含 |
|---|---|
| `fetch_normalize` | `01_原始数据/hot_topics_skill_raw.json`、`01_原始数据/hot_topics_raw.xlsx`、`02_标准化/hot_topics_normalized.xlsx` |
| `sample` | `03_抽样/sample_500.xlsx`（full/demo/smoke） |
| `c0` / `c3` | `04_标注/{任务目录}/*_r{N}.xlsx` + `*_completion_meta.json`（C3 缺失时跳过） |
| `c0_filter` | `04_合并/phase1_merged_{mode}_r{N}.xlsx`、`04_标注/_可用子集/usable_subset_{mode}_r{N}.xlsx` |
| `r1`~`r5` | 已完成各路 `*_r{N}.xlsx` + `*_completion_meta.json` |
| `c2` | `04_标注/C2_事件归档/c2_event_result_r{N}.xlsx`、`c2_run/c2_status.json`、可选 `c2_run/work/skipped_edges.jsonl`（不得发布整个 `c2_run`） |
| `merge` | `05_合并/wide_table_{mode}_r{N}.xlsx`、`05_合并/health_summary_{mode}_r{N}.md`；HC1 轮次附带 phase1_merged 与 usable_subset |

#### Scenario: HC 前未发布宽表
- **WHEN** Coordinator 在 `node=merge` 发布前输出 HC1 载荷
- **THEN** 违反契约：Gateway 无法从 Files API 获取宽表上传飞书

### Requirement: Phase A+B 取数与标准化
Coordinator SHALL 依次运行 `fetch_hot_topics.py --project-dir`（不传 `--start/--end`，窗口取 `run_config.fetch` = date_start−7 天 ~ date_end+1 天）与 `log1p_p1p99_normalize.py --project-dir`，产出 `02_标准化/hot_topics_normalized.xlsx`。关卡：行数 > 200、四平台均有数据、无 NaN，否则退出码 1 并停止。各平台最高分 ≤ 90 时标准化脚本退出码 2 并输出 `low_max_score`：Coordinator MUST 输出 `[need_decision]`；用户选 A 后原命令追加 `--accept-low-max --reason "<用户原话>"` 重跑，选 B/C 停止。A+B 摘要 MUST 直接转述 stdout 末行 `{"status":"ok","summary":{…}}`，不得自行读 xlsx 统计。

#### Scenario: 某平台无数据
- **WHEN** 取数结果中知乎为 0 行
- **THEN** 标准化关卡失败（退出码 1），流程停止

#### Scenario: 整体降温
- **WHEN** 标准化退出码 2
- **THEN** 输出 `[need_decision]`，用户 `/continue A` 后带 `--accept-low-max` 重跑

### Requirement: 抽样
`sample_500.py` SHALL 按模式抽样到固定文件名 `03_抽样/sample_500.xlsx`：`full` 500 条、`smoke` 30 条、`demo` 50 条；`skip_sampling` 跳过抽样直接用全量标准化表。实际行数 MUST 以 `status.sample.sample_rows` 为准。`smoke` 走 full 的完整路径（HC1+HC2+HC3），第一轮传给脚本的 `--mode` 一律为 `full`。

#### Scenario: smoke 第一轮
- **WHEN** mode=smoke
- **THEN** 抽 30 条，各脚本以 `--mode full` 运行，产物文件名与 full 一致

### Requirement: Phase C 第一批（C0 + C3 并发）
Coordinator SHALL 通过 Multi Agent 并行委派两个 `topic6-annotator`（task=`c0`、`c3`），input 为 `03_抽样/sample_500.xlsx`（full/demo/smoke 两轮）或 `02_标准化/hot_topics_normalized.xlsx`（full 的全量轮与初始 skip_sampling）。Annotator 只用 `datahub_annotate.py --launch-background` 幂等启动 worker，收到 `submitted|running|done` 后立即 end_turn，MUST NOT 在子线程中等待。随后 Coordinator 反复调用：

```bash
python /mnt/skills/topic6-annotation/scripts/wait_datahub_batch.py \
  --project-dir "{project_dir}" --tasks c0,c3 --run-id {N} --wait-seconds 105
```

bash 工具调用 MUST 显式 `timeout=115`（低于方舟约 120s 强制后台阈值）；返回 `running` 时原参数重调；MUST NOT 使用 `sleep`、`while`、`tail --pid`、`ps` 或读取 `.bash_bg`。`--run-id` MUST 为整数轮次。DataHub 模型取 `$DATAHUB_MODEL_ID`（默认 `DeepSeek-V4-Flash`，大小写敏感，不得传给方舟 Chat API）。批次 `complete` 后 Coordinator 读取每路 completion_meta 并各调用一次 cost_tracker，`--record-id "datahub:{task}:r{N}:{mode}"` 保证幂等。

#### Scenario: 批次进行中
- **WHEN** `wait_datahub_batch.py` 返回 `running`
- **THEN** 以相同参数再次调用，不插入 sleep

#### Scenario: 仅 C3 缺失
- **WHEN** 返回 `degraded` 且缺失的是 C3
- **THEN** 进入筛选，跳过 `node=c3` 发布

### Requirement: 筛选关卡
Coordinator SHALL 依次运行 `c0_merge_phase1.py` 与 `c0_filter_usable.py`。解析失败/缺失占比 > 5% MUST 熔断停止，不得放大到 R1~R5；≤ 5% 时这些行不进入第二批，仅"是否营销可用=是"的行进入 R1~R5。

#### Scenario: 解析失败过多
- **WHEN** C0 解析失败占比 7%
- **THEN** 停止流程，不启动第二批

### Requirement: Phase C 第二批（R1~R5 + C2 六路并发）
`mode=full|skip_sampling` 时，Coordinator SHALL 先发出 5 个 `topic6-annotator` 委派（task=`r1..r5`，input=`04_标注/_可用子集/usable_subset_{mode}_r{N}.xlsx`），**立即**启动 C2，MUST NOT 等 R1~R5 返回后才启动 C2：

```bash
python /mnt/skills/topic6-event-registry/scripts/launch_c2_background.py \
  --project-dir "{project_dir}" --mode {mode} --run-id {N}
```

（幂等，已在运行返回 `already_running`）。随后用 `wait_datahub_batch.py --tasks r1,r2,r3,r4,r5` 与 `wait_c2_complete.py --project-dir … --run-id {N} --wait-seconds 105` 并行等待，均 `timeout=115`，`running` 原参数重调。C2 内部拓扑固定：四平台并行 `00_clean_titles` → `x0_merge_platforms` → merged 目录执行 `01→…→07→x2→x3→x4` → `08_rank_events` → `09_commit_registry`；MUST NOT 在 x0 前按平台跑 01~04，MUST NOT 把 `00_seed_from_registry.py` 当起点。08/09 失败只记 `c2_status.registry.status=failed`，C2 仍算 `done`，只需在 HC `issues_detected` 注明"Registry 未落库"。C2 Chat 模型 `$C2_CHAT_MODEL_ID`（默认 `Doubao-Seed-Evolving`），Embedding `$EMBEDDING_MODEL_ID`（默认 `Doubao-embedding`）。R 路 `degraded` 时缺失路不发布、不记账，并 MUST 写入 HC `issues_detected`。

`mode=demo` 时 MUST NOT 创建 5 个 annotator 或运行完整 C2，改为单入口（bash `timeout=900`）：

```bash
python /mnt/skills/topic6-annotation/scripts/run_demo_batch2.py \
  --project-dir "{project_dir}" \
  --input "{project_dir}/04_标注/_可用子集/usable_subset_demo_r{N}.xlsx" --run-id {N}
```

失败时先读 `04_标注/demo_routes.log`，最多以相同参数重跑一次；MUST NOT 用 inline Python/临时脚本绕过、精简 Prompt、切换模型、改 batch size 或删 checkpoint。`--demo-fast` C2 仅用于 50 条演示。

#### Scenario: C2 串行启动违约
- **WHEN** Coordinator 等 R1~R5 完成后才启动 C2
- **THEN** 违反并发契约（总时长显著增加）

#### Scenario: demo 第二批失败两次
- **WHEN** `run_demo_batch2.py` 重跑后仍失败
- **THEN** 如实回报错误并停止

### Requirement: Phase D 合并与质量判定
Coordinator SHALL 运行 `merge_annotations.py` 产出 `05_合并/wide_table_{mode}_r{N}.xlsx`（28 列，标识列为 `序号`）。关卡以 stdout `gate_ok` 为准（行数守恒且 `dup_rows=0`），`false` 时停止。综合质量 `quality_status`：
- `fail`（有效率 <80%、C0 缺失或 ≥2 路整批缺失）且上游未经用户裁决 → `[need_decision]`（A 继续送 HC / B 停止）；
- `degraded`（恰 1 路整批缺失）或 `warn`（有效率 80%~92%、营销可用率偏离 37%~41% 基线、某平台 <5%、单簇超过营销可用行 10%）→ 不停，`quality_warnings` 逐条写入 HC `issues_detected`。

关卡通过后 MUST 先发布 `node=merge`，HC1/HC2 的 `wide_table_path` MUST 指向该已发布文件。

#### Scenario: 合并丢行
- **WHEN** `gate_ok=false`
- **THEN** 停止并报告，不输出 HC 载荷

#### Scenario: 警告级质量
- **WHEN** `quality_status=warn`
- **THEN** 继续输出 HC，`issues_detected` 包含每条 warning

### Requirement: HC 结构化输出硬约束
进入 HC1/HC2/HC3 卡点时，Coordinator SHALL 在**同一条且唯一一条** assistant message 中输出 HC 载荷：第一个字符 MUST 是 ``` （```json 块开头），前面不得有铺垫、总结或"现在输出/等用户确认"之类元描述；JSON MUST 可被 `json.loads` 解析；MUST 只含 schema 定义字段（禁止 `next_step_if_passed`、`notes`、`_meta` 等）；块闭合后立即 end_turn。违约将触发 Gateway 文本意图兜底（`__fallback__` 卡片）。

#### Scenario: 先输出进度再输出 JSON
- **WHEN** Coordinator 先发"准备 HC1"文字后结束本轮
- **THEN** JSON 永久缺失，Gateway 只能生成兜底卡片（契约违约）

#### Scenario: 自造字段
- **WHEN** 载荷包含 `next_step_if_passed`
- **THEN** 违反契约，该字段不被采信

### Requirement: HC1/HC2 载荷 schema
HC1（full/demo/smoke 首轮）与 HC2（全量轮）载荷 SHALL 为：

```json
{
  "hc": "HC1|HC2",
  "mode": "HC1: full|demo；HC2: full|skip_sampling",
  "project_dir": "/workspace/Projects/{PROJECT_DIR}",
  "wide_table_path": "/mnt/session/outputs/{PROJECT_DIR}/05_合并/wide_table_{HC1: mode | HC2: skip_sampling}_r{N}.xlsx",
  "distribution_summary": "merge stdout 的 hc_metrics 原样，不得增删或自行计算",
  "issues_detected": ["quality_warnings 逐条 + 批次 degraded/quality_warn 说明"],
  "validator_verdict": "可选，Validator 结论或 SKIP 兜底"
}
```

输出前 MAY 委派 `topic6-validator`（HC1 task=`hc1`，audit_files 为已发布的宽表、health_summary、phase1_merged、usable_subset；HC2 task=`hc2`，audit_files 为全量宽表与 health_summary，`previous_issues` 取 HC1 verdict 的 issues），等待最多 120s；失败/超时 MUST 填 `{"conclusion":"SKIP","audit_summary":"Validator 未就绪"}`，Validator 失败 MUST NOT 阻塞 HC。

#### Scenario: Validator 超时
- **WHEN** Validator 120s 内未返回
- **THEN** HC1 载荷带 `validator_verdict.conclusion=SKIP` 照常输出

### Requirement: HC 决策处理
Gateway 注入的决策消息格式固定为 `HCx pass` / `HCx reject: <原因>` / `HCx remark: <补充>`（remark = 带备注通过）。Coordinator SHALL 用 `run_config_state.py --project-dir … --patch-json '<JSON>'` 写回状态：
- `pass`/`remark` → `{"hc{K}_passed": true}`（remark 另写 `hc{K}_note`），按模式继续；
- `reject` → 写 `{"hc{K}_passed": false, "hc{K}_reject_reason": "…"}`，MUST NOT 自行回退或重跑，MUST 输出 `[need_decision]`：原因原文 → 受影响任务（C0/C3 第一批、R1~R5/C2 第二批、E1~E4）→ 选项。HC1/HC2：A) 以 `run_id=N+1` 从 Phase C 第一批完整重跑本轮（不支持只重跑单路）；B) 停止待 Prompt 升版（本 Session 不得改写 Prompt）；C) 在飞书在线表格修正后改点通过。HC3：A) 按意见修改报告；B) 退回 Phase E 原参数重跑；C) 停止。
- 用户 `/continue <选择>` 后执行对应分支，选 A 重跑时所有 `{N}` 替换为新 run_id。

决策消息含 `/mnt/session/uploads/hc_edits/...` 路径时，Coordinator MUST 先用该文件覆盖消息指明的宽表并核对行列数；否则直接使用沙箱宽表。MUST NOT 自行搜索或下载飞书云空间文件。

#### Scenario: HC1 打回
- **WHEN** 收到 `HC1 reject: R3 风险误报多`
- **THEN** 写回 `hc1_passed=false`，输出 `[need_decision]` 列出 A/B/C 并 end_turn，不自动重跑

#### Scenario: 在线修改后通过
- **WHEN** 决策消息附带 `/mnt/session/uploads/hc_edits/HC1/wide_table_full_r1.xlsx`
- **THEN** 先覆盖 `05_合并/wide_table_full_r1.xlsx` 并核对后再继续

### Requirement: 模式分支与全量轮
HC1 通过后 SHALL 按模式分支：
- `full`：进入"C→D 全量"，输入为 `02_标准化/hot_topics_normalized.xlsx`，再经 HC2；
- `smoke`：进入"C→D 全量"，但输入为 `sample_500.py --size 30 --seed 99` 的重抽样（不同 seed），再经 HC2；smoke 声明由 `pipeline_f.py` 自动写入，Agent 不得追加；
- `demo`：直接进入 Phase E（使用 `wide_table_demo_r{N}.xlsx`），MUST NOT 重跑全量，跳过 HC2；报告 MUST 注明"基于 50 条分层样本，仅供流程演示，不可作为正式全量结论"；
- `skip_sampling`：初始即跑全量 C→D，不进入 HC1，完成后进入 HC2。

全量轮 MUST 固定 `artifact_mode=skip_sampling`：C0/C3、筛选、R1~R5、C2、合并一律传 `--mode skip_sampling`，路径 `{mode}` 一律替换为 `skip_sampling`，产物为 `wide_table_skip_sampling_r{N}.xlsx`；run_config 顶层 `mode` 保持原值（full/smoke）。MUST NOT 改传 `--mode full`、复制或重命名产物绕过。

#### Scenario: full 全量轮查找子集
- **WHEN** full 模式进入全量轮
- **THEN** 使用 `usable_subset_skip_sampling_r{N}.xlsx`，不得查找 `usable_subset_full_r{N}.xlsx`

#### Scenario: demo 通过 HC1
- **WHEN** demo 模式收到 `HC1 pass`
- **THEN** 直接进入 Phase E，不出现 HC2

### Requirement: Phase E 洞察
Coordinator SHALL 直接调用一次 `pipeline_e.py --project-dir … --mode {demo|skip_sampling} --publish-date … --version {N}`（脚本内 E1~E4 流式有限并发、断连重试、版块级 checkpoint），MUST NOT 再委派 4 个 `topic6-insighter`。full/skip_sampling 使用 `wide_table_skip_sampling_r{N}.xlsx`，demo 使用 `wide_table_demo_r{N}.xlsx`。脚本前置校验：非 demo 且 `status.hc2_passed != true` 退出 1；输出 ≤100 bytes 视为版块失败；结果写回 `status.e_insights`。任一版块失败时先读 `06_洞察/v{N}/pipeline_e_report_v{N}.json`，最多原参数重跑一次（复用成功版块）；MUST NOT 拆成四次 `--sections`、用 inline Python 绕过、改 Prompt、换模型、提高并发或删 checkpoint。四个 `e1~e4_v{N}.md` 全部成功才可进入 Phase F，并发布 `node=insight`（`06_洞察/v{N}`）。E2 依赖 C3 标注与 `marketing_calendar_2026.csv`。

#### Scenario: 绕过 HC2
- **WHEN** full 模式未通过 HC2 就运行 `pipeline_e.py`
- **THEN** 脚本退出 1

#### Scenario: 单版块失败
- **WHEN** 首次运行 E3 失败
- **THEN** 原参数重跑一次，只补 E3；仍失败则 Phase E 失败并停止

### Requirement: Phase F 合并、自校验与飞书发布
Coordinator SHALL 运行 `pipeline_f.py --mode {demo|skip_sampling}` 合并报告，然后 MUST 运行 `report_self_check.py --project-dir … --report-file "07_报告/热点报告_{period_label}_v{N}.md"`：`pass`/`warn` 继续（warn 的 issues 写入 HC3 `issues_detected`）；`fail` 时不得在本阶段改写报告，退回 Phase E 仅重跑 `failed_sections`（`--skip-data-prep --sections …`，`--sections` 的唯一合法用途且仅一次），再重跑 F 与自校验；第二次仍 fail → `[need_decision]`（A 带问题发布由 HC3 修订 / B 停止）。

发布 MUST 只用唯一入口：

```bash
python /mnt/skills/topic6-insight/scripts/publish_report_to_feishu.py \
  --project-dir "{project_dir}" --report-file "07_报告/热点报告_{period_label}_v{N}.md" \
  --folder-name "热点周报_{period_label}_{mode}" --doc-name "热点报告_{period_label}"
```

主路径为 `lark-cli --as bot`（身份来自 Gateway 注入的 tenant token），MUST NOT 执行 `lark-cli config init`、自行拼 lark-cli 命令、切换路径重试或编排权限步骤；脚本内部在 lark-cli 不可用时自动切换 `feishu_api_fallback`。成功 stdout 为 `{"status":"success","backend":"lark-cli|feishu_api_fallback","doc_url":".../docx/...","warnings":[...]}`。

`warnings`（转移 owner / admin 授权失败）不阻断，原样记录。脚本退出码 1 + `{"status":"failed","error":…}`（含给发起人授 edit 失败）或 `doc_url` 不是 `/docx/` URL 时，Phase F MUST 判失败、不输出 HC3、原文报告 `error`，不得原参数反复重跑，不得用本地 Markdown 路径代替飞书文档。成功后发布 `node=report`（`07_报告/热点报告_*_v{N}.md`），并写回 `status.f.feishu_doc_url`。MUST NOT 执行 `lark-cli auth login`、Device Flow 或降级 `--as user`。

#### Scenario: 发布失败
- **WHEN** `publish_report_to_feishu.py` 返回 `status=failed`
- **THEN** Phase F 失败，不输出 HC3，向用户报告 error 原文

#### Scenario: 自校验失败
- **WHEN** `report_self_check.py` 返回 `fail` 且 `failed_sections=["E4"]`
- **THEN** 以 `--sections E4 --skip-data-prep` 重跑 Phase E 一次，再重跑 F

#### Scenario: Session 无 bot token
- **WHEN** `LARKSUITE_CLI_TENANT_ACCESS_TOKEN` 未注入
- **THEN** 发布脚本自动走 `feishu_api_fallback`，Coordinator 不额外处理

### Requirement: HC3 载荷与打回
Phase F 成功后，Coordinator MAY 委派 `topic6-validator`（task=`hc3`，audit_files=本地报告 md，context 含 `mode`、`feishu_doc_url`、`report_sections`），HC3 的 `validator_verdict.coverage_note` MUST 非空；兜底同 HC1。HC3 载荷 SHALL 为：

```json
{
  "hc": "HC3",
  "project_dir": "/workspace/Projects/{PROJECT_DIR}",
  "feishu_doc_url": "https://xxx.feishu.cn/docx/xxx",
  "note": "请在飞书文档中审核并按需调整图片,完成后点击卡片按钮",
  "issues_detected": ["自校验 warn 项逐条，或空数组"],
  "validator_verdict": {}
}
```

`feishu_doc_url` 为空时 MUST NOT 输出 HC3（Gateway 也会判失败）。通过后进入 Phase G。打回选项：A) 仅修改报告中对应文字（数字只能来自宽表），重跑自校验与发布（新文档并更新 `status.f.feishu_doc_url`）后重新输出 HC3；若用户已在飞书文档直接修改则以飞书为准，提示用户直接改后点通过；B) 受影响版块 `--sections` 重跑一次 → F → 自校验 → 发布 → HC3；C) 停止。

#### Scenario: 用户已在文档内修改
- **WHEN** HC3 打回意见针对用户已在飞书文档改过的段落
- **THEN** 不用本地 md 覆盖，提示用户在文档中修改后点通过

### Requirement: Phase G 网页生成
HC3 通过后 Coordinator SHALL 只用固定脚本生成网页（MUST NOT 手工拼 `source.json`）：复制 `topic6-web-report/assets/source/` 到 `08_UI发布/热点报告_{period_label}_web` → `validate-template-assets.mjs` → `fetch_case_images.py --doc {feishu_doc_url}`（失败不阻断）→ `md2source.py <merged_report> source.json --case-images` → `validate-case-image-sources.mjs` → `build-report.mjs` → `validate-rendered-content.mjs`。任一校验失败 Phase G 失败，MUST NOT 通过改 source.json 文本迎合校验。案例配图只来自用户在 HC3 上传到飞书文档"案例 N："之后的图片；缺图渲染"配图待补充"占位，MUST NOT 调生图工具、复用模板 `case-*.jpg` 或搜图，也不因缺图停下；`case_images_missing` 写入 `status.g.case_images_missing`。MUST NOT 执行 `upload-html.mjs`。完成后发布 `node=web`（`08_UI发布/**/index.html` 及引用的静态资源），写回 `status.g.completed=true`、`status.current_phase=h_miaoda_publish` 并立即进入 Phase H。

#### Scenario: 无案例配图
- **WHEN** HC3 期间用户未上传任何图片
- **THEN** 网页渲染占位，流程继续进入 Phase H

### Requirement: Phase H 妙搭发布与最终结果
Coordinator SHALL 严格执行 `/mnt/skills/miaoda-web-publish/SKILL.md` 的预检、CLI 解析、用户 OAuth、Git 管理发布与链接解析契约，全程 `--as user`（不存在 `MIAODA_TOKEN`）。`status.h.app_id` 为空时新建 Git 应用，非空时更新同一应用；仅遗留非 Git HTML 应用可用 `+html-publish`。成功时最后一条消息 MUST 包含且只包含一个 JSON：

```json
{"phase":"H","release_status":"finished","online_url":"<发布 API 返回的 HTTPS URL>"}
```

`online_url` MUST 直接取自 `+release-get`/`+get` 响应，不得拼接或改写；`*.aiforce.cloud`、`*.feishuapp.cn/app/app_*`、`{租户}/page/{meta_token}` 均有效；跳转飞书登录页属正常。OAuth 未完成、发布失败或超时 MUST 保持 `status.h.completed=false` 并报告阻塞，MUST NOT 输出"流程完成"或以 `feishu_doc_url`、本地路径、snapshot URL 代替。

#### Scenario: 发布成功
- **WHEN** release_status=finished
- **THEN** 最后一条消息为上述 JSON，Gateway 据此 `mark_done`

#### Scenario: 用户未授权
- **WHEN** 沙箱缺少 `LARKSUITE_CLI_USER_ACCESS_TOKEN`
- **THEN** 报告阻塞，不输出 online_url，Gateway 判 Phase H 失败

### Requirement: 批次结果处理
DataHub 批次结果 SHALL 以 `wait_datahub_batch.py` 返回的 `status` 为唯一依据（worker 内部已原地重提 1 次，Agent 不得再手工重提）：

| status | 含义 | 处理 |
|---|---|---|
| `running` | 仍有任务在跑 | 原参数再调 |
| `complete` | 全部完成且有效率 ≥ 80% | 继续；`quality_warn_tasks`（80%~92%）写入 HC `issues_detected` |
| `degraded` | 恰 1 路非 C0 任务重提后仍失败 | 标记缺失继续，写入 `issues_detected` |
| `quality_fail` | 有任务有效率 < 80% | `[need_decision]`：A) 接受并继续 B) 停止，调整 Prompt 后重跑该批次 |
| `failed` | C0 失败，或同批 ≥ 2 路失败 | 立即停止并报告 `reason` |

#### Scenario: C0 失败
- **WHEN** 第一批返回 `failed`（C0 重提后仍失败）
- **THEN** 立即停止，不进入筛选

### Requirement: 阶段标记与裁决标记
Coordinator SHALL 在每个阶段开始前单独输出 `[phase] <X>`（如 `A`、`C2`、`E`、`F`、`HC3`、`H`），Gateway 据此更新 `current_phase`。需要用户拍板时，该条消息**第一行** MUST 单独输出 `[need_decision]`，随后写阻断报告与选项并 end_turn；普通进度与 HC 卡点 MUST NOT 输出该标记。MUST NOT 使用 AskUserQuestion。

#### Scenario: Phase H 标记
- **WHEN** 进入妙搭发布
- **THEN** 先输出 `[phase] H`，Gateway 在 idle 时据此执行 Phase H 专项判定

### Requirement: 行为规则
Coordinator SHALL 遵守：R01 触发词精确匹配；R02 模式路径（full/smoke HC1→HC2→HC3，demo HC1→HC3，skip_sampling HC2→HC3）；R03 每个 LLM 任务完成后调用 cost_tracker；R04 路径只基于 `/workspace`、`/mnt/*`；R05 每次启动必读 `_版本状态.md`；R06 Prompt 只增不改；R07 报告数字全部来自宽表；R08 阶段内并发、跨阶段串行；R11 API Key 从环境变量读取；R13 memory 只读；R14 节点仅在成功且关卡通过后发布。并发子 Agent MUST NOT 用 edit/write 直接改 `run_config.yaml`，所有状态经 `run_config_state.py` 原子更新；Coordinator 只在阶段边界用 `--phase` 更新 `status.current_phase`（`c0_* → c0_filter_* → c_route_* → d_merge_* → hc1_wait|hc2_wait`）；恢复时已 `done` 的 DataHub/C2 分支 MUST NOT 重新提交。skill 内 prompt/ks 与本 Prompt 冲突时以 Coordinator Prompt 为准。

成本记录：`cost_tracker.py --project-dir … append --phase --task --model-id --platform --input-tokens --output-tokens --raw-cost --currency`，产出在 `costs/`（不进 outputs）。

#### Scenario: 报告数字估算
- **WHEN** 洞察文本中出现宽表中不存在的数字
- **THEN** 违反 R07，应由自校验/Validator 标出

#### Scenario: Session 恢复
- **WHEN** `init_project.py` 返回 `resumed` 且 `status.c2.status=done`
- **THEN** 不重新启动 C2
