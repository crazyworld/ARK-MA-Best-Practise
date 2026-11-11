# Topic 6 客户原始文件与 MA 版本对应表

> 核对日期：2026-09-29
>
> 本表以仓库中的实际文件和 SHA-256 内容比对为准。客户原始文件均位于
> `assets_from_customer/`，MA 部署源码以 `ma-resources/` 为准；根目录下的
> Gateway、HITL 和打包脚本属于 MA 接入层。

## 标记说明

| 标记 | 含义 |
|---|---|
| 原样迁入 | 路径可能变化，但文件内容 SHA-256 一致 |
| 适配迁入 | 源自客户文件，因 MA 路径、接口、模型协议或职责边界而修改 |
| 合并重写 | 多个客户脚本合并为一个 MA 入口 |
| 拆分重写 | 一个客户脚本拆成多个 MA 脚本 |
| MA 新增 | 客户文件中没有直接对应物，为 MA 部署或运行新增 |
| 未迁移 | 客户侧保留，但当前 MA 包和运行链路未使用 |
| 运行产物 | 可重新生成，不作为迁移源码维护 |

## 一、总体对应关系

| 客户原始资源 | MA 版本 | 状态 | 说明 |
|---|---|---|---|
| `topic6/skill/fetch-normalize/` | `ma-resources/skills/topic6-fetch-normalize/` | 适配迁入 | Phase A+B；原单入口被拆为取数、标准化、抽样 3 个脚本 |
| `topic6/skill/annotation/` + `topic6/ks/` + `topic6/prompt/` + `topic6/tool/cost-tracker/` | `ma-resources/skills/topic6-annotation/` | 重组迁入 | Phase C+D；业务口径、活跃 Prompt、标注与合并脚本集中到一个 Skill |
| `blueai-canonical-event-registry/` | `ma-resources/skills/topic6-event-registry/` | 适配迁入 | C2 事件归档；主体原样保留，LLM Relay 改接 OpenAI 兼容协议（bmc-llm-relay） |
| `topic6/skill/insight/` | `ma-resources/skills/topic6-insight/` | 适配迁入 | Phase E+F；保留活跃 Prompt，脚本改为 MA 沙箱路径和 Ark API |
| `artifact-template-bluefocus-hotspot-web-report/` | `ma-resources/skills/topic6-web-report/` | 适配迁入 | Phase G；模板和素材原样保留，Skill 名称改为 `topic6-web-report` |
| `miaoda-web-publish/` | `ma-resources/skills/miaoda-web-publish/` | 原样迁入 | Phase H；保留用户 OAuth、Git 发布和恢复契约 |
| `topic6/CLAUDE.md` + `topic6/prompt/*.md` + `其他相关引用/Agent.Producer_CLAUDE.md` | `ma-resources/agents/coordinator.system.md` | 重新编排 | 原单 Agent 主流程改成 Coordinator + 标注子 Agent + 结构化 HC |
| `topic6/skill/annotation/SKILL.md` + 标注阶段契约 | `ma-resources/agents/annotator.system.md` | MA 新增 | 抽出单路 C0/C3/R1-R5 标注子 Agent |
| `topic6/skill/insight/SKILL.md` + Phase E 契约 | `ma-resources/skills/topic6-insight/` | 适配迁入 | E1-E4 由 `pipeline_e.py` 单入口在脚本内有界并发，不注册洞察子 Agent |
| `topic6/lm/` | `ma-resources/memory/topic6/` | 适配迁入 | 改造成 MA Memory Store 的只读业务记忆 |
| `Validator/`(KS/LM/prompts + Topic6 检查清单) | `ma-resources/agents/validator.system.md` + `ma-resources/skills/validator/` | 适配迁入 | 子 Agent `topic6-validator`(可选，HC1/HC2/HC3 前质检，失败/超时填 SKIP)；新增 `xlsx_checker.py` 确定性检查；Topic1/2/4/5 清单、metaso/tavily 外部搜索、IM 推送未迁移 |

