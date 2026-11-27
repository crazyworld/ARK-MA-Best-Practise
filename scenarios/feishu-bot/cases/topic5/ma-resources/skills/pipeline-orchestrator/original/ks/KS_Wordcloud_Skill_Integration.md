# 品牌及活动内容分析：词云Skill与原始需求交接

> **2026-11-11 词云来源更正（当前规则）**：Part2活动整体、活动中的品牌及可选产品词云必须使用SSJH实际查询返回的词频。品牌词云采用活动条件与品牌词的查询交集，不套用漏斗的AI主对象子集；禁止用既有候选词在本地原帖重计来替代。接口或对应范围不可用时记录阻塞，不自行换方法。完整规定见 `KS_Report_Scope_Contract.md` §3。


> **2026-11-11 通用防错规则**：分析范围、两种漏斗方法、章节词云层级、完整媒体补证与下游更新统一遵循 `KS_Report_Scope_Contract.md`；项目变量从当前需求及源数据读取，案例不作默认值。此条优先于本文件保留的历史描述。

> 版本：v1.4 · 2026-11-11。用于品牌1.3和活动2.N.4③（首个活动即2.1.4）词云与议题解读；执行入口为项目内`../../Skills/wordcloud-insight/SKILL.md` v0.2.6。新增Producer调用补充，保持通用Skill不变。同节的达人层级、UGC参与方式和热帖配方沿用现有流程。

## 先读取前期需求

Producer Step1读取当前项目`01_需求接收及梳理/关键词生成/keyword_generation_result.json`，同时读取需求确认单和分析方案。前者是完整保存的request/result及调用记录，原话在request.user_description，补充/更正在相应用户消息记录中；最新解释在result.resolved_request或平铺resolved_request。保留原话、最新更正、关注问题和来源，不能只把活动名称当背景，也不能使用关键词试查目录中的其他项目结果替代。

字段优先使用最新明确更正和resolved_request，确认单是正式执行口径；两者冲突先核对版本，不自行取更宽范围。原话用于理解重点，生成判断/Planner推断目标分别标明来源，不作为已发生事实。交付要求等整体研究问题不要求本词云模块全部回答；背景不会预设议题或成效。

历史项目未保存完整JSON时读取现有确认单及方案，整理已有背景并记录来源，原话缺失如实留空；缺可选原话/背景不要求用户重新描述。只有对象、词表归属等关键输入无法确定时才澄清该缺口。指定JSON存在却无法读取时修复路径/格式，不能静默改用其他项目或较旧结果。

## 对每个分析层级、对象和实际平台范围建立独立输入

第一章按平台；第二章默认每活动按活动整体、活动中的品牌及可选产品范围综合已选平台。综合上游scope保存实际platforms数组，scope.context.platform按数组顺序用“、”连接，单平台保留原字符串；上下文工具校验平台集合不超出当前已确认范围并将数组留在内部审计，不把它作为通用Skill新字段。另记对象筛选方法；品牌子范围为活动条件与品牌词的SSJH查询交集，不使用AI主对象筛选。详见KS_Report_Scope_Contract§3。

品牌1.3使用Step4的brand_stats.json；活动2.N.4③在Step7.3调用SSJH查询活动整体、品牌提及及可选产品词表，保留真实返回；禁止本地候选词重计替代。保存当前查询实际范围为data-scope.json，例如：

```json
{
  "analysis_level": "activity",
  "campaign_name": "本次活动的已确认名称",
  "dimension": "overall",
  "query_period": {"start": "2026-11-02", "end": "2026-11-02"},
  "context": {
    "subject": "本次活动的整体传播",
    "project": "项目名称",
    "period": "2026-11-02至2026-11-02",
    "platform": "小红书",
    "sentiment": "全部",
    "content_type": "全部",
    "background": "已核实且与当前活动有关的背景；没有则留空"
  },
  "metric": "unknown"
}
```

品牌1.3的scope显式填写`analysis_level: "brand"`、`brand_name`和`dimension: "overall"`，无需伪造campaign_name；subject说明品牌整体讨论，词表来源为brand_stats.json。activity（默认）必须绑定campaign_name，活动中的品牌/产品子范围仍属activity；本模块记录SSJH关键词查询交集，不把AI主对象标签变成词表来源。可选品牌产品子范围须有独立品牌范围来源，不能取活动交集顶替；整体与同源品牌维度不重复展示。

适配工具校验已保存品牌名称和`brand_period`；没有brand_period时可使用已确认顶层campaign_period，单活动可用其同期窗口。多个活动无已确认品牌/合并窗口时不自动取最早到最晚日期；先从确认单核实并保存明确brand_period。原话和活动信息保留供理解，但品牌议题不被强制归因于某个活动。历史未保存JSON时按确认单填写scope及来源，不补造原话。

scope必须来自本词表实际查询条件，不能仅把需求日期贴到不同时段的表上。metric核实为出现帖子数才用post_count，出现次数用occurrence_count，不能确认用unknown，不把接口的未知权重称为声量。整体、品牌子范围、产品交集分别绑定当前subject、范围方法与来源，不能用整体议题顶替其他维度；产品词未提供时不强制生成产品维度。

