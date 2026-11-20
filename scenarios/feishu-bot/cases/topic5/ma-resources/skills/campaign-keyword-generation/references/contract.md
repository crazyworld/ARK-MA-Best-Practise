# 输入输出与执行路由

本Skill采用Topic5关键词能力v1.11约定。下列平台清单是项目当前已知清单，统计和抓取的实际支持须分别按运行环境依赖核对；不把统计支持视为抓取支持。

## 正式数据执行路由

Supervisor生成并交接两套口径；需求与方案阶段完成后，由Producer按以下路由执行：

| 数据用途 | 关键词与过滤词 | 执行Skill | 主要结果 |
|---|---|---|---|
| 品牌传播查询（活动期及品牌基准期） | brand_keyword + brand_filter | SSJH-Social-Stat | 品牌声量、互动、情感分布、趋势、词云、热帖等查询结果；brand_stats.json及基准期结果 |
| 活动原帖抓取（逐活动） | activity_keywords + activity_filter | ssjh-social-task | 原始records.json、导出XLSX、质量报告及任务恢复信息；字段适配后交清洗、去噪、标注和分析 |

活动取数配置字段明确映射：`profiler.name=campaign_name`、`profiler.keywords=activity_keywords`、`profiler.filterwords=activity_filter`；日期映射至`search.start_date/end_date`，`search.keyword_fields=content,title`关闭OCR匹配。平台按取数Skill的实际source/site映射，不能把统计接口的平台支持直接视为取数支持；内容范围按实际能力核对，PGC/UGC不能映射成主帖/评论类型。

关键词生成阶段可使用SSJH-Social-Stat做有限词云/热帖探索，正式执行阶段可保留现有活动词云等辅助查询；这些查询均不代替ssjh-social-task的活动原帖抓取，不把Top热帖或聚合结果当作完整活动数据。活动原帖指标按后续实际数据处理与统计规范计算。

确认单分别保存两条路由、实际关键词/过滤词、日期、平台、内容范围及验证状态。口径ready不表示两个Skill均已执行成功。ssjh-social-task的依赖、数量、预算、有效采集授权及任务恢复门禁在正式执行时按其Skill和Producer接入约定落实；Supervisor生成关键词不创建正式抓取任务。查询结果不能冒充抓取成功，抓取失败如实交接执行状态。

## 调用输入

初始原话即可调用。以下业务字段是执行取数前必须明确的信息，不是调用Skill的前置参数要求；未提供时可省略或留空，日期可只提供已知部分。Supervisor负责传入原话、会话日期/时区及已有状态，由Skill理解需求、判断缺项并提问。

```yaml
capability: campaign_keyword_generation
request:
  user_description: 用户原话及后续更正
  analysis_scope: brand_and_activity  # 固定兼容值，不作需求分流
  brand: 已明确的品牌，可为同一活动的多个合作品牌
  campaign_name: 已知活动名称或自然描述，可为空；ready前须足以识别本次活动，不要求官方全称
  campaign_period: {start: YYYY-MM-DD或空, end: YYYY-MM-DD或空}  # 初次调用可为空或部分日期
  platforms: [用户原话中的平台，可为空；Skill校验支持状态，不能静默丢弃不支持平台]
  business_question: 用户希望回答的问题，可为空
  content_scope: ALL
  known_context: 已确认的活动类型、人物、产品、地点、话题、范围约束等
  existing_keywords: 已有的品牌/活动表达式及确认状态，可为空
  existing_filters: 已有过滤词、依据和导出排除状态，可为空
  conversation_context: {current_date: 会话日期, timezone: 用户时区}  # 用于日期候选，不替代用户确认
  delivery_requirements: 已知交付截止、形式和限制条件，可为空
  prior_result: 上次调用结果，可为空，用于增量更新
```

- Topic5仅接收营销活动复盘需求。Supervisor从活动描述识别品牌、活动主体与边界，必须生成品牌取数关键词和活动取数关键词，并分别判断两套过滤词；过滤词无适用规则或核验依据时可为空。品牌传播与活动传播是同一活动复盘中的两个分析层次，不作为用户需求类型分流。
- `analysis_scope`仅为兼容旧调用方固定记录为`brand_and_activity`，不向用户提问、不设三选一入口，也不据此跳过任一套关键词或报告章节。单项`adoption.scope`的brand/activity/brand_and_activity仍表示词的作用层级，不是需求类型。
- 品牌、足以识别活动的描述、完整统计日期和具体支持平台必须明确。品牌可从活动描述及可靠背景识别，能明确识别时不重复询问；无法识别或有歧义时才澄清。品牌与活动两套取数关键词均为必填生成结果，不因用户只描述活动而省略品牌式。`campaign_period`记录用户要求的统计窗口，实际举办期另记。
- 完整日期与具体支持平台在SSJH试查前须明确。缺项返回`need_clarification`，仍可进行不依赖缺项的背景核实。
- 年份有歧义时提出具体候选待确认，不能静默补年份。用户统计窗口与搜索发现的实际举办期分别记录，不用实际举办期替换用户窗口。
- 支持平台以本环境 SSJH Skill/API 为准。目前清单：微博、微信公众号、小红书、抖音、快手、视频号、B站、知乎、新闻、论坛、电商。懂车帝不支持；先告知不支持，不加入执行范围，不自动改选平台。
- 历史输入或prior_result中的brand/activity需求分流不再沿用；按本次活动复盘补齐品牌和活动两套结果。若只有品牌描述而缺少可识别的活动，澄清本次活动，不转成品牌整体报告。
- 用户已提供的检索表达式原样保留。发现改进点时另给建议，不自动改动已确认口径。

