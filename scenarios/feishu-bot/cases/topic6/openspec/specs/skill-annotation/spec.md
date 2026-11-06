# skill-annotation Specification

## Purpose
Skill `topic6-annotation`（Phase C + D 及共享基础设施）：项目初始化、run_config 状态管理、节点产物发布、DataHub 多路标注（C0/C3/R1~R5）、批次等待、C0 筛选、demo 快速路由、七路合并宽表、成本台账，以及 C0/R1~R5/C3 Prompt 与判定口径知识库。

源码：`ma-resources/skills/topic6-annotation/`（`scripts/`、`prompts/`、`ks/`、`references/marketing_calendar/`、`tool/cost-tracker/`）。

## Requirements

### Requirement: 项目初始化脚本
`scripts/init_project.py --text <原始消息> --mode {demo|smoke|full|skip_sampling}` SHALL 是唯一的周期解析与项目创建入口，stdout 单行 JSON `{status: created|resumed|ambiguous, project_dir, mode, period, fetch, current_phase, [reason, options, resolutions]}`。周期规则：
- 无日期 → 今天所在周之前最近一个完整自然周（周一~周日），`weekly`；
- `W35`/`第35周` → `--today` 所在 ISO 年的该周，`weekly`；
- `MM-DD到MM-DD` → `custom`；
- `双周`/`连续两周` → 起始周 + 次周共 14 天，`biweekly`；
- `继续W36` → 查找唯一的 `/workspace/Projects/W36*/run_config.yaml` 续跑。

目录名 `{period_id}{热点周报|双周报|自定义}_{YYYYMMDD}-{YYYYMMDD}`；`period_label = {ISO年}-W{NN}`；取数窗口 `fetch_start = date_start − 7d`、`fetch_end = date_end + 1d`。以下情况 MUST 返回 `ambiguous` 及 A/B/C 选项与 `resolutions`（参数数组或 null）：多种互斥写法、日期非法、结束早于开始、周次不存在、周期尚未结束（可 `--allow-incomplete`）、续跑目标为 0 个（可 `--new-week`）或多个（`--resume-dir`）、同名项目已存在但 mode 不同（`--resume-dir` 续跑 / `--overwrite` 重跑）。新建时 run_config 原子写入，顶层块 `mode/period/fetch/cost.usd_to_cny=7.2/status{created_at,current_phase=init}`。

#### Scenario: 默认周期
- **WHEN** 今天 2026-09-30（周三），`--text "热点周报 demo"`
- **THEN** `status=created`，周期为 2026-09-21~09-27，`fetch` 为 09-14~09-28

#### Scenario: 周期未结束
- **WHEN** 请求本周 W40 而今天在 W40 内
- **THEN** 返回 ambiguous，选项含 `--allow-incomplete`

#### Scenario: 已存在不同 mode 的项目
- **WHEN** 同周期已有 `mode=full` 项目，本次 `--mode demo`
- **THEN** 返回 ambiguous：A 续跑原项目 / B 覆盖重跑 / C 停止

### Requirement: run_config 原子状态更新
所有脚本 SHALL 通过 `run_config_state.update_run_config(project_dir, patch, current_phase=None)` 修改 `run_config.yaml`：sidecar `.run_config.yaml.lock` 上 `fcntl.flock` 排他锁 → 读取 → 深合并 patch 到 `status` → 写 `status.updated_at` → 临时文件 + `os.replace`。`run_config.yaml` 不存在时返回空 dict 不报错。CLI：`run_config_state.py --project-dir … [--phase X] [--patch-json '{…}']`，patch 非 JSON 对象时退出。`run_config_mode()` 读顶层 mode；`uses_sample_base()` 决定合并基础表（full/demo 用样本；顶层 mode=smoke 时即便传 `skip_sampling` 也用 30 条样本）。

#### Scenario: 并发写
- **WHEN** C0 与 C3 worker 同时写各自状态块
- **THEN** 两个块都保留，无部分写入的 YAML

### Requirement: 节点产物发布脚本
`scripts/publish_node_artifacts.py --project-dir --node --run-id --include … [--optional-include …]` SHALL：要求 project-dir 是 `/workspace/Projects` 的直属子目录（`resolve(strict=True)`）；include 必须为项目相对路径或 glob（禁止绝对路径与 `..`）；必选 include 未命中 → `FileNotFoundError`；拒绝符号链接与逃逸项目目录的文件；目录 include 递归展开；在 outputs 项目目录锁内逐文件原子复制到 `/mnt/session/outputs/{project}/{相对路径}`，并以 `{node, run_id, status:complete, published_at, files:[{source, output:"{project}/{rel}", size, sha256}]}` 替换同 node+run_id 的旧 publication 后原子写 `manifest.json`。

#### Scenario: 同节点重发
- **WHEN** `node=merge run_id=1` 发布两次
- **THEN** manifest 中只保留最新一条 merge/1 记录

#### Scenario: 符号链接
- **WHEN** include 命中一个 symlink
- **THEN** 抛 ValueError，不发布

