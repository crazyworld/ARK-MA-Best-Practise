# topic6 · 变更日志

记录影响 Topic6 运行流程、交付行为和方舟 Managed Agents 适配的变更。纯文案润色不进。

每条至少包含:
- 触发现象(报错/异常表现)
- 根因(为什么这个约束存在)
- 影响文件 & 参考出处

按日期倒序。

---

## 2026-11-11

### W45 业务验收问题修复（Q1 / Q2 / Q4 / Q5）+ Gateway 小修（R3 / R4）

- **Q2 事件簇归类：同一事件未合并、驱动话题出现整句长标题**（job_fe11fa6b2761）
  - 现象：《后会友期》66 条（营销可用 36 条）全部成单条簇，影视综艺驱动话题缺失该节目；「三甲医院推荐患者去三乙医院」知乎/微博两条未合并，
    医疗健康驱动话题为 3 条 1 次长标题。本期 1095 条可用中仅 199 条（18%）进入 ≥2 成员簇（客户原版 W43 为 48%）。
  - 根因：MA 以 `--review-policy record` 无人值守运行 rc.14，non_events 门禁不停等、无人复核，UNCERTAIN/needs_evidence 永不解除；
    `retain_original.py` 把含未决成员的整组拆回原标题（本期 `original_retained_count=280`）。客户原版（ada_topic6_v2_261110，`c2_run_all_platforms.py::_run_stages_once`）逐平台直接调 vendor 00→09 脚本，
    不经 run_c2.py、不调用 retain_original（vendor 代码与 1030 bundle 一致；W43 实跑产物 149 条 UNCERTAIN 中 78 条正常入簇）。
  - 修复：`run_c2.py --uncertain-policy {split|keep}`（默认 split=上游），`run_topic6_c2.py` 传 keep：组不因未决成员拆散，未决成员记入
    `original_retained.json.uncertain_kept` / `c2_quality.uncertain_kept_count`；人工 forced、NON_EVENT 排除、技术降级规则不变。
    E1「主要驱动话题」同次数时按簇内最高热度分、再按事件名确定性排序（此前同次数顺序随输入行序）。
  - **聚簇链路整体对齐原版（除改进点）**：逐段对比原版 00→09 后，MA 封装再改两处——`--frontend legacy`（00 清洗 / 01 事件性 / 02 Frame
    分开调用，00 由模型清洗标题，与原版同口径；此前 F1 融合锁定原标题不清洗）、`run_c2.py --member-review off`（新增，默认 on=上游；跳过原版没有的
    x2 置信度筛查与 x3 双向复核）。保留的改进：跨平台合并 + top_k=60、平台并行与 03∥04 并行、阶段 checkpoint、03 截断拆批、DeepSeek、降级不阻断。
    代价：前端调用由每平台 1 次变为 00 每平台 1 次 + 01/02 全量各 1 轮，C2 耗时预计上升，需 212 实测；x3 省掉后部分抵消。
  - 测试：`test_topic6_c2_uncertain_policy.py`（含离线 mock 端到端；去掉 keep 时端到端用例失败）、`test_topic6_c2_rc14.py`（legacy 调用计数、无 x2/x3）、
    `test_topic6_report_rule_fixes.py`。mock relay 支持 legacy 三段 prompt。
    mock relay 新增「如何看待」→ UNCERTAIN。**需 update_ma.sh**；真实合并率需 212 实跑确认。
- **E4 说明字段不再程序截断**：`e4_marketing.py` 去掉合作动态说明 [:60]、风险判断/营销发现/消费者行为说明 [:80]（原版同样截断），
  字数只由写作 Prompt 约束；Q5 放宽输出字数后，输入截断会让模型凑字。输入 token 略增。
- **过程关键文件归档到飞书（新需求）**：新增 `process_files.py`。Gateway 在 HC 发卡前（后台）和任务终态时，从 artifact_sync 已同步的
  节点发布清单中挑白名单关键文件，上传到 job 文件夹（与 HC 宽表同一个 `{项目名}_{job_id前12位}`）下的「过程数据」子文件夹，原文件上传
  （`upload_all`，单文件 ≤20MB），文件名为相对路径以 `__` 拼接；同类文件只取最新轮次，按 sha256 幂等，失败只记 warning。
  白名单：数据质量报告、标准化表、C0 合并（营销可用打标）、可用子集、C2 结果/c2_status/c2_quality/original_retained/01_eventness、
  健康度摘要、E1~E4 统计输入、E2 节点判定、E3 词频审计、E4 仲裁审计、Phase E 执行报告。不传原始抓取、抽样、单路原始标注、
  宽表（已是 HC 在线表格）、报告（已是飞书文档）、checkpoint、洞察草稿。发起人对 job 文件夹授予 edit；任务在 HC 前失败也会建文件夹；
  终态有新上传时 @ 发起人附文件夹链接。W45 实测选中 13 个文件 2.1MB（补发布 C2 诊断后约 17 个）。
  coordinator `node=c2` 以 optional 附带 c2_quality/original_retained/01_eventness，`node=fetch_normalize` 附带 data_quality_report。
- **artifact_sync 物化修复（排查时发现）**：Files API 列表不返回 size，index 中 `fingerprint.size=None`，按 size 过滤候选导致
  212 上 job_fe11fa6b2761 已下载 92 个对象、0 个物化到项目目录（HC 宽表靠 `.objects` 兜底才找得到）。改为 size 未知时不排除、
  以 sha256 定案并复用 index 中已存的 sha256；用 212 真实 index 回放，物化 57 个文件。
- **Q1 E3 热门事件 TOPN 排序**：第一排序事件簇热点数量，第二排序由簇内最高分改为簇内平均标准化热度分；「热度值」列口径不变（最高分），
  表格末列新增「平均热度值」。E3 Prompt v7（只补输入列说明）。
- **Q4 「锚点未核实」**：日历查不到锚点日期的节点不再进入「上周节点」，只在 stderr 记录；E2 Prompt v8 删除保留该标注的规则。
- **Q5 字数**：E4 B 组撰写 v2——「营销发现」100-120 字（营销观察案例卡与消费洞察洞察卡两处）、消费洞察「行为反应」80-100 字（信息不足不得编造凑字数）。
- **R3**：`session.error` 的 `retry_status=retrying` 判断提前到 provider 重试之前，平台重试期间 Gateway 不叠加重试/催促。
- **R4**：回复已完成/已停止/正在停止任务的进度卡片发 `/continue` 时直接提示，不再回落续跑同一用户的其他任务；沙箱故障文案去掉 topic6 入口不存在的按钮。
- 影响文件：`process_files.py`（新）、`artifact_sync.py`、`gateway.py`、`topic6_hitl.py`、`job_notifier.py`、`arkagent/feishu.py`（upload_file）、
  `topic6-insight/01_统计/e4_marketing.py`、`topic6-event-registry/scripts/{retain_original,run_c2,run_topic6_c2}.py`、`topic6-insight/01_统计/{e1_industry,e2_nodes,e3_platforms}.py`、
  `topic6-insight/02_洞察/{E2_营销节点/v8,E3_平台新鲜事/v7,E4_营销发现/_writing_marketing_consumer/v2}.md`、`topic6_runner.py`、`pipeline_store.py`、
  openspec（session-runner / skill-insight / skill-event-registry）、版本状态/version_mapping/ks 口径文档。

### W45 实跑问题修复（P1-1 / N1 / N2 / N3 / N5 / N6）

详见 Obsidian `MA_ADA_DEV/review/topic6_W45_session问题复盘_2026-11-09.md`。N4(妙搭访问权限)按原版设计不改。

- **P1-1 网页同名链接塌缩**
  - 现象：W45 Phase G `Missing rendered source link: https://www.douyin.com/search/...成都蓉城致歉`。
  - 根因：`build-report.mjs::blockSourceLinks` 以链接文字为 Map key，同一 block 内抖音/微博同名标题后者覆盖前者（客户 V2 模板同样存在）。
  - 修复：`SourceLinkIndex`(文字 → 候选列表)，按行内平台标注匹配域名，否则取未用过的链接。120 条链接的样例新旧输出字节一致。
  - 文件：`topic6-web-report/assets/source/build-report.mjs`；测试 `test_topic6_web_report_alignment.py`。**需 update_ma.sh**。
- **N1 `/continue` 报 `_start_token_keepalive` 不存在**
  - 根因：`febc60a` 删除每任务保活方法时漏删 gateway 调用点；测试注入同名方法掩盖了问题。
  - 修复：删除调用点(token 由常驻 `start_token_refresher` 刷新)；新增 AST 守卫 `test_topic6_self_method_integrity.py`。
- **N2 待裁决时单发「A」只得到帮助文案**
  - 修复：仅当本会话/所回复卡片存在「等待用户裁决」的失败任务且消息为单个字母时，提示 `/continue A`，不自动执行。
- **N3 「…W45的热点周报」误触发 full**
  - 根因：`parse_trigger` 按子串包含「热点报告/热点周报」触发。
  - 修复：改为 `/start [模式] [周期]` 命令，缺省 full，模式写错回用法；提到关键词不再触发。README/QUICKSTART/spec/提示词同步。
- **N5 可恢复 Session 被判死、他人无法 `/continue`**
  - 根因：① `session.error` 不看 `retry_status` 一律记 session 级失败；② 方舟控制台 interrupt 记为不可逆 stopped；
    ③ `/continue` 按 (chat, 发送者, thread) 精确匹配，袁杰松发起的任务吕玉新回复卡片也找不到；④ 沙箱故障期间仍连续 nudge 3 次(input 1.13M→2.08M)。
  - 修复：`retrying` 继续监听、`terminal`/status_failed/terminated 为 session 级、其余可恢复(`continue_job` 仍以 GET /sessions 复核)；
    控制台 interrupt 记可恢复失败(卡片停止按钮流程不变)；回复进度卡片 `/continue` 按卡片定位，限发起人或 `TOPIC6_ADMIN_OPEN_IDS`；
    连续 3 次沙箱故障停止 nudge，可恢复失败并提示平台恢复后继续。`get_resumable_job_by_session_key` 不改(topic5 在用)。
  - 已真正终止的 Session 从断点新开续跑列入二期。
- **N6 隐藏临时文件被当作节点产物发布**
  - 根因：`Path.glob("*_r1.xlsx")` 匹配 `.c0_retry_input_r1.xlsx`。
  - 修复：`publish_node_artifacts.py` 跳过任一路径段以 `.` 开头的文件/目录；全部 include 规则无点开头产物。**需 update_ma.sh**。

## 2026-11-10

### 客户 V2 对照复核：补齐 4 处确认遗漏（不改变节点结构与交付结构）

逐文件 + 逐功能点对照客户 V2（`ada_topic6_v2_261110`）后，二次复核确认以下为真实遗漏。
CHANGELOG 2026-11-04「未纳入」清单中的业务决策项（周期 4 周回溯、交付目录 6 子夹、G/H 非阻塞、A-2.5 退出码 3）不在本次范围。

- **网页正文以 HC3 定稿飞书文档为准（FX03）**
  - 触发现象：HC3 阶段用户可在飞书文档里改文字、表格和配图。`/republish` 只重新读取配图，正文仍取本地
    `status.f.merged_report_file`，飞书里改的文字不会进网页；与 coordinator「用户在飞书文档里直接改过的内容以飞书为准」自相矛盾。
  - 根因：迁移时客户 `pipeline_g.py::fetch_feishu_markdown` 只移植了配图下载部分（`fetch_case_images.py`）。
  - 修复：新增 `topic6-web-report/scripts/fetch_report_md.py`，复用 `fetch_case_images.py` 的 tenant token 与 blocks 读取，
    把文档块逆向还原为 Markdown 后交给 `md2source.py`（后续构建/校验链不变）。读取失败、无章节、章节少于本地、
    或本地有表格/链接而还原后为 0 时自动回退本地稿，stdout 给出 `source/reason`，不阻断 Phase G；
    Coordinator 写 `status.g.report_source(_reason)` 并在回退时提示用户。`REPUBLISH_MESSAGE` 与 `/republish` 回复改为“重新读取正文与配图”。
- **E2 发布日口径（FX01）**：coordinator 只有 `{publish_date}` 占位符、全文未定义取值，`run_stats.py` 缺省用运行当天，
  E2 上周/本周窗口会随运行日期漂移。`run_stats.py` 缺省改为 `run_config.period.date_end + 1`（显式传参仍优先，
  不一致时 WARN；无 run_config 时退回今天并 WARN）；coordinator/SKILL.md 写明口径。
- **E2 节点日历（FX02）**：`marketing_calendar_2026.csv` 比客户 V2 E2 实际使用的 `2026年营销节点_byday.md` 少 4 个电商节点
  （年货节、618预售启动、99大促、双11预售启动），618/双11/双12 大促类型误为「大众节日」；C3 能提取这些节点但 E2 查不到锚点
  → 落入「锚点未核实」。补齐并修正类型后节点集合/类型与客户一致（223），保持 BOM+CRLF。
  `ks/04_节点判定.md`、`ks/07_报告结构.md`、`prompts/06_阶段_E洞察.md`、`prompts/运行前置.md` 仍写“`MARKETING_CALENDAR_PATH`
  注入外部 byday.md / 缺失静默跳过”，与代码（默认 csv、0 节点报错）及错误案例 E13 结论相反，已改正。
