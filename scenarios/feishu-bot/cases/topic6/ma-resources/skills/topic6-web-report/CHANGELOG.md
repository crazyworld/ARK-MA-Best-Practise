# 变更记录

## 2026-11-06（MA 适配，版本号不变）

- `build-report.mjs`：周刊/月刊只按页头「数据周期」的周期标签判定（`YYYY-Www` → 周刊；`YYYY年M月` 等 → 月刊）。
  此前扫描文件名 + 全部正文，W44 正文「2026年10月车企销量…」被误判为月刊。周期解析提前到文件头，缺失仍报错。
- `SKILL.md` 第 5/9/10 步、`CLAUDE.md`、`assets/source/DESIGN_SPEC.md`、`agents/openai.yaml`：删除「必须上传 HTML
  snapshot」指令，与 coordinator「禁止执行 upload-html.mjs」对齐（W44 Agent 照 skill 上传、无 key 后升级为阻断）。
  `upload-html.mjs`、`references/html-snapshot-upload.md` 保留不引用。

## 1.1.0 — 2026-09-24

- 脚本挂载路径:`<skill-directory>/scripts/*.mjs` 在部署环境下解析为
  `/mnt/skills/topic6-web-report/scripts/`(调用方按 skill 挂载路径拼接,无需改脚本代码)。
- 无脚本代码逻辑改动、无参数/环境变量增减、无模板资产替换。
