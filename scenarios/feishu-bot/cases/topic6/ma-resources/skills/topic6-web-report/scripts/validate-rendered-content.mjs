#!/usr/bin/env node

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const [sourceInput, htmlInput] = process.argv.slice(2);
const sourcePath = resolve(sourceInput ?? "");
const htmlPath = resolve(htmlInput ?? "");

if (!sourceInput || !htmlInput || !existsSync(sourcePath) || !existsSync(htmlPath)) {
  throw new Error("Usage: node validate-rendered-content.mjs /absolute/path/source.json /absolute/path/index.html");
}

const blocks = JSON.parse(readFileSync(sourcePath, "utf8"));
const html = readFileSync(htmlPath, "utf8");

function cleanText(value = "") {
  return String(value)
    .replace(/[\u200b\ufeff]/gu, "")
    .replace(/\s+/gu, " ")
    .trim();
}

function comparableText(value = "") {
  return decodeEntities(value)
    .replace(/[\u200b\ufeff]/gu, "")
    .replace(/\s+/gu, "");
}

function decodeEntities(value = "") {
  return value
    .replace(/&quot;/gu, '"')
    .replace(/&#39;/gu, "'")
    .replace(/&amp;/gu, "&")
    .replace(/&lt;/gu, "<")
    .replace(/&gt;/gu, ">");
}

function sourceLines(value = "") {
  return String(value)
    .replace(/[\u200b\ufeff]/gu, "")
    .split(/\r?\n/gu)
    .map((line) => cleanText(line).replace(/^(?:[-•]\s*)/u, ""))
    .filter((line) => line.length >= 4);
}

const renderedVisibleText = comparableText(html.replace(/<[^>]*>/gu, ""));
const renderedMarkupText = comparableText(html);
const activeBlocks = blocks.filter((block) => !block?.hidden);
const errors = [];

function renderedContains(sourceText) {
  const expected = comparableText(sourceText);
  return expected && (renderedVisibleText.includes(expected) || renderedMarkupText.includes(expected));
}

if (/blob:/iu.test(html)) errors.push("Rendered HTML still contains a browser-session blob URL");
if (/<p(?:\s[^>]*)?>\s*•(?:\s|<)/u.test(html)) errors.push("Rendered prose contains a standalone bullet marker");
if (/<ul class="cell-list">[\s\S]*?<li>\s*[•·|｜-]\s*/u.test(html)) errors.push("Rendered data-card list contains a decorative text prefix");
if (/(?:^|\n)\.cell-list li::before\s*\{[^}]*content:\s*["']?•/mu.test(html)) errors.push("Rendered plain data-card list still draws decorative bullets");
if (/\.data-card-grid--industry-ranking \.data-card-field--drivers \.cell-list li\s*\{[^}]*border-left:\s*(?:[1-9]\d*px|thin|medium|thick)/su.test(html)) errors.push("Rendered main-driver list still draws vertical separators");
if (!/\.data-card-grid--industry-ranking \.data-card-field--drivers \.cell-list li::before\s*\{[^}]*content:\s*"•"[^}]*color:\s*inherit/su.test(html)) errors.push("Rendered main-driver list is missing its same-color bullets");
if (!/\.data-card-grid--industry-ranking \.data-card-field--drivers \.cell-list li:first-child\s*\{[^}]*padding-left:\s*10px/su.test(html)) errors.push("Rendered first main-driver item is missing its bullet inset");
if ([...html.matchAll(/<article class="data-card data-card--marketing-finding"[\s\S]*?<\/header>/gu)].some(([header]) => /data-card-kicker/u.test(header))) errors.push("Rendered brand-collaboration card still contains an event-type pill");

const caseBlocks = activeBlocks.filter((block) => /docx-grid-block/u.test(String(block?.className ?? "")));
const renderedCaseCount = (html.match(/class="case-card case-card--visual"/gu) ?? []).length;
if (renderedCaseCount !== caseBlocks.length) {
  errors.push(`Marketing-observation case count mismatch: source=${caseBlocks.length}, rendered=${renderedCaseCount}`);
}

for (const block of caseBlocks) {
  const title = sourceLines(block.text)[0] ?? "";
  if (!title || !renderedContains(title)) errors.push(`Missing rendered case title: ${title || "(untitled case)"}`);
}

for (const block of activeBlocks) {
  const className = String(block?.className ?? "");
  if (/docx-divider-block|docx-grid-block|quote_container/u.test(className)) continue;

  if (/docx-table-block/u.test(className)) {
    for (const row of (block.tables ?? []).flat()) {
      for (const cell of row ?? []) {
        for (const line of sourceLines(cell)) {
          if (!renderedContains(line)) errors.push(`Missing rendered table content: ${line.slice(0, 80)}`);
        }
      }
    }
    continue;
  }

  for (const line of sourceLines(block.text)) {
    const normalizedHeading = /docx-heading[12]-block/u.test(className)
      ? line.replace(/^(?:(?:第?[一二三四五六七八九十]+)|(?:\d+(?:\.\d+)*))[、.]\s*/u, "")
      : "";
    if (!renderedContains(line) && (!normalizedHeading || !renderedContains(normalizedHeading))) {
      errors.push(`Missing rendered text: ${line.slice(0, 80)}`);
    }
  }
}

for (const block of activeBlocks) {
  for (const link of block?.links ?? []) {
    const href = String(link?.href ?? "").trim();
    if (href && !html.includes(href)) errors.push(`Missing rendered source link: ${href}`);
  }
}

// ---- 布局断言(1027 分析师测试 MA-01~06):内容全在但布局退化也必须失败 ----
const mainHtml = html.slice(Math.max(0, html.indexOf("<main")));
function listGridFields(label) {
  const re = new RegExp(`<div class="data-card-field[^"]*data-card-field--list-grid[^"]*"><dt>${label}</dt><dd>([\\s\\S]*?)</dd></div>`, "gu");
  return [...mainHtml.matchAll(re)].map((m) => m[1]);
}
for (const [label, code] of [["主要驱动话题", "MA-01"], ["具体热搜", "MA-02/03"]]) {
  const fields = listGridFields(label);
  // 单值字段(只有一条热搜)允许不成列表;多值(含 <br>)未渲染为 cell-list 即退化为单列
  const degraded = fields.filter((f) => !f.includes('<ul class="cell-list">') && /<br\s*\/?>/u.test(f));
  if (degraded.length) errors.push(`${code} ${label}: ${degraded.length}/${fields.length} 个多值字段未渲染为 cell-list 网格(退化为单列)`);
}
// MA-04:页面上「案例 N：」标题数必须等于左文右图卡片数(旧模板会退化为 heading+bullet)
const renderedCaseTitles = (mainHtml.replace(/<[^>]+>/gu, "\n").match(/^\s*案例\s*\d+\s*[：:]/gmu) ?? []).length;
if (renderedCaseTitles && renderedCaseTitles !== renderedCaseCount) {
  errors.push(`MA-04 营销观察: ${renderedCaseTitles} 个案例只有 ${renderedCaseCount} 个渲染为左文右图卡片`);
}
// MA-05:源数据「触发事件」带链接时,页面上每条「触发事件」都必须包含 source-link
const insightSourceHasLinks = activeBlocks.some((block) => /触发事件/u.test(String(block?.text ?? "")) && (block?.links ?? []).length);
const triggerItems = [...mainHtml.matchAll(/<li[^>]*>(?:(?!<\/li>)[\s\S])*?触发事件[\s\S]*?<\/li>/gu)].map((m) => m[0]);
const unlinkedTriggers = triggerItems.filter((item) => !/source-link/u.test(item));
if (insightSourceHasLinks && unlinkedTriggers.length) {
  errors.push(`MA-05 消费洞察: ${unlinkedTriggers.length}/${triggerItems.length} 条「触发事件」热搜未渲染为超链接`);
}
const footerLabel = mainHtml.match(/<aside class="footer-note"[^>]*><strong>([^<]*)<\/strong>/u)?.[1] ?? "";
if (!/^热度值说明/u.test(decodeEntities(footerLabel))) errors.push(`MA-06 尾注应为「热度值说明」,实际为「${footerLabel || "(无)"}」`);

if (errors.length) throw new Error(`Rendered-content validation failed:\n- ${[...new Set(errors)].join("\n- ")}`);
console.log(`Validated complete rendered coverage for ${activeBlocks.length} source blocks and ${caseBlocks.length} case cards.`);