- **飞书发布 warnings 进入 HC3（FX06）**：admin 授权失败等 `warnings` 原来只要求“原样记录”，审核人看不到；
  改为逐条写入 HC3 `issues_detected`（前缀「飞书发布：」）。
- **文档与知识（FX09）**：`references/E3_统计口径说明.md`、`综合热度指数算法说明.md` 此前误迁客户 09-15 旧版（描述 v1_legacy，
  代码 `ACTIVE_SCORING_VERSION="v2"`），改为客户 10-13 校正版；`memory/topic6/错误案例库.md` 追加客户 E5~E17 的 MA 适配摘要
  （E11/E12 标注不适用）；`CUSTOMER_TO_MA_FILE_MAPPING.md` 补登 pipeline_g / feishu_push_hc / feishu_pullback / pipeline_deliver /
  preflight_check / run_logger / KS 等去向，并纠正 `md2source.py`“未迁移”的误记。
- **测试**：新增 `tests/test_topic6_phase_g_feishu_source.py`（8 条：飞书块↔Markdown 往返结构一致、飞书改文字/单元格进入 source.json、
  兜底、还原结果可构建并通过 validate-rendered-content、编排指令）与 `tests/test_topic6_v2_gap_fixes.py`（11 条）。
  两文件在修复前代码上全部失败（19/19），修复后全部通过；Topic6 全量 `532 passed, 5 skipped`（修复前基线 513 passed）。
- **待办**：`tools/pack_skills.sh` → `tools/upload_skills.py` 上传 topic6-web-report / topic6-insight / topic6-fetch-normalize /
  topic6-annotation 新版本；`create_all.sh --update-env --update-memory` 同步 coordinator 与 Memory；Gateway 重启加载
  `topic6_runner.py`/`gateway.py`。212 上真实飞书文档验证一次 Phase G（`/republish` 即可，不重跑 A~F）。
- **影响文件**：`topic6-web-report/{scripts/fetch_report_md.py(新增),SKILL.md}`、`topic6-insight/{01_统计/run_stats.py,SKILL.md,references/*}`、
  `topic6-fetch-normalize/references/marketing_calendar_2026.csv`、`topic6-annotation/{ks/04,ks/07,prompts/06,prompts/运行前置}.md`、
  `agents/coordinator.system.md`、`memory/topic6/错误案例库.md`、`topic6_runner.py`、`gateway.py`、`TOPIC6_DEBUG_QUICKSTART.md`、
  `TOPIC6_DATA_FILTER_RULES.md`、`CUSTOMER_TO_MA_FILE_MAPPING.md`、`openspec/specs/{skill-web-publish,coordinator-orchestration,session-runner,skill-fetch-normalize}/spec.md`

---

## 2026-11-06

### C2 实体归一（03）首批撞 max_tokens=16000 阻断 full；Agent token_limits 恢复模型原生规格

- **现象**：job_d1f123f2c933（W44 full）C2 EV 的 E 阶段 `03_normalize_entities.py` 首批 50 个实体即
  `ValueError: 输出被 max_tokens=16000 截断`，run_c2 内部重试 + Agent 续跑共 2 次逐字一致失败，停在 HC2 前。
- **根因**：C2 各阶段 `--max-tokens` 默认（16000/24000）是上游 rc.14 按 `claude-sonnet-4-6` 调校的；MA 把
  `C2_CHAT_MODEL_ID` 换成 DeepSeek-V4.1-Flash（推理 token 计入 max_tokens，同 Session F1 单次输出已到 23,345/24000）
  却沿用原值；03 无截断兜底（07 有），temperature=0 + 失败不落缓存 → 每次同一请求、确定性失败。原版 V2 用 Sonnet
  且按平台各跑 00~09，未触发。
- **修复**：
  - `relay.py`：新增 `MODEL_MAX_OUTPUT_TOKENS`（V4.1-Flash 384000），表内模型按原生输出上限请求、不再被脚本默认值
    压低；截断报错附 `completion_tokens / reasoning_tokens / 正文字符数`，`Relay.reasoning_tokens` 累计。
  - `03_normalize_entities.py`：截断先原样重试一次、仍截断对半拆（与 07 `archive_split` 一致），两半串行累积注册表；
    拆到单名仍截断才失败；`auto_split`、`reasoning_tokens` 记入成本台账。
  - 3 个 Agent `token_limits` 256000/200000/56000 → 1024000/1024000/384000（模型原生：上下文 1,024K 只计输入、
    输出 384K），不人为压低。
- **上线**：均为 ma-resources 改动 → `update_ma.sh`，只对新 Session 生效；失败的 job 需新 Session 重跑。
- **待验证（212）**：full 跑通 C2；关注 03/F1 日志中的 reasoning_tokens 与 `auto_split`，以及 Agent 窗口放大后的
  单步耗时/成本。
- **影响文件**：`ma-resources/skills/topic6-event-registry/{scripts/relay.py,scripts/03_normalize_entities.py,CHANGELOG.md}`、
  `ma-resources/agents/{coordinator,annotator,validator}.json`、`openspec/specs/coordinator-orchestration/spec.md`、
  `DEPLOY_v2.1.md`、`CUSTOMER_TO_MA_FILE_MAPPING.md`、`tests/test_topic6_{ma_resources,c2_output_budget}.py`
- **补充**：03 缓存键记录生效的 `relay.max_tokens`（而非脚本参数），原生上限表变化时缓存随之失效。
  不做 relay 探测，按原生配置上线；212 首轮观察各 C2 阶段有无 HTTP 400、07 有无超时/失败块。

### W44 Review 修复（B1~B7 + 报告链接入口；见 Obsidian `MA_ADA_DEV/review/topic6_W44_整体Review_2026-11-06.md`）

- **B1 HC2 宽表推送失败 + 3 张同名空表**（Gateway）：`arkagent/feishu.py` 写入改为「≤5000 行且单批 JSON ≤
  `FEISHU_SHEET_WRITE_MAX_CHARS`（默认 300000，按 requests 实际请求体计、中文为 `\uXXXX`；待 212 用 W44 HC2 宽表实测）」切批；建表后失败删除该表并抛
  `SpreadsheetWriteError`（新增 `delete_drive_file`）。`feishu_doc_publisher._publish_wide_table` 飞书失败返回
  `{wide_table: {error}}`，`send_hc_card` 不再把它当「未同步」重试建表，HC 卡显示「宽表推送飞书失败，请在任务 outputs 中下载」。
- **B2 E3/E4 子步骤解析**：8 个解析点统一按期望表头定位伪表格（截止于下一已知表头或 `### `），定位不到再按标题取段；
  非空输出都找不到时告警并写入版块结果 `parse_warnings`。修复 W44 E3「0 个候选词 / 0 个事件」与 E4 漏写一个标题时串表。
- **B3 E3 串平台**：拼接前 `_isolate_e3_platform` 剔除其他平台标题起的段落与「上周{其他平台}无营销可用热点」行；
  标题归一（`### 微博热点`/`### **抖音**`/`### 抖音：`）；找不到本平台标题保留原文并告警。不改 Prompt。
- **B4 网页误判月刊**：`build-report.mjs` 周/月刊只按页头周期标签判定，周期解析提前到文件头（避免 TDZ）。
- **B5 热搜链接损坏**：新增 `_common.md_link_title`（换行/`<br>`→空格、`[]`→全角、`|`→`\|`），只用于 5 个链接渲染函数；
  `_escape_md_cell` 不变（不改非标题单元格字形）。
- **B6 snapshot 上传指令冲突**：web-report `SKILL.md` 第 5/9/10 步、`CLAUDE.md`、`DESIGN_SPEC.md`、`agents/openai.yaml`
  删除上传要求；`upload-html.mjs` 保留不引用。
- **B7 自校验**：`report_self_check` 新增 fail 级「E3 平台重复」（只认裸平台标题，`### 微博 · 平台热点词 TOP 3` 不算）「E3 串平台」，走现有 `--sections e3` 定点重跑路径。
- **D2 飞书报告入口**：报告仍建在机器人云空间根目录（不迁移）；终态卡（完成/失败/停止）增加「打开飞书报告文档」按钮，
  终态通知追加「飞书报告：{url}」，链接取自最新一条 HC3 事件。
- **上线**：B1、D2 只动 Gateway → 重启 Gateway；B2~B7 为 ma-resources 改动 → `update_ma.sh`（只对新 Session 生效）。
- **待 212 验证**：B1 单批字符上限；B2/B3 用真实 E3/E4 输出确认无误剔除；B4 W44 网页标题为周刊。
- **影响文件**：`arkagent/feishu.py`、`feishu_doc_publisher.py`、`topic6_hitl.py`、`topic6_runner.py`、`topic6_progress_card.py`、
  `job_notifier.py`、`topic6-insight/{scripts/pipeline_e.py,scripts/report_self_check.py,01_统计/_common.py,01_统计/e4_marketing.py}`、
  `topic6-web-report/{assets/source/build-report.mjs,assets/source/DESIGN_SPEC.md,SKILL.md,CLAUDE.md,agents/openai.yaml,CHANGELOG.md}`、
  `coordinator.system.md`（自校验项列表）、`openspec/specs/{hitl-review,skill-insight,skill-web-publish}`、
  `CUSTOMER_TO_MA_FILE_MAPPING.md`、`tests/test_topic6_{hc_sheet_roundtrip,web_report_alignment,e_stage_parsing,report_doc_link}.py`

---

## 2026-11-05

### 对话模型统一 DeepSeek-V4.1-Flash（DataHub、向量除外）

- **变更**：3 个 Agent（coordinator/annotator/validator）`DeepSeek-V4-Flash-0731` → `DeepSeek-V4.1-Flash`，
  `token_limits` 按其原生 256K：256000/200000/56000（与 11-03 首次接入该模型时一致）；Phase E `pipeline_e.py
  --model` 默认、demo R1~R5 `run_demo_routes.py` 默认（`DEMO_CHAT_MODEL_ID` → `C2_CHAT_MODEL_ID` → V4.1-Flash）
  由 `Doubao-Seed-Evolving` 改为 `DeepSeek-V4.1-Flash`；demo 计价补 V4.1-Flash（¥3.5/¥14 per M）；
  Phase E/F/G cost_tracker `--model` 同步。
- **不变**：DataHub `DeepSeek-V4-Flash`（DataHub 侧不支持 V4.1）；Embedding `Doubao-embedding`（换维度需重置 registry）。
  价格表中保留的 `doubao-seed-evolving` 仅用于历史成本核算。
- **风险**：Agent 上下文由 1M 降到 256K，长流程会更早触发方舟历史压缩；Phase E Prompt 与 max_tokens 按豆包调校，
  文风/长度可能变化。均需 212 smoke 验证。
- **影响文件**：`ma-resources/agents/{coordinator,annotator,validator}.json`、`topic6-insight/{scripts/pipeline_e.py,SKILL.md}`、
  `topic6-annotation/{scripts/run_demo_routes.py,prompts/0{6,7,8}_*.md}`、`openspec/{project.md,specs/{coordinator-orchestration,
  sub-agents,skill-insight,ma-resource-deployment}}`、`tests/test_topic6_{ma_resources,c2_rc14}.py`

### C2 升级到 blueai-canonical-event-registry 3.0.0-rc.14 + Chat 模型切 DeepSeek-V4.1-Flash（提速）

- **触发现象**：MA C2 实测 W41 skip_sampling 1484 行约 3h20m、full 197 行约 1h；V2 W43 四平台 1436 行约 32 分钟、
  B站 116 行约 6 分钟。同量级数据相差约 10 倍。
- **根因**：① Chat 模型 `Doubao-Seed-Evolving` 为推理模型，每次调用含长推理，C2 每阶段都按批调用，延迟被整条链放大；
  ② 前端 00/01/02 三轮独立调用（01/02 并发仅 4），03 实体在合库后单条串行链覆盖全量；③ 2.2.0 无阶段 checkpoint，
  中断后重跑粒度粗。
- **做法**：整包迁入 rc.14（统一入口 `run_c2.py`、F1 融合一次调用、E/V 并行、07 覆盖重试 3 次、x3 ID 反馈重试、
  阶段 checkpoint + 内容寻址缓存、c2_quality/provenance）；`run_topic6_c2.py` 改为调用 `run_c2.py run --mode
  cross-platform --frontend fused --max-requests 8 --front-workers 2`（总并发 16），保留跨平台合并（MA 优于 V2 生产按平台独立跑）；
  `C2_CHAT_MODEL_ID` 改 `DeepSeek-V4.1-Flash`（非推理，relay 已开通）。MA 无人值守：`--review-policy record`
  （门禁不停，待审项进 `c2_status.quality.review_pending` 交 HC1）、`--degraded-policy accept`。按需补证不启用。
  `relay.py` 保留 MA 传输层。结果契约（`c2_event_result_r{N}.xlsx` 两列）不变。
