---
name: validator
version: 1.0.0
description: 通用质量验证 Agent 知识库。承载检查清单、验证规则、审计能力定义和辅助脚本。由 Validator sub-agent 在 HC 卡点前按 task 路由加载对应规则执行审计。
---

# Validator · 通用质量验证 Agent 知识库

> **版本**: v1.0
> **打包名**: validator
> **适用角色**: Validator sub-agent (由各 Topic Coordinator 委派)
> **挂载路径**: `/mnt/skills/validator/`

---

## 一、Skill 定位

本 Skill 是 Validator sub-agent 的**知识库与工具集**，承载：

1. **KS (Knowledge Source)** — 检查清单、验证规则、QC 规则
2. **prompts** — 分层能力定义（L1 核心身份、L2 审计能力、L3 行为规范）
3. **LM (Long-term Memory)** — 审计案例库、校准记录、误报记录
4. **scripts** — 辅助检查脚本（确定性数据验证）

Validator agent 的 system prompt 定义在 `ma-resources/agents/validator.system.md`，
不在本 Skill 内。本 Skill 只提供 agent 运行时按需 `cat` 读取的知识文件。

---

## 二、目录结构

```
validator/
├── SKILL.md                        # 本文件
├── KS/                             # 知识源
│   ├── KS_README.md
│   ├── KS_Checklists/              # 检查清单（按 Topic/场景分）
│   │   ├── Topic6_热点报告检查清单.md
│   │   ├── 行业报告检查清单.md
│   │   ├── 商业问题检查清单.md
│   │   └── 品牌档案检查清单.md
│   ├── KS_Validation_Rules/        # 通用验证规则
│   │   ├── 数据一致性规则.md
│   │   ├── 逻辑完整性规则.md
│   │   ├── 格式规范规则.md
│   │   ├── 商业问题规则.md
│   │   └── 脚本验证规则.md
│   ├── KS_QC_Rules/                # 方案 QC 规则
│   │   └── KS_QC_Rules_v2.md
│   └── KS_Check_Rules/            # Check 角色细化规则
│       ├── Check_格式规范规则.md
│       ├── Check_逻辑完整性规则.md
│       └── Check_数据一致性规则.md
├── prompts/                        # 能力定义
│   ├── _index.md
│   ├── output_formats.md           # 输出格式规范
│   ├── L1_核心/核心身份.md
│   ├── L2_能力/
│   │   ├── 数据审计.md
│   │   ├── 脚本审计.md
│   │   ├── 格式审计.md
│   │   ├── 逻辑审计.md
│   │   ├── 意图理解审计.md
│   │   └── 商业问题审计.md
│   └── L3_规范/
│       ├── 外部验证.md
│       ├── 阻断上报.md
│       ├── 审计日志.md
│       └── 审计报告格式.md
├── LM/                             # 长期记忆
│   ├── LM_README.md
│   ├── 审计案例库.md
│   ├── 校准记录.md
│   └── 误报记录.md
└── scripts/                        # 辅助脚本
    └── xlsx_checker.py             # pandas 确定性数据检查
```

---

## 三、使用方式

Validator agent 在 system prompt 中按 task 路由，动态读取对应的知识文件：

```bash
# 示例：HC1 审计时读取检查清单
cat /mnt/skills/validator/KS/KS_Checklists/Topic6_热点报告检查清单.md

# 示例：加载数据审计能力
cat /mnt/skills/validator/prompts/L2_能力/数据审计.md

# 示例：调用确定性检查脚本
python /mnt/skills/validator/scripts/xlsx_checker.py \
  --file /mnt/session/outputs/.../wide_table_full_r1.xlsx \
  --task hc1
```

---

## 四、扩展指南

新增 Topic 的检查清单：
1. 在 `KS/KS_Checklists/` 下新增 `{Topic}_检查清单.md`
2. 在 Validator agent 的 system prompt 路由表中加一行
3. 重新打包本 Skill 并上传

新增审计能力：
1. 在 `prompts/L2_能力/` 下新增 `{能力名}.md`
2. 在路由表中引用
3. 重新打包并上传
