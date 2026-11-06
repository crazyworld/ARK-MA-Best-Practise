---
name: topic6-event-registry
description: 将热点标题归档为标准事件、传播角度和事件关系，输出记录归属、事件表及可跨窗口延续的 Registry。支持微博、抖音、知乎、B站单平台与跨平台输入，可对证据不足的事件按需检索原文辅助复核；适用于事件归档、热点聚合、周报事件整理和跨周追踪，不负责批量采集、C0筛选或跨平台热度加总。
version: 3.0.0-rc.14
metadata:
  version: 3.0.0-rc.14
  upstream: blueai-canonical-event-registry 3.0.0-rc.14（客户 V2 bundle_20261030 内嵌版）
---

# C2 事件归档

## Topic6 / MA 用法（先读）

Topic6 只通过封装入口运行，不直接调用下方 `run_c2.py` 或编号脚本：

```sh
python /mnt/skills/topic6-event-registry/scripts/launch_c2_background.py --project-dir <项目> --mode {full|skip_sampling|demo} --run-id N
python /mnt/skills/topic6-event-registry/scripts/wait_c2_complete.py --project-dir <项目> --run-id N --wait-seconds 105
```

`run_topic6_c2.py` 把可用子集转成 input.csv，调用 `run_c2.py run --mode cross-platform --frontend fused`，
产出 `04_标注/C2_事件归档/c2_event_result_r{N}.xlsx`（row_id + 一级事件名，契约不变）。MA 适配点：

- **模型通道**：`relay.py` 走 bmc-llm-relay（`LLM_RELAY_BASE_URL/LLM_RELAY_API_KEY`，OpenAI 兼容流式 + 整体时限），
  不读 `ANTHROPIC_*`，不回退 `ARK_*`。Chat 默认 `$C2_CHAT_MODEL_ID`（DeepSeek-V4.1-Flash），Embedding `$EMBEDDING_MODEL_ID`。
- **无人值守**：`--review-policy record` 让四道审核门禁只落 `review_requests/*.json` 并汇总进
  `out/c2_quality.json.review_pending`，由 HC1/HC2 人工复核，不返回 WAITING_REVIEW；成员边界带由模型裁定（x2 `--verdict` + `--model`）。
- **降级不阻断**：`--degraded-policy accept` 让 07 块级降级 / 03 实体漏返继续产出，明细保留在 `c2_quality.degraded_ids`。
- **提速**：F1 融合（00+01+02 一次调用）、F1 两平台并行（`--front-workers 2 × --max-requests 8` = 16 路）、03∥04 并行；run_c2 阶段级 checkpoint
  + 内容寻址缓存，同签名重跑只补失败节点。
- **未启用**：按需原文补证（`supplement` / `--supplement-bundle`，需数说凭据），代码随包保留。
- 下文 rc.14 原文中的 `ANTHROPIC_*`、`~/.claude/skills`、Claude 模型名均为上游 CC 环境说明，MA 以本节为准。

rc.14：最终名称审核发现误合或无法确认时，可用 `python scripts/run_c2.py revise --source-run 原运行 --run-dir 新目录 --retention-plan 审核计划.json` 在新目录整组拆回原热点。计划必须含source_review_id、reviewer、note、retain_events（每项event_id、完整member_record_ids、reason）。仅接受rc.13/rc.14已完整计算且停在event_names的当前窗口运行，校验源产物、前置审核和未改计算脚本哈希；不改原运行，不重跑模型，不解决技术降级。修订耗时与复用上游耗时分开记录，不能称冷启动。详见[末端审核修订](references/final-review-revision.md)。

rc.13：拆分复核返回错误事件ID或成员覆盖不全时，将精确ID约束反馈给模型，最多追加2次重新裁定；不自行猜测ID、不放宽覆盖校验，持续失败仍阻断。修复来自rc.12真实隔离运行，尚不等于本版全流程真实验收。

完整 Skill，使用统一入口完成输入检查、融合事实抽取、实体与向量并行、候选召回、有界归档、成员筛查、先合并后拆分复核，以及原有事件/角度/关系/Registry 与七列明细输出。原逐阶段脚本与 00/01/02 路线仍保留作兼容和对照。

rc.12 接受“未能识别就保留原热点”：完成既定归档与复核后，仍为 UNCERTAIN 或 needs_evidence 的记录，按原标题独立输出，不要求100%合并。若合并组含这类记录且没有逐成员解除未决的证据，整组保守拆回原热点，清空未经确认的身份、描述、角度和事实，保留热度及原行。未决保留本身不阻断最终输出；明确事实错误、误排、技术失败和结构错误仍需处理。具体输出及审核口径见 [容错与原热点保留](references/original-retention.md)。