- **验证**：新增 `tests/test_topic6_c2_rc14.py`（mock relay 端到端：四平台 44 行，F1 4 次调用覆盖 00/01/02，跨平台
  同组 8 条归一事件，同签名重跑零调用）。真实耗时与归档质量待 212 小样本对照（付费，等指令）。
- **影响文件**：`skills/topic6-event-registry/`（整包）、`environment.json`、`agents/coordinator.system.md`、`DEPLOY_v2.1.md`、
  `CUSTOMER_TO_MA_FILE_MAPPING.md`、`openspec/specs/skill-event-registry/spec.md`、`openspec/project.md`、
  `tests/test_topic6_{c2_rc14,pipeline_optimizations,ma_resources}.py`、`tests/fixtures/c2_mock_relay.py`

### C0 解析失败/缺失行：补全 → 重提 → 保留交 HC1

- **触发现象**：V2 W43 全量 C0 3771 行有 21 行无结果（0.56%）：12 行模型只返回推理、正文为空
  （relay_empty_completion），9 行答案完整但 JSON 结尾残缺（缺 `}` 或多 `"]`）。MA `_parse_dict` 只能救回 2/9；
  MA 筛选把未决行直接排除，HC1 审核人看不到这些行。
- **根因**：结尾残缺是格式问题，重提解决不了还要再付费；排除口径与 V2「默认保留 + HC 抽查」不一致，属静默丢数据。
- **做法**：① `datahub_annotate._parse_dict` 增加受控结尾补全，仅在主字段为「是/否」时采纳；② 空返回继续走已有
  3 轮行级重提；③ `c0_filter_usable` 保留剩余未决行进入 R1~R5/C2（「是否营销可用」留空，E 阶段只取「是」，报告不受
  影响），stdout 输出 `unresolved_rows`；`merge_annotations` 新增 `C0 未决行` warn 写入 HC1 issues。>5% 熔断不变。
- **验证**：W43 真实 raw 结果离线重放，未决 21→12（剩余全部为空返回，由重提覆盖）；新增解析与筛选回归测试。
- **影响文件**：`skills/topic6-annotation/scripts/{datahub_annotate,c0_filter_usable,merge_annotations}.py`、`SKILL.md`、
  `prompts/run_config契约.md`、`agents/coordinator.system.md`、`openspec/specs/skill-annotation/spec.md`、
  `tests/test_topic6_{annotate_parse_retry,parity_gaps}.py`

### C0 活跃版本 v5 → v13（客户 V2 锁定版原样迁入）

- **触发现象**：MA C0 v5 为自研压缩版，从未做真实 HC1 对比；客户 V2（bundle_20261030）已在 DeepSeek-V4-Flash
  下按 W43 人工 ground truth 迭代到 v13 并锁定。MA 标注模型已是 DeepSeek-V4-Flash，但质量检查仍用 v4+豆包时代的
  55%~58% 基线，v13 真实占比（37%~41%）会每次误报 warn。
- **做法**：`prompts/C0_基础事实/v13.md` 原样迁入（SHA-256 a7573188…，9 字段输出契约与 `{{platform}}/{{title}}/{{desc}}`
  占位符不变，`datahub_annotate.py` 无需改动）；v5 保留归档。`USABLE_BASELINE` 改为 (0.37, 0.41)，coordinator / openspec
  / ks/02 基线描述同步。顺带修正 SKILL.md「Prompt 版本」段遗留的 R2 v1 / R5 v2（实际活跃 R2 v2 / R5 v3）。
- **目录只留 v13**：删除 v1~v5（v5 与客户 `_archive/v5.md` 同号不同内容，多版本并存易被误取），历史以 git 为准；
  openspec 对 C0 改为「目录只保留活跃版本」。
- **影响文件**：`skills/topic6-annotation/{SKILL.md,prompts/C0_基础事实/v13.md,ks/02_营销可用判定.md,scripts/merge_annotations.py}`、
  `memory/topic6/{_版本状态.md,MEMORY.md}`、`version_mapping.md`、`agents/coordinator.system.md`、
  `openspec/specs/{skill-annotation,coordinator-orchestration}/spec.md`、`CUSTOMER_TO_MA_FILE_MAPPING.md`、
  `tests/test_topic6_{parity_gaps,pipeline_optimizations}.py`
- **待办**：上传新 SkillHub version + 同步 Memory Store 镜像；212 小样本真实 DataHub 验证（付费，待指令）。

## 2026-11-04

### 新增 /republish：HC3 后补营销观察配图，只重跑网页与妙搭发布

- **触发现象**：营销观察配图只来自 HC3 定稿飞书文档里的人工图片；HC3 通过后才发现漏传，只能在网页里
  看到「配图待补充」。`/continue` 对已完成（done）任务不生效，重新触发又要从取数跑起。
- **做法**：新命令 `/republish [job_id|session_id]`，默认取本会话最近一个已过 HC3 的任务，在原 Session
  注入自包含指令：不重跑 A~F，只重跑 Phase G（从飞书文档重新读取配图并生成网页）和 Phase H（更新
  `status.h.app_id` 同一个妙搭应用）。新发一张进度卡跟踪本轮进度。
- **旧 Session 兼容**：指令自包含，不依赖 Session 创建时的 coordinator 版本；BYOK Header 改为按
  Session 的 agent 快照决定（`agent.model.provider` 非空才带），切模型前用方舟原生模型的旧 Session
  不会因多带 Header 被 400 InvalidModelHeader 拒绝。
- **防误判**：判定本轮完成时只看 `/republish` 之后的消息，历史旧 `online_url` 不会把本轮发布失败误判
  为完成。有其他任务在运行、或 Session 非 idle 时拒绝，不改本地状态。
- **影响文件**：`gateway.py`、`topic6_runner.py`、`pipeline_store.py`（`list_jobs_for_chat`）、
  `arkagent/ark.py`（`get_session`）、`openspec/specs/{gateway-command-routing,session-runner}`、
  `TOPIC6_DEBUG_QUICKSTART.md` §2.5.1、`tests/test_topic6_{new_command,interrupt_recovery}.py`

### Agent 模型改为 DeepSeek-V4-Flash-0731，token_limits 按模型原生规格

- **变更**：4 个 Agent（`agents/{coordinator,annotator,insighter,validator}.json`）`model.id`
  `DeepSeek-V4.1-Flash` → `DeepSeek-V4-Flash-0731`（bmc-llm-relay 模型列表标注：上下文 1,000k、
  输出上限 384k、OpenAI/Anthropic/OpenAI-response 协议）。`token_limits` 256000/200000/56000 →
  1000000/1000000/384000（该模型的上下文只计输入，输出上限另计，所以最大输入 = 上下文）。
- **原因**：256K 是为避开"~180K 平台 bug"而人为压低的；该 bug 已查明是 BYOK Header 漏带（见下条），
  不再需要压低窗口，按模型原生能力配置，长流程不再在 ~200K 被迫压缩历史。
- **实测**：212 → relay 以 BYOK Header 调 `DeepSeek-V4-Flash-0731` 带 tools 请求，`finish_reason=tool_calls`。
  PRICING 由现有 `DeepSeek-V4-Flash` 键按子串匹配命中，无需新增。
- **待验证**：212 部署后 smoke/full 的单步耗时与成本（窗口放大后每步输入更长）；若明显变慢再评估折中值。
- **影响文件**：`ma-resources/agents/*.json`、`topic6-annotation/prompts/0{6,7,8}_*.md`（cost_tracker `--model`）、
  `openspec/{project.md,specs/coordinator-orchestration,specs/sub-agents}`、`tests/test_topic6_ma_resources.py`

### BYOK 鉴权 Header 漏带导致 relay 模式 model_request_failed（10/27 切回方舟原生的真实根因）

- **触发现象**：Agent 走 bmc-llm-relay（BYOK）时，长流程中途连续 `model_request_failed_error`
  （`retry_status=exhausted`，`span.model_request_end` input=0/output=0），Gateway 重试 3~5 次都失败，
  只有用户手动 `/continue` 能恢复；此前被归因为"方舟 ~180K 上下文 bug"，10/27 因此把 Agent 切回方舟原生。
- **根因**：方舟 BYOK 的模型鉴权 Header 按请求生效（不持久化到 Agent/Session）。`topic6_runner.py` 只有
  首条消息（`_drive_session`）带了 `extra_headers`；idle 催促、provider 重试、Gateway 重启恢复三处
  `send_message` 都没带。Gateway 自动发的这类消息触发的模型请求因缺鉴权直接失败，而失败后的
  provider 重试同样不带 Header，于是连续失败。
- **证据**：212 上 4 个 relay Session（sesn-20261022115226-co63m、sesn-20261022212837-g2kib、
  sesn-20261025233436-o7mfl、sesn-20261026150456-vmpfi）共 30 条 user.message：Gateway 自动发出的
  "你在 Phase X 中途停止了"/"[系统] 模型服务已恢复"**全部**失败（input=0）；触发、HC 通过、手动
  `/continue`（走首条消息路径，带 Header）**全部**成功，最高 184,029 input 也成功；vmpfi 首次失败时仅
  140,507 input，与上下文长度无关。
- **修复**：新增 `Topic6Runner._send_message` 作为注入 user.message 的唯一入口，统一携带 BYOK Header；
  四处调用全部改走它。Header 值只在内存中透传给 HTTP 请求，不记日志、不落库、不进消息正文
  （`ArkError` 只含状态码/request-id/响应体，不含请求头）。
- **测试**：`TestByokHeaderOnEveryMessage` 断言 provider 重试与 idle 催促都带 Header、日志不含 Header 值，
  并静态断言 Runner 内除 `_send_message` 外不直接调用 `self._ark.send_message`。修复前 2 条均失败，修复后通过。
- **影响文件**：`topic6_runner.py`、`tests/test_topic6_interrupt_recovery.py`、`openspec/specs/session-runner/spec.md`

### 按 CC V2.0 继续提效（节点功能不变）

- **E 阶段并发上限 2 → 6**（`pipeline_e.DEFAULT_MAX_WORKERS`）：拆分后 E 阶段峰值为 6 路
  (E1、E2、E3 打标∥初稿、E4 打标∥枚举)，上限 2 时 E3 四平台和 E4 A/B 组实际排队，V2 的并发提效吃不到。
  CC V2.0 不设上限；6 与峰值相等，不排队又保留护栏。bmc-llm-relay 6 路并发实测全部成功、无 429。
- **E1/E2 起始 max_tokens 对齐 V2**（1536→4000、4096→5000）：E1 实测输出约 1200 + thinking，1536 易触发
  一次升档重跑；起始预算贴近实际用量，省一次重试往返。升档逻辑不变。
- **行级失败自动重提 1 轮 → 最多 3 轮**（`datahub_annotate.FAILED_ROW_RETRY_ROUNDS`，对齐 V2 补标"最多
  自动 3 轮"）：每轮只提交上一轮仍失败的行；把补标闭环收在脚本内，减少 5% 熔断后 coordinator 再委派一次
  的往返。第 2 轮起审计文件 `{task}_retry_N_meta.json` 按轮分开、可续跑。
- **E4 A 组组内表格校验**：`validate_e4_group` 对 A 组补上表格列数检查（V2 validate-writing
  `_check_table_integrity`），错列只重写 A 组，不再等版块级校验失败后整个 E4 重跑。
- **demo 路由 Prompt 版本对齐（git diff review 发现）**：`run_demo_routes.py` 硬编码 R2=v1、R5=v2，
  正式模式已按 `_版本状态.md` 升到 R2 v2 / R5 v3，demo 会静默沿用旧口径。已改为 v2/v3，并新增测试
  `test_demo_routes_prompt_versions_match_version_status` 锁定两处一致。
- **未纳入（会改变节点功能/交付结构）**：任务周期 4 周回溯限制、交付目录 6 子夹与按人大根、网页改读飞书最终稿、
  G/H 发布非阻塞、C2 rc.14、C0 v13、A-2.5 软预警退出码 3。见 `Topic6_MA对照V2.0流程说明_覆盖度Review.md`。
- **影响文件**：`topic6-insight/{scripts/pipeline_e.py,SKILL.md}`、`topic6-annotation/scripts/datahub_annotate.py`、
  `openspec/specs/{skill-insight,skill-annotation}/spec.md`、
  `topic6-annotation/scripts/run_demo_routes.py`、
  `tests/test_topic6_{v2_efficiency,annotate_parse_retry}.py`

## 2026-11-03

### 模型从火山方舟切换到 bmc-llm-relay（blueai 模型资源）