## 二、`topic6-fetch-normalize` 对应表

| MA 文件 | 客户原始文件 | 状态 | 主要变化 |
|---|---|---|---|
| `skills/topic6-fetch-normalize/SKILL.md` | `topic6/skill/fetch-normalize/` 无独立 `SKILL.md` | MA 新增 | 增加 SkillHub frontmatter、目录和执行契约 |
| `scripts/fetch_hot_topics.py` | `topic6/skill/fetch-normalize/pipeline_ab.py` | 拆分重写 | 提取 Phase A；改为 MA MCP、环境变量和 `/workspace` 路径 |
| `scripts/log1p_p1p99_normalize.py` | `topic6/skill/fetch-normalize/pipeline_ab.py` | 拆分重写 | 提取 Phase B；保留 log1p + P1/P99 核心算法 |
| `scripts/sample_500.py` | `topic6/skill/fetch-normalize/sample_500.py` | 适配迁入 | 改为 `--project-dir` 和 MA 工作区路径 |
| `references/平台热度基准_2026.json` | `topic6/skill/fetch-normalize/references/平台热度基准_2026.json` | 原样迁入 | SHA-256 一致 |
| `references/marketing_calendar_2026.csv` | `topic6/skill/fetch-normalize/references/marketing_calendar_2026.csv` | 适配迁入 | 2026-11-10 按客户 V2 E2 实际使用的 `references/2026年营销节点_byday.md` 补齐 4 个电商节点（年货节/618预售启动/99大促/双11预售启动），618/双11/双12 大促类型改为「电商大促」；节点集合与客户 md 一致（223），保持 BOM+CRLF |

## 三、`topic6-annotation` 对应表

### 3.1 Skill、知识库与流程 Prompt

| MA 文件 | 客户原始文件 | 状态 | 说明 |
|---|---|---|---|
| `skills/topic6-annotation/SKILL.md` | `topic6/skill/annotation/SKILL.md` | 适配迁入 | 重写为 MA Skill 入口，补 frontmatter、挂载路径和一体化编排说明 |
| `ks/_字段速查.md`、`00_产品定位.md`～`07_报告结构.md` | `topic6/ks/` 下同名 9 个文件 | 原样迁入 | SHA-256 全部一致 |
| `prompts/00_角色与触发.md`～`09_阶段_H妙搭发布.md` | `topic6/prompt/` 下同名 10 个文件 | 原样迁入 | SHA-256 全部一致 |
| `prompts/run_config契约.md`、`prompts/运行前置.md` | `topic6/prompt/` 下同名文件 | 原样迁入 | SHA-256 一致 |

### 3.2 标注 Prompt

| MA 文件 | 客户原始文件 | 状态 | 说明 |
|---|---|---|---|
| `prompts/C0_基础事实/v13.md` | `agent/skill/topic6-annotation-skill/prompts/C0_基础事实/v13.md`（bundle_20261030） | 原样迁入 | SHA-256 一致；目录内唯一版本。v1~v4（客户原样）与 v5（MA 精简版）已于 2026-11-05 移除，历史见 git |
| `prompts/R1_平台借势/v1.md`～`v2.md` | 客户侧同路径文件 | 原样迁入 | 2 个版本全部保留 |
| `prompts/R2_商业合作/v1.md` | 客户侧同路径文件 | 原样迁入 | SHA-256 一致 |
| `prompts/R2_商业合作/v2.md` | 客户 V2.0 bundle(20261030)同路径文件 | 原样迁入(去 CRLF) | 2026-11-03 活跃版 |
| `prompts/R3_风险预警/v1.md` | 客户侧同路径文件 | 原样迁入 | SHA-256 一致 |
| `prompts/R4_创意借鉴/v1.md`～`v2.md` | 客户侧同路径文件 | 原样迁入 | 2 个版本全部保留 |
| `prompts/R5_消费者行为/v1.md`～`v2.md` | 客户侧同路径文件 | 原样迁入 | 2 个版本全部保留 |
| `prompts/R5_消费者行为/v3.md` | 客户 V2.0 bundle 同路径文件 | 原样迁入(去 CRLF) | 2026-11-03 活跃版 |
| `prompts/节点标注/v7.md` | `topic6/skill/annotation/prompts/节点标注/v7.md` | 原样迁入 | 只打包当前活跃版 |
| 无 | `prompts/节点标注/v1.md`～`v6.md` | 未迁移 | 历史版本未进入 MA Skill 包 |
| 无 | `prompts/内容标注/v1.md`～`v9.md` | 未迁移 | 已被 C0 + R1-R5 分层标注方案替代 |