使用项目适配工具传入原始需求，不重新向用户索取。`--project`只检查当前项目的`01_需求接收及梳理/关键词生成/keyword_generation_result.json`及历史位置`01_需求接收及梳理/keyword_generation_result.json`，不搜索其他项目或测试目录。两处同时存在时报错，执行Agent先核对版本和确认单，再用`--requirement`指定已确认文件。两处均无JSON时按上述历史项目规则先读取确认单及方案，把背景、关注问题和来源填入scope；适配工具在内部记录JSON未保存。指定文件存在却损坏时仍报错，不静默降级。

```powershell
python -X utf8 tools/prepare_wordcloud_context.py --project "<当前项目目录>" --scope "<data-scope.json>" --out "<新工作目录>"
```

工具输出context.json及requirement-context.json（完整需求快照、来源路径/哈希、当前分析层级、对象和范围）。执行Agent先读取两份文件，context.json作为Skill prepare命令的--context输入；JSON词表则将其context及metric与完整words合成input.json。requirement-context.json供执行Agent和质检追溯，不进入客户报告。历史项目无JSON时保留scope.context中已读确认单的背景和关注问题，另记录其版本/来源。适配工具不从原话猜日期、不替用户选平台，不改词频、不自动采集。

## 调用新Skill

Step8生成品牌及活动词云前读取Skill入口及contracts.md、analysis.md，同时读取`topic_configs/marketing-activity-review/prompts/wordcloud_call_brief.md`。按该调用补充从已有需求提取本模块的核心问题，保存带需求来源及调用补充版本/哈希的call-brief.json，当前Agent据此生成议题。通用Skill仍按原契约执行；工作数据放项目目录。preflight与依赖安装按runtime.md，在项目隔离环境完成，不改全局Python。先检查退出码及JSON状态。

1. 将完整原词表与context.json整理为input.json。CSV/XLSX使用Skill的prepare命令（多sheet显式指定）；JSON以原词原数值转换，保留来源。运行validate。不得预先取Top100；完整分析输入≤500词，超出按Skill契约处理，不静默截断。
2. 已有同范围原帖时传给Skill，依posts.md和post-evidence.md抽样阅读全文、消歧并写阅读证据；没有原帖照常按词频分析，不新建取数任务。有原帖却读取失败不能静默跳过。仅使用已有封面识别，不开启OCR检索。
   话题命中与实质内容相关分开判断：标题、正文和已有画面证据均无品牌/产品/活动关联、只有活动标签的样本，不作为正式品牌或活动议题；保留内部相关性及反例记录，不能解释为参与范围、内容共创或跨界传播。历史保留数据的授权不改变该判断。不直接删词或扣减API词频，也不将所有穿搭类内容一概排除。
3. 当前Agent依据全词表、前期需求和call-brief生成topics.json，围绕复盘问题形成有依据的核心发现，不只按词语类别分类。正文先给判断，再给证据支持，必要时补业务含义；标题仍为简短具体的内容名称。需求决定关注重点，不将目标写成已实现成效。议题自然归纳、最多5个，通常2–4个；代表词保留原词，通常1–3句紧凑段落，不机械凑80–120字或强加建议。常规边界保留在rationale，重要的内容性质区别直接写成发现；不以词频之和计算去重议题占比，不自动删品牌词或否定词。
4. 运行render生成同源wordcloud.png（透明）和wordcloud-white.png（白底），默认1400×880；运行compose接续同目录，生成analysis.md、preview.html、render.json、analysis-evidence.json。两命令及图片检查按新Skill要求完成。
5. 逐张查看图片，再复核议题与证据。配图Top100由Skill执行，超过100词未入图的词仍可参与分析；保持原词原数值。明确词表调整须保存依据和原表；不能将本地剔帖数从API词频机械扣除。

工作目录建议`03_数据接收及处理/词云分析/<brand或activity>/<品牌或活动>/<平台>/<维度>/<run_id>/`，render/compose输出到其下新建的render目录。不要覆盖已存在的分析产物。

## 报告和上传衔接

品牌及活动词云在Step8完成图文并检查；Step10仅上传报告选用的白底PNG，CDN映射按分析层级/对象/平台/维度记录实际源文件。Step10.5读取已生成analysis.md、call-brief和内部证据，核对需求版本、调用关注点及数据指纹后复用，直接复用已验证的同源图文。input或需求发生影响解释的更正时按Skill异常契约更新相关图文，不复用过期结论。

Step11将品牌整体议题写入1.3、对应活动及品牌/产品子范围议题写入2.N.4③；Step12第一章按平台、第二章按活动及对象分别用左图右解读grid，默认不按平台拆第二章。正常报告不复制analysis.md中已有的范围说明，也不输出Skill名称、原始需求记录、抽样日志或内部rationale；仅正文议题和实际图像进入模块，常规口径与全报告拉齐。明确模块目标未完成时说明受影响部分，不能删掉真实失败说明冒充完成。

ERROR/NEEDS_SETUP/PARTIAL停止受影响步骤并保留已验证产物。没有当前平台词表时按该平台无数据处理，不拿其他平台顶替，不新设需求重填门槛。生成成功不代表上传、完整报告或新环境端到端验收成功。
