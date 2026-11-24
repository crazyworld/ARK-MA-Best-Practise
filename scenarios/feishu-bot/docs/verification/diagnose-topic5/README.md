# 验证:排障 Bot 适配 crazyworld 版 topic5

- 目的:确认排障 Bot 的 `PipelineJobSource` 能读 crazyworld/main 的 topic5 `pipeline_jobs`(含 `task_id`、`stop_requested_*` 等新增列)。
- 环境:基于 crazyworld/main(7702570)+ 排障改动;临时目录 SQLite,无需密钥。
- 执行:`PYTHONPATH=$PWD:$PWD/cases python docs/verification/diagnose-topic5/verify_topic5_adapter.py`(在 scenarios/feishu-bot 下)
- 输出:`PASS: topic5 pipeline_jobs 可被排障 adapter 读取(list_refs / resolve / 群范围过滤)`
- 结论:适配器按存在的列读取,与 topic5 现有表结构兼容;`list_refs` 的群范围过滤、按 session/job id 的 `resolve` 均正常。
- 日期:2026-11-24
- 资源清理:仅临时目录,脚本退出自动删除;未触碰真实库与云资源。
- 未覆盖:端到端(真实 topic5 任务 + 排障 Bot 问答)需部署后在飞书验证;要让它生效,排障 `config.env` 需配置 `TOPIC5_PIPELINE_DB_PATH` 指向 topic5 的库(与 topic5 gateway 同机)。
