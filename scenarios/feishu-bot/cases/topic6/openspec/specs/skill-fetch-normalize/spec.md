# skill-fetch-normalize Specification

## Purpose
Skill `topic6-fetch-normalize`（Phase A+B + 抽样）：从 hot-topics MCP 拉取四平台热搜、清洗并锁定完整 ISO 周、log1p+P1/P99 标准化到 [40,100]、分层抽样。

源码：`ma-resources/skills/topic6-fetch-normalize/`（`SKILL.md`、`scripts/fetch_hot_topics.py`、`scripts/log1p_p1p99_normalize.py`、`scripts/sample_500.py`、`references/平台热度基准_2026.json`、`references/marketing_calendar_2026.csv`）。

## Requirements

### Requirement: 路径约定
三个脚本 SHALL 接受 `--project-dir`，绝对路径直接使用，相对路径以 `/workspace`（或 `MA_WORKSPACE`）为根；产出固定落在项目目录下的 `01_原始数据/`、`02_标准化/`、`03_抽样/`。脚本 SHALL 通过 `run_config_state.update_run_config` 原子写回 `status`，`run_config.yaml` 不存在时只 warning。

#### Scenario: 相对项目目录
- **WHEN** `--project-dir Projects/W35_x`
- **THEN** 实际目录为 `/workspace/Projects/W35_x`

### Requirement: Phase A 取数与清洗
`fetch_hot_topics.py` SHALL 通过平台挂载的 hot-topics MCP 取数（API Key 优先级 `BLUEAI_API_KEY` > `HOT_TOPICS_API_KEY`，由平台注入；MUST NOT 回退 `ARK_API_KEY`，方舟 Key 对取数无效），`--start/--end` 缺省时读 `run_config.fetch.fetch_start/fetch_end`（由 `init_project.py` 写入）。清洗 MUST：只保留平台 `{微博, 抖音, B站, 知乎}`；去重；选取数据中最近的**完整 7 天** ISO 自然周（找不到时退出 1 并说明最近周仅有几天）；生成 `week_num=week{NN}`、`row_id=T{i:04d}`。产出 `01_原始数据/hot_topics_skill_raw.json` 与 9 列 `hot_topics_raw.xlsx`：`row_id, platform, title, hottopic_desc, hot_index, hotpost_time, hottopic_url, hot_thumbnail_url, week_num`。关卡：四平台齐全且去重后行数 ≥ `MIN_ROWS=200`，否则退出 1。支持 `--skip-fetch` 复用已有原始 JSON。

#### Scenario: 缺平台
- **WHEN** 取数结果没有 B站
- **THEN** 退出码 1，stderr 报缺失平台

#### Scenario: 窗口不足一周
- **WHEN** 数据只覆盖最近 ISO 周的 5 天
- **THEN** 退出码 1，提示未找到完整 7 天 ISO 周

### Requirement: Phase A 数据质量校验
清洗后、关卡校验前，`fetch_hot_topics.py` SHALL 写 `01_原始数据/data_quality_report.json`（原始条数、清洗后行数、各字段空值率、hot_index≤0 行数、row_id 重复数、hard_errors、soft_warnings）。必填列（row_id、platform、title、hot_index、hotpost_time）整列缺失、空值率 ≥99% 或 row_id 重复 MUST 退出 1，不可人工放行；空值率或 hot_index≤0 占比 >5% 只预警，写入 `status.ab.quality_warnings`，由 HC1 列入 `issues_detected`。

#### Scenario: 标题列整列为空
- **WHEN** 上游返回的 title 全部为空
- **THEN** 退出码 1，报告 hard_errors 含 title

### Requirement: Phase B 标准化
`log1p_p1p99_normalize.py` SHALL 按 `references/平台热度基准_2026.json` 中各平台 p1/p99 计算：`heat_raw = hot_index × 10000`，`log_heat = log1p(heat_raw)`，`heat_score = clip((log_heat − p1)/(p99 − p1), 0, 1) × 60 + 40`，并在 `hot_index` 右侧插入 `heat_score` 输出 10 列 `02_标准化/hot_topics_normalized.xlsx`。校验：`hot_index` 非空但 `heat_score` 为 NaN（平台名与基准不一致）或某平台无有效分 → 退出 1；任一平台最高分 ≤ 90 且未传 `--accept-low-max` → 输出 `{"status":"low_max_score","platforms":[…],"stats":…,"options":[A,B,C]}` 并退出 2；传 `--accept-low-max --reason` 时放行并写入 `low_max_accepted`。成功时 stdout 末行为 `{"status":"ok","summary":{…}}`，写回 `status.ab`。

#### Scenario: 平台名不一致
- **WHEN** 数据中平台写作 `bilibili`
- **THEN** heat_score 为 NaN，退出码 1

#### Scenario: 放行低分
- **WHEN** 带 `--accept-low-max --reason "本周整体降温"` 重跑
- **THEN** 退出码 0，run_config 记录放行理由

### Requirement: 分层抽样
`sample_500.py --size N [--seed S]` SHALL 从标准化表按平台行数占比用最大余数法分配名额，每平台 `sample(random_state=seed)`；某平台不足时差额从行数最多的平台补齐（排除已抽 row_id）。默认 seed 42 保证可复现；smoke 第二轮使用 `--seed 99`。输出固定文件名 `03_抽样/sample_500.xlsx`（与实际行数无关），实际行数写入 `status.sample.sample_rows`。

#### Scenario: 可复现
- **WHEN** 同一输入、相同 size 与 seed 运行两次
- **THEN** 两次抽样 row_id 集合一致

#### Scenario: smoke 重抽样
- **WHEN** `--size 30 --seed 99`
- **THEN** 抽出 30 条，与 seed 42 的结果不同

### Requirement: 参考数据
`references/平台热度基准_2026.json` 与 `references/marketing_calendar_2026.csv` SHALL 为与客户原始文件 SHA-256 一致的原样迁入文件；修改它们属于业务口径变更，MUST 同步 `CUSTOMER_TO_MA_FILE_MAPPING.md` 与 `TOPIC6_CHANGELOG.md`。2026-11-03 起日历在客户原始 212 行基础上追加 7 个补充节点（冬奥会、迎财神、高考、世界杯、暑期档、开学季、亚运会，对齐 CC V2.0），由 `topic6-annotation/references/marketing_calendar/` 的 generator 与 supplement 生成。`marketing_calendar_2026.csv` 同时被 Phase E 的 E2 使用。

#### Scenario: 更新年度基准
- **WHEN** 需要 2027 年热度基准
- **THEN** 新增文件并经 change 评审，脚本默认基准路径同步更新

### Requirement: 文档一致性
`SKILL.md` 的示例 SHALL 与 Coordinator 契约一致：Coordinator 调用 `fetch_hot_topics.py` 时不传 `--start/--end`。SKILL.md 中保留的 `--start/--end` 示例仅表示手工调试用法。

#### Scenario: Agent 照抄 SKILL 示例
- **WHEN** Coordinator 读到 SKILL.md 中带日期的示例
- **THEN** 仍以 coordinator.system.md 为准，不自行推算日期
