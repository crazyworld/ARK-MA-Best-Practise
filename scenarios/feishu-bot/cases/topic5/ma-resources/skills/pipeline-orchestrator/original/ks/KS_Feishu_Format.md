# KS_Feishu_Format — Topic5 飞书文档格式规范

> **2026-11-11 品牌趋势图更正（当前规则）**：第一章品牌传播概况的声量/互动趋势图必须连续展示已确认基准期和活动期，并在图内标注两期名称、起止日期及分界。两期均使用同口径品牌数据，不能混入活动AI去噪集；仅扩展趋势图，不改变本章活动期总量及其他统计窗口。2.N.1活动趋势仍只展示活动子周期。


> **2026-11-11 通用防错规则**：分析范围、两种漏斗方法、章节词云层级、完整媒体补证与下游更新统一遵循 `KS_Report_Scope_Contract.md`；项目变量从当前需求及源数据读取，案例不作默认值。此条优先于本文件保留的历史描述。

> **2026-11-05 Skill集中核对**：可用Skill统一位于`../../Skills/`。本规范§一仍引用Export_to_Feishu的resolve_operator_identity.py和resolve_project_folder.py；本次已检查项目、用户安装目录及经授权的Cherry Studio目录，未找到该旧包/脚本。缺口已记录在`../../Skills/skill_manifest.json`，未生成占位脚本、未绕过文件夹解析和所有权交接门禁。实际执行此步前须恢复辅助依赖。

> **更新（2026-11-05）**：品牌1.3同步新版词云入口及Producer调用补充，品牌整体与活动交集分开绑定。
> **2026-11-05 品牌及活动词云统一入口**：品牌1.3和活动2.N.4③均使用项目`../../Skills/wordcloud-insight/SKILL.md` v0.2.6；按`KS/report_pack/KS_Wordcloud_Skill_Integration.md`读取前期需求和最新更正，在Step8完成分析及双PNG、Step10上传白底版、Step10.5复用分析。两层均加载Producer调用补充提炼核心发现，按当前Skill契约组织议题和证据。品牌整体使用已确认品牌窗口的`brand_stats.json`，活动交集单独绑定；通用Skill保持不变。


