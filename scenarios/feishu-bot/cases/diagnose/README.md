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
| 私聊 | **暂不开放**:私聊要判断"自己名下",需要跨应用稳定的 user_id,各库目前没记录 |
| 覆盖的 bot | topicx、topic5、topic6(读 `task_runs` / `pipeline_jobs`) |
| 不覆盖 | 分析师、ada-pm、数字员工(会话库结构不同,adapter 未写) |
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

# 3. 参考 cases/diagnose/env.example 填 TOPICX/TOPIC5/TOPIC6 的库路径(至少一个)

# 4. 启动
python -m arkagent run --case diagnose
```

## 已知限制

- **不读 gateway 日志**:当前基线日志不落盘,也没有统一的任务标识,读不了。"我发的命令没反应"这类没有 session 事件的问题查不到。
- **多智能体看不到子 Agent 内部**:主 session 事件流里只有"派发"和"回传"。
- **`/new` 之后旧会话可能失联**(通用 `Gateway` 类 bot)——本 MVP 不覆盖这类 bot。
- 方舟在 session 14 天无新事件后会清理,之后只能看数据库侧状态。
- 事件只在提问时实时拉取,不落库。

完整规划见 `docs/OBSERVABILITY_PLAN.md`。