## 澄清与增量交互

- 本Skill从初始原话识别品牌、活动及边界、各活动日期、平台、业务诉求、产品、内容范围、交付要求和用户已有口径，保留字段来源、更正及冲突；未给内容范围默认ALL，产品类别/活动目标未给时交Planner推断，不逐项补问。
- 缺关键业务信息时返回need_clarification、resolved_request、missing_fields和clarification_questions，关键词未生成时留空，不能把空值标成ready。每轮最多两个问题，可在一个问题中收集完整时间范围；已有明确字段不重问。多活动分别记录，时间窗口归属或不同品牌项目拆分存在歧义时由Skill澄清。
- 在同一会话中运行且可使用提问工具时由Skill直接提问，clarification_delivery=direct；隔离调用或无法向用户交互时记录caller，由Supervisor按返回问题转达一次。无澄清需求记not_needed。direct不得由Supervisor重复问，caller不得声称已向用户提问。同步或异步工具按实际运行时行为处理，用户答案未到前不执行依赖该答案的查询。
- 用户补充后以prior_result中的resolved_request、已有口径和证据为基础合并；明确更正更新并留痕，冲突未解不能静默覆盖。相同需求、表达式和查询条件未变时复用已存证据，不重跑搜索或SSJH；条件变化时只核验受影响部分，旧证据不冒充新条件核验。没有新答案时保持等待，不反复调用或提问。
- 相对日期或缺年份时结合会话日期/时区提出具体候选待确认；不能把交付截止当作统计结束日，不能静默推定单日或起始日含义。明确单日时起止同日。平台简称可规范化，“全平台”须确认具体清单；不支持的平台先告知再澄清支持范围。“只看用户评价”须澄清是否仅UGC，用户明确只看UGC则直接记录。

## 返回约定

返回一个结构化结果，外加简短的人类可读说明；没有依据的字段留空，不伪造证据。