- **范围**：4 个 MA Agent 与所有沙箱脚本的模型调用统一走 bmc-llm-relay
  （`https://bmc-llm-relay.bluemediagroup.cn/v1`，OpenAI 兼容）。DataHub 标注模型由 DataHub 侧调度，
  不变；`ARK_API_KEY/ARK_BASE_URL` 只保留给 Managed Agent 控制面（Agent/Environment/Session/Skill/Vault）。
- **Agent 层**：`agents/{coordinator,annotator,insighter,validator}.json` 的 `model` 改为 BYOK
  `DeepSeek-V4.1-Flash` + `provider=bmc-llm-relay` + `token_limits` 256000/200000/56000（BYOK 必填；
  256K 让方舟在 ~200K 自动压缩，避开 ~180K 平台 bug）。Gateway 发消息附加
  `X-User-Model-Header-Authorization`；coordinator 走 relay 而缺 `BYOK_MODEL_AUTH_HEADER_*` 时
  `_byok_model_headers` 启动即报错（原来静默不附加，每次模型请求都会被 relay 拒绝）。
- **脚本层**：`pipeline_e.py`、`relay.py`（C2 chat + embedding）、`run_demo_routes.py` 只读
  `LLM_RELAY_*`，删除 `ARK_*`/`OPENAI_*` 回退，缺失直接报错；成本台账 platform 改记 `bmc-llm-relay`。
  `fetch_hot_topics.py` 删除 `ARK_API_KEY` 作为取数 key 的回退（方舟 Key 对热点 MCP 无效）。
  `environment.json` 不再向沙箱注入 `ARK_API_KEY/ARK_BASE_URL`。
- **模型 ID**：C2/E/demo `Doubao-Seed-Evolving`、Embedding `Doubao-embedding`（relay 路由到
  `doubao-embedding-text-240715`，2560 维，与历史 registry 向量维度一致）不变；Phase E/F/G 成本记录的
  `--model` 改为 `DeepSeek-V4.1-Flash`；PRICING 删除方舟原生 ID。
- **部署**：`update_ma.sh` 与 `create_all.sh` 新增校验 `LLM_RELAY_BASE_URL/API_KEY`（及
  `BYOK_MODEL_AUTH_HEADER_*`），缺失时不部署；`env.example`、`TOPIC6_DEBUG_QUICKSTART.md` §2.2.1 同步。
- **实测（本机→relay）**：`/v1/models` 含上述 4 个模型 ID；`DeepSeek-V4.1-Flash` 带 tools 请求返回
  `finish_reason=tool_calls`；`relay.Relay`(Doubao-Seed-Evolving) 返回正常；`relay.Embedder` 返回 2 条
  2560 维向量；`pipeline_e.call_llm_async` 流式返回 `stop`。MA 侧 Agent 经 BYOK 的端到端尚未在 212 验证。
- **影响文件**：`ma-resources/agents/*.json`、`ma-resources/environment.json`、`ma-resources/create_all.sh`、
  `update_ma.sh`、`gateway.py`、`config.py`、`topic6_runner.py`、`env.example`、`TOPIC6_DEBUG_QUICKSTART.md`、
  `topic6-insight/{scripts/pipeline_e.py,SKILL.md}`、`topic6-event-registry/{scripts/relay.py,SKILL.md,references/runbook.md,references/pipeline.md}`、
  `topic6-annotation/{scripts/run_demo_routes.py,prompts/06~08}`、`topic6-fetch-normalize/scripts/fetch_hot_topics.py`、
  `memory/topic6/MEMORY.md`、`CUSTOMER_TO_MA_FILE_MAPPING.md`、`openspec/{project.md,specs/*}`、
  `tests/test_topic6_{ma_resources,pipeline_optimizations,v2_efficiency}.py`

### 参照 CC V2.0 提效:E3/E4 分步并发、截断升档、版块级校验、取数/标注质量前置

- **触发现象**：客户 V2.0(bundle 20261030)实测 E3 打标+初稿 92.3s→60.9s、E4 撰写
  449.5s→266.2s(-40.8%)；MA 版存在 E3/E4 单次长输出易截断、截断后整段重跑、`--sections`
  定点重跑会新开只含部分版块的轮次导致 Phase F 缺文件、日历缺 7 个节点、R1~R5 主字段为空
  仍计为有效等问题，均会引起返工。依据：Obsidian《Topic6 MA 提效优化清单》。
- **改动**：
  - `pipeline_e.py`：`finish_reason=length` 时 max_tokens 翻倍重试(≤2 次，上限 32000)，截断稿不进
    checkpoint；版块完整性闸(空/截断/表格列数)不过重跑 1 次；单版块异常隔离；Prompt 版本按数字
    排序；`--sections` 无 `--version` 时复用最新轮次；E3 打标∥初稿→四平台并发撰写；E4 打标∥枚举
    →仲裁+同池去重→脚本排序→A/B 两组并发撰写，每组做卡片格式+链接溯源校验，不过则重写 1 次；
    伪表格解析修复漏写表头吞第一条、"无重复事件"被当数据行；子步骤 Prompt 纳入 checkpoint 签名。
  - `fetch_hot_topics.py`：A-2.5 数据质量报告，硬错误退出 1，软预警进 HC1。
  - `datahub_annotate.py`：主字段为空计入 `parse_error`，走失败行重提和有效率熔断。
  - `01_统计`：升级到 V2.0(E1 无上期不输出环比块；E2 用全量 df、代表热搜 TOP3、品牌IP候选列；
    E3 热点词改到 pipeline_e 打标后拼接，新增 `e3_word_candidates.json`/`e3_event_fulltext.json`；
    E4 新增营销观察/消费洞察结构化候选)；日历 CSV +7 节点，日历解析为 0 行时报错。
  - Prompt：R2 v2、R5 v3、E1 v3、E2 v7、E3 v6 + 子 Prompt、E4 五个子 Prompt；旧版保留可回退。
- **未采纳**：V2 E4 子 Agent 分步模式(MA 改为脚本内直连方舟)、C2 审核门禁与联网补证、C0 v13
  (先做对照实验)。
- **待验证**：提效数字均为 V2 实测，尚未在 MA 上跑过。（并发上限已于 11-04 调为 6，见下条）
- **影响文件**：`ma-resources/skills/topic6-insight/{scripts/pipeline_e.py,01_统计/*,02_洞察/*}`、
  `topic6-fetch-normalize/{scripts/fetch_hot_topics.py,references/marketing_calendar_2026.csv}`、
  `topic6-annotation/{scripts/datahub_annotate.py,prompts/R2_商业合作/v2.md,prompts/R5_消费者行为/v3.md}`、
  `ma-resources/agents/{coordinator,insighter}.system.md`、`ma-resources/memory/topic6/_版本状态.md`、
  `ma-resources/version_mapping.md`、`openspec/specs/{skill-insight,skill-fetch-normalize,skill-annotation}`、
  `tests/test_topic6_v2_efficiency.py`、`tests/test_topic6_mock_nodes.py`、`tests/test_topic6_pipeline_optimizations.py`

---

## 2026-10-29

### 停止任务改为不可逆删除 Topic6 Session

- `user.interrupt` 只中断当前执行轮并让 Session 回到 `idle`，语义是暂停，不是终止。
- Topic6 停止按钮现在执行 `interrupt → 等待新 idle → DELETE Session`；删除成功或确认
  Session 已不存在后，才把任务记为 `stopped` 并释放 Topic6 lease。
- 删除失败或超时保持 `stopping`，禁止显示“已停止”；`stopped` 不再允许 `/continue`。
- Topic5 仍只使用 interrupt，不删除 Session。

### C2 流式 Chat 请求增加整体截止时间

- **触发现象**：C2 批次使用 SSE 读取模型响应时，socket `timeout` 只约束单次阻塞读取；
  对端持续发送零碎数据或半开连接时，单次调用可能超过配置时限且长期占住并发槽。
- **根因**：`urllib` 的 socket 超时不是请求整体时限，原实现没有独立 watchdog 主动关闭
  响应流。
- **修复**：每次流式请求启动整体时限 watchdog；到期后主动 shutdown/close 底层
  SSL socket 与响应对象，把阻塞读取转换为 `TimeoutError` 并进入既有有限重试。
- **协调器等待**：C2 运行期间只允许 90 秒内的同步等待并直接读取 `c2_status.json`；
  禁止超过沙箱后台阈值的 sleep 和 `task_get`/`.bash_bg` 轮询，避免产生无业务价值的
  后台等待任务。

### 标注 Prompt 版本契约改为显式且 fail-closed

- **触发现象**：`job_fe89f760ce50` 的 R1 子 Agent 未收到 `prompt_version`，误把 System
  Prompt 示例中的 `v4` 当作实际版本；挂载 Skill 只有 R1 v1/v2，worker 首次启动失败。
- **根因**：Coordinator 把 Memory Store 当作 `/mnt/memory/$STORE_ID/...` 文件系统读取，
  未取得版本镜像后仍继续；Annotator 输入契约也没有对缺失版本和文件存在性 fail-closed。
- **修复**：Memory 改为使用 Session Memory 工具按 path 读取；版本始终从已挂载
  `topic6-annotation/SKILL.md` 权威表解析并预检文件。所有 Annotator 委派必须携带显式
  `prompt_version`，缺失、格式非法或文件不存在时禁止猜测和启动 worker。

## 2026-10-28

### 清理临时恢复与旧客户端兼容

- 删除根据 Agent 自由文本推断 HC 类型并伪造审核载荷的 Gateway 兜底；Coordinator 未输出
  合法 HC JSON 时按契约 fail-closed，不再以正则推断业务状态。
- 删除整库清空本地任务/HC 的 `reset_pipeline_state.py`。正常中断统一由 Gateway 启动
  恢复、SSE 重连、`/continue` 和 `/new` 处理，不再留下远端 Session 与本地状态脱节的入口。
- `TOPIC6_SKIP_MIAODA_OAUTH` 保留为显式测试开关且默认关闭；开启后跳过扫码与用户 Vault
  注入，允许前序流程测试，Phase H 缺凭据失败视为预期结果。
- 删除废弃的 `test/小样本/试跑` 显式模式别名和历史备注卡片回调，仅保留当前
  `demo/full/skip_sampling` 与 `pass/reject` 契约。

## 2026-10-27

### Phase H online_url 取消域名白名单

- **触发现象**：Session `sesn-20261026175807-2r20q` 妙搭发布 `release_status=finished`，
  API 返回 `https://bluefocus.feishu.cn/page/AsbomkwecdDshSaehW5cNisVn7c`，Gateway 因域名
  不在 `aiforce.cloud / feishuapp.cn / bluemediacdn.com` 白名单内判定"未输出有效 online_url"，
  任务失败。
- **根因**：妙搭创意模式（html）应用的发布链接就是 `https://{租户域名}/page/{meta_token}`
  （lark-cli `lark-apps` 文档明确说明，开发态与发布态同一链接，形似飞书文档）。域名格式随应用
  类型/租户变化，2026-09-30 已因新增 `feishuapp.cn` 失败过一次；白名单每次都滞后。
- **修复**：`extract_final_online_url` 只认 Phase H 结构化 JSON 的 `online_url`，不再校验域名；
  仅拒绝非 http(s)（本地路径 / file://）、`release_status` 显式非 finished、飞书云文档路径
  （`/docx/`、`/wiki/`、`/sheets/`、`/file/` 等，防止 Phase F 文档链接冒充）。Coordinator 与
  Phase H prompt 删除"`*.larkoffice.com/page/*` 不可作为完成链接"，登录可见页面以
  finished + 链接取自 API 为准。
- **影响文件**：`topic6_runner.py`、`ma-resources/agents/coordinator.system.md`、
  `ma-resources/skills/topic6-annotation/prompts/09_阶段_H妙搭发布.md`、
  `tests/test_topic6_new_command.py`、`tests/test_topic6_fix_regression.py`
- **补充（同日，/continue 仍失败）**：白名单放开上线后对同一 Session 执行 `/continue`，Gateway
  注入「继续」，Agent 回复 `[need_decision]` 收尾文本（不再重复发布 JSON），`_handle_idle`
  只看最后一条消息 → 再次进 P0-4 判"未输出有效 online_url"。修复：`continue_job` 先倒序扫描
  Session 历史，已交付过有效 `online_url` 则直接 `mark_done` 并回复链接，不再注入消息；
  Phase H idle 时最后一条无链接也回查历史。影响 `topic6_runner.py`、`gateway.py`。

---

## 2026-10-12

### Environment 更新优先复用已记录 ID

- **触发现象**：执行 `update_ma.sh` 时，创建同名 `topic6-prod-env` 返回
  `409 ResourceConflict`，提示该名称已存在于 `default` 项目。
- **排查结论**：`GET /environments` 仍返回
  `{"data":[...], "next_page":"..."}`，并非响应结构变更；但旧脚本只读取
  `page_size=200` 的第一页，未消费 `next_page`。本次运行同时证明记录的旧 Environment
  ID 已不可访问，而同名环境仍存在于后续页，因此名称查找漏检后错误发起创建。另有风险是
  `ark_get` 会静默吞掉 GET 的 HTTP 错误，将其误判为“环境不存在”。