> **版本**：v1.17（2026-11-11）
> **更新**：活动整体净情感度明确绑定全量活动相关帖；去除正文验算公式，分组名称遵循指标规范。
> **更新**：v1.12 §4.4 三级漏斗 Tier3 行改为写明分母：「品牌内容中的产品提及占比 X%（Tier3 N / Tier2 M）」+「占活动总量 X%」（LM #48）
> **更新**：v1.11 §4.1 单平台改为不用 grid（两行文字 + 内容类型结构饼图通栏），并写入 grid 的通用使用条件：只在两栏都有图或实质内容时使用；来源：冠益乳项目用户在飞书文档评论「单个平台没有出图也就不要分grid了」（LM #47）
> **更新**：v1.10 §4.1 补充单平台写法（左栏文字 + 右栏 PGC/UGC 饼图；不生成平台分布饼图）
> **更新**：v1.9 §4.4 标题改为「提及漏斗分析（2.N.2）」，与报告模板 v2.15 一致
> **更新**：v1.7 §二 Section 结构顺序规范补全——明确「洞察段落→数据图→数据表格→代表性内容」核心顺序规则，新增「禁止数据汇总表格在图表之前」和「禁止正文分析结论在图表之后输出」两条禁止规则；对应 KS_Report_Template.md v2.4 同步
> **更新**：v1.6 ①§三 Callout 颜色表：📊 从 `light-blue` 改为 `light-orange`（裁定与 KS_Report_Template.md §整体洞察规范一致，含裁定记录）；②新增 §4.3「口碑分析情感分布 grid」XML 模板；③新增 §4.4「声量渗透三级漏斗 grid」XML 模板；④§七 标题从「内容主题标签分布规则」更名为「词云议题展示规则」（原概念已废弃）；⑤§10.1 cos_uploader 参数修正（删除不存在的 `--report-name`，改为 `-s charts`）
> **变更（2026-09-24）**：§4.2 新增「多平台场景」分支——每平台一组 grid 上下叠放时，右栏首行必须加 `<p><b>{平台名}核心议题解读</b></p>`；§4.2 重构为单平台/多平台两个 XML 模板。来源：沃尔玛×玩具总动员测试轮发现多平台 grid 右栏缺平台标题导致阅读混乱，本次正式写入规范
> **背景**：基于沃尔玛×玩具总动员复盘报告修复经验总结，替代原 `drive +import` Markdown 方案
> **变更（2026-09-23）**：新增 §10.4「Grid/Column block_replace 操作红线」——block_replace 对 **COLUMN 块**存在子块数量上限（最多持久化 2 个子块），多段内容必须对 **GRID 块**整体替换；block_insert_after 在 column 内部段落上调用返回 ok 但不写入，禁止用此方式扩充 column 子块；同步修正 §8.1（词云 grid 平台标签要求更新为已修复）
> **变更（2026-09-03）**：权限模型重构——§九 整段替换：废除硬编码 4 人邮箱授权循环（`permission.members create`），改为把文档所有权转移给当前操作人（`transfer_owner`，`member_type=openid`）；§一 同步改为 `resolve_operator_identity.py`（openid 直取，替代已删除的 `resolve_operator_email.py`）+ `--operator-open-id`，文件夹层由「独占授权」升级为「所有权转移」。同批次改动覆盖全部 Topic（Topic2 除外，用户明确指示暂不处理）及全局 `.claude/rules/feishu-routing.md` 的同一份 4 人清单
> **变更（2026-08-12，二次）**：修正 §十.10.1 图表 Skill 引用——此前写作"通常为 blueai-echarts-skill-new 或专用图表 skill"，与 Topic5 实际固定使用的 `blueai-echarts-skill`（见 KS_Chart_Spec.md）不一致；同步修正 `.claude/rules/skill-routing.md` 规则#3 标题（Topic 2 → Topic 2/Topic5 共用），消除跨 Topic 理解冲突
> **变更（2026-08-12）**：新增「十、发布后修正工作流」——`media-insert` 在当前环境下已确认为环境级限制（非偶发故障），固化 COS 重上传 + `block_replace` 原地替换的正确流程；新增「十一、说明类正文的灰色斜体样式」——固化数据剔除/口径说明类正文的既有但此前未成文的样式约定；§八 安全 Hook 注意补充 `--content @file` 规避方式（沃尔玛×玩具总动员联名复盘报告案例化过程中修正后固化）

---

## 一、飞书文档创建方式（强制）

**禁止**使用 `lark-cli drive +import` 导入 Markdown 文件——该方式无法实现 callout 颜色、grid 双栏、议题加粗段落等富格式。

**必须**使用 XML 直接创建。发布前先解析项目专属文件夹（复用 Export_to_Feishu skill 的通用文件夹解析脚本，不使用该 skill 的其他部分），并把项目文件夹本身的**所有权转移给操作人**（Topic 层文件夹维持不转移；文档本身的所有权转移见 §九）：

```bash
SKILL_DIR="{PRODUCER_ROOT}/.claude/skills/Export_to_Feishu"
OPERATOR_OPEN_ID=$(python3 "$SKILL_DIR/scripts/resolve_operator_identity.py") || { echo "❌ 操作人 open_id 解析失败，停止发布"; exit 1; }
PROJECT_FOLDER=$(python3 "$SKILL_DIR/scripts/resolve_project_folder.py" \
  --project-dir "{项目目录}" --project-name "{项目名}" \
  --topic "Topic5 营销活动复盘报告" \
  --operator-open-id "$OPERATOR_OPEN_ID") || { echo "❌ 项目文件夹解析/所有权转移失败，停止发布"; exit 1; }

lark-cli docs +create --api-version v2 --content "$(cat /tmp/report.xml)" \
  --parent-token "$PROJECT_FOLDER" --as bot
```

报告 XML 通过 Python 脚本生成（写入 `/tmp/`），用 Write 工具创建脚本文件后执行。