### 3.3 标注脚本与依赖

| MA 文件 | 客户原始文件 | 状态 | 主要变化 |
|---|---|---|---|
| `scripts/datahub_annotate.py` | `datahub_submit.py` + `datahub_poll.py` + `annotation_postprocess.py` + `c3_v6_flatten.py` + `c3_v6_timewindow.py` | 合并重写 | 提交、轮询、下载、JSON 展开、C3 时窗计算合并为单入口 |
| `scripts/c0_merge_phase1.py` | 客户侧同名脚本 | 适配迁入 | MA 路径化；移除 `--patch-run-id` 越权补丁分支 |
| `scripts/c0_filter_usable.py` | 客户侧同名脚本 | 适配迁入 | MA 路径化；移除强制放行参数，超过 5% 交由协调器处理 |
| `scripts/merge_annotations.py` | 客户侧同名脚本 | 适配迁入 | 对齐 MA 产物路径、28 列宽表和健康摘要 |
| `references/marketing_calendar/generate_marketing_calendar.py` | 客户侧同路径文件 | 原样迁入 | SHA-256 一致 |
| `references/marketing_calendar/nodes_definition.csv` | 客户侧同路径文件 | 原样迁入 | SHA-256 一致 |
| `references/marketing_calendar/nodes_definition_supplement.csv` | 客户侧同路径文件 | 原样迁入 | SHA-256 一致 |
| `tool/cost-tracker/cost_tracker.py` | `topic6/tool/cost-tracker/cost_tracker.py` | 适配迁入 | 精简为 MA 共用成本台账，路径改为 `/workspace` |
| 无 | `annotation/scripts/c0_retry_missing.py`、`retry_missing.py` | 未迁移 | 重试决策上移 Coordinator；当前 MA 包无同名重试脚本 |
| 无 | `annotation/scripts/c2_run_all_platforms.py` | 未迁移 | V2 生产按平台独立跑 00~09（不跨平台合并）；MA 用 `run_topic6_c2.py` → rc.14 `run_c2.py --mode cross-platform` |
| 无 | `annotation/scripts/c3_postprocess.py` | 未迁移 | 能力并入 `datahub_annotate.py` |

## 四、`topic6-event-registry` 对应表

客户根目录：V2 bundle_20261030 `Topic6 热点报告/agent/skill/_vendor/blueai-canonical-event-registry/`（3.0.0-rc.14，2026-11-05 由 2.2.0 升级）
MA 根目录：`ma-resources/skills/topic6-event-registry/`