```yaml
status: ready | need_clarification | blocked
resolved_request: 合并原话、补充、更正与prior_result后的需求及字段来源；未知项留空，多活动分别保存
missing_fields: [缺失或有歧义、影响执行的字段；ready时为空]
clarification_delivery: direct | caller | not_needed  # 实际直接提问/待调用方转达/无需提问
analysis_scope: brand_and_activity  # 固定兼容值，不作需求分流
dependency_state:
  metaso: available | unavailable | not_used
  ssjh: available | unavailable | not_used
  details: 实际错误、未执行步骤及恢复条件，可为空
legacy_status: ready_for_confirmation | need_clarification | dependency_unavailable  # 仅旧调用方适配使用
campaign_name: 核实后的活动名称或足以识别活动的自然描述
# Topic5始终记录活动类型与边界；未知项按依据与澄清状态留痕
campaign_type:
  primary: 主活动类型
  secondary: [辅助形式，可为空]
  basis: 用户关注对象及证据ID
  state: explicit | inferred | needs_confirmation
activity_boundary:
  included: 本次活动包含的传播内容
  excluded_or_pending: 其他活动与尚需明确的内容范围
  query_period: 用户确认的统计窗口
  actual_event_period: 已核实的举办期，可为空或按站点列出
brand_keyword: 必填结果；最终采用的品牌表达式，覆盖品牌传播及达到并入门槛的活动传播
activity_keywords: 必填结果；最终采用的本次活动表达式，多活动分别记录
product_keywords: [单独供产品分析的名称、型号、系列]
brand_filter: 按品牌类型主动带入用户确认的完整默认式；无适用规则及其他依据时为空
activity_filter: 无依据时为空
filter_export_status: yes | no | unknown | not_applicable
filter_candidates:
  - expression: 候选排除词或表达式
    scope: brand | activity
    noise_type: 普通词义/周边商品推广/衍生服务推广/借话题卖无关商品/其他活动等
    source: user_rule | language_pattern | activity_boundary | user_case | ssjh_probe
    evidence_ids: [E1]
    status: pending_validation | supported_by_sample | adopted | not_adopted | user_confirmed
    validation_state: not_run | sampled | applied_and_compared
    excluded_category: 本次明确排除的商品/服务品类，可为空
    false_exclusion_check: 是否命中仍在分析范围内的内容、检查方法及限制
    adoption_reason: 采用/暂不采用的依据
candidate_terms:
  - term: 候选词
    role: entity | topic | activity_feature | product | filter
    decision: include | analysis_only | probe_only | reject | pending
    reason: 是否指向本次活动、歧义与覆盖依据
    evidence_ids: [E1]
evidence:
  - id: E1
    source: user | metaso_reference | ssjh_post
    title_or_author: 来源标题或作者
    url_or_local_path: 实际来源链接或证据文件
    excerpt: 支持判断的原文片段
    limits: 截断、来源可信度、尚未证实的专属性等
execution_routes:
  brand: {skill: SSJH-Social-Stat, keyword_field: brand_keyword, filter_field: brand_filter, purpose: brand_aggregate_query}
  activity: {skill: ssjh-social-task, keyword_field: activity_keywords, filter_field: activity_filter, purpose: activity_raw_posts}
  # 正式执行由Producer负责；此处仅交接路由，不代表已调用或已抓取
expression_validation:
  keyword_groups: 正式关键词展开组数，最多200
  filter_groups: 正式排除词展开组数，最多200
  parentheses: valid | needs_repair
  literal_escape: absent | unsupported
  dedup_changes: [原词/分支、删除依据与新式]
precheck_plan:
  executed: false  # 按实际调用更新，规划不冒充已执行
  batch_limit: 50
  batches: [每批实际表达式及关键词/排除词展开组数]
  estimate_relation: MATCHED | UPPER_BOUND | UNCERTAIN | UNAVAILABLE
  limits: 重叠、统计侧放宽、未调用或其他可比性缺口
residual_noise:
  - noise_type: 已发现但未安全排除的噪音类型
    evidence_ids: [E1]
    handling: 已采用处理 / 未采用过滤候选 / 执行环节继续识别
    owner: executor
    next_action: 具体核验、逐帖识别或记录统计限制的动作
    state: pending | handled | limitation_recorded
    impact: 实际已知影响与未验证范围，不编造噪音比例
probe_log:
  - query: 实际执行的检索表达式
    platforms_and_period: 具体试查范围
    match_fields: content,title
    filter_expression: 本次实际使用的过滤词
    ocr_retrieval: false
    result_path: 结果保存位置
    returned_posts: 返回帖子数量，不冒充总声量
    findings: 扩词、噪音或覆盖差异
    limits: Top样本、正文截断、未验证全量等
adoption:
  - expression: 取数表达式、品牌式中并入的活动分支或过滤词
    object_type: keyword | filter | product
    scope: brand | activity | brand_and_activity
    tier: A | B | C | null  # 新增过滤词只用A/C；已确认规则、口径沿用或产品分析用途为null
    decision: adopted | candidate | not_adopted | carried
    source: generated | user_explicit | user_rule
    validation_state: not_run | sampled | applied_and_compared
    basis: 定档依据及证据ID
    limits: 验证缺口、样本限制或不采用原因；产品分析词按用途记录依据，不沿用取数词检索门槛
clarification_questions: [仅影响活动识别或取数边界的关键问题]
confirmation_state: auto_adopted | carried_from_user_confirmation | mixed  # mixed表示自行采用与用户口径混合
```

`ready`要求品牌与各活动两套必填取数口径均就绪；仅一套可用不能ready。`ready`表示所需取数字段已有主体明确、限定合理的可执行核心口径（A／B或用户给定口径），可由Supervisor直接采用并继续后续流程，不等待用户审词，不等于全量核验或去噪完成。存在残余噪音、新增过滤词未通过或有限样本核验不足，均不单独构成blocked；记录`residual_noise`及验证缺口随口径交接。`need_clarification`只用于关键必填项缺失、主体/需求范围无法确定。`blocked`用于需求已明确但命中明显偏离且无法形成可用核心式，或必需依赖故障导致无法执行；依赖不可用但仍可形成B档核心式时可先交接口径。口径可交接与实际取数成功分开，取数失败不能伪报执行成功。

旧调用方适配：`ready`映射为`ready_for_confirmation`；`need_clarification`保持；`blocked`映射为`dependency_unavailable`。映射仅为兼容，不保留“等待用户确认”的行为。新调用方使用`status`与`dependency_state`，不根据legacy_status推断所有依赖是否可用。

`confirmation_state`记录整体来源，单项来源见adoption；自行采用不冒充用户确认。完整沿用同范围用户口径记`carried_from_user_confirmation`，混合来源记`mixed`。用户给定口径变更仍遵守第6.7节。

## 依赖异常


依赖不可用或请求失败时，在dependency_state记录实际错误、未完成步骤和恢复条件，不声称调用成功、不编造结果。秘塔不可用时可用明确用户信息与SSJH核实；SSJH不可用但主体、归属明确时，可返回B档核心式和ready，新增未核验过滤词不采用，已确认默认式仍沿用。没有可执行口径且需求明确时返回blocked；活动、年份、平台或业务边界不明时返回need_clarification。

关键词口径ready与实际取数成功是两件事：Supervisor可交接含B档限制的口径，正式取数依赖仍失败时由执行环节记录阻塞，不能伪报数据或报告完成。本能力不创建项目、不执行完整取数。