---

## 二、Section 结构顺序（固定，不可调整）

每个分析 section 严格遵循以下顺序：

```
<h2>X.X 节标题</h2>

<callout emoji="💡" background-color="light-orange" border-color="orange">
<p>洞察结论（1-3 句，含核心数字）</p>
</callout>

{正文段落（洞察分析说明、背景描述等）}   ← 必须在图表之前

{图表 <img href="CDN_URL"/>}

{数据汇总表格 <table>}                  ← 如有，必须在图表之后

{热门内容表格（仅 1.4 / 2.N.5 节）}
```

**核心顺序规则**：**洞察段落（callout + 正文分析）→ 数据图 → 数据表格（如有）→ 代表性内容/示例（如有）**

**禁止**：
- 正文段落在 callout 之前
- 图表在洞察正文之前（正文分析文字必须先于图表输出）
- 数据汇总表格在图表之前（表格只能在图表之后）
- 正文分析结论在图表之后（即不允许「图→表→分析文字」顺序，要用「分析文字→图→表」）

---

## 三、Callout 颜色规范

| 用途 | emoji | background-color | border-color |
|------|-------|-----------------|--------------|
| 洞察结论（每个 section 开头） | 💡 | light-orange | orange |
| 执行摘要（报告顶部整体洞察） | 📊 | light-orange | orange |
| 数据说明 / 注意事项 | ⚠️ | light-yellow | yellow |

> **裁定记录（2026-10-06）**：📊 整体洞察 Callout 统一使用 `light-orange`（与 KS_Report_Template.md §整体洞察规范一致），视觉上 📊 与 💡 同色系，但位置唯一（仅报告开头出现一次），不与 section 级 💡 callout 混淆。

---

## 四、双栏 Grid 规范

### 4.1 传播概况（1.1 / 2.N.1）— 两个饼图并排

**多平台时**，平台分布图（左）+ 内容类型结构图（右）必须用 grid 并排：

```xml
<grid>
<column width-ratio="0.5">
<img href="CDN_URL_platform_pie.png"/>
</column>
<column width-ratio="0.5">
<img href="CDN_URL_ugc_pgc_pie.png"/>
</column>
</grid>
```

**单平台时**（只出现 1 个平台）：不生成平台分布饼图，**不用 grid**：依次放两行文字 `平台分布：本次仅监测{平台名}，共 XX 条（100%）`、`内容类型结构：UGC XX 条（XX%）、PGC XX 条（XX%）、未知 XX 条（XX%）`，再放内容类型结构饼图（通栏，显示宽度约 360px）；`content_scope`≠全部时无饼图可放，用两句话说明现状：`平台分布：本次仅监测{平台名}，共 XX 条（100%）`、`内容类型结构：本次仅分析{UGC|PGC}内容，共 XX 条（100%）`。

> **grid 的通用使用条件（LM #47）**：分栏只在两栏都有图或有实质内容时使用，某一栏只有一句话（没有图、没有表）时不分栏，改为顺序排列。

趋势折线图（双轴）单独一行，在 grid 之前。

### 4.2 内容分析（1.3 / 2.N.4）— 词云（左）+ 议题解读（右）

**单平台或合并词云场景**（整体词云，不区分平台）：

```xml
<grid>
<column width-ratio="0.5">
<p><b>词云</b></p>
<img href="CDN_URL_wordcloud.png"/>
</column>
<column width-ratio="0.5">
<p><b>核心议题解读</b></p>
<p><b>议题一：{标题}</b></p>
<p>代表性关键词：{词1（频次）、词2（频次）}</p>
<p>{议题解读正文，2-4 句}</p>
</column>
</grid>
```

**第一章多平台场景**（每平台一组 grid，上下叠放）：

第二章默认每活动按活动整体、活动中的品牌及可选产品范围各一组grid，综合已选平台，右栏标题写分析对象，不照搬以下分平台示例。用户明确要求第二章分平台时记录覆盖项；真实词表范围与对象方法按KS_Report_Scope_Contract§3。