| MA 文件 | 客户原始文件 | 状态 | 说明 |
|---|---|---|---|
| `SKILL.md` | 同名文件 | 适配迁入 | frontmatter name 保持 `topic6-event-registry`；新增「Topic6 / MA 用法」节 |
| `CHANGELOG.md` | 同名文件 | 适配迁入 | 顶部追加 MA 适配说明 |
| `references/runbook.md` | 同名文件 | 适配迁入 | 授权节改为 `LLM_RELAY_*`，路径改 `/mnt/skills` |
| `references/*.md`（其余 8 个）、`MANIFEST.json`、`examples/synthetic.csv` | 同名文件 | 原样迁入(去 CRLF) | MANIFEST 记录上游哈希，适配文件不匹配属预期 |
| `scripts/relay.py` | 同名文件 | 适配迁入 | 传输层保留 MA（bmc-llm-relay 流式 + watchdog + 多模态 embedding）；run_batches 版本缓存、Embedder 校验/原子落盘对齐 rc.14；`MODEL_MAX_OUTPUT_TOKENS` 按模型原生输出上限请求（上游默认按 Sonnet 调校），截断报错附 reasoning_tokens（2026-11-06） |
| `scripts/03_normalize_entities.py` | 同名文件 | 适配迁入 | 截断先原样重试、仍截断对半拆，串行累积注册表（同 07 `archive_split`；2026-11-06 W44 full 阻断修复） |
| `scripts/run_c2.py` | 同名文件 | 适配迁入 | 新增 `--review-policy`/`--degraded-policy`/`--uncertain-policy`/`--member-review`/`--front-workers`（默认上游语义）；x2 透传 `--model`；端点读 `LLM_RELAY_BASE_URL` |
| `scripts/retain_original.py`、`x2_confidence_filter.py` | 同名文件 | 适配迁入 | `allow_degraded`、`uncertain_policy`（keep=未决成员不拆组，对齐原版旧链路）参数；恢复 `--model` 覆盖 |
| `scripts/run_ev.py`、`04_build_embeddings.py`、`check_environment.py` | 同名文件 | 适配迁入 | `ANTHROPIC_*` → `LLM_RELAY_*` |
| `scripts/f1_extract.py`、`c2_runtime.py`、`c2_quality.py`、`front_evidence.py`、`link_registry.py`、`review_revision.py`、`verify_package.py`、`00~11_*.py`（03 除外）、`x0~x4_*.py`、`eventlib.py` | 同名文件 | 原样迁入(去 CRLF) | rc.14 主体 |
| `scripts/run_topic6_c2.py`、`launch_c2_background.py`、`wait_c2_complete.py` | 无 | MA 新增 | Topic6 封装：调用 `run_c2.py run --mode cross-platform --frontend legacy --member-review off`（聚簇对齐原版 00→09，保留跨平台合并） |
| `scripts/__pycache__/` | 客户侧缓存 | 未迁移 | Python 缓存，不属于源码 |

## 五、`topic6-insight` 对应表