- **修复**：`create_all.sh` 现在优先校验并复用
  `TOPIC6_ENVIRONMENT_ID`，仅在 ID 缺失、失效或不可访问时才按名称查找；名称查找会
  消费完整的 `next_page` 分页。Environment 继续原地更新，不删除已有资源。
- **影响文件**：
  - [ma-resources/create_all.sh](./ma-resources/create_all.sh)

## 2026-10-10

### 节点产物发布与 Gateway 本地同步

- **触发现象**：节点中间产物只保留在沙箱 `/workspace/Projects/`，Gateway 和本地主机
  无法在任务运行期间检查；把整个工作区迁到 outputs 又会把日志、PID、checkpoint 和
  临时分块全部注册成 Files。
- **修复**：新增统一发布脚本。节点通过质量关卡后，按项目相对路径复制稳定文件到
  `/mnt/session/outputs/{PROJECT_DIR}/`，并以文件锁和原子替换维护 `manifest.json`。
  Gateway 按 Session 轮询 Files API，以 `file_id` 增量下载，再按 manifest 的大小和
  SHA-256 校验后重建本地可读目录。
- **用户可见性**：默认不额外发送飞书通知；Gateway 日志输出 `artifact synced`。
  本地默认路径为 `data/artifacts/<job_id>/<PROJECT_DIR>/`，可通过
  `TOPIC6_ARTIFACT_SYNC_DIR` 修改。
- **影响文件**：
  - `ma-resources/skills/topic6-annotation/scripts/publish_node_artifacts.py`
  - `ma-resources/agents/coordinator.system.md`
  - `arkagent/ark.py`
  - `artifact_sync.py`
  - `gateway.py`
  - `config.py`

### Gateway 持久化真实沙箱项目目录

- **触发现象**：Topic6 实际在 `/workspace/Projects/{PROJECT_DIR}/` 运行，但 Gateway
  创建 Job 时把 `/workspace/topic6-{mode}-{session}` 占位路径写入
  `pipeline_jobs.project_dir`，导致调试、HC 兜底产物路径和真实工作目录不一致。
- **根因**：业务目录名依赖 Coordinator 解析出的报告周期，Gateway 创建 Session 时尚
  不知道 `{PROJECT_DIR}`，却提前写入了一个不会被后续更新的假路径。
- **修复**：Job 初始目录保持未知；Coordinator 创建或恢复项目后立即输出
  `[project_dir] /workspace/Projects/{PROJECT_DIR}`，Gateway 校验该路径必须是
  `/workspace/Projects` 的直属子目录后原地回写。HC1-HC3 的 `project_dir` 同步统一为
  绝对路径。
- **边界**：运行工作区继续使用 `/workspace/Projects/{PROJECT_DIR}/`；仅节点完成后的
  稳定产物和最终交付物复制到 `/mnt/session/outputs/`，避免临时文件全部注册到 Files API。
- **影响文件**：
  - `pipeline_store.py`
  - `topic6_runner.py`
  - `ma-resources/agents/coordinator.system.md`
  - `ma-resources/memory/topic6/MEMORY.md`
  - `tests/test_topic6_hc_fallback.py`
  - `tests/test_topic6_new_command.py`

## 2026-10-09

### DataHub 默认模型切换为 gpt-4o-mini

- **变更**：Topic6 的 C0、C3、R1-R5 七路 DataHub 任务统一通过
  `DATAHUB_MODEL_ID=gpt-4o-mini` 提交。
- **边界**：C2 与洞察阶段的 Ark Chat 模型不在本次调整范围内，继续使用各自独立配置。
- **影响文件**：
  - `ma-resources/environment.json`
  - `ma-resources/agents/coordinator.system.md`
  - `ma-resources/agents/annotator.system.md`
  - `topic6_pipeline_overview.html`
  - `tests/test_topic6_pipeline_optimizations.py`

## 2026-10-01

### 网页报告周期抬头禁止回落到 W32 示例值

- **触发现象**：本次 W39 报告正文与妙搭应用标题均正确，但 HTML banner 显示
  `2026-W32 / 2026-08-03 至 2026-08-09`。
- **根因**：`pipeline_f.py` 产出半角格式
  `数据周期:2026-W39 (2026-09-21 ~ 2026-09-27)`，而
  `build-report.mjs` 只匹配全角冒号和括号；匹配失败后又静默使用模板示例
  `2026-W32` 及其日期。
- **修复**：Phase F 统一输出全角中文声明格式；网页构建器兼容历史全角/半角格式，
  同时删除 W32 周期兜底。周期无法解析时构建直接失败，禁止带错误抬头发布。
- **验证**：回归用例确认历史半角 W39 输入正确渲染为
  `2026·W39 / 2026-09-21 至 2026-09-27`，且缺失周期时构建失败、不生成 HTML；
  原妙搭应用 `app_17f4v8qapgx` 已原地发布 commit `2e2d54a`，线上 HTTP 200，
  W39 与日期范围均命中、W32 与旧日期均为 0 处。
- **影响文件**：
  - `ma-resources/skills/topic6-insight/scripts/pipeline_f.py`
  - `ma-resources/skills/topic6-web-report/assets/source/build-report.mjs`
  - `tests/test_topic6_pipeline_optimizations.py`

## 2026-09-30

### 妙搭新运行时域名兼容

- **触发现象**：Session `sesn-20260930113448-4kfn0` 的 Phase H 发布接口返回
  `https://bytedance.larkoffice.com/page/...`，该地址实测为 HTTP 404；改用
  `frontend` 应用发布后，API 返回
  `https://bytedance.feishuapp.cn/app/app_17f4v8qapgx/`，GET 返回 HTTP 200 且包含
  完整报告，但 Gateway 因只允许旧 `*.aiforce.cloud` 域名而把成功任务标为 stopped。
- **修复**：最终 URL 白名单增加妙搭当前 `*.feishuapp.cn/app/app_*` 运行时格式，
  同时继续拒绝 `*.larkoffice.com/page/*`、伪造后缀域名、query/fragment 和非应用路径。
  Coordinator 与 Phase H 契约改为接受发布 API 返回的两类运行时域名，并要求用 GET
  跟随重定向验证 HTTP 200 和报告正文。任务从 stopped 恢复及最终标记 done 时同步
  清理旧的 `finished_at` / `last_error`，避免成功记录残留历史失败原因。
- **验证**：当前发布页 GET 返回 HTTP 200，页面包含报告标题和完整正文；补充
  `feishuapp.cn` 正向用例、三类非运行时 URL 反向用例及终态字段清理用例。
- **影响文件**：
  - `pipeline_store.py`
  - `topic6_runner.py`
  - `ma-resources/agents/coordinator.system.md`
  - `ma-resources/skills/topic6-annotation/prompts/09_阶段_H妙搭发布.md`
  - `tests/test_topic6_new_command.py`

### HC1 后全量阶段的 artifact mode 显式固定

- **触发现象**：顶层运行模式为 `full` 的 Session 通过 HC1 后，筛选产物按既定契约
  写成 `usable_subset_skip_sampling_r2.xlsx`，但 Coordinator 启动 C2 时误传
  `--mode full`，转而查找不存在的 `usable_subset_full_r2.xlsx`。
- **修复**：Coordinator 在 HC1 后的全量 C→D 阶段固定
  `artifact_mode=skip_sampling`，并明确 C0/C3、筛选、R1~R5、C2 和合并脚本全部
  使用 `--mode skip_sampling`；顶层 `run_config.mode` 仍保持 `full`。
- **影响文件**：
  - `ma-resources/agents/coordinator.system.md`
  - `tests/test_topic6_pipeline_optimizations.py`

### DataHub 并发输入转换原子化

- **触发现象**：C0/C3 服务端任务 `5041/5042` 同时成功后，并发后处理都把同一份
  `hot_topics_normalized.xlsx` 转换为 `.hot_topics_normalized_prepared.xlsx`；
  C0 恰好在 C3 覆写过程中读取该文件，触发 `BadZipFile`。原始 3950 行输入未损坏。
- **修复**：每个 worker 先写带 PID 和纳秒时间戳的独立 `.tmp.xlsx`，完整写入后再
  用 `os.replace` 原子发布到共享 prepared 路径；异常时清理该 worker 自己的临时文件。
- **验证**：新增测试确认转换输出先写独立临时路径，再原子替换最终文件。
- **影响文件**：
  - `ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py`
  - `tests/test_topic6_pipeline_optimizations.py`

### DataHub 内联结果分页参数修正

- **触发现象**：同一 Session 的 C3 任务 `5042` 已进入
  `TASK_STATUS_SUCCESS`，但 DataHub 未提供 `result_url`，worker 改读
  `result_list` 时四次都拿到第一页 1000 行，无法组成预期的 3950 行结果。
- **根因与修复**：DataHub 查询接口的页码参数是 `page_num`；旧实现误传 `page`，
  服务端忽略后始终返回第一页。现保留内部 `page` 参数名，但请求时正确映射为
  `page_num`，并继续用 `page_size=1000` 分页下载。
- **验证**：新增 HTTP 参数级回归测试，明确第二页请求必须携带
  `{"page_num": 2, "page_size": 1000}`。
- **影响文件**：
  - `ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py`
  - `tests/test_topic6_pipeline_optimizations.py`

### DataHub 全量 worker 轮询上限延长

- **触发现象**：Session `sesn-20260930113448-4kfn0` 的全量 C3 服务端任务 `5042`
  持续处于 `TASK_STATUS_RUNNING` 且失败数为 0，但本地 worker 在约 7200 秒后因
  `POLL_TIMEOUT=7200` 主动退出；C0 同样可能在服务端完成前撞到该上限。
- **修复**：把 DataHub worker 的单任务轮询上限从 2 小时提升到 24 小时。Coordinator
  仍通过 `wait_datahub_batch.py` 做最长 105 秒的短检查，因此不会增加单次 MA 工具
  调用时长；worker 恢复继续绑定原 task id，不重复上传或创建计费任务。
- **验证**：增加回归断言锁定 24 小时上限；运行中 Session 已临时按同一上限恢复
  `5041/5042`，正式资源待本轮完成后部署。
- **影响文件**：
  - `ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py`
  - `tests/test_topic6_pipeline_optimizations.py`

### DataHub 任务查询瞬时故障重试

- **触发现象**：Session `sesn-20260930113448-4kfn0` 的全量 C0 服务端任务 `5041`
  仍为 `TASK_STATUS_RUNNING`，但 worker 在一次 `GET /api/v1/task/5041` 返回 HTTP
  503 后直接退出；批次检查器因此把健康的远端任务误报为失败。
- **修复**：`datahub_annotate.py` 查询任务状态时，对连接错误、HTTP 429 和 5xx 最多
  重试 5 次并指数退避；其他 4xx 与 DataHub 业务错误仍立即失败，避免掩盖确定性问题。
  worker 重启时还会校验已有 `submit_meta` 的 task、run、源输入、Prompt 和模型；全部
  一致才绑定原 task id 继续轮询与后处理，不重复上传或创建计费任务。新建任务的
  `submit_meta` 改为原子落盘，避免中断后读到半份 JSON。
- **验证**：覆盖“首次 503、第二次成功”和“恢复原 task id 时不上传、不建任务”；
  相关 DataHub worker 聚焦用例 `4 passed`，Python 编译与 `git diff --check` 通过。
- **影响文件**：
  - `ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py`
  - `tests/test_topic6_pipeline_optimizations.py`

### DataHub 长任务改为单 worker + 短时批次检查

- **触发现象**：Session `sesn-20260930111343-s4s9r` 中，C0 的 bash 已显式传
  `timeout=7200`，C3 也声明前台执行，但方舟仍在约 120 秒把两条命令转为后台任务。
  子 Agent 随后继续创建 `tail --pid`、`while sleep` 等等待命令；这些等待命令也会在
  120 秒后再次转后台，旧的“长时间前台等待”约束无法阻止后台槽累积。
- **根因**：`timeout` 是调用方期望值，不会覆盖当前 MA bash 工具约 120 秒的强制
  detach 阈值。让子 Agent 等待一个小时级 DataHub 任务，本身就与运行时边界冲突。
- **修复**：`datahub_annotate.py --launch-background` 幂等启动每路唯一 worker 后立即
  返回，Annotator 随即 `end_turn`，不再自行等待。Coordinator 使用
  `wait_datahub_batch.py` 统一检查 C0/C3 或 R1~R5；单次最多等待 105 秒，返回
  `running` 后原参数重调，且 bash 工具显式设置 `timeout=115`，因此不会产生后台
  等待进程。worker 完成时直接按真实 completion_meta 幂等记录成本。
- **影响文件**：
  - `ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py`
  - `ma-resources/skills/topic6-annotation/scripts/wait_datahub_batch.py`
  - `ma-resources/skills/topic6-annotation/tool/cost-tracker/cost_tracker.py`
  - `ma-resources/agents/annotator.system.md`
  - `ma-resources/agents/coordinator.system.md`
  - `ma-resources/skills/topic6-annotation/SKILL.md`
  - `tests/test_topic6_pipeline_optimizations.py`

