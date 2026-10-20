# Validator 输出格式规范

> **版本**: v1.0（2026-06-26）
> **适用角色**: Validator V2 · Check

---

## 一、意图理解审计输出（§四，Validator V2 直接执行）

```
意图审计结论: PASS / WARN / BLOCK

命中问题（无则省略）:
  - [级别] 维度：问题简述

日志路径: Agent.Validator/LM/Logs/{date}_intent_audit_{n}.md
```

### 结论级别

| 级别 | 触发条件 | Supervisor 后续动作 |
|------|---------|-------------------|
| `PASS` | 无任何命中问题 | 继续 Phase 1.5 → HC-2 不触发 |
| `WARN` | 仅 P1/P2 问题 | 附带问题清单，Supervisor 决定是否调整理解 |
| `BLOCK` | 存在 P0 问题 | 触发 HC-2，等待用户确认后方可继续 |

### 检查维度（4 项）

| 维度 | 严重度 | 检查要点 |
|------|--------|---------|
| 需求完整性 | P0 | 关键业务问题是否被识别；时间范围/数据粒度/交付物是否明确 |
| 理解准确性 | P0 | Supervisor 理解与用户原话是否存在实质偏差；Topic 路由是否正确 |
| 任务拆解合理性 | P1 | 拆解步骤是否覆盖关键阶段；是否存在明显遗漏 |
| 假设显性化 | P1 | 无法确认的信息是否已标注为待确认，而非直接作为已知条件处理 |

---

## 二、方案 QC 审计输出（§五，Validator V2 直接执行）

> 格式见 `KS/KS_QC_Rules/KS_QC_Rules_v2.md`（该文件内含输出模板）

---

## 三、Check 角色质检输出（CLAUDE_pending.md §五）

```
结论: PASS / WARN / BLOCK

命中问题（无则省略）:
  - [P0/P1/P2] 章节.序号 细分类型：问题简述

日志路径: Agent.Validator/LM/Logs/{date}_{topic}_check_{n}.md
```

### 结论级别

| 级别 | 触发条件 | Validator V2 后续动作 |
|------|---------|----------------------|
| `PASS` | 无任何命中问题 | 直接返回调度方 |
| `WARN` | 仅有 P1/P2 问题 | 附带问题清单返回，不阻断 |
| `BLOCK` | 存在 P0 问题 | 阻断，上报问题清单，等待 Producer 修复后重新 spawn |

### 问题简述格式

- 章节编号：对应清单章节号（如 `§2` = 数据准确性）
- 序号：清单内条目序号（如 `2.3`）
- 细分类型：命中的子类（如 `数值矛盾`、`结论无支撑`）
- 示例：`[P0] §1.2 需求不一致：报告覆盖3月数据，需求要求2月`

---

## 三·bis、Topic6 HC1/HC2/HC3 质检输出（含 IM 推送，§4.6，由 Producer spawn）

结论级别沿用上方"三、Check 角色质检输出"的 `PASS/WARN/BLOCK` 定义，不新造级别。区别仅在于：本维度审计完成后，除 return 结果给 Producer 外，**必须额外**把结论通过 `lark-cli im` 推送进触发会话的飞书聊天（原因见 `CLAUDE.md` §4.6）。

### HC1/HC2 IM 推送文本模板

```
🔍 Validator {HC1/HC2} 质检结论: {PASS/WARN/BLOCK}
{命中问题清单，无则省略}
处理建议: {改提示词 / 改标注结果 / 无需处理}
```

发送命令（chat_id 只能从继承的 `CC_SESSION_KEY` 第二段解析，见 `.claude/rules/feishu-routing.md`）：

```bash
CHAT_ID=$(echo "$CC_SESSION_KEY" | cut -d: -f2)
lark-cli im +messages-send --as bot --chat-id "$CHAT_ID" \
  --text "🔍 Validator {HC1/HC2} 质检结论: {PASS/WARN/BLOCK}
{命中问题清单，无则省略}
处理建议: {改提示词 / 改标注结果 / 无需处理}"
```

### HC3 IM 推送文本模板

HC3 仅检查清单第 1-3 条（版块完整性/输出格式/内容质量），第 4-5 条（营销案例截图、案例归类准确性）依赖视觉判断，Validator 不判断——**"覆盖说明"行为必填，不得省略、不得暗示已覆盖第 4-5 条**：

```
🔍 Validator HC3 质检结论: {PASS/WARN/BLOCK}
{命中问题清单，无则省略}
处理建议: {改版块结构 / 改格式 / 改内容 / 无需处理}
覆盖说明: 本次检查仅覆盖第1-3条（版块完整性/格式/内容质量），第4-5条（营销案例截图、案例归类准确性）需人工核实
```

发送命令：

```bash
CHAT_ID=$(echo "$CC_SESSION_KEY" | cut -d: -f2)
lark-cli im +messages-send --as bot --chat-id "$CHAT_ID" \
  --text "🔍 Validator HC3 质检结论: {PASS/WARN/BLOCK}
{命中问题清单，无则省略}
处理建议: {改版块结构 / 改格式 / 改内容 / 无需处理}
覆盖说明: 本次检查仅覆盖第1-3条（版块完整性/格式/内容质量），第4-5条（营销案例截图、案例归类准确性）需人工核实"
```

---

## 四、System Health 健康报告输出（§七，Validator V2）

```
# Ada 系统健康报告 {date}

## 总览
- 扫描时段: {time_window}
- Auditor 结构性问题: {N} 条（P0: {n0}, P1: {n1}, P2: {n2}）
- Reflector 运行层问题: {N} 条（P0: {n0}, P1: {n1}, P2: {n2}）

## P0 问题（立即处理）
- [{来源}] {问题简述} → 改进提案已写入 pool/pending/

## P1 问题（本周处理）
- [{来源}] {问题简述}

## P2 问题（待观察）
- [{来源}] {问题简述}

## 本周已改进
- {已关闭的改进跟踪条目}
```

> 报告写入 `Agent.Validator/LM/Logs/SYSTEM_HEALTH_{date}.md`
> 飞书通知：摘要文本 + 报告文件附件，发送至触发群聊

---

**文档结束**
