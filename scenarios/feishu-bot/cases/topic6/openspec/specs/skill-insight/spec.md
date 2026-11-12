# skill-insight Specification

## Purpose
Skill `topic6-insight`（Phase E + F）：从标注宽表生成 E1~E4 四个周报版块（统计预处理 + 流式有限并发 LLM 洞察 + 版块级 checkpoint），合并为完整报告，进行发布前确定性自校验，并发布为飞书云文档。

源码：`ma-resources/skills/topic6-insight/`（`scripts/pipeline_e.py`、`pipeline_f.py`、`report_self_check.py`、`publish_report_to_feishu.py`、`feishu_api_fallback.py`；`01_统计/run_stats.py`、`_common.py`、`e1_industry.py`、`e2_nodes.py`、`e3_platforms.py`、`e4_marketing.py`；`02_洞察/` Prompt；`references/`）。

## Requirements

### Requirement: Phase E 单入口
`pipeline_e.py --project-dir --mode {demo|skip_sampling} [--publish-date] [--model 默认 DeepSeek-V4.1-Flash] [--version N] [--skip-data-prep] [--data-run-id] [--sections e1,…] [--max-workers 6] [--max-attempts 5]` SHALL 一次完成：`01_统计/run_stats.py` 数据预处理 → 进程内以有限并发（默认 6，等于 E 阶段峰值路数）流式调用 E1~E4。`--mode full` MUST 被拒绝（500 条仅用于校准）。非 demo 且 run_config 存在时，`status.hc2_passed != true` MUST 以"HC2 未通过前禁止进入 Phase E"退出 1。LLM 凭据只读 bmc-llm-relay 的 `LLM_RELAY_BASE_URL`/`LLM_RELAY_API_KEY`，缺失时抛 `EnvironmentError`，MUST NOT 回退 `ARK_*`/`OPENAI_*`；成本台账 platform 记 `bmc-llm-relay`。

#### Scenario: full 模式
- **WHEN** `--mode full`
- **THEN** 直接拒绝生成洞察

#### Scenario: demo 豁免 HC2
- **WHEN** `--mode demo` 且 `hc2_passed` 未设置
- **THEN** 正常运行

### Requirement: 版块定义与产物
四个版块 SHALL 固定为：

| id | 名称 | Prompt 目录 | 数据文件 | 章节标题 | max_tokens |
|---|---|---|---|---|---|
| e1 | 行业及热门话题 | `E1_行业话题` | `e1_data.md` | `# 一、行业及热门话题` | 1536 |
| e2 | 营销节点 | `E2_营销节点` | `e2_flags.json` | `# 二、营销节点` | 4096 |
| e3 | 平台新鲜事 | `E3_平台新鲜事` | `e3_data.md` | `# 三、平台新鲜事` | 18000 |
| e4 | 营销发现 | `E4_营销发现` | `e4_data.md` + `e4_candidates.json` | `# 四、营销发现` | — |

产出 `06_洞察/v{N}/e{1..4}_v{N}.md`、数据快照（含 `e3_word_candidates.json`、`e3_event_fulltext.json`）、子步骤审计（`e3_tagging_audit.md`、`e3_insight_draft_audit.md`、`e4_tagging_audit.md`、`e4_curation_audit.md`、`e4_arbitration_audit.md`）、`e3_word_freq_audit.md`、`pipeline_e_checkpoint_v{N}.json`、`pipeline_e_report_v{N}.json`。版本 N 默认自增，每轮独立子目录、不覆盖历史；`--sections` 定点重跑且未传 `--version` 时 MUST 写回最新一轮。E2 上周节点回顾 SHALL 使用全量宽表（不限营销可用子集），代表热搜按事件簇取 TOP3。E2 依赖 C3 结果与 `MARKETING_CALENDAR_PATH`（默认 `/mnt/skills/topic6-fetch-normalize/references/marketing_calendar_2026.csv`，兼容旧 Markdown 表格）；无节点时跳过 LLM。

#### Scenario: 新一轮洞察
- **WHEN** 已有 `06_洞察/v1/`，不传 `--version`
- **THEN** 写入 `06_洞察/v2/`