当报告按平台分别展示词云和议题解读时，右栏首行**必须加平台标题**，格式为 `<p><b>{平台名}核心议题解读</b></p>`，否则上下两组 grid 内容区分不清。

```xml
<!-- Grid 1：小红书 -->
<grid>
<column width-ratio="0.5">
<p><b>小红书词云</b></p>
<img href="CDN_URL_xhs_wordcloud.png"/>
</column>
<column width-ratio="0.5">
<p><b>小红书核心议题解读</b></p>  <!-- ← 必须含平台名 -->
<p><b>议题一：{标题}</b></p>
<p>代表性关键词：{词1（频次）、词2（频次）}</p>
<p>{议题解读正文}</p>
</column>
</grid>

<!-- Grid 2：抖音 -->
<grid>
<column width-ratio="0.5">
<p><b>抖音词云</b></p>
<img href="CDN_URL_dy_wordcloud.png"/>
</column>
<column width-ratio="0.5">
<p><b>抖音核心议题解读</b></p>  <!-- ← 必须含平台名 -->
<p><b>议题一：{标题}</b></p>
<p>代表性关键词：{词1（频次）、词2（频次）}</p>
<p>{议题解读正文}</p>
</column>
</grid>
```

> ⚠️ 注意：多平台词云 grid 的右栏必须整体替换（`block_replace` 作用于 GRID 块），不可用 `block_replace` 作用于 COLUMN 块或 `block_insert_after` 扩充列内段落（见 §十二 操作红线）。

---

### 4.3 口碑分析（2.N.3）— 情感饼图（左）+ 数据（右）

> **v1.6 新增**（2026-10-06）：本节补充 2.N.3 活动口碑情感分布 grid XML 模板，与 KS_Report_Template.md §2.N.3 布局规范对齐。

```xml
<grid>
<column width-ratio="0.5">
<p><b>整体情感分布</b></p>
<img href="{情感饼图 CDN URL}" name="sentiment_pie.jpg" mime="image/jpeg" scale="0.4"/>
</column>
<column width-ratio="0.5">
<p><b>情感分布数据</b></p>
<table>
<thead><tr>
<th vertical-align="top"><p>情感倾向</p></th>
<th vertical-align="top"><p>数量</p></th>
<th vertical-align="top"><p>占比</p></th>
</tr></thead>
<tbody>
<tr><td vertical-align="top"><p>正面</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p>{XX.X%}</p></td></tr>
<tr><td vertical-align="top"><p>中性</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p>{XX.X%}</p></td></tr>
<tr><td vertical-align="top"><p>负面</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p>{X.X%}</p></td></tr>
</tbody>
</table>
<p>活动整体净情感度（NSR）：<b>+{XX.X%}</b></p>
<p><em><span text-color="rgb(143,149,158)">注：活动层情感为 AI 二次标注，与品牌层监测平台算法输出口径不同，两者不建议直接横向对比。</span></em></p>
</column>
</grid>
```

**说明**：
- 上述整体值取全部活动相关帖，不按主要讨论对象筛选；分组图另用完整子集名称，按 `KS_Metrics_Calculation.md`「活动整体与主要讨论对象分组」执行。正文及图中不附验算公式或正负面计算分母。
- 左栏图片 `scale` 建议 0.35–0.45，根据实际渲染效果微调
- 右栏 NSR 行在表格之后单独一个 `<p>`，不并入表格
- 品牌层（1.2）情感饼图按原规范单独全宽输出，不使用本 grid 格式

---

### 4.4 漏斗分析（2.N.2）— 漏斗图（左）+ 数据表（右）

**先按方法选模板（P0）**：以下XML为关键词提及方法示例，仅该方法使用“提及品牌/产品提及占比”。AI主对象方法复用左图右表布局，但标题及下层替换为“活动中的品牌主导讨论”“品牌为主要讨论对象”，比例叫“品牌主导讨论占比”；数据从主对象标签另取，不改名沿用提及统计，不接入互斥的产品主对象组。活动起点均为最终活动相关全量。详细方法及范围见KS_Report_Scope_Contract§2。

