# F1 融合抽取与对照

3.0.0-rc.9 的 scripts/f1_extract.py 接入 run_c2 默认 fused 路线。legacy 路线仍调用原 00/01/02，其模型清洗行为保留用于兼容/对照。

原标题由程序锁定，clean_title 原样复制 title（含空格、换行、标点和话题/署名）。模型只输出每条的 eventness/reason_code/confidence/frame，不再加载旧的删词清洗提示词。兼容模型回传 clean_title 时必须与输入逐字一致；不能先接受改写后的Frame再把标题补回来。NON_EVENT 必须 frame=null，其他记录必须有合法 Frame；information_score 沿用本地公式。

回传改写标题、漏返/重复/schema 错误时，仅对受影响记录使用完整原标题重新请求事件性与 Frame。修改标题的尝试记录为 MODEL_TITLE_CHANGE；第二次输出仍不合法即停止该批次，不偷偷补为 NON_EVENT。批缓存绑定完整输入与版本；更换模型、Prompt 或源材料不能复用旧结果。

标题全量比对与NON_EVENT抽检门禁保留。新增exclusion_supported/needs_evidence字段，信息不足不能直接排除；缺乏明确排除支持的NON_EVENT转UNCERTAIN。补证在F1后、non_events审核前重判绑定记录并从零重算Frame，x3仍使用相同证据。晚到证据可通过新修订运行回流，详见[前置补证](front-evidence.md)。没有改标题不代表Frame或归档正确。

真实 A/B 使用同一 C0 子集和固定模型修订、配额。W39 用于开发；W40 已在rc.7独立验证中暴露清洗缺陷，后续测试标为回归。分别记录冷/热缓存、调用量、token、耗时、技术降级及人工错误类型；没有请求级排队/重试分解时明确标未知。旧输出只是对照，不当金标准。