### DataHub worker 幂等键覆盖输入文件

- **触发现象**：Session `sesn-20260930113448-4kfn0` 通过 HC1 后进入 3950 行全量
  C0/C3，但 worker 因发现同一 `task + run_id=1` 的 500 行样本 completion_meta，
  立即返回 `done`，险些复用样本结果。
- **根因与修复**：样本与全量在同一 pipeline 中允许共用 run_id，旧后台启动器却只用
  run_id 判断完成。现将源输入绝对路径写入 completion_meta，并要求幂等命中同时满足
  run_id 与输入文件一致；兼容旧 completion_meta 时会从
  `.<stem>_prepared.xlsx` 反推源文件再比较。
- **成本一致性**：DataHub worker 不再自动写成本；Coordinator 在批次完成后统一读取
  completion_meta，并使用 `datahub:{task}:r{N}:{mode}` record-id 幂等追加，避免恢复
  或人工检查导致双记。
- **运行中处置**：该 Session 已识别冲突并把全量轮次切到 run_id=2，未使用样本结果
  进入全量合并；正式修复在下次部署后生效。

### DataHub 长任务禁止后台轮询堆积

- **触发现象**：Session `sesn-20260930072249-tqjt9` 在 HC1 通过后的 3950 行全量
  C0/C3 阶段，把 `datahub_annotate.py` 放到后台后反复创建 `sleep`、`while ps`
  和嵌套监控任务。C0 在服务端任务运行约 59 分钟时占满沙箱 32 个后台任务槽，
  返回 `resource_exhausted`，随后子 Agent 因运行时问题终止。
- **根因**：Annotator 契约没有规定长命令的 bash 超时和等待方式。默认约 120 秒的
  工具等待会把较长的 `sleep` 自动转成后台任务；Agent 又持续创建新监控命令，
  最终后台任务数不断累积。DataHub 任务本身当时仍为 `TASK_STATUS_RUNNING`。
- **修复**：C0/C3/R1~R5 的 `datahub_annotate.py` 统一使用单次前台 bash 工具调用，
  显式设置 `timeout=7200`；禁止 `run_in_background=true`，禁止额外创建
  `sleep`、`while ps` 或其它后台轮询任务。工具调用自身失败时立即按失败契约回报，
  不再通过监控进程续命。
- **影响文件**：
  - `ma-resources/agents/annotator.system.md`
  - `ma-resources/agents/coordinator.system.md`
  - `tests/test_topic6_pipeline_optimizations.py`

### HC 结构化载荷与 C2 长响应稳定性

- **HC1 触发现象**：Session `sesn-20260930072249-tqjt9` 完成 500 行样本校验后，
  Agent 只输出“输出 HC1 结构化卡片：”便结束本轮，没有真正输出 JSON。Gateway
  因此触发 `__fallback__` 占位卡，卡片只显示模式与项目目录，缺少宽表路径和分布指标。
- **HC1 根因与修复**：Coordinator Prompt 同时要求“HC 消息第一个字符为 JSON”和
  “可先输出进度、再新起一条 message”，但 MA 运行时不保证同轮创建第二条 assistant
  message。现改为 HC1/HC2/HC3 轮只能输出一条完整 JSON，闭合后立即 `end_turn`；
  Gateway 兜底同时从已输出的核验摘要恢复行列数、C0/R1~R5 有效率和契约化宽表路径，
  未出现的指标不猜测。
- **C2 触发现象**：同一 Session 的完整 C2 在 `00_clean_titles` 调方舟
  `chat/completions` 时，多次于约 60 秒收到
  `Remote end closed connection without response`；短请求成功，长请求非流式失败，
  沙箱临时改成流式后恢复并完成 12 个阶段。
- **C2 根因与修复**：`topic6-event-registry/scripts/relay.py` 原先使用 urllib
  非流式等待完整响应，长推理期间没有响应字节而被中间层关闭。现正式改为 SSE 流式
  请求，启用 `stream_options.include_usage`，逐块聚合正文、usage 与 finish_reason，
  保留断连重试和 `max_tokens` 截断检查。
- **验证与生效方式**：完整测试集 `439 passed`，Python 编译与 `git diff --check`
  通过。改动只影响后续上传的 Skill/Agent 和重启后的 Gateway；当前运行 Session
  继续使用其创建时的资源快照。

### Demo 第二批恢复六路并发

- **触发现象**：`run_demo_routes.py` 虽使用线程池，但默认只有 2 个 worker，且按
  R1 的全部 chunk、R2 的全部 chunk依次提交，运行效果接近逐路推进，不符合
  R1~R5 与 C2 六路并发的流程定义。
- **调整**：改为 5 个 route worker 并发执行 R1~R5；每个 route 内仍按 5 条一批
  串行处理并逐批 checkpoint。Coordinator 同时启动 C2，因此共六路并发。
- **边界**：本次仅更新本地资源，不影响已创建的 Session；需后续执行
  `update_ma.sh` 并新建 Session 才会生效。

### 运行模式重命名

- **触发现象**：原 `full` 实际会跳过 500 条采样校准，名称容易被理解为客户完整流程；
  原 `test` 才是包含 HC1、全量重跑和 HC2 的完整模式。
- **调整**：三种模式统一为 `demo`、`full`、`skip_sampling`。`full` 对应原 `test`，
  `skip_sampling` 对应原 `full`；全量阶段产物使用
  `wide_table_skip_sampling_r{N}.xlsx`，避免与 `full` 的 500 条校准产物重名。
- **影响范围**：Gateway 触发词、Coordinator/子 Agent Prompt、C/D/E/F 脚本参数、
  HC 文案、流水线图和调试文档同步更新。

### 活跃任务用户 Token 保活

- **触发现象**：Session `sesn-20260930023459-r9zoi` 从 10:34 运行至 12:39；Phase G
  已完成，但 Phase H 调用 `lark-cli apps +list --as user` 返回
  `99991677 token_expired`，最终因没有妙搭 `online_url` 被 Gateway 标记为 stopped。
- **根因**：Gateway 原先只在创建 Session 前检查并刷新一次用户 Token。长任务超过约
  2 小时后，已挂用户 Vault 中的短期 access token 过期。
- **修复**：Gateway 为每个活跃 Job 启动保活协程，每分钟检查一次，覆盖 `running` 与
  `wait_hc`；在过期前 15 分钟使用 refresh token 原地更新同一个 Vault Credential。
  刷新失败会记录日志并继续重试，任务终态或 `/new` 取消后停止保活。
- **身份稳定性**：全程保持原 `session_id`、Vault ID 和 Credential ID，不动态追加
  Vault。方舟运行时周期性重新解析已挂 Vault 的凭据轮换。

### Phase E 流式有限并发与版块级恢复

- **触发现象**：Session `sesn-20260930023459-r9zoi` 中，旧 `pipeline_e.py` 使用非流式
  四路并发，E1~E4 同时出现 `Connection error`；用相同 Prompt 单独流式调用 E1 可成功。
- **调用优化**：E1~E4 改为默认最多 2 路并发的流式响应；断连、429 和 5xx 最多尝试
  5 次，指数退避且每次创建全新 `AsyncOpenAI` 客户端，避免复用失效连接。
- **可靠恢复**：每个成功版块立即原子写入 Markdown 和
  `pipeline_e_checkpoint_v{N}.json`。相同输入、模型、Prompt 和版本重跑时只补失败版块，
  已完成版块不重复调用或计费；显式 `--sections` 仍表示强制重跑指定版块。
- **编排约束**：Coordinator 只可原参数重跑正式入口一次，禁止 inline Python、拆成
  四次调用、改写 Prompt、切模型、提高并发或删除 checkpoint。
- **生效方式**：执行 `update_ma.sh` 更新 Skill 和 Coordinator，重启 Gateway 后创建
  新 Session；已有 Session 不会自动获得更新后的资源快照。

### Demo R1~R5 小批次恢复与失败收口

- **触发现象**：Session `sesn-20260930023459-r9zoi` 中，R1~R5 直接 Ark 批量请求出现
  `Remote end closed connection without response`。旧入口五路各发一个长请求，且要
  等五路全部成功后才统一落盘；任一路失败会丢失其余已成功结果。
- **执行优化**：`run_demo_routes.py` 保持原 R1~R5 Prompt 和输出契约，改为默认每批
  5 行、最多 2 请求并发。断连、超时、429 和 5xx 最多重试 5 次，使用指数退避与抖动。
- **传输层修正**：Session `sesn-20260930045929-siwtz` 证明仅创建新的
  `urllib.Request` 不会隔离底层失效连接，R1 第二个 chunk 起仍持续断连。正式入口改用
  `httpx`，每次 attempt 都创建并关闭独立 Client，禁用 keep-alive 与 HTTP/2；
  Environment 显式预装 `httpx>=0.27`。
- **可靠恢复**：每个成功 chunk 立即原子写 JSON checkpoint；每个 route 完成后立即写
  raw/postprocess/completion_meta、run_config 和成本，不再等待其他 route。重跑按输入、
  模型、Prompt、run_id 和 batch size 只补缺失 chunk，已完成 route 直接复用。
- **编排约束**：Coordinator 失败后只可按原参数重跑正式入口一次，禁止 inline Python、
  改写 Prompt、切模型、修改 batch size 或删除 checkpoint 来绕过问题。

### 妙搭用户 OAuth 预授权与每用户 Vault

- **根因**：MA Session 创建后不能追加 Vault；Gateway 主机上的 lark-cli 登录态也不会
  自动进入方舟沙箱。仅在 Agent 内提示 `auth login` 会让 Phase H 等到流水线末尾才失败。
- **授权入口**：新增 `authorize_miaoda_user.py`。Device Flow 完成后通过用户信息接口
  自动取得扫码者 `open_id`，不要求手工传 ID；可选 `--expected-open-id` 仅用于防止
  扫错账号，校验失败时不写凭据。
- **凭据边界**：短期 `LARKSUITE_CLI_USER_ACCESS_TOKEN` 写入每用户独立 Ark Vault；
  refresh token 只保存在权限为 `0600` 的 Topic6 SQLite 中，不进入 MA 沙箱或日志。
- **运行时接入**：Gateway 在创建任务前按消息发送者查授权并刷新短 token，再把用户
  Vault 传给新 Session。未授权或授权失效时直接拒绝启动并提示运行授权脚本。
- **权限事实**：授权范围限定为 `offline_access`、`auth:user.id:read`、
  `spark:app:read`、`spark:app:write`，覆盖身份识别、续期和妙搭发布。

## 2026-09-29

### Phase H 终态校验与报告链接修复

- **轨迹证据**：Session `sesn-20260929132115-ol7cr` 共 412 个事件。Phase G 的 HTML
  snapshot 上传因缺少 API Key 失败，Agent 将 `status.h.completed=false` 写入配置后仍
  宣称流程完成；Gateway 又从自由文本中抓取首个飞书文档 URL，并连同 JSON 尾部的
  `",` 标点写入卡片，飞书打开时进一步附加一次性登录参数。
- **终态收紧**：Gateway 不再抓取任意 URL，只接受 Phase H 结构化结果中的
  `online_url`，并校验为 HTTPS `*.aiforce.cloud`；否则任务进入 `stopped`，不显示
  “打开报告”完成按钮。
- **编排收口**：Phase G 只负责构建、校验和落盘 HTML，禁止把 snapshot 上传视为妙搭
  发布。补充迁入客户后续提供的 `miaoda-web-publish` v1.0.1，作为第 6 个 Coordinator
  Skill；按其用户 OAuth + Git 管理发布契约执行，不引入 `MIAODA_TOKEN`。OAuth 未完成
  或发布失败时不得宣称完成。
- **验证**：完整测试集 `423 passed`；6 个 Skill 已上传并重建 Coordinator。

### Demo 第二批与洞察阶段提速

- **轨迹证据**：线上 Session `sesn-20260929115416-cgepu` 共 315 个事件。
  R1~R5 只处理 14 行仍各耗时 10.0~11.6 分钟；完整 C2 与其并发但耗时约
  12.6 分钟，成为 Phase C 第二批关键路径。E1~E4 子线程耗时 2.4~5.3 分钟，
  且每个子线程都重复调用一次 `pipeline_e.py` 和全量统计预处理。
- **R1~R5 demo 快速入口**：新增 `run_demo_routes.py`，复用正式 R1~R5 Prompt，
  将每路 14 次逐行 DataHub 推理改为每路一次 Ark 批量推理，五路并发并产出原有
  raw/postprocess/completion_meta 文件与 run_config 状态。test/full 继续走 DataHub。
- **C2 demo 快速入口**：`run_topic6_c2.py --demo-fast` 用一次批量模型调用完成小样本
  事件归并，直接产出原契约 `row_id + 一级事件名`；完整 00→x4 链仍用于 test/full。