> **v1.8 修订**（2026-10-14）：统一漏斗方向——二级漏斗（无产品词）和三级漏斗（有产品词）均使用本 grid 格式；废弃旧"A方案全宽进度条图"；「渗透率」列名改为「关联率」；品牌大盘对比行改为「声量贡献参考」并加注。

```xml
<!-- ① 二级漏斗（无产品层关键词） -->
<grid>
<column width-ratio="0.55">
<p><b>活动 → 品牌 二级提及漏斗</b></p>
<img href="{二级漏斗 CDN URL}" name="funnel.jpg" mime="image/jpeg" scale="0.5"/>
</column>
<column width-ratio="0.45">
<p><b>提及漏斗数据</b></p>
<table>
<thead><tr>
<th vertical-align="top"><p>层级</p></th>
<th vertical-align="top"><p>声量</p></th>
<th vertical-align="top"><p>占比</p></th>
</tr></thead>
<tbody>
<tr><td vertical-align="top"><p>活动总声量（Tier 1）</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p>100%</p></td></tr>
<tr><td vertical-align="top"><p>提及品牌（Tier 2）</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p><b>{XX.X%}</b></p></td></tr>
</tbody>
</table>
<p><em><span text-color="rgb(143,149,158)">* 声量贡献参考（非主指标）</span></em></p>
<table>
<thead><tr>
<th vertical-align="top"><p>指标</p></th>
<th vertical-align="top"><p>数值</p></th>
</tr></thead>
<tbody>
<tr><td vertical-align="top"><p>品牌同期总声量</p></td><td vertical-align="top"><p>{N} 条</p></td></tr>
<tr><td vertical-align="top"><p>活动声量占比</p></td><td vertical-align="top"><p>{X.XX%}</p></td></tr>
<tr><td vertical-align="top"><p>活动条均互动</p></td><td vertical-align="top"><p>{XX} 次/条</p></td></tr>
<tr><td vertical-align="top"><p>品牌条均互动</p></td><td vertical-align="top"><p>{XX} 次/条</p></td></tr>
<tr><td vertical-align="top"><p>条均互动倍数</p></td><td vertical-align="top"><p><b>{X.XX×}</b></p></td></tr>
</tbody>
</table>
</column>
</grid>

<!-- ② 三级漏斗（有产品层关键词，在二级基础上增加 Tier 3 行） -->
<grid>
<column width-ratio="0.55">
<p><b>活动 → 品牌 → 产品 三级提及漏斗</b></p>
<img href="{三级漏斗 CDN URL}" name="funnel.jpg" mime="image/jpeg" scale="0.5"/>
</column>
<column width-ratio="0.45">
<p><b>提及漏斗数据</b></p>
<table>
<thead><tr>
<th vertical-align="top"><p>层级</p></th>
<th vertical-align="top"><p>声量</p></th>
<th vertical-align="top"><p>占比</p></th>
</tr></thead>
<tbody>
<tr><td vertical-align="top"><p>活动总声量（Tier 1）</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p>100%</p></td></tr>
<tr><td vertical-align="top"><p>提及品牌（Tier 2）</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p><b>{XX.X%}</b>（品牌提及率）</p></td></tr>
<tr><td vertical-align="top"><p>提及产品（Tier 3）</p></td><td vertical-align="top"><p>{N} 条</p></td><td vertical-align="top"><p>品牌内容中的产品提及占比 <b>{XX.X%}</b>（Tier3 {N} / Tier2 {M}）<br/>占活动总量 {XX.X%}</p></td></tr>
</tbody>
</table>
<p><em><span text-color="rgb(143,149,158)">* 声量贡献参考（非主指标）</span></em></p>
<table>
<thead><tr>
<th vertical-align="top"><p>指标</p></th>
<th vertical-align="top"><p>数值</p></th>
</tr></thead>
<tbody>
<tr><td vertical-align="top"><p>品牌同期总声量</p></td><td vertical-align="top"><p>{N} 条</p></td></tr>
<tr><td vertical-align="top"><p>活动声量占比</p></td><td vertical-align="top"><p>{X.XX%}</p></td></tr>
<tr><td vertical-align="top"><p>活动条均互动</p></td><td vertical-align="top"><p>{XX} 次/条</p></td></tr>
<tr><td vertical-align="top"><p>品牌条均互动</p></td><td vertical-align="top"><p>{XX} 次/条</p></td></tr>
<tr><td vertical-align="top"><p>条均互动倍数</p></td><td vertical-align="top"><p><b>{X.XX×}</b></p></td></tr>
</tbody>
</table>
</column>
</grid>
```

