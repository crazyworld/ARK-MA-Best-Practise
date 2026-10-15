# Insight 能力知识库索引

> **版本**: v1.0（2026-06-26）
> **定位**: System Health Mode — Insight Lane 知识源
> **用途**: 使用提效教练，输出可直接使用的操作卡片（快捷键 · 上下文技巧 · 沟通范式 · 最佳示范）

---

## 目录结构

```
prompts/
├── _index.md              # 本索引文件
├── shortcuts.md           # Claude Code 快捷键 & 操作技巧
├── context_tips.md        # 上下文管理技巧（压缩 · 注入 · 会话结构）
├── patterns.md            # 沟通范式 & prompt 模板（指令结构 · 角色切换 · 任务拆解）
└── examples.md            # 最佳示范（高效对话样本 · 反例对照）
```

> V1 操作提示词（L1_核心/ · L2_能力/ · L3_规范/）已迁入 CLAUDE.md，保留原目录备查。

---

## 触发条件

`check_context.mode = "system_health"` 且 `scope = "insight"` 或 `scope = "full"`

## 输出格式

飞书消息卡片，每条 Insight 一个 tile，可直接复制操作。不写入 pool/pending/，不产出改进提案。

---

**文档结束**