- **Phase E 去重**：整轮改为只调用一次 `pipeline_e.py`，复用其内部 E1~E4 并发，
  避免四次 `run_stats.py`、四个子 Agent 冷启动和重复自检。
- **附带修复**：修正 C2 四平台并发时 `_run_command` 参数重复传递，并统一 Coordinator
  读取真实状态路径 `C2_事件归档/c2_run/c2_status.json`。

### 单任务单卡片与 Phase F 发布契约修复

- **卡片合并**：进度与 HC1/HC2/HC3 审核态复用同一条飞书消息；HC 到来时 patch
  主卡，回调处理后继续把该卡更新为运行或终态。按 job 串行 card patch，避免 SSE
  进度与审核回调相互覆盖；主卡初始化失败时才降级补发一张。审核通过或提交备注后
  会在启动下一段 SSE 前立即恢复运行卡和已有工具进度，不等待下一条 progress 事件。
- **卡片协议统一**：运行卡此前使用 schema v1，审核卡使用 schema v2；进入审核态后
  再恢复运行卡会被飞书以 `230099 / schemaV2 card can not change schemaV1` 拒绝。
  现已将运行、等待审核和最终状态全部统一为 schema v2，保证同一消息可双向切换状态。
- **审核记录保留**：HC 回调除审核结果、备注和时间外，新增持久化审核人显示名；恢复
  运行后在独立“审核记录”区展示 `HC1 · ✅ 通过 · 审核人 · 时间`，后续 HC2/HC3
  按发生顺序追加，不占用最近工具调用行数。旧记录因历史上未保存审核人会明确显示
  “审核人未记录”，不做不可靠推断。
- **Phase F 目录与身份**：统一由应用身份动态创建报告目录，不再依赖
  `FEISHU_HOTREPORT_FOLDER_TOKEN`；操作人改读 Gateway 注入的
  `FEISHU_USER_OPEN_ID`，沙箱不再依赖 `CC_SESSION_KEY` 自行发送重复通知。
- **URL 与结果判断**：文档 URL 直接读取 `drive +import` 响应，不再硬编码租户域名；
  同时强制检查 JSON `ok == true`，避免把“退出码为 0 的业务失败”误判为成功。
- **权限与凭证安全**：补充 `docs:document.media:upload`、
  `docs:document:import` 权限要求；Coordinator 禁止枚举环境或打印 Secret、Token、
  API Key，避免敏感值进入 Session 轨迹。
- **文档可访问性**：补充 `docs:permission.member:create`、
  `docs:permission.member:transfer`、`docs:permission.member:retrieve`。Phase F 只允许
  Bot 身份发布，禁止缺 scope 时降级 Device Flow；导入后先给任务发起人 `edit`，再转移
  owner，并逐项校验授权 JSON，任一步失败均不得进入 HC3。
- **目录响应解析**：`drive +create-folder` 的 token 位于 `data.folder_token`；旧 Prompt
  误读 `data.token`，导致目录创建成功后仍被判空并触发临场 OAuth 降级，现已修正。
- **验证**：Topic6 / Gateway 完整测试集 `413 passed`。

### C2 单入口、并发状态与 C0 成本优化

- **触发现象**：最新轨迹中 C2 占 71 次模型请求里的 48 次、产生 47 次 bash 调度，
  且运行二十多分钟仍未完成；原文档还把 01~04 放在 x0 前按平台执行，与
  `x0_merge_platforms.py` 实际只合并 `clean_titles.jsonl` 的契约冲突。
- **C2 拓扑与入口**：新增 `run_topic6_c2.py`，固定为四平台并行 00 → x0 →
  merged 目录统一执行 01~07 → x2 → x3 → x4，并直接产出
  `c2_event_result_r{N}.xlsx`。Coordinator 只启动一次后台入口，不再临场编排十多个命令。
- **可靠恢复**：入口用 `c2_status.json` 记录阶段；输入、Chat 模型、Embedding 模型和
  x2 策略组成运行签名。签名不变时续跑，变化时清理旧阶段产物重跑；项目级进程锁阻止
  重复 runner 并发写缓存。
- **真正并发**：R1~R5 五个委派发出后立即后台启动 C2，六路同时运行；不再等待五路
  DataHub 任务结束后才开始 C2。
- **模型隔离**：Environment 分设 `DATAHUB_MODEL_ID=Doubao-Seed-1.6-lite`、
  `C2_CHAT_MODEL_ID=doubao-seed-evolving` 和
  `EMBEDDING_MODEL_ID=doubao-embedding-vision-251215`，避免跨 API 混用模型 ID。
- **状态一致性**：新增 `run_config_state.py`，通过文件锁、YAML 深合并和
  `os.replace` 原子更新并发任务状态；DataHub、C0 合并/筛选、C2 和宽表合并均写入成功
  或失败状态，不再由多个 Agent 直接覆盖 `run_config.yaml`。
- **C0 Prompt**：活跃版升级为 v5，在保留 9 字段输出契约和关键边界的前提下，从
  44,649 字符压缩到 11,263 字符（减少 74.8%）。该项只完成静态契约校验，仍需用真实
  DataHub 结果在 HC1 与 v4 对比后确认质量。
- **验证**：Topic6 聚焦测试 `20 passed`，加入 Gateway 全局单任务测试后仓库全量
  `409 passed`。

### HC3 状态一致性与轨迹问题收口

- **Gateway 全局单任务**：此前只按 `(chat_id, thread_id, user_open_id)` 防止同一用户重复
  触发，不同用户仍会并行创建 Session。现在整个 Topic6 Gateway 只允许一个
  `running` / `wait_hc` Job；新触发在创建方舟 Session 前即被拒绝，并提示等待前一任务
  结束。启动检查使用异步锁串行化，避免同时到达的消息穿透检查。
- **HC3 卡片错显 HC1**：Gateway 先把数据库阶段更新为 HC3，但发送审核卡时复用了更新前的 Job 快照。审核卡现直接以 `hc_kind` 渲染阶段，Runner 在写库后也会重新读取 Job。
- **备注补充消息未续跑**：点击“备注”后再 @bot 的正文此前会落入默认帮助回复。Gateway 现优先识别等待补充说明的 HC，将正文作为 `HCx remark` 注入原 Session 并继续执行。
- **备注交互改为卡片内完成**：HC 卡片增加必填多行备注输入框和“提交备注并继续”按钮，表单提交后直接携带 `form_value.remark_note` 续跑，不再要求用户二次 @bot；文本补充入口仍作为兼容兜底保留。
- **群聊 `/new` 误触发 test**：入站文本保留了 `@热点周报助手`，导致机器人名称中的“热点周报”命中触发词。Gateway 现先剥离开头的机器人 mention，再解析 `/new` 和运行模式。
- **后台停止误显示完成**：方舟人工停止会发送 `user.interrupt`，随后仍发送 `session.status_idle(end_turn)`；旧逻辑忽略 interrupt 并把 idle 当完成。Gateway 现将其落为 `stopped` 并显示“已停止”；无 HC 且无最终发布 URL 的提前结束也不再标记完成。
- **无效 HC3 拦截**：`feishu_doc_url` 为空说明 Phase F 未发布成功，Gateway 现在将其标记为失败，不再生成可误点“通过”的 HC3 卡片；Coordinator 同步禁止用本地 Markdown 路径替代飞书文档。
- **飞书权限说明**：创建报告目录使用应用身份权限 `space:folder:create`（“创建云空间文件夹”）；旧文档中的 `drive:drive` 表述已移除，明确不得误选 `drive:drive:version`。
- **Phase E 依赖**：Environment 增加 `openai>=1.0`，避免沙箱运行时临时安装。
- **E2 日历**：默认改为直接读取 `topic6-fetch-normalize/references/marketing_calendar_2026.csv`，并保留 Markdown 日历兼容。
- **报告周期**：`pipeline_f.py` 避免在 `period_label` 已含日期时重复拼接日期范围。
- **验证**：Topic6 完整测试集 `397 passed`。

### C2 向量模型切换为 Doubao-embedding-vision

- **触发现象**：旧默认模型 `Doubao-embedding` 调用标准 `/api/v3/embeddings` 时返回 `InvalidEndpointOrModel.NotFound`。
- **依据**：方舟模型详情页当前首推准确模型 ID `doubao-embedding-vision-251215`；文本输入使用 `POST /api/v3/embeddings/multimodal`。
- **实现**：
  - `04_build_embeddings.py` 默认模型切换为 `doubao-embedding-vision-251215`，支持 `EMBEDDING_MODEL_ID` 和 `--model` 覆盖。
  - vision 接口每条文本单独请求，使用 `--concurrency` 并发；兼容响应 `data` 为对象或列表的两种结构。
  - 显式指定标准 embedding 模型时继续使用 `/embeddings` 批量协议，保留兼容性。
  - Coordinator 和 Event Registry 操作文档同步准确模型 ID、接口及故障排查口径。
- **验证**：Topic6 完整测试集 `392 passed`。

### MA 资源全量更新单入口

- 新增 `update_ma.sh`，统一完成配置加载与校验、全量测试、5 个 Skill 打包和强制上传、Environment/Memory 更新、3 个 Agent 重建、资源 ID 校验与 Gateway 配置回写。
- 脚本不管理 Gateway 进程；执行完成后仍需手动重启 Gateway，使新 Agent ID 和本地代码生效。

### demo 抽样量降至 50 条

- **触发现象**：demo 使用 500 条样本时，C0/C3 仍受 DataHub 吞吐限制，端到端演示等待时间过长。
- **实现**：demo 调用 `sample_500.py --size 50`，test 继续使用 `--size 500`；兼容既有合并与断点恢复逻辑，产物文件名仍为 `sample_500.xlsx`，实际行数以 `status.sample.sample_rows` 为准。
- **口径**：50 条仅用于流程演示，不用于标注质量或正式业务结论；报告和 HC1 卡片同步标明 demo 样本量。

### DataHub 默认模型切换为 Doubao-Seed-1.6-lite

- **依据**：方舟 Environment 实际调用 `/api/v1/model/list` 返回 113 个模型，确认精确 ID `Doubao-Seed-1.6-lite` 可用。
- **实现**：Coordinator 与 Annotator 的默认 `--model-id` 从 `Doubao-pro-32k` 切换为 `Doubao-Seed-1.6-lite`；仍以接口返回列表做运行前精确校验，并以 completion metadata 的实际模型和成本记账。

### DataHub 模型 ID 预检与单次纠错重试

- **触发现象**：C3 使用 `doubao-pro-32k` 创建任务时，DataHub 因模型 ID 大小写敏感返回 `invalid model_id`；子 Agent 受“出错立即结束”约束，没有用查询到的 `Doubao-pro-32k` 重试。
- **根因**：Coordinator/Annotator 契约固化了错误大小写，且模型列表查询发生在输入文件上传和任务创建之后。
- **实现**：
  - 模型 ID 统一修正为 `Doubao-pro-32k`。
  - `datahub_annotate.py` 在上传前调用 `/api/v1/model/list`，打印模型数量和完整 ID 列表，并做大小写敏感的精确校验；无效 ID 会提示唯一的大小写候选，不再产生无用 Data Source。
  - Annotator 遇到 `invalid model_id` 时允许按模型列表候选修正参数并最多重试一次；其他错误仍立即回报。
- **本地验证限制**：本机直调模型列表返回 `ip not allowed`，需在已加入 DataHub IP 白名单的方舟 Environment 中观察真实列表。

### Phase C 性能与稳定性优化，新增 500 条演示模式

- **触发现象**：一次 test 轨迹中，C0/C3 虽于 `10:35:24` 并发启动，但 500 条数据分别耗时约 41/42 分钟；C0 使用约 572 万 tokens，C3 使用约 258 万 tokens。两路 DataHub 任务成功后均未返回 `result_url`，导致统一脚本报错，子 Agent 被迫手工从 `result_list` 分页恢复结果。
- **根因**：
  - DataHub 单任务吞吐约 12 行/分钟，是本轮墙钟时间的主要瓶颈；C0/C3 Prompt 较长则进一步放大 token 成本。
  - DataHub 成功响应存在两种结果形态：下载链接 `result_url`，或内联/分页 `result_list`；原脚本只支持前者。
  - 子 Agent 契约包含不存在的 `--output`、字符串 `--run-id` 和错误的成本命令，造成启动前纠错与手工兜底。
  - C0 解析失败行此前仍会进入 R1~R5，一条失败最多放大为五路无效调用。
- **实现**：
  - `datahub_annotate.py` 在 `result_url` 缺失时自动解析并分页拉取 `result_list`，兼容嵌套输入字段和结果字段别名，严格校验最终行数及 `llm_result`。
  - C0 筛选改为只有明确判定“是否营销可用=是”的记录进入 R1~R5；解析失败/缺失占比超过 5% 仍熔断。
  - 修正 Annotator/Coordinator 契约：Prompt 由脚本直接读取，不再先灌入 Agent 上下文；`run_id` 使用整数；成本按 completion metadata 的实际模型、平台、token 和 `total_consume` 记录。
  - Environment 增加 `lunardate>=0.2.2`，避免 C3 后处理临时安装依赖。
