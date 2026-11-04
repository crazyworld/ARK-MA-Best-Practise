# skill-event-registry Specification

## Purpose
Skill `topic6-event-registry`（C2 事件归档）：把四平台热搜标题清洗、判定事件性、抽取事件框架、向量召回候选、分块后由 LLM 整块归档为一级事件簇，跨平台合并并双向复核，最终输出每行的"事件簇名"，并维护跨窗口 Registry。

源码：`ma-resources/skills/topic6-event-registry/`（`SKILL.md`、`references/*.md`、`scripts/00~11_*.py`、`x0~x4_*.py`、`eventlib.py`、`relay.py`、`run_topic6_c2.py`、`launch_c2_background.py`、`wait_c2_complete.py`）。主体为客户 `blueai-canonical-event-registry` 原样迁入，`relay.py` 改为 OpenAI 兼容协议。

## Requirements

### Requirement: Topic6 封装入口
Topic6 SHALL 只通过 `run_topic6_c2.py --project-dir --mode {demo|full|skip_sampling} --run-id N [--input] [--chat-model] [--embedding-model] [--x2-verdict off|opus|sonnet(默认)] [--demo-fast]` 运行 C2，产出 `04_标注/C2_事件归档/c2_event_result_r{N}.xlsx`（供 `merge_annotations.py` 读取"事件簇名"）与 `c2_run/c2_status.json`。固定拓扑：输入中实际存在的平台并行执行 `00_clean_titles` → `x0_merge_platforms` → merged 目录 `01_eventness → 02_extract_frames → 03_normalize_entities → 04_build_embeddings → 05_recall_candidates → 06_build_blocks → 07_block_archive → x2_confidence_filter → x3_review_bidirectional → x4_detail_table` → `08_rank_events` → `09_commit_registry`。同一项目 MUST 通过 `.c2_runner.lock` 文件锁保证单实例运行。

#### Scenario: 输入只有三个平台
- **WHEN** 输入中无知乎记录
- **THEN** 只并行运行三个平台的 00，`skipped_platforms` 记录知乎

### Requirement: 阶段断点与重试
每个完成的阶段 SHALL 记录到 `c2_status.json.completed_stages`，重跑同一命令从第一个未完成阶段继续；底层 LLM 脚本保留内容寻址缓存，已完成批次不重复计费。`00_platforms`、`04_embeddings`、`07_archive` 失败后退避 15s 重跑 1 次。`08_rank`、`09_registry` 为 best-effort：失败只记 `status.registry={status: failed, errors}`，C2 仍为 `done`；成功时写 `merged/out/registry_snapshot.jsonl`。运行中 MUST 同步写 run_config `status.c2_cluster={status, stage}`。重跑时 MUST NOT 修改 `--batch-size`/`--cap`/`--top-k`（断点按批次编号对应）。

#### Scenario: 进程被杀后重跑
- **WHEN** 07 阶段中途进程被杀，原命令重跑
- **THEN** 从 07 续跑，已完成块走缓存

#### Scenario: Registry 失败
- **WHEN** 09_commit_registry 退出非 0
- **THEN** C2 状态 `done`，`registry.status=failed`，Coordinator 在 HC 中注明"Registry 未落库"

### Requirement: 后台启动与等待
`launch_c2_background.py --project-dir --mode --run-id [--demo-fast]` SHALL 用 nohup 启动 `run_topic6_c2.py`、重定向日志并写 `c2_runner.pid`；PID 存活时返回 `already_running` 并退出 0。`wait_c2_complete.py --project-dir --run-id --wait-seconds 105 [--interval 5]` SHALL 读取 `c2_status.json` 与 PID：状态文件未生成但进程存活 → `running`；未生成且无进程 → `missing`；`done` → `done`；`failed` → `failed`（退出 1）；其他 → `running`。

#### Scenario: 重复启动
- **WHEN** C2 已在运行时再次调用 launch
- **THEN** 返回 `already_running`，不启动第二个进程