rc.11 保留原标题锁定，并将定向补证重判接在 NON_EVENT 审核与排除之前。默认 fused 初判必须明确支持排除；缺乏上下文时保留 UNCERTAIN。补证重判从零重算 Frame，后续发现误排可用 --reuse-front-run 在新目录回流。经实际审核仍未决的记录可在bundle中加review_holds，程序强制保留UNCERTAIN，模型不能自行解除。重判若提出排除原EVENT/UNCERTAIN，先转UNCERTAIN待审，不自动删除。原运行与审核不覆盖。具体边界及命令见 [前置补证与回流](references/front-evidence.md)。此修复不代表最终归档已准确。

本版为待真实数据验收的 RC：已做本地模拟服务与合成样本的全流程验证；真实模型质量、成本、耗时和 T6 生产接入未验收。不要引用历史 references 中的作者实测数字作为本版结果。

## 运行前

需要 Python 3.10+ 与 numpy（见 requirements.txt）。用 `python scripts/check_environment.py` 检查本地依赖与配置存在性；它不验证服务，不输出凭证。根据当前任务授权使用既有模型环境；不自动安装依赖、付费调用、覆盖已有全局 Skill 或改生产。

Chat 使用 ANTHROPIC_BASE_URL 和 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN；Embedding 使用 EMBEDDING_BASE_URL / EMBEDDING_API_KEY，缺省沿用 Chat 配置。配置存在不等于费用授权，密钥只放环境中。

输入 CSV 为调用方指定的分析范围，包含 record_id 或 row_id、title，以及 platform（或单平台 --platform）。ID 全局唯一且非空，保留原列与源行序。热度可选，用 --heat-col 显式指向实际列，如 hot_index；不能把缺失当零或推定单位。XLSX 的导入/导出仍由 T6 外层承担。C0 子集由上游提供，Skill 不自动筛选营销可用性。

## 统一入口

默认打印计划，不发请求：

```sh
python scripts/run_c2.py run --input input.csv --run-dir runs/week1 --mode single --platform 微博 --heat-col hot_index --window 2026W39
```

已获当前任务真实服务授权后追加 --execute。正式对照应显式指定 --chat-model、--embedding-model、--chat-revision 和 --embedding-revision，使用固定模型修订与同一输入。新运行目录不可与原始材料或既有输出混用。

- 跨平台：`--mode cross-platform`，保留输入中的四平台记录。先按平台融合抽取，再确定性合流，不对平台原始热度加总；统一跨平台结果不生成误导性的混池排名。
- 原三阶段对照：`--frontend legacy`；默认 `--frontend fused`。两路保留相同原始行和身份口径。
- 跨窗口：增加 `--previous-registry previous/out/registry_snapshot.jsonl`。先完成本周归档，再召回历史候选、由模型裁定延续关系；匹配时保留历史 event_id 与身份核，未触及历史保留在快照，周内明细只含本次输入。
- `--max-requests` 控制阶段内请求并发；E/V 共享此上限。它不是服务端 RPM/TPM 配额控制。平台前端目前依次调度，平台内批次并发。
- `--cap` 与 `--out-budget` 控制候选块范围，保留 skipped_edges。图或向量绝不直接决定事件归属。

事件身份与非事件判据见 [identity.md](references/identity.md)。不要把“同一人/同一品牌/同题材”当作同一事件；不确定保留 UNCERTAIN。

## 按需原文补证

当标题无法确定节目/作品、主体动作或事件边界，或审核发现名称无出处、同人误合时，先生成绑定原始记录 ID 的定向查询计划，再取对应平台原文。不要为所有记录默认补证。使用 [supplement.md](references/supplement.md) 的计划结构、回溯规则和命令。

`run_c2.py supplement` 提供独立轻量入口，只请求原文，最多3并发、请求起始间隔0.5秒；使用既有真实结果回放可离线验证。取数与模型运行分别记录耗时。沿用原数说Skill接口配置，包括用户已授权的HTTP地址；环境变量可覆盖，本机私有配置只读，不复制到包内。不自动升级协议或迁移域名。

`run --supplement-bundle evidence_bundle.json` 先对绑定的原记录执行前置重判，再将同一证据交给x3合并/拆分复核。即使初判 NON_EVENT 也能恢复为 EVENT/UNCERTAIN 并重算 Frame。它不增加输入记录、不把查回帖子计入热度或成员数。空结果与证据不足保留 UNCERTAIN，服务/校验失败明确停止；仍须审核，不能把取数成功当语义通过。关键词计划和相关性选择由调用 Agent 完成，不默认全量取数或无限循环。晚到证据在新目录修订，不能热改旧运行。

## 审核与恢复

流程在必要门禁返回 WAITING_REVIEW（退出 3），此时不会生成正式 run-dir/out：

