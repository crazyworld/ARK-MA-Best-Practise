# diagnose · 排障 Bot(MVP)

在飞书群里 @ 它,问"这个任务为什么失败 / 卡在哪 / 这一步的输入输出是什么"。

## 它能做什么

- 找到**当前群**最近的任务,给出首个异常点和原因(确定性诊断,不让模型自己翻事件找原因)
- 追问某一步时,展示那一步的输入输出正文(密钥已脱敏,过长截断并标注原长度)
- 同样的诊断结论也可以用命令行得到:`python -m arkagent trace show <id>`

## 范围(MVP)

| 项 | 规则 |
|---|---|
| 群聊 | 可排查**本群全部**任务,不区分发起人 |
| 私聊 | 只能查**自己发起、且发生在私聊里**的任务,用租户级 `user_id` 判断(`open_id` 按应用隔离,不能用)。应用须开通 `contact:user.id:readonly`,否则取不到 `user_id`,私聊会回复无法确认身份。**升级前创建的任务没有 `user_id`,私聊查不到**(不回填,没有可靠依据) |
| 覆盖的 bot | topicx、topic5、topic6(`task_runs` / `pipeline_jobs`);**分析师、ada-pm**(通用 `Gateway` 的 `conversations` + `session_history`,各 case 一个库);**数字员工**(`sessions` + `session_history`) |
| 对象类型 | **任务**(topicx 系)有状态和 `last_error`;**会话**(分析师 / ada-pm / 数字员工)跨多轮、没有成败状态,`status` 只有 `active`(在用)/ `archived`(被 `/new` 归档),`archived` 不代表出错 |
| 部署 | 与被排查的 bot **同机**,直接读本机的库;不考虑分布式 |

权限在工具层强制:chat_id 取自飞书消息,工具参数里没有这个字段,传了会被拒;越权的任务一律回"没找到",不暴露其存在。

## 启动

```bash
conda activate topic6
cd scenarios/feishu-bot

# 1. 创建飞书应用并写入 ~/.arkagent/cases/diagnose/config.env(前四项)
python -m arkagent init --case diagnose

# 2. 创建 MA 资源(Environment + Agent),把打印出的两个 ID 追加到 config.env
export ARK_API_KEY=...
bash cases/diagnose/ma-resources/create_all.sh

# 3. 参考 cases/diagnose/env.example 填各 bot 的库路径(至少一个;未配置的 bot 不参与排查)

# 4. 启动
python -m arkagent run --case diagnose
```

## 已知限制

- **不读 gateway 日志**:当前基线日志不落盘,也没有统一的任务标识,读不了。"我发的命令没反应"这类没有 session 事件的问题查不到。
- **多智能体看不到子 Agent 内部**:主 session 事件流里只有"派发"和"回传"。
- **升级前创建的数据**没有 `chat_type` / `user_id`:群范围按 `chat_id` 精确匹配放行(依赖"私聊 chat_id 不外泄"这一假设,不是强校验),私聊范围一律不放行。
- 数字员工的私聊归属从 `session_memory_scopes`(单聊 `scope_type=user`,`scope_id` 即 user_id)反推;没有该记录的私聊会话查不到。
- 方舟在 session 14 天无新事件后会清理,之后只能看数据库侧状态。
- 事件只在提问时实时拉取,不落库。

完整规划见 `docs/OBSERVABILITY_PLAN.md`。

## session 日志定时落飞书文档(可选)

每 2 分钟扫一遍各 bot 的 session,有新事件就追加到该 session 的飞书文档,可直接转发给用户。
这是**交付视图**,不是分析依据:写入可能失败或缺段,排障 Bot 的结论始终以事件流和数据库为准。

**取代了此前的"每轮结束钩子"**:不用改任何业务 bot 的代码,只读它们的库(与排障 adapter 同一套),
五类 bot 全覆盖;运行中的长 session 也能写到当前进度;**写文档的权限收敛到排障应用一个**。
(通用 `Gateway` 上的 `after_turn` 钩子和 `make_after_turn` 仍保留,但没有任何 case 在用。)