### Requirement: E3/E4 分步路由
E3 SHALL 并发执行热点词打标（`_tagging`）与事件洞察初稿（`_insight_draft`），本地过滤主体不明词并复用 `e3_platforms._score_word_candidates` 重新打分取热点词 TOP3，再按微博/抖音/B站/知乎四平台并发撰写并按该顺序拼接；拼接前每个平台结果 MUST 剔除其他平台标题起的段落与「上周{其他平台}无营销可用热点」行，找不到本平台标题时保留原文并告警；任一平台失败 MUST 使 E3 失败。E3/E4 子步骤输出（打标/初稿/枚举/仲裁）SHALL 按期望表头行定位伪表格（截止于下一个已知表头或 `### `），定位不到再按 `### 标题` 取段；非空输出两者都找不到时告警并写入该版块结果的 `parse_warnings`。热搜标题渲染为链接文字时 SHALL 经 `_common.md_link_title`（换行/`<br>`→空格、`[]`→全角、`|`→`\|`）。打标失败时本轮 MUST NOT 展示未过滤的热点词；初稿失败时撰写阶段退回自行判断。
E4 SHALL 并发执行打标（`_tagging`）与草稿枚举（`_curation`），再做跨板块归属仲裁与同池去重（`_arbitration`），由脚本完成分组、组间与组内排序，然后 A 组（`_writing_coop_risk`）与 B 组（`_writing_marketing_consumer`）并发撰写并按 A→B 拼接。打标失败保守保留全部候选；枚举失败时营销观察/消费洞察本轮为空；仲裁失败不移除不合并。每组输出 MUST 通过格式校验（B 组卡片字段齐全）与链接溯源校验（引用 URL 全部来自本组输入），不通过则该组重写 1 次，仍不通过则 E4 失败。所有子调用共用版块并发上限、max_tokens 升档与网络重试，token 计入所属版块。

#### Scenario: B 组引用候选池外链接
- **WHEN** B 组撰写结果包含输入数据中不存在的 URL
- **THEN** 该组重写 1 次；仍有则 E4 status=error，不写 checkpoint

#### Scenario: 仲裁输出无重复
- **WHEN** 仲裁返回"无重复事件"占位句
- **THEN** 解析为 0 组，不产生伪数据行

### Requirement: Prompt 选择
`load_prompt_template` 与子步骤 Prompt 加载 SHALL 选取目录下版本号数值最大的 `v*.md`，截取 `<!-- PROMPT_START -->` 之后内容，并在前面拼接 `_shared/role_style.md`。因此新增 Prompt 版本即生效；回滚 MUST 通过新增更高版本号实现，MUST NOT 删除或改写旧版本。当前活跃：E1 v3、E2 v8、E3 v8 + `_tagging/v3` + `_insight_draft/v1`、E4 `_tagging/v5` + `_curation/v3` + `_arbitration/v3` + `_writing_coop_risk/v4` + `_writing_marketing_consumer/v2`。子步骤 Prompt 内容纳入 checkpoint 签名。

#### Scenario: v10 排序
- **WHEN** 目录内同时有 `v9.md` 与 `v10.md`
- **THEN** 选用 `v10.md`

### Requirement: 重试与断点
每次版块调用 SHALL 最多尝试 `--max-attempts`（默认 5）次，429/5xx/断连等可恢复错误使用指数退避与全新客户端。每个成功版块立即原子写入 Markdown 与 checkpoint；checkpoint 与模型等参数匹配时重跑复用成功版块，只补失败版块。输出文件 ≤ `MIN_SECTION_BYTES=100` 视为失败。`finish_reason=length` 时 max_tokens 翻倍重试（最多 2 次，上限 32000），截断稿不写 checkpoint；版块结果 MUST 通过完整性闸（正文非空、未截断、表格行列数与表头一致），不通过重跑该版块 1 次；单版块异常 MUST NOT 影响其他版块。结果写回 `status.e_insights`（round_version、e1_file~e4_file、failed_sections）。`--sections` 只用于 Phase F 自校验 fail 后的定点重跑。成本按实际模型定价调用 cost_tracker（未知模型记 0 并告警）。

#### Scenario: 断连
- **WHEN** E3 流式响应中途断开
- **THEN** 以新客户端退避重试，成功后写 checkpoint

### Requirement: Phase F 合并
`pipeline_f.py --project-dir [--mode] [--version N] [--period-label] [--date-start] [--date-end]` SHALL 读取 `06_洞察/v{N}/e1~e4_v{N}.md` 拼接为 `07_报告/热点报告_{period_label}_v{N}.md`。period 优先级：参数 > run_config `period` > 由日期推导；展示用 period_label 不符合 `YYYY-Www` 时规范化并告警，但输出文件名沿用原值。demo 报告 MUST 在开头写入"> 演示说明：本报告基于 50 条分层样本生成，仅供流程演示，不可作为正式全量结论。"；run_config 顶层 `mode=smoke` 时 MUST 写入冒烟测试说明（含实际行数）。页尾"热度值说明"卡为固定文案，不交给 LLM。