**说明**：
- 二级/三级漏斗均使用本 grid 格式（55:45），不再区分"A方案全宽进度条图"
- 左栏 `scale` 建议 0.45–0.55
- 「占比」列：Tier2 行填品牌提及率（Tier2/Tier1），Tier3 行填产品提及占比（Tier3/Tier2）及对 Tier1 的占比；列名与指标口径见 `KS_Metrics_Calculation.md`「指标口径与表述规范」
- 「声量贡献参考」表格标注灰色斜体说明，与主指标漏斗数据视觉区分

---

## 五、议题格式（固定）

**禁止**使用 `<h4>` 标题作为议题标题。

**必须**使用加粗段落：

```xml
<!-- 正确 -->
<p><b>议题一：{议题标题}</b></p>
<p>代表性关键词：{词1（频次）、词2（频次）}</p>
<p>{议题解读正文}</p>

<!-- 错误 -->
<h4>议题一：{议题标题}</h4>
<ul><li>代表性关键词：...</li></ul>
```

---

## 六、图片插入

所有图表必须使用 CDN URL（`https://` 开头），禁止本地路径：

```xml
<img href="https://bmc-aigc-hb.bluemediacdn.com/..."/>
```

---

## 七、词云议题展示规则（原「内容主题标签分布规则」，已更名）

> **背景（v1.5 更新）**：「内容主题(AI)」字段自 Producer v0.4（2026-07-28）起已废弃，内容主题分布统计不再输出。本节保留以规范**词云议题 grid** 内是否展示来源说明的判断规则：

| 数据来源 | 处理方式 |
|---------|---------|
| SSJH-Social-Stat 全量词云数据（Step 4/7.3 拉取） | 正常展示，无需特别标注数据来源 |
| 仅基于 TOP10 热门内容人工聚类估算 | 在议题解读前注明「基于 TOP10 热门内容人工分析，不代表全量分布」 |
| 无取数结果（词云数据为空） | 整个词云 grid 不输出，在该节 callout 中说明原因 |

**禁止**出现任何「仅供方向参考」「非全量标注」等免责措辞——有数据就展示，没有数据就整节说明，不展示估算结果。

---

## 八、报告 XML 生成方式

1. 使用 Write 工具将 Python 脚本写入 `/tmp/build_{项目名}_doc.py`
2. 运行 `python3 /tmp/build_{项目名}_doc.py` 生成 `/tmp/{项目名}_report.xml`
3. 复用 §一 已解析的 `$PROJECT_FOLDER`（同一次发布内不重复调用脚本），运行：
   ```bash
   lark-cli docs +create --api-version v2 --content "$(cat /tmp/{项目名}_report.xml)" \
     --parent-token "$PROJECT_FOLDER" --as bot
   ```
4. 获取 document_id 后执行文档所有权转移（见 §九）

**安全 Hook 注意**：脚本中若含 XML 标签内容，使用 Write 工具而非 Bash heredoc 写脚本文件，避免安全拦截。若需对已发布文档执行 `lark-cli docs +update --content`，且内容含 `<em>`/`<span>` 等尖括号标签，内联传参会触发"无空格重定向写入红线路径"误判——`--content` 支持 `@file` 语法（已通过 `--dry-run` 验证与内联传参生成的 API 请求体完全一致），应改为：用 Write 工具将 XML 片段写入 `scratch/` 下的 `.xml` 文件 → `--content @该文件路径`。

---

## 九、飞书文档所有权转移（固定流程）