### Requirement: DataHub 单任务标注
`scripts/datahub_annotate.py --task {c0,c3,r1..r5} --project-dir --input --prompt-file --model-id [--run-id N] [--mode] [--launch-background]` SHALL 完成上传 → 建任务 → 轮询（间隔 20s，超时 24h）→ 下载（成功态无 `result_url` 时分页读 `result_list`）→ JSON 展开（剥离 ``` 围栏与 `<think>`）→ C3 时效窗口计算 → 写 `04_标注/{任务目录}/{task}_result_raw_r{N}.xlsx`、`{task}_postprocess_r{N}.xlsx`、`{task}_completion_meta.json`。API Key 优先级 `DATAHUB_API_KEY_OFFICE` > `DATAHUB_API_KEY_HOME` > `DATAHUB_API_KEY`；服务地址 `https://bmc-data-hub.bluemediagroup.cn`。任务级失败 MUST 原地重提最多 `MAX_TASK_RESUBMITS=1` 次，计数落 `submit_meta`（worker 重启不无限重提）。有效率阈值 pass ≥ 0.92、warn ≥ 0.80；有效行 MUST 为 JSON 可解析且主字段非空，主字段为空的行计入 `{task}_parse_error` 并参与失败行重提（stats 记 `empty_primary_count`）。失败行 MUST 自动重提最多 `FAILED_ROW_RETRY_ROUNDS=3` 轮，每轮只提交上一轮仍失败的行，各轮 task_id 落 `{task}_retry[_N]_meta.json` 可续跑，tokens/消耗计入 completion_meta。失败时 MUST 写回对应 status 块 `{status:failed, run_id, error, failed_at}` 并退出 1。

`--launch-background` MUST 显式带 `--run-id`，幂等语义：`completion_meta` 与本次请求（输入/run_id 等）匹配 → `done`；同 run_id 但输入不同 → 删除旧 completion 后重启；PID 文件 `{task}_worker_r{N}.pid` 对应进程存活 → `running`；否则启动 worker 并原子写 PID → `submitted`。

C3 时效窗口：节点定义来自 `references/marketing_calendar/nodes_definition*.csv`；类型覆盖 `大众节日=(30,7)`，节点覆盖 `高考=(14,90)`。

#### Scenario: 重复启动
- **WHEN** 同一 task/run_id 的 worker 仍在运行时再次 `--launch-background`
- **THEN** 返回 `running` 与现有 PID，不启动第二个 worker

#### Scenario: 同轮换输入
- **WHEN** run_id=1 已 done，但本次 input 不同
- **THEN** 作废旧 completion_meta 并重新提交

### Requirement: 批次等待器
`scripts/wait_datahub_batch.py --project-dir --tasks <csv> --run-id N --wait-seconds ≤105` SHALL 在单次调用内最多阻塞 `wait-seconds`，按各任务 status 块与 PID 存活判定（PID 不存活且未完成视为 `missing`），返回：
- `failed`（退出 1）：C0 失败（`CRITICAL_TASKS={c0}`）或同批 ≥ 2 路失败/缺失；
- `running`：仍有任务在跑；
- `quality_fail`：无系统性失败但有任务有效率 < 0.80（附 `quality_fail_tasks`）；
- `degraded`：恰 1 路非 C0 失败/缺失（附 `missing_tasks`）；
- `complete`：全部完成且有效率 ≥ 0.80（80%~92% 列入 `quality_warn_tasks`）。

阈值 MUST 与 `datahub_annotate.VALID_THRESHOLDS` 一致。

#### Scenario: 两路失败
- **WHEN** R2、R4 均失败
- **THEN** 返回 `failed`，退出码 1

#### Scenario: 单路失败
- **WHEN** 仅 R3 失败
- **THEN** 返回 `degraded`，`missing_tasks=["r3"]`

### Requirement: C0 合并与可用子集筛选
`c0_merge_phase1.py --project-dir --mode --run-id` SHALL 把 C0 + C3 结果 LEFT JOIN 到基础表输出 `04_合并/phase1_merged_{mode}_r{N}.xlsx`。`c0_filter_usable.py --project-dir --mode --run-id` SHALL 把 C0"是否营销可用=是"的行与 C0 未决行（解析失败/缺失）输出到 `04_标注/_可用子集/usable_subset_{mode}_r{N}.xlsx`（保留 phase1_merged 全部列），明确"否"的行排除；未决行的"是否营销可用"保持为空，stdout MUST 含 `unresolved_rows`（row_id/platform/title）供 HC1 判定。解析失败/缺失占比 > `UNRESOLVED_THRESHOLD=0.05` MUST 抛错退出，MUST NOT 提供强制放行参数。`datahub_annotate.py` 解析 C0/R1~R5 结果时 SHALL 对残缺结尾（缺 `}`、多余 `"`/`]`）做受控补全，补全结果的主字段 MUST 为"是/否"才计为有效。