| MA 文件 | 客户原始文件 | 状态 | 主要变化 |
|---|---|---|---|
| `skills/topic6-insight/SKILL.md` | `topic6/skill/insight/SKILL.md` | 适配迁入 | 补 SkillHub frontmatter、MA 路径和 Ark 环境变量 |
| `scripts/pipeline_e.py` | `topic6/skill/insight/pipeline_e.py` | 适配迁入并移动 | 去除本地目录上溯，改 `/mnt/skills`、`/workspace`、Ark OpenAI 接口；2026-11-03 移植 V2.0 的 E3/E4 分步路由（E4 的仲裁、A/B 分组撰写与校验取自 V2 `pipeline_e4_agent_steps.py`，改为脚本内直连方舟，不用子 Agent）；2026-11-06 子步骤按表头解析、E3 平台隔离、链接标题清洗（W44 B2/B3/B5） |
| `scripts/pipeline_f.py` | `topic6/skill/insight/pipeline_f.py` | 适配迁入并移动 | 改项目路径解析和 MA 产物目录 |
| `01_统计/e1_industry.py`、`e3_platforms.py` | 客户 V2.0 bundle 同路径文件 | 原样迁入(去 CRLF，保留 `from __future__`) | 2026-11-03 由 V1 升级到 V2.0 |
| `01_统计/_common.py` | 客户 V2.0 bundle 同路径文件 | 适配迁入 | 2026-11-03 由 V1 升级到 V2.0；2026-11-06 新增 `md_link_title`，链接标题清洗换行与方括号（W44 B5） |
| `01_统计/e2_nodes.py` | 客户侧同路径文件 | 适配迁入 | 对齐 C3 节点结果和 MA 日历路径；合入 V2.0 代表热搜 TOP3、30 天信号上限、锚点未核实、品牌IP候选列；日历解析为 0 行时报错 |
| `01_统计/e4_marketing.py` | 客户 V2.0 bundle 同路径文件 | 适配迁入 | 新增营销观察/消费洞察结构化候选；2026-11-06 链接标题改用 `md_link_title`（W44 B5） |
| `01_统计/run_stats.py` | 客户侧同路径文件 | 适配迁入 | 对齐 MA 项目路径和统计编排；新增 E3 两个 JSON 输入，E2 改用全量 df |
| `02_洞察/_shared/role_style.md` | 客户侧同路径文件 | 原样迁入 | SHA-256 一致 |
| `02_洞察/E1_行业话题/v1.md` | 客户侧同路径文件 | 原样迁入 | 当前活跃版 |
| `02_洞察/E2_营销节点/v3.md` | 客户侧同路径文件 | 原样迁入 | 当前活跃版；v1-v2 未打包 |
| `02_洞察/E3_平台新鲜事/v1.md` | 客户侧同路径文件 | 原样迁入 | 当前活跃版 |
| `02_洞察/E4_营销发现/v6.md`、`_tagging/v1.md` | 客户侧同路径文件 | 原样迁入 | 归档保留(回退用) |
| `02_洞察/E1_行业话题/v3.md`、`E2_营销节点/v7.md`、`E3_平台新鲜事/v6.md` + `_tagging/v3.md` + `_insight_draft/v1.md`、`E4_营销发现/_tagging/v5.md` + `_curation/v3.md` + `_arbitration/v3.md` + `_writing_coop_risk/v4.md` + `_writing_marketing_consumer/v1.md` | 客户 V2.0 bundle 同路径文件 | 原样迁入(去 CRLF) | 2026-11-03 活跃版 |
| `references/E3_统计口径说明.md`、`综合热度指数算法说明.md` | 客户侧 `01_统计/references/` 下同名文件 | 原样迁入并移动 | 从统计子目录提升到 Skill 的 `references/`；2026-11-10 改为客户 2026-10-13 校正版（v2 口径，此前误迁 09-15 旧版）。`综合热度指数算法说明.md` SHA-256 一致；`E3_统计口径说明.md` 仅打标 Prompt 版本号一处注记 |

## 六、`topic6-web-report` 对应表

客户根目录：`assets_from_customer/artifact-template-bluefocus-hotspot-web-report/`  
MA 根目录：`ma-resources/skills/topic6-web-report/`

