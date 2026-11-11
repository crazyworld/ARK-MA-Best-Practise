# Topic6 热点报告 · Agent 长期记忆索引

> 每次启动前必读本文件,再按索引决定是否读其他记忆文件。
> 本文件由 gateway 侧 Memory Store API 维护,Agent 只读。方舟不把 Memory Store 挂成目录,一律用 `memory_read /{memstore_id}/topic6/xxx.md` 读取(`/mnt/memory/` 为空)。

---

## 必读清单

| 文件 | 何时读 | 说明 |
|---|---|---|
| `/{memstore_id}/topic6/错误案例库.md` | **每次启动** | 历史错误 + 修正,防止重蹈覆辙 |
| `/{memstore_id}/topic6/_版本状态.md` | **每次启动** | 八任务(C0/R1~R5/C2/C3)当前活跃 Prompt 版本镜像 |

> `lm/执行日志.md`(项目级临时状态)不进 memory;
> 执行状态由 gateway `pipeline_jobs` 表 + 沙箱内
> `/workspace/Projects/{PROJECT_DIR}/run_config.yaml` 承接。

---

## 关键规则索引

- 启动:飞书 `/start [模式] [周期]` 命令(由 Gateway 解析模式后创建 Session;提到「热点周报」不再触发)
- 日期默认:当前时间的上一个完整自然周(周一到周日)
- 人工干预点:**3 个 HC,不可跳过,不可合并**;由 gateway 渲染飞书交互卡片,回帖以 `user.message` 塞回 MA Session
- Prompt 管理:不通过验收时,skill 内新建 `v{N+1}.md`,不覆盖旧版本;重新打包上传 → SkillHub 生成新 version → 协调器 pin 到明确 version
- 成本记录:每个 LLM 任务完成后立即调用 `python3 /mnt/skills/topic6-annotation/tool/cost-tracker/cost_tracker.py --project-dir /workspace/Projects/{PROJECT_DIR} ...`,不攒着批量写

---

## 当前 Skill 版本

| Skill | 版本 | 挂载路径 | 说明 |
|---|---|---|---|
| 主编排 | — | 协调器 system prompt(`ma-resources/agents/coordinator.system.md` 的运行时投影 `/workspace/AGENTS.md`) | 本 topic 执行时序的唯一权威 |
| `topic6-fetch-normalize` | v1 | `/mnt/skills/topic6-fetch-normalize/` | Phase A+B 取数标准化 + 抽样;通过挂载的 hot-topics MCP 直连 |
| `topic6-annotation` | v1(内含 C0=v13 / R1=v2 / R2=v2 / R3=v1 / R4=v2 / R5=v3 / C3=v7) | `/mnt/skills/topic6-annotation/` | Phase C:C0+R1~R5+C3 七路标注;`datahub_annotate.py` 5 合 1 精简版(提交+轮询+合并+筛选+时窗) |
| `topic6-insight` | v1(内含 E1=v1 / E2=v3 / E3=v1 / E4=v6 + _tagging=v1) | `/mnt/skills/topic6-insight/` | Phase E:E1~E4 四版块洞察;Ark(OpenAI 兼容)接口 |
| `topic6-event-registry` | v3.0.0-rc.14 | `/mnt/skills/topic6-event-registry/` | Phase C:C2 事件合并；封装调用 run_c2.py(cross-platform + legacy 前端,聚簇对齐原版 00→09,无 x2/x3)，阶段 checkpoint 续跑，Chat=DeepSeek-V4.1-Flash |
| `topic6-web-report` | v1.1.0 | `/mnt/skills/topic6-web-report/` | Phase G:UI 网页版报告 |
| `miaoda-web-publish` | v1.0.1 | `/mnt/skills/miaoda-web-publish/` | Phase H:妙搭 Git 发布与链接确认 |

---

## 架构要点

1. **路径基准**:所有 skill 相对路径走沙箱挂载 `/mnt/skills/topic6-xxx/`;记忆文件走 memory 工具 `/{memstore_id}/topic6/`(不是文件系统路径);项目运行工作区走 `/workspace/Projects/{PROJECT_DIR}/`;节点完成后仅用 `publish_node_artifacts.py` 把稳定产物复制到 `/mnt/session/outputs/{PROJECT_DIR}/` 并更新 manifest,禁止复制日志、PID、checkpoint 和临时分块。
2. **工具依赖**:
   - 消息与卡片:gateway 侧 `FeishuSender`(卡片渲染与消息推送)
   - 数据接入:MA MCP 挂载(`datahub-cli` / `hot-topics-mcp`)
   - LLM:3 个 Agent 与沙箱脚本(C2/E/demo 路由)统一走 bmc-llm-relay(blueai 模型网关,OpenAI 兼容);脚本只读 `LLM_RELAY_API_KEY` / `LLM_RELAY_BASE_URL`,不回退 `ARK_*`;DataHub 标注模型由 DataHub 侧调度
3. **子 Agent 并发**:Phase C 第一批 C0/C3 两路、第二批 R1~R5 五路由 `topic6-annotator` 子 Agent 并发启动;Phase E 不再委派 `topic6-insighter`,由 `pipeline_e.py` 脚本内部有限并发 E1~E4;HC 前质检委派 `topic6-validator`(可选);其余阶段串行走协调器。
4. **HITL**:HC1/HC2/HC3 关卡输出结构化 JSON + `end_turn`,由 gateway 渲染飞书交互卡片,回帖以 `user.message` 塞回 MA Session。