**不再有固定授权名单。** 文档所有权转移给当前操作人（`member_type=openid`），open_id 复用 §一 已解析的 `$OPERATOR_OPEN_ID`，不查邮箱、不查 userid。

```bash
DOC_TOKEN="{document_id}"
lark-cli drive permission.members transfer_owner \
  --params "{\"token\": \"$DOC_TOKEN\", \"type\": \"docx\"}" \
  --data "{\"member_id\": \"$OPERATOR_OPEN_ID\", \"member_type\": \"openid\"}" \
  --as bot --yes || { echo "❌ 文档所有权转移失败，操作人无法访问该文档，停止交付并人工处理"; exit 1; }
```

转移使用飞书 API 默认 `remove_old_owner=false` / `old_owner_perm=full_access`（不传覆盖参数）——转移后 Ada 应用仍保留 `full_access`，§十 的发布后原地修正（`docs +update`、图表 `block_replace`）不受影响。

🔴 **文件夹层（§一）与文档层（本节）两次转移必须顺序执行、不可并发**；任一失败即停止交付，不重试已成功的另一次。所有权没落到操作人身上时文档只有 bot 可见，等于交付了一份用户打不开的报告。

---

## 十、发布后修正工作流（图表/文本 in-place 更新）

> **背景**：沃尔玛×玩具总动员联名复盘报告发布后，Validator 审计发现历史文字修正未同步重渲染配图（图文不一致），需要对已发布文档做原地修正。这是一个此前从未成文的场景——本节固化正确流程，禁止再走试错路径。

### 10.1 图表替换

**禁止**尝试 `lark-cli docs +media-insert --selection-with-ellipsis` 做图表替换——已在当前环境反复验证为**环境级限制、非偶发故障**：
- `--as bot` → MCP locate-doc 报 `invalid token`
- `--as user` → 报 `missing_scope: docx:document:write_only`

**正确流程**：
```
1. 重新渲染图表 PNG（Topic5 固定使用 `blueai-echarts-skill`，见 `KS_Chart_Spec.md` 与 `.claude/rules/skill-routing.md` 规则#3，禁止改用通用降级 skill）
2. COS 上传取 CDN 直链：
   python blueai-echarts-skill/scripts/cos_uploader.py \
     -f <png路径> -p <project_name> -v <version> -s charts
   （注：无 --report-name 参数，-s 指定子目录 "charts"）
3. lark-cli docs +update --command block_replace \
     --block-id <原图片 block-id> \
     --content '<img href="{CDN_URL}" width=".." height=".."/>'
```
原地替换旧图片 block，不产生孤立/重复 block，不需要先删除再插入。

### 10.2 文本替换

单句/单段文字修正，使用 `str_replace`：
```bash
lark-cli docs +update --command str_replace --block-id <block-id> \
  --content "旧文本" --new-content "新文本"
```

整段替换（含富格式如加粗/颜色），使用 `block_replace`，内容通过 `@file` 传入（见 §八）。

### 10.3 定位 block-id

先执行 `lark-cli docs +fetch --scope full --detail with-ids --format json` 获取全文块结构，重定向到项目目录下的 `scratch/` 子目录（**禁止**直接重定向到 `/tmp`，会触发安全 hook 的红线路径拦截），再用脚本按关键词/内容定位目标 block-id。

---

## 十一、说明类正文的灰色斜体样式

> **适用场景**：数据剔除说明（如"以下 N 条因与品牌无实质关联已排除，不计入下表"）、口径说明（如"该字段不含独立标题字段，X 与 Y 存在重合"）等**声量构成透明说明**类正文——即向读者交代"为什么某些数据没有出现在正文/图表里"的辅助性说明段落。

**不适用**：
- 报告开头的"数据说明"区块（平台/周期/关键词等固定信息表，见 §二 Section 结构）——保持默认样式，不做灰色斜体
- 起标题作用的加粗段落（如"议题四：XXX（声量构成透明说明）"、"关键词噪音说明（context-check 核实结论）"这类与"议题一/二/三"平级的小标题）——保留加粗，不转灰色斜体；只对标题下方的说明正文本身应用本样式
- 正文中作为动词使用的"说明"（如"这一结构说明活动传播素材……"）——不触发本样式