| MA 文件 | 客户原始文件 | 状态 | 说明 |
|---|---|---|---|
| `SKILL.md` | 同名文件 | 适配迁入 | Skill 名改为 `topic6-web-report`，补充 MA 场景分工；第 5 步周/月刊按周期标签判定、第 9/10 步删除 snapshot 上传（2026-11-06） |
| `agents/openai.yaml` | 同名文件 | 适配迁入 | `default_prompt` 中的 Skill 名改为 `$topic6-web-report`；去掉 snapshot 上传描述（2026-11-06） |
| `CHANGELOG.md` | 无 | MA 新增 | 记录模板在 MA 侧的变更 |
| `artifact-template.json` | 同名文件 | 原样迁入 | SHA-256 一致 |
| `CLAUDE.md` | 同名文件 | 适配迁入 | 删除 snapshot 上传指令，Topic6 网页只走妙搭（2026-11-06） |
| `references/document-image-extraction.md`、`html-snapshot-upload.md` | 同名文件 | 原样迁入 | SHA-256 一致 |
| `scripts/md2source.py`、`fetch_case_images.py` | 客户 `pipeline_g.py`（SourceJsonBuilder / download_case_images） | MA 重写 | 报告 md → source.json 确定性转换；HC3 飞书文档案例配图下载 |
| `scripts/fetch_report_md.py` | 客户 `pipeline_g.py::fetch_feishu_markdown` | MA 新增（2026-11-10） | 网页正文以 HC3 定稿飞书文档为准：tenant token 读 docx blocks 还原 Markdown；读取失败/还原不完整时回退本地合并报告 |
| `scripts/upload-html.mjs`、`validate-case-image-sources.mjs`、`validate-rendered-content.mjs`、`validate-template-assets.mjs` | 同名文件 | 原样迁入 | 4 个脚本 SHA-256 一致 |
| `assets/preview.png` | 同名文件 | 原样迁入 | SHA-256 一致 |
| `assets/source/app.js`、`index.html`、`source.example.json` | 同名文件 | 原样迁入 | 3 个模板源码文件 SHA-256 一致 |
| `assets/source/build-report.mjs`、`styles.css` | 同名文件 | 适配迁入 | 此前已有 MA 适配（与 bundle SHA 不一致，原表记为一致属误记）；2026-11-06 build-report 周/月刊改为只按周期标签判定 |
| `assets/source/DESIGN_SPEC.md` | 同名文件 | 适配迁入 | 「云端交付」节改为不上传 snapshot（2026-11-06） |
| `assets/source/bluefocus-logo-white.png`、`hero-social-bg.jpg`、`title-monthly.png`、`title-weekly.png` | 同名文件 | 原样迁入 | 4 个根级素材 SHA-256 一致 |
| `assets/source/assets/case-1-car-livestream.jpg`～`case-4-brand-notice.jpg` | 同名文件 | 原样迁入 | 4 张案例图 SHA-256 一致 |
| `assets/source/assets/fonts/Poppins-{Bold,Light,Medium,Regular,SemiBold}.ttf` | 同名文件 | 原样迁入 | 5 个字体 SHA-256 一致 |
| `assets/source/assets/platform-{bilibili,douyin,weibo,zhihu}.svg`、`platform-logos-combined.svg` | 同名文件 | 原样迁入 | 5 个平台图标 SHA-256 一致 |
| `assets/source/assets/rank-{1,2,3}.svg` | 同名文件 | 原样迁入 | 3 个排名图标 SHA-256 一致 |
| `assets/source/.openai/hosting.json` | 同名文件 | 原样迁入 | 隐藏配置文件，SHA-256 一致 |

### Phase H `miaoda-web-publish`

客户根目录：`assets_from_customer/miaoda-web-publish/`
MA 根目录：`ma-resources/skills/miaoda-web-publish/`

`SKILL.md`、`agents/`、`references/`、`scripts/` 和 `CHANGELOG.md` 全部原样迁入；
Prompt 仅把客户侧 `Agent.Producer/.claude/skills/...` 引用适配为 MA 挂载路径
`/mnt/skills/miaoda-web-publish/`。发布身份仍为用户 OAuth，不引入 `MIAODA_TOKEN`。

## 七、Agent、Memory 与 MA 部署资源

| MA 文件 | 客户原始文件/依据 | 状态 | 说明 |
|---|---|---|---|
| `agents/coordinator.system.md` | `topic6/CLAUDE.md`、`topic6/prompt/*.md`、`其他相关引用/Agent.Producer_CLAUDE.md` | MA 新增 | 汇总主流程并加入 Multi Agent、HC1-HC3、MA 挂载路径 |
| `agents/annotator.system.md` | 标注 Skill 与 Phase C 契约 | MA 新增 | 定义单路标注子 Agent 输入输出 |
| `agents/coordinator.json`、`annotator.json` | 无直接客户文件 | MA 新增 | MA Agent 资源、模型、Skill、工具和标注子 Agent 关系 |
| `memory/topic6/MEMORY.md` | `topic6/lm/MEMORY.md` | 适配迁入 | 改为 MA 资源索引、挂载路径和执行职责 |
| `memory/topic6/_版本状态.md` | `topic6/lm/_版本状态.md` | 适配迁入 | 改为 MA 当前活跃 Prompt/Skill 版本 |
| `memory/topic6/错误案例库.md` | `topic6/lm/错误案例库.md` | 适配迁入 | 保留业务经验并改写 MA 相关规避规则 |
| `memory-store.json` | `topic6/lm/` | MA 新增 | 声明 3 个 Memory 文件及挂载路径 |
| `environment.json` | `其他相关引用/Topic6_环境变量依赖清单.md` + 各工具契约 | MA 新增 | 声明依赖、网络和环境变量占位符 |
| `create_all.sh` | 无 | MA 新增 | 创建/更新 Environment、Memory Store 和 2 个 Agent，并按旧 ID 精确清理已下线的 Insighter |
| `skill_ids.json.example` | 无 | MA 新增 | Skill 上传结果模板 |
| `skill_ids.json`、`created_ids.json` | 无 | 运行产物 | 上传/创建后生成的实际资源 ID，不是客户源码 |
| `version_mapping.md` | 无 | MA 新增 | 维护 SkillHub 版本、源码哈希和上传记录 |

