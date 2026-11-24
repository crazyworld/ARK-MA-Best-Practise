# XLSX 回车符导出失败的恢复（v0.11.6）

适用：v0.11.5 或更早版本在正文含 CRLF（`\r\n`）或 CR（`\r`）时，出现 `EXPORT_CELL_VALUE_MISMATCH` / `EXPORT_FAILED_RESUMABLE`，而下载页面已经完整。普通 LF（`\n`）换行仍直接显示。

## 原因与修复

openpyxl 3.1.5 未使用可选 lxml 后端时，写出的回车字符在 XML 读回时被规范化为 LF；逐格核验因此拒绝交付。不是帖子数据错误，也不是供应商任务失败。不同后端的行为不同，不宜称为所有环境必然失败。

v0.11.6 将含 CR 的文本交给既有 `text_overflow` 无损通道：主表显示内容位置提示，辅助表保存带顺序和 SHA256 的 ASCII JSON 分片，解码后逐字符比对原值；`records.json` 保留原文。不能用删除回车或放宽逐格核验来“修复”。这不要求安装 lxml。

## 恢复原任务

1. 停止该任务的并发执行，保留完整原工作目录、页面、状态、导出清单、临时文件及原登记库。用完整 v0.11.6 包作为执行程序；不要用包内示例覆盖原任务配置。
2. 核对原 `state.json` 中的 job_id / request_id、原账户、原配置和 registry_path。找到含 `state.json`、`pages/manifest.json` 的实际运行目录；`pages/manifest.json` 的 complete 必须为 true。缺失或未完成时不把下述操作视为纯本地导出。
3. 保持原查询、账户、API 配置、输出 XLSX 路径和模板内容。若之前尝试 CSV，先恢复原 XLSX 输出路径。不要删除或编辑 `export.json`、手动把状态改成 COMPLETED，也不要删除部分发布的目标文件绕过冲突。
4. 用原来的解释器和同一套凭证配置执行下面命令，替换所有占位符为实际路径；含空格的路径加引号。

```text
python NEW_SKILL_DIR/ssjh_social_task.py --action resume --config ORIGINAL_TASK_CONFIG --api-config ORIGINAL_API_CONFIG --run-dir ORIGINAL_RUN_DIR --registry ORIGINAL_REGISTRY
```

下载完整且绑定/哈希校验通过时，程序只校验已有页面并重做本地导出，不查声量、不新建任务、不重下载。恢复时会重新生成质量报告，其哈希改变是预期行为；最终 result 将绑定新报告。表格契约仍是 `v0.11.1-lossless-xlsx-json-chunks`，原 PREPARING 清单可继续使用，无需迁移或手工改指纹。

5. 确认状态为 COMPLETED、job_id 保持原值、downloaded 与原任务量一致、export.json 为 PUBLISHED，且 result 中的 XLSX / records.json 文件及质量报告存在。完成状态由程序在核验后写入，文件存在本身不等于完成。
6. 如需 CSV，完成原 XLSX 后，在任务配置中仅将输出改为一个新的 `.csv` 路径，再用相同参数执行 `--action export`。CSV 中 CR/CRLF 原样保留；多行字段按 CSV 引号规则读取，不能按物理行数计算记录数。

未完成阶段直接换格式/路径仍会被门禁阻断。这是保护已付费任务现场的约束；本补丁未开放失败状态下任意改导出契约。若仍报错，保留错误码和原目录交维护方，不重建任务。

## 问题报告任务的核对点

报告中的任务是 `23952008` / `bluefocus-xhs-20261105-v0115-t1`，工作目录为 `~/ssjh-workspace/bluefocus-xhs-20261105-v0115/`。应由原机器上的执行 Agent 定位其中真实配置和 run 目录，不能把该工作目录直接假定为 run 目录。预期恢复 27 条，预检 24 条的差异不需手工改成一致。

本版已用冻结旧包及 27 条合成数据（2 页）离线复现失败，再验证新版恢复、缓存和 CSV 导出，禁用网络并核对页面哈希。维护方未取得上述 macOS 原运行目录，因此不宣称任务 23952008 已在原机恢复，也未验证真实 Excel 的视觉展示。
