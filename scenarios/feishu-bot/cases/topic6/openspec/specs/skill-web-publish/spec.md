# skill-web-publish Specification

## Purpose
Phase G + H 的两个 Skill：
- `topic6-web-report`：把 Phase F 定稿报告 Markdown 确定性转换为 `source.json`，用保留模板构建自包含 HTML（营销观察案例左文右图卡片，配图来自 HC3 飞书文档）。
- `miaoda-web-publish`：把成品 HTML 以用户身份发布为飞书妙搭应用并返回 API 给出的 `online_url`；也支持查询妙搭分析数据。

源码：`ma-resources/skills/topic6-web-report/`（`SKILL.md`、`CLAUDE.md`、`artifact-template.json`、`assets/source/*`、`scripts/md2source.py`、`fetch_case_images.py`、`validate-*.mjs`、`upload-html.mjs`）、`ma-resources/skills/miaoda-web-publish/`（`SKILL.md`、`references/*.md`、`scripts/validate_html.py`、`resolve_lark_cli.py`、`send_progress.py`、`progress_payload.py`）。两者模板/脚本主体为客户原样迁入。

## Requirements

### Requirement: 模板保持不变
`assets/source/` SHALL 作为保留模板只读使用：构建前 MUST 运行 `validate-template-assets.mjs`（任一保留 PNG/JPEG/TTF 缺失或无效即停止），然后复制到工作目录（Topic6 为 `08_UI发布/热点报告_{period_label}_web`）再修改。除用户明确要求外，HTML 结构、CSS、JS、logo、字体、图标、图表、tab、粘性导航与响应式规则 MUST 保持；周报/月报由 builder 按周期选择 `title-weekly.png` / `title-monthly.png`。

#### Scenario: 模板资产损坏
- **WHEN** `Poppins-Bold.ttf` 损坏
- **THEN** 校验失败，Phase G 停止

### Requirement: Markdown 到 source.json 的确定性转换
Topic6 SHALL 只用 `md2source.py <report.md> <source.json> [--case-images <dir>]` 生成 `source.json`，MUST NOT 手工拼接。映射：报告总标题丢弃；首章节前 `>` 引用 → `docx-quote_container-block`，其中 ⚠️/演示说明/冒烟测试行拆为首章节首个 text block；`# 一、…` → heading1；章节内最浅子标题 → heading2；更深子标题与整行加粗 → heading3；段落 → text；`- ` → bullet（跳过 `[配图：待补充]`）；表格 → table（`<br>` 换行、`\|` 还原、热度列去"分"）；`[标题](url)` → 正文留标题、链接写 `block.links`；`---` → divider；末章节后的 `>` → 页尾说明卡；`#### 案例 N：…` + 其后 bullet → `docx-grid-block`。stdout 输出 `{"cases", "case_images_missing", …}`。

#### Scenario: 热度列
- **WHEN** 表格热度单元格为 `81.7分`
- **THEN** source.json 中为 `81.7`

#### Scenario: 链接
- **WHEN** 段落含 `[某热搜](https://weibo.com/x)`
- **THEN** 正文文本为"某热搜"，`links` 含 `{text, href}`

### Requirement: 案例配图来源
`fetch_case_images.py --doc <docx URL|document_id> --out <dir>` SHALL 用 tenant token（`LARKSUITE_CLI_TENANT_ACCESS_TOKEN` 优先，否则 `LARK_APP_ID`+`LARK_APP_SECRET` 现换）读取文档完整 blocks 树，对每个 `案例 N：` 标题，下载其后、下一个标题前的第一张图片块为 `case-N.<ext>`；无图案例记入 `missing` 不报错。输出 `{"status":"success","cases","downloaded","missing"}`，不打印 token。配图 MUST 只来自源文档：MUST NOT 调用生图工具、搜图、复用模板 `case-*.jpg`，最终 HTML MUST NOT 含 `blob:` URL。Topic6 中缺图渲染"配图待补充"占位并继续（SKILL.md Workflow 第 4 步"取不到图就停"对 Topic6 不适用）。

#### Scenario: 用户只给案例 2 上传图片
- **WHEN** 文档中仅案例 2 后有图片
- **THEN** 下载 `case-2.*`，`missing=[1,3,…]`，网页其余案例显示占位