#### Scenario: 未解析 6%
- **WHEN** C0 有 6% 行解析失败
- **THEN** 筛选脚本报错退出

#### Scenario: 未决 2%
- **WHEN** C0 有 2% 行解析失败或缺失
- **THEN** 这些行与"是"行一起进入可用子集，`unresolved_rows` 列出明细，合并宽表 `C0 未决行` 为 warn 并写入 HC1 issues

#### Scenario: JSON 结尾残缺
- **WHEN** C0 返回 `{...,"是否营销可用":"是","判断说明":"..."` 缺结尾 `}`
- **THEN** 补全后计为有效行，不进入失败重提

### Requirement: demo 第二批快速路径
`run_demo_batch2.py --project-dir --input --run-id` SHALL 并行启动 `run_demo_routes.py`（R1~R5）与 `run_topic6_c2.py --demo-fast`，等待两者并输出结构化 JSON，任一失败退出 1。`run_demo_routes.py` SHALL 复用 R1~R5 正式 Prompt 与输出列，默认 5 个 route worker、每批 5 行、最多 5 次尝试，经 Ark 调用；每个成功批次原子写 `{route}_demo_checkpoint_r{N}.json`，输入/模型/Prompt/run_id/batch size 不匹配时 checkpoint 失效；重跑只补缺失批次。该路径 MUST 仅用于 demo。

#### Scenario: demo 中断后重跑
- **WHEN** R4 第 3 批失败后重跑同一命令
- **THEN** 只重新调用缺失批次

### Requirement: 七路合并宽表
`merge_annotations.py --project-dir --mode --run-id` SHALL 输出 28 列 `05_合并/wide_table_{mode}_r{N}.xlsx`（基础 9 列 + C0 7 列〔商业实体、热点驱动词、行业归属、营销触发方式、营销维度、是否营销可用、判断说明〕+ 提取节点、事件簇名 + R1~R5 各 2 列〔是否平台玩法/平台玩法说明、是否商业合作/合作动态说明、是否风险预警/风险判断说明、是否营销发现/营销发现说明、是否消费者行为/消费者行为说明〕），`row_id` 改名为 `序号`；单路缺失补空列只告警不中断，并写 `health_summary_{mode}_r{N}.md`。stdout JSON MUST 含 `gate_ok`（行数守恒且 `dup_rows=0`）、`quality_status`、`quality_checks`、`quality_warnings`、`hc_metrics`。质量判定：任一路有效率 <0.80 或 ≥2 路整批缺失或 C0 缺失 → `fail`；恰 1 路缺失 → `degraded`；有效率 0.80~0.92、C2 营销可用行事件簇非空率 ≤0.50、C0 未决行 >0、C0 营销可用率偏离 37%~41% 超过 5pp、某平台占比 <5%、最大事件簇 >营销可用行 10% → `warn`；否则 `pass`。`quality_status` MUST NOT 改变 `gate_ok`。

#### Scenario: 行数守恒
- **WHEN** 基础表 500 行，合并后 500 行且无重复序号
- **THEN** `gate_ok=true`

#### Scenario: C2 缺失
- **WHEN** C2 结果文件不存在
- **THEN** `事件簇名` 为空列，`quality_status=degraded`，health_summary 醒目提示

### Requirement: 成本台账
`tool/cost-tracker/cost_tracker.py --project-dir … append --phase --task --model-id --platform --input-tokens --output-tokens [--raw-cost --currency --round --prompt-version --mode --record-id]` SHALL 追加到 `costs/cost_tracker.jsonl` 并刷新 `costs/本周成本汇总.md`；`record_id` 非空且已存在时 MUST 跳过（幂等）。DataHub 记账使用 completion_meta 的实际模型与 `total_consume`。

#### Scenario: 恢复后重复记账
- **WHEN** 以相同 `--record-id datahub:c0:r1:full` 再次 append
- **THEN** 打印"已存在，跳过重复记录"，jsonl 不变

### Requirement: Prompt 版本管理
活跃版本 SHALL 为 C0 v13、R1 v2、R2 v2、R3 v1、R4 v2、R5 v3、节点标注 v7（SKILL.md 为权威源，memory `_版本状态.md` 为镜像）。Prompt 文件 MUST 只增不改：调整时新建 `v{N+1}.md`，旧版保留（C0 例外：`C0_基础事实/` 目录只保留活跃版本，升版后旧版移除、历史以 git 为准，避免多版本并存造成误取），重新打包上传生成新 SkillHub version，并同步 `SKILL.md`、`_版本状态.md`、`version_mapping.md`。`ks/`（9 篇判定口径）与 `prompts/00~09`、`run_config契约.md`、`运行前置.md` 为客户原样迁入文件，修改属于业务口径变更。

#### Scenario: 升级 C0
- **WHEN** 需要调整 C0 判定
- **THEN** 新增 `C0_基础事实/v14.md` 并移除 v13（目录内只留 v14），更新三处版本记录并重新上传 Skill
