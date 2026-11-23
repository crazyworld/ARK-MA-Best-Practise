# 变更记录

## 2026-11-23（营销观察案例字段与消费洞察卡同一呈现）

- 现象：营销观察「原始热点：」整段（含「（平台）」）包成一个蓝色链接，与「营销发现」用两个 `<p>` 紧贴排列，
  长标题时平台标注孤行、字段之间没有分隔，视觉上堆在一起；消费洞察「触发事件」则是链接只包话题、平台标注灰字、字段间细分隔线。
- 处理：`build-report.mjs` `renderGridCase` 改为与洞察卡同一渲染路径——字段输出为 `ul.insight-list.case-field-list`，
  每行走 `renderListItem`（`renderLinkedLine` + `SourceLinkIndex`），链接只覆盖话题原文；source.json 未把「链接：」并入
  `block.links` 时，用独立 URL 以话题原文补一条链接，保持原有兜底。`styles.css` 新增 `.case-field-list` 样式（与
  `.behavior-insight-card .insight-list` 同款），删除不再使用的 `.case-source-link`。
- 验证：W41/W44/W45 真实报告与 source.example.json 改动前后各 build 一次，全页 href 序列逐条一致；
  `validate-rendered-content.mjs` 全部通过；无 block.links 仅有「链接：」行的构造用例链接正常；桌面 1280 与移动 390 截图核对。

## 2026-11-14（合入客户模板 2026-11-12 版的修复，版本号不变）

三方合并：基线=客户 V2 vendor 版（`ada_topic6_v2_261110/agent/skill/_vendor/...`，11-10），
上游=`ada_topics/artifact-template-bluefocus-hotspot-web-report`（11-12）。只合入上游的修复，MA 已有行为不回退。

合入：
- `build-report.mjs`：普通段落和 ⚠️ 提示行按 `block.links` 挂链接（此前只有 bullet 挂链接）；
  营销观察「原始热点：」整段原文渲染为蓝色外链，存在「原始热点」行时隐藏独立「链接：URL」行；
  风险表识别放宽为「风险事件 + (风险类型 | 风险影响)」，并改为匹配 `(品牌)?舆情风险` 小节——
  E4 实际产出的三列表、「舆情风险」小节此前落入普通卡片分支；`block.hidden` 跳过渲染；
  热点总计改为标题行胶囊，热度改为补充指标；新增节点汇总卡、节点预告卡、营销发现卡、风险卡的布局类；
  风险卡不再显示兜底胶囊「风险事件」；导航标签「行业分布 / 平台热点」改为「行业热点 / 平台新鲜事」。
- `styles.css`：Hero 背景层改为 z-index 0、Hero 透明（修复 iframe/WebView 顶部断层）；行业 TOP10、热门事件、
  平台热点词、节点汇总卡按卡片容器宽度切换布局（@container）；热搜列表改为 auto-fit 网格；
  ≤1140px 导航不裁字；≤600px Hero 改为堆叠；风险卡改为单列；案例图改为 cover 居中裁切。
- `validate-rendered-content.mjs`：新增 9 条 CSS 结构断言（Hero 层级、容器断点、auto-fit、排名光学校正等）。
- `SKILL.md` 第 6、8 步，`DESIGN_SPEC.md`：同步补充上述规则说明。

保留 MA 行为（上游删掉或回退的部分一律不采纳）：
- 缺图渲染「配图待补充」占位、不阻断（上游会 throw "Missing reusable image"）；
- `SourceLinkIndex` 处理同标题跨平台链接（W45 成都蓉城）；行内链接逐段渲染改为抽成 `renderLinkedLine`，
  供列表和段落复用（上游的 `renderLinkedEmphasis` / `linkifyRenderedMarkup` 按文本 key 建 Map，会退回 W45 的问题，因此不采纳）；
- 周期只看「数据周期」标签，缺失即报错（上游仍扫全文、缺失时回退到 W32 默认值）；
- 尾注只认「热度值说明」，缺失时用 HEAT_NOTE 兜底（MA-06）；
- 中文字体栈 Noto Sans SC 优先（上游改回 PingFang 优先）；保留 `validate-template-assets.mjs`、MA-01~06 断言、
  不上传 snapshot 的约束；不引入上游的 `extract-hotspot-docx.py`、Sites 发布步骤，以及含 blob: 的 `source.example.json`。

验证：topic6 全部 pytest；W41/W43/W44/W45 真实报告合并前后各 build 一次：链接 href 序列逐条一致，
案例数、占位、周期、标题、尾注、cell-list 数一致；390–1440px 无水平溢出、无标题与指标重叠。

## 2026-11-06（MA 适配，版本号不变）

- `build-report.mjs`：周刊/月刊只按页头「数据周期」的周期标签判定（`YYYY-Www` → 周刊；`YYYY年M月` 等 → 月刊）。
  此前扫描文件名 + 全部正文，W44 正文「2026年10月车企销量…」被误判为月刊。周期解析提前到文件头，缺失仍报错。
- `SKILL.md` 第 5/9/10 步、`CLAUDE.md`、`assets/source/DESIGN_SPEC.md`、`agents/openai.yaml`：删除「必须上传 HTML
  snapshot」指令，与 coordinator「禁止执行 upload-html.mjs」对齐（W44 Agent 照 skill 上传、无 key 后升级为阻断）。
  `upload-html.mjs`、`references/html-snapshot-upload.md` 保留不引用。

## 1.1.0 — 2026-09-24

- 脚本挂载路径:`<skill-directory>/scripts/*.mjs` 在部署环境下解析为
  `/mnt/skills/topic6-web-report/scripts/`(调用方按 skill 挂载路径拼接,无需改脚本代码)。
- 无脚本代码逻辑改动、无参数/环境变量增减、无模板资产替换。