**格式**：
```xml
<p><em><span text-color="rgb(143,149,158)">说明正文内容……</span></em></p>
```

**检查方法**：报告定稿前，可用 `lark-cli docs +fetch --scope full --detail with-ids --format json` 拉取全文，正则扫描含"说明"关键词的 `<p>` 段落逐一核对是否已按上述规则分类处理。

---

## 十二、Grid / Column block_replace 操作红线

> **背景**：沃尔玛×玩具总动员联名报告修复过程中确认的 Feishu API 行为，已通过实测固化为操作规范。

### 12.1 核心限制

| 操作对象 | 行为 | 结论 |
|---------|------|------|
| `block_replace` 作用于 **COLUMN 块** | 仅持久化前 **2 个子块**，其余静默丢弃，且返回 `ok: true` | ❌ 禁止用此方式写入多段内容 |
| `block_replace` 作用于 **GRID 块** | 正确持久化所有列及其全部子块 | ✅ 多段内容必须从 GRID 层整体替换 |
| `block_insert_after` 作用于 **column 内段落** | 返回 `ok: true` 但内容不写入（静默失败） | ❌ 禁止用此方式扩充 column 子块 |
| `block_replace` 作用于 **P 段落块** | 正常工作，替换单一段落文本 | ✅ 仅用于修改已有段落内容 |

### 12.2 正确操作流程（含图表的词云 grid）

需要修改词云 grid 右栏全部内容时，必须整体替换 GRID 块：

```bash
# 1. 获取 grid block-id（非 column block-id）
lark-cli docs +fetch --doc <DOC_URL> --scope full --detail with-ids | \
  python -c "..." # 找 <grid id="..."> 包含目标 column 的 grid

# 2. 构造完整 grid XML（包含左栏图片 token + 右栏所有段落）
# 注意：img src 用已上传的 Feishu 内部 token，无需重新上传
cat > /tmp/grid_full.xml << 'EOF'
<grid>
<column width-ratio="0.5">
<p><b>平台词云</b></p>
<img src="<FEISHU_TOKEN>" width="1000" height="1000" mime="image/jpeg"/>
</column>
<column width-ratio="0.5">
<p><b>核心议题解读</b></p>
<p><b>议题一：...</b></p>
<p>代表性关键词：...</p>
<!-- 议题二、三同结构 -->
</column>
</grid>
EOF

# 3. 替换 GRID 块（不是 column 块）
lark-cli docs +update --doc <DOC_URL> \
  --command block_replace --block-id <GRID_BLOCK_ID> \
  --content @/tmp/grid_full.xml --as user
```

### 12.3 图片 token 获取方式

已上传到飞书文档的图片无需重新上传，直接从文档 XML 中读取 `src` 属性即可：

```python
import re, json
raw = json.loads(fetch_output)
doc = raw['data']['document']['content']
imgs = re.findall(r'<img\s+[^>]*src="([^"]+)"', doc)
# 按 column 位置定位对应 token
```

### 12.4 获取 img src token 的前提

- 品牌1.3及活动2.N.4③词云均由wordcloud-insight生成并视觉检查，使用各自范围wordcloud-white.png上传COS后的CDN URL  
- 首次建 grid 时用 `<img href="CDN_URL">` 创建，Feishu 自动内化为 `src=TOKEN`  
- 后续对同一 grid 的任何结构修改，用 `src=TOKEN` 引用已内化的图片，**禁止再次 href 上传**（会产生重复图片块）


## 品牌趋势图的两期呈现（2026-11-11）

1.1的新趋势PNG须真实包含基准期和活动期数据，并在图片内部写明区间名称/日期/天数及分界，遵循KS_Chart_Spec。两期标签、轴单位和峰值不得裁切；图片可在原横向尺寸下适当增加高度以保持可读。发布后修改沿用COS新图上传及原图片block_replace流程，仅替换目标图，回读新token/尺寸和完整正文；不得同时修改其他指标窗口。