#### Scenario: smoke 报告
- **WHEN** run_config 顶层 mode=smoke，脚本以 `--mode skip_sampling` 运行
- **THEN** 报告开头含冒烟测试声明

### Requirement: 发布前自校验
`report_self_check.py --project-dir --report-file [--version]` SHALL 只扫正文（剥离链接文字/URL 与表格内热搜标题），输出单行 `{"status":"pass|warn|fail","failed_sections":[…],"issues":[…]}`，fail 时退出 1。fail 级：缺章节标题、版块无洞察正文（<20 字）、非 http(s) 链接或"链接"行无 URL、LLM 痕迹（综上所述/作为AI/以下是…洞察等）、版块意图泄露（本版块旨在/写作要求/Prompt）、E4 打标内部措辞（乙方能不能接等）、越界推断（年度趋势/密集月份）、热度单位写成"万"、E4 消费洞察缺"营销发现"、E3 同一平台标题重复、E3 某平台小节内出现「上周{其他平台}无营销可用热点」。warn 级：模糊数字（约/大约/估计/左右）、执行性建议（务必/立即行动等）、E3 跨平台比较。`failed_sections` 为含 fail 问题的版块集合。

#### Scenario: LLM 痕迹
- **WHEN** E2 正文含"综上所述"
- **THEN** status=fail，`failed_sections=["e2"]`

#### Scenario: 标题中的"万"
- **WHEN** 表格中的热搜标题含"10万游客"
- **THEN** 不判定为热度单位错误

### Requirement: 飞书文档发布
`publish_report_to_feishu.py --project-dir --report-file --folder-name --doc-name [--admin-emails]` SHALL 执行：创建文件夹 → 导入 docx → 给 `FEISHU_USER_OPEN_ID` 授 `edit` → 转移 owner（旧 owner 保留 full_access）→ 给 admin 邮箱授 `full_access`。
- 主路径 lark-cli `--as bot`，身份来自 Vault 注入的 `LARKSUITE_CLI_APP_ID` + `LARKSUITE_CLI_TENANT_ACCESS_TOKEN`（env-provider），MUST NOT 执行 `config init` 或打印 token；可用性用只读 `GET /drive/explorer/v2/root_folder/meta` 探测（`--dry-run` 不能作为探测）。
- lark-cli 不可用/探测失败/在文档创建前失败 → 自动切换 `feishu_api_fallback`（Open API 直连，需 `LARK_APP_ID`/`LARK_APP_SECRET`，上传 md → import_tasks 转 docx 轮询 ≤120s → 在任何所有权转移前删除 md 源文件），可复用已建文件夹，并在 warnings 中记录切换原因。
- 文档已创建后的失败 MUST NOT 切 fallback（避免第二份文档），直接返回错误。
- 授 edit 失败 → `status=failed` 退出 1；转移 owner / admin 授权失败 → 只写 warnings。
- 返回 URL 不是 `/docx/` → 失败。报告文件不存在或 `FEISHU_USER_OPEN_ID` 未注入 → 失败。
- 成功 stdout：`{"status":"success","backend":"lark-cli|feishu_api_fallback","doc_token","doc_url","folder_token","operator","admins","warnings"}`。

#### Scenario: Bot token 未注入
- **WHEN** `LARKSUITE_CLI_APP_ID` 未注入
- **THEN** 探测失败，使用 fallback 发布并在 warnings 中记录

#### Scenario: 导入后授权失败
- **WHEN** lark-cli 已导入文档，但授 edit 失败
- **THEN** 返回 failed，不再走 fallback

### Requirement: Gateway 侧本地测试
`/test_phase_f` 命令与 `tests/test_phase_f_local.py`/`.sh` SHALL 能在不连接方舟的情况下用 mock 数据执行 `publish_report_to_feishu.py`，用于验证 lark-cli → fallback 链路。修改发布脚本 MUST 先通过该测试。

#### Scenario: 修改 fallback
- **WHEN** 修改 `feishu_api_fallback.py`
- **THEN** 运行 `tests/test_phase_f_local.sh` 并在飞书侧确认生成 docx
