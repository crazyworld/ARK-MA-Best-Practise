"""ada 全链路可观测(通用层)。

把 task / job / MA session 事件还原为统一的 trace 模型,供 CLI、HTTP 接口、看板与
诊断 Agent 共用。本包不得 import ``cases/*``:各 topic 通过 ``adapters`` 接入。
规划见 ``docs/OBSERVABILITY_PLAN.md``。
"""
