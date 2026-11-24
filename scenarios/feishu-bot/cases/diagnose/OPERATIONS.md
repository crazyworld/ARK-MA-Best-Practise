# 排障 Bot · 操作与配置说明(最终版)

适用:合入 crazyworld/main 之后,让排障 Bot 排查 topic5(及其他 bot)。
范围、已知限制见 `README.md`,规划见 `../../docs/OBSERVABILITY_PLAN.md`。

## 1. 部署前提

| 项 | 要求 |
|---|---|
| 机器 | 排障 Bot 与被排查的 bot **同一台机器**(直接读本机 SQLite 库;分布式部署不支持,是本需求已确认的例外) |
| 飞书应用 | 排障 Bot 用**独立的飞书应用**,不与任何业务 bot 共用 `FEISHU_APP_ID`(同一应用只允许一个进程) |
| 方舟 | 一个 `ARK_API_KEY`;排障 Agent + Environment 由 `ma-resources/create_all.sh` 创建 |
| 运行环境 | `conda activate topic6`,用 `python -m arkagent`,不要用 PATH 上的 `arkagent` 命令 |

## 2. 飞书应用权限(应用身份,开通后要**发布新版本**才生效)

启动时会自检并在终端告警缺哪项,缺失不阻断启动。

| 权限 | 用途 | 缺失的影响 |
|---|---|---|
| `contact:user.employee_id:readonly` | 取租户级 `user_id`,判断私聊"是不是本人" | 私聊提问回复"无法确认你的身份" |
| `docx:document:create`、`docx:document:write_only` | 写 session 日志文档(仅配置了 `SESSION_DOC_PARENT_TOKEN` 时需要) | 日志写不进飞书文档,不影响排查回复 |
| 通讯录入口(任一):`contact:contact.base:readonly` / `contact:contact:access_as_app` / `contact:contact:readonly` / `contact:contact:readonly_as_app` | 文档信息卡显示用户真名/群名 | 信息卡只显示 ID |
| `contact:user.base:readonly` | 同上(用户字段) | 同上 |

文档父节点(wiki 节点或文件夹):排障应用需是该空间成员且有可写权限。

## 3. 配置(`~/.arkagent/cases/diagnose/config.env`,权限 0600)

样板见 `env.example`。**含密钥,不要提交、不要贴到聊天里。**