| 项 | 说明 |
|---|---|
| 开启 | 在排障 Bot 的 config.env 设 `SESSION_DOC_PARENT_TOKEN=<父节点 token>`;不设则完全不启用 |
| 周期 | `SESSION_DOC_INTERVAL_S`,默认 120 秒 |
| 范围 | `SESSION_DOC_LOOKBACK_DAYS` 默认 7:只看最近这些天内**开始**的 session;**已经开始同步的不受限**(长任务照常跟进) |
| 单周期上限 | `SESSION_DOC_MAX_PER_CYCLE` 默认 20,按"最久没检查"优先,防止积压打满方舟接口 |
| 静止降频 | 连续 3 / 10 / 30 次没有新事件,检查间隔放大 3 / 15 / 60 倍;任务型进入终态且确认无新内容后停止轮询;会话型没有终态,只降频不停 |
| 身份 | **排障应用**(`FEISHU_APP_ID` 那个),经 SDK 直调,不依赖 `lark-cli` |
| 权限 | 排障应用须有 `docx:document:create`、`docx:document:write_only`(应用身份),并能写入父节点 |
| 失败 | 只记 warning,不影响任何 bot;失败那次不推进进度,下个周期自动补上 |

### 增量拉取:没有新事件时每个 session 只要 1 次请求

倒序(`order=desc`)从最新往旧翻,**遇到上次记录的最后事件 id 就停**。2026-11-12 对真实数据实测:
周期内无新事件,2 个 session 共 2 次方舟请求、0.3 秒。

- **为什么不"记分页游标"**:实测游标内容是 `{time, object_id}`,绑定某个事件;读到**末页时 `next_page` 为空**,
  没有"指向末尾之后"的游标可记,所以游标解决不了"读完了、等新事件"。
- **也不做跨周期的游标续翻**:倒序翻页先拿到的是最新的页,下次续翻拿到更旧的页,**最新那几页上次翻过却没保存,
  两段拼不起来,会静默丢内容**。所以回填必须在一个周期内翻完;翻到页数上限(默认 200 页 × 100 = 2 万事件)
  就放弃本周期、不写、下次重来,宁可重做也不丢。
- **`order=desc` 不在方舟文档里**,是实测可用的行为,可能变。拉到的数据会自检(不是严格倒序就退回全量拉取并记 warning)。

### 文档格式

按 **Turn → Span → 原始事件** 三级分块,样式参照 `可观测demo.png`:

| 层 | 内容 |
|---|---|
| Turn(第 N 轮推理) | 灰/绿/红卡片:Span 数、总耗时、模型 / 工具耗时占比、结束原因(`stop_reason`) |
| Span(模型推理 / 工具调用 / 错误) | 绿勾 / 红叉 / 灰色(未完成)卡片:名称、类型、耗时、`in/out/cache` token、线程。时间重叠的工具并排显示("并行 N") |
| 原始事件 | 每个事件一个 JSON 代码块,**全文**,`caption` 带事件类型和 id |

- **不过滤、不截断**:每个事件恰好出现一次(有测试保证),包括 `agent.thinking`、成功的模型调用、`session.usage`、状态事件。
  没有挂靠键的 `thinking` / `message` 归到最近的模型 Span;状态类事件原样附在本轮末尾。
- **唯一的加工是密钥脱敏**(`Authorization`、`api_key`、token 等,规则见 `redact.py`),发生在渲染前,不修改原始事件。
- 飞书 `callout` 里**不能放代码块**,所以"关键信息卡片"和"原始 JSON"是前后相邻的两个块,而不是图里一个卡片包住一切。
- `callout` 的 `background-color` 要用 `light-*`(基础色相会被渲染成灰底,2026-11-12 实测)。
- 体量:真实会话 79 / 208 / 340 个事件,渲染后 52 / 158 / 277 KB。**没有查到飞书单篇文档的大小上限**,超限时写入会失败并记日志。

> **MVP 不做隔离(2026-11-12 决策)**:所有 session 的日志——**包括私聊**——都写进同一个父节点下。
> wiki 文档权限继承父节点,**有该空间访问权的人能看到所有人的日志**,包括别人的私聊。这是已知风险,不是疏漏。
> 文档**写出去就无法收回**(会被索引、缓存),脱敏规则是启发式的,漏掉的凭据形态会随文档外泄。

## 诊断口径:按原始场景,不做汇总

`findings` 逐条列出每一次异常,**不合并、不截断**,每条带自己的轮次和事件 id,可单独追问。
(曾做过"重复警告合并为一条",已按用户要求撤销:长会话里每一轮的重试都可能对应不同原因,合并会丢掉逐轮的事件 id。)