### Requirement: 构建与校验链
Topic6 Phase G SHALL 依次执行 `validate-case-image-sources.mjs source.json` → `build-report.mjs source.json index.html` → `validate-rendered-content.mjs source.json index.html`。任一校验失败视为输入准备失败：MUST 修复源数据映射，MUST NOT 删除字段、重编号案例、改写 source.json 文本迎合校验或回退到模板图片。产物为自包含 HTML。

#### Scenario: 内容覆盖不足
- **WHEN** `validate-rendered-content.mjs` 报某段落缺失
- **THEN** Phase G 失败并报告原文错误

### Requirement: Topic6 不使用 HTML snapshot 上传
`upload-html.mjs`（含 `--dry-run`、`--setup-key`）SHALL 保留在 Skill 中但 Topic6 MUST NOT 执行（沙箱无 snapshot API Key，且不属于交付）；Topic6 的网页交付唯一出口是妙搭发布。web-report 的 `SKILL.md`、`CLAUDE.md`、`DESIGN_SPEC.md`、`agents/openai.yaml` MUST NOT 指示上传 snapshot。

### Requirement: 周刊/月刊判定
`build-report.mjs` SHALL 只按页头「数据周期：<标签>（…）」的周期标签判定：`YYYY-Www` 为周刊；标签为月份（`YYYY年M月`、`YYYY-MM`、含月刊/月报）为月刊。MUST NOT 扫描正文（W44 正文「2026年10月…」曾误判月刊）。缺少周期声明 MUST 报错。

#### Scenario: Agent 按 SKILL.md 第 9 步上传
- **WHEN** Coordinator 试图运行 `upload-html.mjs`
- **THEN** 违反 coordinator-orchestration 的 Phase G 要求

### Requirement: 妙搭发布执行契约
`miaoda-web-publish` SHALL 按固定步骤执行：
1. 确认输入（源路径、新建或现有 `app_id`、应用名）；
2. 预检 `scripts/validate_html.py <path>`，有错误即停止，警告呈现；
3. `scripts/resolve_lark_cli.py` 选定唯一 lark-cli 绝对路径并在后续复用；仅当 CLI 明确返回未登录/缺 scope 时才 `auth login --domain apps`（Topic6 沙箱中用户身份来自 Vault 注入的 `LARKSUITE_CLI_USER_ACCESS_TOKEN`，不得发起登录）；
4. 路由：新应用 `apps +create` → 初始化 Git → 精确 `git add` → commit → `git push origin HEAD:sprint/default` → `+release-create` → `+release-get`；现有应用更新同一仓库；仅确认的遗留非 Git HTML 应用可用 `+html-publish`；
5. `+release-get` 短间隔轮询直到 `finished|failed`，单次等待 ≤60s；超时不等于失败，保留 `app_id`、`release_id` 从 `+release-get` 恢复，MUST NOT 重复创建应用或发布；
6. 链接只取发布响应、`+get --app-id` 或按 `app_id` 精确过滤的 `+list` 中的 `online_url`，MUST NOT 手工拼接；
7. 交付 `app_id`、`release_id`、状态、`online_url`、源文件 SHA-256、是否新建、是否复用原链接。

所有命令 MUST 带 `--as user --format json`；MUST NOT 持久化 token/cookie/发布人姓名或邮箱；目标只能是飞书妙搭，MUST NOT 让妙搭 AI 重新生成页面。

#### Scenario: 发布轮询超时
- **WHEN** `+release-get` 60 秒内仍为 running
- **THEN** 返回 `release_id`，下一轮从 `+release-get` 恢复

#### Scenario: push 失败
- **WHEN** `git push` 失败
- **THEN** 按 references/publish.md 处理后重试 push，MUST NOT 改走 `+html-publish`

### Requirement: Topic6 应用复用
Topic6 SHALL 以 `run_config.status.h.app_id` 决定新建或更新：为空时新建 Git 应用并写回 `app_id`，非空时更新同一应用。进度播报（`send_progress.py`）仅在用户确认接收人、模板与身份后使用；发送失败记 `progress_delivery_error`，不回滚已成功发布。

#### Scenario: 同一项目第二次发布
- **WHEN** `status.h.app_id` 已存在
- **THEN** 更新该应用，通常沿用原 `online_url`