1. cleaning：fused 路线核对全部输入与 title/clean_title 的 ID 和原文相等，并核对回退/重算记录；legacy 路线仍需逐条语义核对清洗改动，尤其数字、主体和情态。
2. non_events：前置补证重判已执行，核对 out/front_evidence_review.json、更新后的事件性与 Frame，并抽检剩余非事件。out/front_evidence_pending.csv 列出 UNCERTAIN；这些记录后续按原热点保留，可在核对该处置后通过审核，无需逐条证明是何事件。发现误排或已确认事实错误仍须修正，可按前置补证说明建立新修订运行。
3. 成员边界带：默认输出待裁定 CSV。填写“属于/不属于”并通过 --member-verdict 传回，不能用空表跳过。也可在首次 run 配置 --verdict sonnet 或 opus，需包含该模型调用授权。
4. event_names：仅原热点保留项无需再次停等；其他名称人工复核项及已采纳补证的结果仍核对原成员证据。发现已确认错误须修正。保留原热点不等于确认了其事件身份。

完成实际审核后记录当前门禁的确认：

```sh
python scripts/run_c2.py review --run-dir runs/week1 --gate cleaning --reviewer 审核人 --note "核对范围与具体结论"
python scripts/run_c2.py resume --run-dir runs/week1
# 非事件审核同理：--gate non_events
# 成员裁定文件用：
python scripts/run_c2.py resume --run-dir runs/week1 --member-verdict filled.csv
```

不要仅为跑通流程自行填入审核确认。确认绑定当次证据哈希，材料改变后失效。需修改输入/规则/模型/程序时开新运行目录，不复用已固定的 run 配置。

每阶段独立目录、尝试日志、输入/配置/代码/规则文件哈希和输出校验。失败可按原配置 resume；有效阶段复用 checkpoint，失败批次独立缓存恢复。--reuse-front-run 仅导入经校验的原前端判断，必须带补证并使用新目录，不继承审核与下游结果；这是修订运行，不能作为冷启动速度。run_c2 默认严格复核，x2/x3 漏返或失败不能静默当成功；07 技术降级保留但阻止正式输出。旧脚本的宽松退出语义保留，单独运行 x4 时用 --strict-quality 显式启用严格出口。

F1 的标题保留与局部恢复见 [F1-experiment.md](references/F1-experiment.md)：模型改写标题、漏返或 schema 异常时，按完整原标题局部重做事件性与 Frame；无法恢复则明确失败。legacy 为旧三阶段对照，仍使用原模型清洗，不包含本次 fused 标题锁定修复。

## 结果与接口

读取 run-dir/result.json，只有 status=COMPLETE 且 outputs 哈希相符才接收 run-dir/out。COMPLETE 表示本次执行和必要审核门禁完成，不表示统计质量或性能已达标。

保留原业务产物：

- 07 归档明细和记录级归属，x4 的热点明细_含事件归属.csv、事件清单.csv。
- event_registry.csv、record_to_event.csv、relation_graph.csv、non_events.csv、human_review.csv。
- 单平台的事件排名.csv、热度明细.csv。
- registry_snapshot.jsonl、output_manifest.json、口径与执行说明.md。

七列明细保持：事件ID、一级事件名、二级角度、事件成员数、归属置信度、状态、说明。NON_EVENT 可为空事件；技术缺失须另行报告。重复成员、未知 ID 和成员数冲突阻断输出。

原热点保留项使用“独立热点”状态、原标题作一级事件名、成员数1、空角度及空置信度，说明明确标记未决保留。out/original_retained.json 列出原因和受影响记录；c2_quality.json 单列 original_retained_ids 及 business_disposition=ACCEPTED_WITH_ORIGINALS。跨窗口快照保留原热点标记，暂不将其当成已确认事件身份参与历史归并。

新增 c2_quality.json、c2_provenance.json、c2_run_metrics.json 提供质量状态、输入/模型版本和阶段台账；失败请求费用可能未知，零价格占位不能解释为免费。历史窗口 identity_core 缺失时不能假定已经完整恢复历史事实。

恢复状态：COMPLETE 退出 0；WAITING_REVIEW 退出 3；FAILED/DEGRADED 退出 2。原 x4 默认完成导出仍退出 0，质量 sidecar 与提示始终保留，避免悄然改变旧调用方。

## 验收边界

本版交付可独立安装/调用的完整软件包，不是仅 E/V 实验模块。测试使用合成样本与本地模拟服务，安装包包含自身所需源码和规则，不依赖研发目录或原 Skill 安装位置。

W39 用于开发；W40 已暴露问题并用于修复验证，后续均称回归，不再称未接触的独立留出验证。正式验收仍需要同一 C0 子集、未用于调参的数据及完整产出审核。通过前保持 RC 状态，不替换生产。实体归一仍使用每次运行的顺序累积注册表。