### Requirement: 归档判定原则
C2 SHALL 遵守两条硬性禁止：不让图算法（Union-Find/连通分量）产出"是否同一事件"的结论；不让任何 Embedding 阈值直接决定是否同一事件。图传递闭包只用于**分块**（召回 + 有界，规模按预估输出 token 控制以防截断），归档精度由 LLM 对整块一次性判断承担（块内无顺序依赖）。一级事件 = 一个创作灵感方向；`event_description` 必填；事件名必须覆盖成员；单成员记录不加工；锚点是元数据不是主键。

#### Scenario: 调阈值替代 LLM
- **WHEN** 提议以 embedding 相似度 ≥0.6 直接合并
- **THEN** 违反硬性禁止，须改为分块后由 LLM 判定

### Requirement: 模型与凭据
Chat 模型 SHALL 取 `--chat-model` > `$C2_CHAT_MODEL_ID` > `Doubao-Seed-Evolving`；Embedding 取 `--embedding-model` > `$EMBEDDING_MODEL_ID` > `Doubao-embedding`。`relay.py` 使用 OpenAI 兼容 `/chat/completions`，模型统一走 bmc-llm-relay（blueai 模型网关）：BASE_URL 只读 `LLM_RELAY_BASE_URL`，Key 只读 `LLM_RELAY_API_KEY`（Embedding 可用 `EMBEDDING_BASE_URL/API_KEY` 覆盖），缺失 MUST 报错退出，MUST NOT 回退 `ARK_*`/`OPENAI_*`；BASE_URL MUST 含版本前缀。`ARK_*` 只用于部署脚本调用方舟管理 API。Embedding 默认 `Doubao-embedding`（relay 路由到 2560 维文本模型，标准 `/embeddings` 批量协议），更换向量模型 MUST 先重置上一周 registry 快照。C2 模型 MUST NOT 与 DataHub 的 `DeepSeek-V4-Flash` 混用。

#### Scenario: 走 relay
- **WHEN** 已配置 `LLM_RELAY_BASE_URL`/`LLM_RELAY_API_KEY`
- **THEN** C2 走 bmc-llm-relay，部署脚本仍用 `ARK_*` 调方舟控制面

#### Scenario: 缺 relay 凭据
- **WHEN** 只配置了 `ARK_BASE_URL`/`ARK_API_KEY`
- **THEN** relay.py 报错退出，不调用方舟模型

### Requirement: demo 快速路径
`--demo-fast` SHALL 用一次批量 LLM 调用把少量标题归入事件簇（输出 `{"results":[{"row_id","一级事件名"}]}`），直接产出同契约 XLSX；该路径 MUST 仅用于 50 条样本的 demo，MUST NOT 用于 full/skip_sampling 或正式业务结论。

#### Scenario: 在 full 中使用 demo-fast
- **WHEN** full 模式传入 `--demo-fast`
- **THEN** 违反契约

### Requirement: 跨窗口增量
`00_seed_from_registry.py` SHALL 仅用于存在上一窗口 Registry 的增量场景（在 01~04 之后插入），MUST NOT 作为 C2 起点。`09_commit_registry` 的 `--window` 由 `run_config.period.period_label` 推导。

#### Scenario: 首次运行
- **WHEN** 无上一窗口 Registry
- **THEN** 不调用 00_seed_from_registry

### Requirement: 原样迁入与修改边界
除 `SKILL.md`、`CHANGELOG.md`、`references/runbook.md`、`scripts/relay.py`、Topic6 封装三件套（`run_topic6_c2.py`、`launch_c2_background.py`、`wait_c2_complete.py`）外，主流水线脚本与 references SHALL 保持与客户版 SHA-256 一致。修改这些文件 MUST 走 openspec change，并在 `CUSTOMER_TO_MA_FILE_MAPPING.md` 标注为"适配迁入"。异常排查以 `references/runbook.md` 为准。

#### Scenario: 修 07 的 bug
- **WHEN** 需要修改 `07_block_archive.py`
- **THEN** 提 change，并更新映射表状态