必填:`ARK_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`DIAGNOSE_AGENT_ID`、`DIAGNOSE_ENVIRONMENT_ID`。

待排查的库(**至少配一个**,未配置的 bot 不参与排查;路径写**绝对路径**):

| 变量 | 被排查的 bot | 默认库位置 |
|---|---|---|
| `TOPIC5_PIPELINE_DB_PATH` | topic5 | `cases/topic5/data/topic5_pipeline.db`(若 topic5 配了 `TOPIC5_PIPELINE_DB_PATH` 则以它为准;必须与 topic5 gateway 用同一个) |
| `TOPICX_CONTROL_DB_PATH` | topicx 统一入口 | 以 topicx 实际配置为准 |
| `TOPIC6_PIPELINE_DB_PATH` | topic6 | 以 topic6 实际配置为准(默认见 `cases/topic6/pipeline_store.py`) |
| `ANALYST_GATEWAY_DB_PATH` | 分析师 | `~/.arkagent/cases/analyst/gateway.db` |
| `ADA_PM_GATEWAY_DB_PATH` | ada-pm | `~/.arkagent/cases/ada-pm/gateway.db` |
| `DIGITAL_EMPLOYEE_SESSION_DB_PATH` | 数字员工 | `~/.arkagent/cases/digital-employee/topic_bot_sessions.db` |

校验规则:配了但文件不存在 → 启动报错;两个变量指向同一个文件 → 启动报错(防串号)。

可选:`AUTHORIZED_OPEN_IDS`(限定谁能用)、`SESSION_DOC_PARENT_TOKEN` 及 `SESSION_DOC_*` 调优项(见 `env.example` 注释)。

## 4. 操作步骤

### 4.1 首次上线(以让它排查 topic5 为例)

```bash
conda activate topic6
cd scenarios/feishu-bot
# 1. 创建飞书应用并生成 config.env 骨架
python -m arkagent init --case diagnose
# 2. 创建 MA 资源,把打印的两个 ID 写进 config.env
export ARK_API_KEY=...        # 仅当前 shell,不写文件
bash cases/diagnose/ma-resources/create_all.sh
# 3. 在 config.env 里填 TOPIC5_PIPELINE_DB_PATH=<topic5 库的绝对路径>(及其他要排查的库)
# 4. 在飞书开放平台开通第 2 节的权限并发布新版本
# 5. 启动,观察终端的"权限自检"输出
python -m arkagent run --case diagnose
```

### 4.2 topic5 升级后的重启顺序

1. **先重启 topic5 gateway**(加载新代码)。首次打开库时会自动给 `pipeline_jobs` 补 `user_id`、`chat_type` 两列(只增不删,旧数据保留)。
2. 再重启排障 Bot。
3. 之后**新建**的 topic5 任务才记录发起人身份。

### 4.3 日常

- 群里 @ 排障 Bot 提问;私聊直接发消息。
- 命令行同样可用:`python -m arkagent trace show <task_id | session_id>`。
- 会话重置 `/new` 对排障 Bot 自身有效;被排查 bot 的 `/new` 会归档旧会话,旧会话仍可被查到(`status=archived`)。

## 5. topic5 的排查范围与限制

| 场景 | 是否可查 |
|---|---|
| 群聊里发起的 topic5 任务,在**同一个群**提问 | 可以,不论新旧任务 |
| 私聊里发起的 topic5 任务,**本人**在私聊提问 | 仅**升级后新建**的任务可查 |
| 升级前的 topic5 任务,私聊提问 | 查不到(没有身份记录,不回填,宁可查不到) |
| 他人私聊的任务 | 一律查不到,回复"没找到",不暴露其存在 |
| topic5 经 **topicx 统一入口**发起的任务 | 与上面一致(身份取自 topicx 任务记录) |
| topic6 的私聊 | **暂不支持**(`pipeline_jobs` 无身份列,群聊可用);需要时按 topic5 的做法同样改 topic6 |

## 6. 验证

- 单元测试:`PYTHONPATH=$PWD:$PWD/cases python -m pytest tests/test_diagnose_mvp.py -q`
  (含 topic5 身份写入、读取、旧库自动补列三项)。
- 读库兼容验证:`docs/verification/diagnose-topic5/`。
- 上线后的端到端自测:
  1. 在私聊里让 topic5 跑一个任务;
  2. 私聊问排障 Bot"我刚才那个任务为什么这样",应能给出该任务的结论;
  3. 换另一个账号私聊问同样的问题,应回复没找到。

## 7. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动报"指向的文件不存在" | `*_DB_PATH` 路径错或 topic5 还没产生过库;先让被排查的 bot 跑起来再配 |
| 私聊回复"无法确认你的身份" | 缺 `contact:user.employee_id:readonly` 或未发布 |
| 私聊查不到 topic5 任务 | 任务是升级前创建的,或 topic5 gateway 还没重启到新代码 |
| 文档信息卡只有 ID 没有名字 | 缺通讯录入口权限(第 2 节),不影响排查 |
| 查不到事件内容 | 方舟 14 天无新事件后会清理 session,之后只剩数据库侧状态 |

## 8. 数据迁移到其他机器

排障 Bot 与被排查的 bot **必须一起迁**。步骤:停旧机器 gateway → 用 `sqlite3 <库> ".backup '<目标>'"` 导出(不要直接拷运行中的 `.db`,会漏 `-wal`/`-shm`)→ 安全传输 `config.env`(含密钥)→ 把 `config.env` 里的库路径改成新机器的绝对路径 → 保留 `session_docs.db`(否则会给所有 session 重复建飞书文档)→ 校验行数后再启动。方舟云端事件不需要迁移。