- **demo 模式**：
  - 新增触发词 `热点周报 demo`，固定使用 500 条分层样本。
  - 流程为 `A/B → 抽样 → C/D → HC1 → E/F → HC3 → G/H`；HC1 通过后明确跳过全量 C/D 和 HC2。
  - demo 使用独立的 `wide_table_demo_r{N}.xlsx`，不伪装成 full；最终报告自动标注“基于 500 条分层样本，仅供流程演示，不可作为正式全量结论”。
  - 原 `test` 保持“样本校准后继续全量”的语义，`full` 保持直接跑全量。
- **未默认启用**：Prompt 大幅裁剪、模型切换和 DataHub 多分片并发仍需先做质量、限流与重复计费基准，避免以未经验证的方式影响生产结果。
- **验证与发布**：完整测试集 `382 passed`；5 个 Skill 打包校验通过；已更新 Environment 并重建 Annotator、Insighter、Coordinator。
- **影响文件**：
  - [topic6_runner.py](./topic6_runner.py)
  - [agents/coordinator.system.md](./ma-resources/agents/coordinator.system.md)
  - [agents/annotator.system.md](./ma-resources/agents/annotator.system.md)
  - [agents/insighter.system.md](./ma-resources/agents/insighter.system.md)
  - [datahub_annotate.py](./ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py)
  - [c0_filter_usable.py](./ma-resources/skills/topic6-annotation/scripts/c0_filter_usable.py)
  - [pipeline_e.py](./ma-resources/skills/topic6-insight/scripts/pipeline_e.py)
  - [pipeline_f.py](./ma-resources/skills/topic6-insight/scripts/pipeline_f.py)
  - [environment.json](./ma-resources/environment.json)

### 对齐 2026-09-29 Managed Agents 文档更新

- **MCP Toolset**：`mcp_toolset` 必须作为 `tools[]` 条目，并通过 `mcp_server_name` 与 `mcp_servers[]` 一一对应；权限策略形状为 `default_config.permission_policy.type`。已修正 `agents/coordinator.json`，继续对只读热点数据 MCP 显式使用 `always_allow`。
- **SSE 启动顺序**：打开事件流后必须等到 `: ready`，再发送首个 `user.message`。已由共享 `_EventStream` 在进入上下文前消费 ready 信号，Topic 6 runner 无需自行解析 SSE 注释。
- **Session 终态**：`session.status_terminated` 是不可继续发送事件的终态，已按失败处理；正常轮次完成仍以 `session.status_idle` 为准。
- **Skill 上传限制**：上传 ZIP 不超过 30 MiB；解压后单文件不超过 30 MiB、总大小不超过 120 MiB、最多 500 个文件；统一顶层目录下直接包含唯一 `SKILL.md`。`tools/pack_skills.sh` 已同步全部门禁。
- **Memory 更新**：创建同路径 Memory 不会覆盖原内容。`create_all.sh --update-memory` 现在先按 path 查找 Memory ID，存在则调用更新接口，不存在才创建。
- **无需修改**：Topic 6 仅挂载 5 个 Skills，未触及单 Agent 50 个上限；Memory 继续使用 `read_only`，不启用本次新增明确化的 `read_write` 能力；Multi Agent 仍是一层协调器到子 Agent，符合嵌套限制。

## 2026-09-24

### Agent Prompt 静态路径与 skill 挂载目录逐字符对齐

- **现象**:coordinator/insighter prompt 里 8 处 `/mnt/skills/...` 死链,导致 Coordinator 首次运行读契约文件时报 `not_found`。具体包括 `topic6-annotation/prompt/`(单复数错)、`/mnt/memory/topic6/xxx.md` 占位举例、`topic6-event-registry/scripts/00_run_all.sh` 不存在、`marketing_calendar_2026.csv` 错误 skill 前缀、`topic6-web-report/scripts/build-report.mjs` 不存在、`topic6-insight/ks/07_报告结构.md` 错误 skill 前缀等。
- **根因**:方舟沙箱把 skill 包挂载在 `/mnt/skills/{skill_key}/`,是**包内目录的直投射**——不做路径重写、不做别名、不容错。prompt 里所有静态路径必须逐字符对应 `ma-resources/skills/{skill_key}/` 下的真实布局。
- **影响文件**:
  - [agents/coordinator.system.md](./ma-resources/agents/coordinator.system.md)
  - [agents/insighter.system.md](./ma-resources/agents/insighter.system.md)
  - [skills/topic6-annotation/prompts/](./ma-resources/skills/topic6-annotation/prompts)(补齐 11 个 prompt md,含 `run_config契约.md` / `00_角色与触发.md` / `01_pipeline总览.md` 等)
  - [skills/topic6-fetch-normalize/references/marketing_calendar_2026.csv](./ma-resources/skills/topic6-fetch-normalize/references/marketing_calendar_2026.csv)(补齐)
- **应对规约**:后续任何 Agent Prompt 修改,必须跑一次静态校验(见 `/tmp/check_topic6_paths.py`)作为准入 gate;新 skill 上线时同步核对 SKILL.md 里的目录索引与磁盘实际结构。

## 2026-09-24

### SKILL.md 必须带 YAML frontmatter,且 `name` 匹配 `^[a-z0-9-]{1,64}$`

- **现象**:`POST /api/v3/skills` 返回 `400 InvalidParameter`,body 里明确报 `SKILL.md frontmatter name must match ^[a-z0-9-]{1,64}$ (got "topic6-fetch-normalize · v1")`。
- **根因**:方舟 CreateSkill 会强制解析 `SKILL.md` 的 YAML frontmatter,`name` 是 skill 的稳定标识,只允许小写字母/数字/连字符,长度 1~64。若 frontmatter 缺失,则退化到用 H1 标题当 name——H1 含空格、中文、`·` 就直接 400。
- **影响文件**:
  - [topic6-fetch-normalize/SKILL.md](./ma-resources/skills/topic6-fetch-normalize/SKILL.md)
  - [topic6-annotation/SKILL.md](./ma-resources/skills/topic6-annotation/SKILL.md)
  - [topic6-insight/SKILL.md](./ma-resources/skills/topic6-insight/SKILL.md)
- **参考**:[火山方舟 MA 文档](../../../../common/docs/火山方舟_ManagedAgents_docs.md#L1451-L1470)("按以下约束组织自定义 Skills")、event-registry / web-report 两个已经带 frontmatter 的 SKILL.md 做参照。
- **应对规约**:后续新增 skill 必须先写 frontmatter(`name` / `version` / `description`),再落 Markdown 正文。命名统一走 `topic6-<kebab-case>`。

### CreateSkill 必须带 `X-Ark-Beta: agentic-2026-06-01` header

- **现象**:`POST /api/v3/skills` 返回 `404 Not Found`,body 为空(不是 401/403,方舟直接当路径不存在)。同样规律也命中 `/api/v3/environments` `/api/v3/memory_stores` `/api/v3/agents` `/api/v3/sessions`——只要不带 beta header,curl 一律 404。
- **根因**:整个 Managed Agents 面(environments / memory_stores / agents / sessions / skills)都属方舟 **agentic beta 面**,和普通 v3 API(chat/embedding 等)不共用路由。beta 面要求请求头显式声明版本 `X-Ark-Beta: agentic-2026-06-01`,不带就路由不到,直接 404。
- **影响文件**:
  - [tools/upload_skills.py](./tools/upload_skills.py)
  - [ma-resources/create_all.sh](./ma-resources/create_all.sh)(所有 curl 统一走 `ark_post` 封装,顺带解决 response 有时包 `.data` 壳的问题——用 `.data.id // .id` 兼容)
- **参考**:同仓 [ark_min.py](../../../ma-replica/skills/ma-replica-builder/scripts/ark_min.py#L29-L30) 已验证过的写法、[MA 文档 L1477](../../../../common/docs/火山方舟_ManagedAgents_docs.md#L1477)。
- **应对规约**:所有直接打 `/api/v3/*` (MA 面) 的脚本必须带 beta header。走火山官方 SDK 的话 SDK 会自动补,不用管。

### CreateEnvironment 请求体必须嵌套在 `config` 下,且字段名固定为 `packages.pip` / `env` / `tos`

- **现象**:`POST /api/v3/environments` 返回 `400`。
- **根因**:方舟 Environment 的 API 契约把所有沙箱配置塞在 `config` 对象里,顶层只放 `name` / `description`。若把 `type` / `networking` / `pip_packages` / `env_vars` 直接平铺到顶层,或用 `pip_packages` / `env_vars` 这类自造字段名,后端解析不到必需字段,返回 InvalidParameter。方舟对未知字段的容忍度比想象中低——不认识的字段直接 400,不会 silently ignore。
- **影响文件**:[ma-resources/environment.json](./ma-resources/environment.json) 整体重构。
- **正确形状**(节选,详见 [MA 文档 L2430-L2490](../../../../common/docs/火山方舟_ManagedAgents_docs.md#L2430)):
  ```json
  {
    "name": "...",
    "description": "...",
    "config": {
      "type": "cloud",
      "networking": { "type": "unrestricted" },
      "packages": { "pip": [...], "apt": [...] },
      "env": { "KEY": "VALUE" },
      "tos": { "bucket": "...", "prefix": "..." }
    }
  }
  ```
- **额外副作用**:方舟对 `config.env` 里的 `${VAR}` 字面串**不做二次插值**,不预处理就会把 `"${DATAHUB_ENDPOINT}"` 死字符串灌进沙箱。故 [create_all.sh](./ma-resources/create_all.sh) 在 POST 前用 `envsubst` 展开一次。
- **应对规约**:后续任何 environment.json 改动,`config` 之外只加 `name`/`description`;未知字段(如自造的 `_output_storage_disabled`)禁止入库,注释走 markdown/changelog。

### Memory `path` 必须以 `/` 开头

- **现象**:`POST /api/v3/memory_stores/{id}/memories` 返回 `400 InvalidParameter: path must start with /`。
- **根因**:方舟 Memory Store 的 path 用绝对路径语义(会被沙箱只读挂载到 `/mnt/memory/{path}`),入参必须以 `/` 开头。写成相对路径(如 `topic6/MEMORY.md`)后端不做归一化,直接 400。
- **影响文件**:
  - [ma-resources/memory-store.json](./ma-resources/memory-store.json)(3 条 path 全部补 `/` 前缀)
  - [ma-resources/create_all.sh](./ma-resources/create_all.sh)(POST 前兜底补 `/`,防未来漏改)
- **应对规约**:后续任何 memory-store.json 新增条目,path 必须写 `/topic6/...`。Agent prompt 里引用时,挂载点是 `/mnt/memory` + path,即 `/mnt/memory/topic6/MEMORY.md`,与旧口径完全一致。

### 内置工具必须通过 `agent_toolset_20260701` 配置

- **现象**:`POST /api/v3/agents` 返回 `400 InvalidParameter: tools[0].type: unsupported tool type "bash"`。
- **根因**:方舟 MA 把 `bash` / `read` / `write` / `edit` / `glob` / `grep` / `web_fetch` / `web_search` **聚合成一个内置工具集**,type 只写一个 `agent_toolset_20260701` 就默认全开(见 [MA 文档 L1881-L1898](../../../../common/docs/火山方舟_ManagedAgents_docs.md#L1881))。除内置工具集外，`tools[]` 还可包含 `custom`、`evolution` 和 `mcp_toolset`；不能把 `bash`、`read` 等单个内置工具名直接写成 type。
- **影响文件**:
  - [agents/annotator.json](./ma-resources/agents/annotator.json)
  - [agents/insighter.json](./ma-resources/agents/insighter.json)
  - [agents/coordinator.json](./ma-resources/agents/coordinator.json)
  - 全部改为 `[{"type": "agent_toolset_20260701"}]`
- **应对规约**:后续新增 Agent 定义，内置工具统一使用 `agent_toolset_20260701`；通过 `configs[].enabled` 控制单个工具启停，通过 `default_config.permission_policy` 或 `configs[].permission_policy` 控制执行前是否确认。

### Agent `mcp_servers[].type` 必填,当前仅支持 `"url"`

- **现象**:`POST /api/v3/agents` 返回 `400 InvalidParameter: mcp_servers[0].type: must be "url" (got "")`。
- **根因**:方舟 MCP server 定义走 discriminated union,`type` 是分派字段——即便当前实现只有 URL 一种,也必须显式声明(未来可能引入 `stdio`/`sse`/`streamable_http` 等子类)。省略等价于类型未定,后端一律拒绝。
- **影响文件**:[agents/coordinator.json](./ma-resources/agents/coordinator.json) `mcp_servers[0]` 补 `"type": "url"`。
- **应对规约**:后续任何 Agent 定义,`mcp_servers[]` 每条必须至少含 `type` / `name` / `url` 三字段,不留隐式默认。