## 八、Bot 接入层与本地发布工具

这些文件属于 MA 版本，但不打进 Skill 包。

| MA 文件 | 客户原始文件 | 状态 | 作用 |
|---|---|---|---|
| `config.py`、`env.example` | 无 | MA 新增 | Gateway 配置和环境变量样例 |
| `gateway.py` | 无 | MA 新增 | 飞书事件入口及 Topic 6 运行器装配 |
| `topic6_runner.py` | 无 | MA 新增 | 创建 MA Session、消费 SSE、驱动完整作业 |
| `pipeline_store.py` | 无 | MA 新增 | 作业、Session、HC 事件状态持久化 |
| `topic6_hitl.py` | 原流程中的 HC1/HC2/HC3 规则 | MA 新增 | 飞书审核卡片回调和继续执行 |
| `topic6_progress_card.py` | 无 | MA 新增 | 飞书进度卡片渲染 |
| `tools/pack_skills.sh` | 无 | MA 新增 | 校验并打包 6 个 Skill |
| `tools/upload_skills.py` | 无 | MA 新增 | 上传 SkillHub 并回填 ID/版本 |
| `tools/out/*.zip`、`*.sha256` | 无 | 运行产物 | 可由 `pack_skills.sh` 重建 |
| `README.md`、`TOPIC6_CHANGELOG.md`、`TOPIC6_DEBUG_QUICKSTART.md` | 无直接对应 | MA 新增 | MA 维护、约束变更和调试文档 |
| `topic6_ma_architecture.html`、`topic6_pipeline_overview.html` | 客户流程文档的可视化重述 | MA 新增 | MA 架构与流水线可视化 |

## 九、客户侧未进入 MA 运行链路的资源

