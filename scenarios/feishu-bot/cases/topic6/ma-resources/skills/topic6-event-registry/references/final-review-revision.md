# 末端审核修订

调用Agent完成实际审核后可撤回已发现的误合或无法确认的名字，按整组原热点保留。不能用未经阅读的自动批准代替审核。计划绑定原event_names请求id和每组完整成员集合，reason记录具体问题。旧输入、模型结果和失败耗时保持。

只接受当前窗口且source-run处于WAITING_REVIEW/event_names；cleaning/non_events审核、阶段完成和产物哈希均须有效。前置计算代码必须与支持的来源版本相同，技术缺失、降级、重复归属不可通过此入口豁免。普通run仍完整支持单平台、跨平台与跨窗口；新增revise入口暂不处理跨窗口历史重关联。

新目录的review_retention、registry、detail、finalize各自计时。输出review_revision_audit.json和provenance记录上游版本/请求/审核计划哈希。保留已有原热点审计，单列manual_retained_count，不把手动撤回计作模型识别正确。未被改动的业务事件保持；被拆原组及关联关系撤回，原行、热度、顺序保持。若仍有系统flagged名称则不输出COMPLETE，需要另作实际审核修订。

示例计划：
```json
{"source_review_id":"来自review_requests/event_names.json的id","reviewer":"实际审核人","note":"审核范围","retain_events":[{"event_id":"e1","member_record_ids":["r1","r2"],"reason":"不同城市演唱会不能合为同一场"}]}
```