| 客户原始文件/目录 | MA 状态 | 原因或替代方式 |
|---|---|---|
| `Validator/` 中与 topic6 无关部分(ada-daily-output-annotate、independent-report-auditor、system-auditor、metaso/tavily-search、Topic1/2/4/5 清单) | 未迁移 | 与 topic6 无关；topic6 检查清单已随 `skills/validator` 迁入 |
| `topic6/lm/执行日志.md` | 未迁移 | 运行状态改由 `pipeline_store.py` 和项目 `run_config.yaml` 承载 |
| `topic6/others/_待办登记.md` | 未迁移 | 非运行依赖 |
| `topic6/Projects/_项目模板/**`、`Projects/W37热点周报_*/**` | 未迁移 | MA 在 `/workspace/Projects/` 动态创建项目和产物 |
| `topic6/skill/_external_event-registry.md` | 未迁移 | 被实际挂载的 `topic6-event-registry` Skill 替代 |
| `topic6/skill/_external_node-tagging.md` | 未迁移 | 外部节点 Skill 已取消；能力由 C3 + 日历基线承担 |
| `topic6/skill/_external_web-report.md` | 未迁移 | 被实际挂载的 `topic6-web-report` Skill 替代 |
| `topic6/tool/anthropic-llm/契约.md` | 未迁移 | 模型调用统一改为 Ark/OpenAI 兼容协议 |
| `topic6/tool/datahub/契约.md` | 未直接迁入 | 契约落实在 `datahub_annotate.py` 和环境变量配置中 |
| `topic6/tool/hot-topics-mcp/契约.md` | 未直接迁入 | MCP 定义落实在 `coordinator.json` |
| `topic6/tool/lark-cli/契约.md` | 未直接迁入 | 发布职责由 Coordinator 与飞书接入层承担 |
| `topic6/tool/web-report/README.md` | 未迁移 | `md2source.py` 已以 MA 版重写在 `topic6-web-report/scripts/`（见第六节） |
| `topic6/其他相关引用/authorized_users.json`、`permission_registry.json`、`permission-model.md`、`topic_registry.json`、`根_CLAUDE.md` | 未迁移 | 权限、Topic 路由和会话隔离由 Gateway/MA 资源配置承担 |
| `agent/skill/scripts/pipeline_g.py`、`publish_miaoda.py` | 拆分重写 | Phase G 由 Coordinator 固定命令序列（`fetch_case_images.py` → `fetch_report_md.py` → `md2source.py` → 构建/校验）承担；妙搭发布独立为 Phase H（`miaoda-web-publish`），失败为阻塞 |
| `agent/skill/scripts/feishu_push_hc.py`、`feishu_pullback.py` | 由 Gateway 替代 | HC1/HC2 宽表推飞书在线表格与通过后回读改由 `feishu_doc_publisher.py` + `topic6_hitl._reread_edited_sheet` 承担；健康度摘要不推 docx（仅卡片展示） |
| `agent/skill/scripts/pipeline_deliver.py`、`config/delivery_manifest.yaml`、`config/delivery_root.json` | 由 Gateway 替代（2026-11-11） | 不迁 7 子夹与按人大根；`process_files.py` 把关键节点文件（白名单、最新轮次）传到 job 文件夹/「过程数据」，发起人授权 + 终态 @ 通知；宽表仍为 HC 在线表格，报告仍由 Phase F 发布 |
| `agent/skill/scripts/preflight_check.py` | 未迁移 | 环境检查由 `TOPIC6_DEBUG_QUICKSTART.md` 清单、Gateway `/test_phase_f` 探测与 Coordinator 启动 Prompt 预检承担 |
| `agent/skill/scripts/run_logger.py` | 未迁移 | 阶段状态由 `pipeline_store.py`、进度卡片与 `tools/session_triage.py` 承担 |
| `agent/skill/scripts/pipeline_ab.py`、`fetch_and_clean.py`、`normalize.py` | 拆分重写 | 由 `fetch_hot_topics.py` + `log1p_p1p99_normalize.py` 承担 |
| `agent/skill/scripts/pipeline_e4_agent_steps.py` | 合并重写 | E4 分步逻辑并入 `pipeline_e.py`（脚本内直连） |
| `agent/KS/KS_*.md`（4 个） | 未直接迁入 | 判定口径与报告结构由 `topic6-annotation/ks/*`、当前生效 Prompt 与统计脚本承载 |
| `agent/LM/错误案例库.md` E5~E17 | 摘要迁入 | `memory/topic6/错误案例库.md` 按 MA 适配摘要收录；E11/E12 标注不适用 |
| 所有 `.DS_Store`、`__pycache__/` | 未迁移/忽略 | 本地系统或解释器缓存，不属于源码 |

## 十、维护规则

1. 客户文件只作为迁移基线，MA 生效源码统一修改 `ma-resources/`。
2. 修改 Skill 后依次运行 `tools/pack_skills.sh`、`tools/upload_skills.py`、`ma-resources/create_all.sh`。
3. 原样迁入项可用 SHA-256 检查漂移；适配项应以本表记录的来源和职责为审查基线。
4. 新增、拆分、合并或删除文件时，同步更新本表和 `ma-resources/version_mapping.md`。

## 十一、核对中发现的待修正引用

| 引用位置 | 当前引用 | 实际文件 | 结论 |
|---|---|---|---|
| `agents/coordinator.system.md` 的筛选失败处理 | `retry_missing.py --task c0` | MA Skill 中无 `retry_missing.py` 或 `c0_retry_missing.py` | Prompt 残留客户版命令，需要改成现有重试方案或补回脚本 |
