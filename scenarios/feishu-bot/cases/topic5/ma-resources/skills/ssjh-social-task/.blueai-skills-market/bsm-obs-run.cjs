#!/usr/bin/env node
/* eslint-disable @typescript-eslint/no-require-imports */
/**
 * BlueAI Skills Market bsm-obs-run.cjs Wrapper 参考实现
 *
 * 定位：脚本/CLI 启动代理、静态节点识别、透明转发、结果保持。
 * 不负责：Agent 规划、LLM 推理、内容脱敏、云端聚合、MCP 协议代理、完整生命周期管理。
 *
 * 用法：
 *   bsm-obs-run.cjs run <node_id> [--interpreter <x>] -- <business_arguments...>
 *                                                      静态节点：目标命令由 manifest 提供，
 *                                                      --interpreter 覆盖 script 节点解释器
 *   bsm-obs-run.cjs external -- <command...>                   动态外部调用：完整命令由调用方给出
 *   bsm-obs-run.cjs validate                                   干跑校验：加载 manifest 并逐节点
 *                                                      校验命令可解析性与 skill_version
 *                                                      一致性，不执行任何业务命令（供接入
 *                                                      流程实测无法触达敏感状态的节点时做
 *                                                      交付前验证）
 *
 * 行为契约：
 *   - 目标命令在调用方当前工作目录启动，路径解析语义与原命令一致
 *   - stdin/stdout/stderr 原样透传，成功与业务失败时不输出任何 wrapper 日志
 *   - 目标进程退出码/信号原样返回（信号按 128+signum 惯例）
 *   - SIGINT/SIGTERM/SIGHUP/SIGQUIT 转发到子进程
 *   - wrapper 自身错误输出 `[bsm-obs-run wrapper_error]` 前缀，退出码 64-69，与业务失败可区分
 *   - run 模式发现 manifest skill_version 与 SKILL.md metadata.version 漂移时，
 *     向 stderr 输出一行告警后继续执行，不改变业务退出码
 *   - 不采集业务输入输出与环境变量，不做任何网络上报
 *   - manifest 解析与内容校验 fail-closed：结构非法、字段缺失、node_id 重复、
 *     parent_node_ids 悬空引用或成环、command 含 shell 元字符/越界，一律拒绝执行
 *
 * 参考实现版本：2.3.0（支持 spec_version 1.1）
 * 平台正式版就绪后可整体替换本文件。
 */
const { spawn } = require('node:child_process');
const { existsSync, readFileSync } = require('node:fs');
const path = require('node:path');

const SUPPORTED_SPEC_VERSIONS = ['1.1'];

const WRAPPER_EXIT = {
  USAGE: 64,    // 命令行用法错误
  MANIFEST: 65, // manifest 不存在 / 格式错误 / spec_version 不支持 / 内容校验失败
  NODE: 66,     // node_id 不存在或节点定义非法
  BOUNDARY: 67, // 目标越过 skill 包目录安全边界
  SPAWN: 69,    // 目标命令无法启动
};

const SIGNAL_NUM = { SIGHUP: 1, SIGINT: 2, SIGQUIT: 3, SIGKILL: 9, SIGTERM: 15 };

// command 条目禁止的 shell 元字符：管道、命令拼接/后台、命令替换、重定向。
// 复杂逻辑应封装为 skill 包内脚本，再由 Wrapper 调用。
const SHELL_META_RE = /[|;&`<>]|\$\(/;

function wrapperError(code, message) {
  process.stderr.write(`[bsm-obs-run wrapper_error] ${message}\n`);
  process.exit(code);
}

function usageError(message) {
  wrapperError(
    WRAPPER_EXIT.USAGE,
    `${message}；用法：bsm-obs-run.cjs run <node_id> [--interpreter <x>] -- <args...> 或 bsm-obs-run.cjs external -- <command...> 或 bsm-obs-run.cjs validate`,
  );
}

// ---------- SKILL.md frontmatter 版本读取（仅用于版本一致性校验/告警） ----------
function readSkillVersion(skillRoot) {
  const skillMdPath = path.join(skillRoot, 'SKILL.md');
  if (!existsSync(skillMdPath)) return null;
  const text = readFileSync(skillMdPath, 'utf8').replace(/^﻿/, '');
  const fm = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!fm) return null;
  const lines = fm[1].split(/\r?\n/);
  let inMetadata = false;
  for (const line of lines) {
    if (/^metadata:\s*$/.test(line)) {
      inMetadata = true;
      continue;
    }
    if (inMetadata) {
      if (/^\S/.test(line)) break;
      const m = line.match(/^\s*version:\s*["']?([^"'\s#]+)/);
      if (m) return m[1];
    }
  }
  return null;
}

function skillVersionMismatch(manifest, skillRoot) {
  const skillVersion = readSkillVersion(skillRoot);
  if (skillVersion === null) {
    return { missing: true, skillVersion: null };
  }
  if (manifest.skill_version !== skillVersion) {
    return { missing: false, skillVersion };
  }
  return null;
}

// ---------- 极简 YAML 子集解析器 ----------
function parseScalar(raw) {
  const v = raw.trim();
  if (v === 'true') return true;
  if (v === 'false') return false;
  if (v === 'null' || v === '~') return null;
  if (v.startsWith('[') || v.startsWith('{')) {
    throw new Error(`不支持的标量形态: ${v}`);
  }
  if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
    return v.slice(1, -1);
  }
  return v;
}

function parseManifestText(text) {
  const rawLines = text.replace(/^﻿/, '').split(/\r?\n/);
  const items = [];
  for (const raw of rawLines) {
    const line = raw.replace(/\t/g, '  ');
    const t = line.trim();
    if (t === '' || t.startsWith('#')) continue;
    items.push({ t, indent: line.length - line.trimStart().length });
  }

  let pos = 0;

  function parseBlock(indent) {
    if (pos >= items.length) return null;
    if (items[pos].t === '-' || items[pos].t.startsWith('- ')) return parseSeq(indent);
    return parseMap(indent);
  }

  function parseMap(indent) {
    const obj = {};
    while (pos < items.length) {
      const it = items[pos];
      if (it.indent < indent) break;
      if (it.indent > indent) throw new Error(`意外缩进: ${it.t}`);
      const m = it.t.match(/^([^:]+):(.*)$/);
      if (!m) throw new Error(`无法解析键值行: ${it.t}`);
      const key = m[1].trim();
      const rest = m[2].trim();
      pos += 1;
      if (rest === '[]') {
        obj[key] = [];
      } else if (rest === '{}') {
        obj[key] = {};
      } else if (rest === '') {
        if (pos < items.length && items[pos].indent > indent) {
          obj[key] = parseBlock(items[pos].indent);
        } else {
          obj[key] = null;
        }
      } else {
        obj[key] = parseScalar(rest);
      }
    }
    return obj;
  }

  function parseSeq(indent) {
    const arr = [];
    while (pos < items.length) {
      const it = items[pos];
      if (it.indent < indent) break;
      if (it.indent > indent) throw new Error(`序列缩进异常: ${it.t}`);
      if (it.t === '-') {
        pos += 1;
        if (pos < items.length && items[pos].indent > indent) {
          arr.push(parseBlock(items[pos].indent));
        } else {
          arr.push(null);
        }
        continue;
      }
      const m = it.t.match(/^- (.*)$/);
      if (!m) throw new Error(`无法解析序列项: ${it.t}`);
      const rest = m[1].trim();
      if (/^[^:]+:(\s|$)/.test(rest)) {
        items[pos] = { t: rest, indent: indent + 2 };
        arr.push(parseMap(indent + 2));
      } else {
        pos += 1;
        arr.push(parseScalar(rest));
      }
    }
    return arr;
  }

  return parseBlock(0);
}

function loadManifest() {
  const wrapperDir = path.dirname(__filename);
  const skillRoot = path.dirname(wrapperDir);
  const manifestPath = path.join(wrapperDir, 'bsm-obs-manifest.yaml');
  if (!existsSync(manifestPath)) {
    wrapperError(WRAPPER_EXIT.MANIFEST, `manifest 不存在: ${manifestPath}`);
  }
  let manifest;
  try {
    manifest = parseManifestText(readFileSync(manifestPath, 'utf8'));
  } catch (err) {
    wrapperError(WRAPPER_EXIT.MANIFEST, `manifest 解析失败（fail-closed）: ${err.message}`);
  }
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    wrapperError(WRAPPER_EXIT.MANIFEST, 'manifest 结构非法：顶层必须是映射');
  }
  if (typeof manifest.spec_version !== 'string') {
    wrapperError(WRAPPER_EXIT.MANIFEST, 'manifest 缺少 spec_version');
  }
  if (!SUPPORTED_SPEC_VERSIONS.includes(manifest.spec_version)) {
    wrapperError(
      WRAPPER_EXIT.MANIFEST,
      `spec_version "${manifest.spec_version}" 不受支持（本 wrapper 支持: ${SUPPORTED_SPEC_VERSIONS.join(', ')}）`,
    );
  }
  if (typeof manifest.skill_version !== 'string' || manifest.skill_version === '') {
    wrapperError(WRAPPER_EXIT.MANIFEST, 'manifest 缺少 skill_version');
  }
  if (!Array.isArray(manifest.nodes) || manifest.nodes.length === 0) {
    wrapperError(WRAPPER_EXIT.MANIFEST, 'manifest nodes 为空或缺失');
  }
  const seen = new Set();
  const idRe = new RegExp(`^${manifest.skill_id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$`, 'i');
  for (const node of manifest.nodes) {
    if (!node || typeof node !== 'object' || typeof node.node_id !== 'string' || node.node_id === '') {
      wrapperError(WRAPPER_EXIT.MANIFEST, '存在缺少合法 node_id 的节点');
    }
    if (!idRe.test(node.node_id)) {
      wrapperError(
        WRAPPER_EXIT.MANIFEST,
        `node_id 格式非法（须 <skill_id>.<base64url(scriptPath)>.<base64url(operationKey)>）: ${node.node_id}`,
      );
    }
    if (seen.has(node.node_id)) {
      wrapperError(WRAPPER_EXIT.MANIFEST, `node_id 重复: ${node.node_id}`);
    }
    seen.add(node.node_id);
    if (node.node_type !== 'script' && node.node_type !== 'cli') {
      wrapperError(
        WRAPPER_EXIT.MANIFEST,
        `节点 ${node.node_id} 的 node_type 非法（允许: script, cli；mcp 暂不支持）: ${node.node_type ?? '(缺失)'}`,
      );
    }
    if (!Array.isArray(node.parent_node_ids) || !node.parent_node_ids.every((p) => typeof p === 'string')) {
      wrapperError(WRAPPER_EXIT.MANIFEST, `节点 ${node.node_id} 的 parent_node_ids 必须是字符串数组（首节点为 []）`);
    }
    if (typeof node.terminal !== 'boolean') {
      wrapperError(WRAPPER_EXIT.MANIFEST, `节点 ${node.node_id} 的 terminal 必须是布尔值`);
    }
    if (
      !Array.isArray(node.command) ||
      node.command.length === 0 ||
      !node.command.every((c) => typeof c === 'string' && c.length > 0)
    ) {
      wrapperError(WRAPPER_EXIT.MANIFEST, `节点 ${node.node_id} 的 command 必须是非空字符串参数数组`);
    }
    for (const c of node.command) {
      if (SHELL_META_RE.test(c)) {
        wrapperError(
          WRAPPER_EXIT.MANIFEST,
          `节点 ${node.node_id} 的 command 含 shell 元字符（复杂逻辑应封装为包内脚本）: ${c}`,
        );
      }
    }
  }
  const nodeIds = new Set(manifest.nodes.map((n) => n.node_id));
  for (const node of manifest.nodes) {
    for (const p of node.parent_node_ids) {
      if (!nodeIds.has(p)) {
        wrapperError(WRAPPER_EXIT.MANIFEST, `节点 ${node.node_id} 的 parent_node_ids 引用不存在的节点: ${p}`);
      }
    }
  }
  const indegree = new Map(manifest.nodes.map((n) => [n.node_id, 0]));
  const children = new Map(manifest.nodes.map((n) => [n.node_id, []]));
  for (const node of manifest.nodes) {
    for (const p of node.parent_node_ids) {
      indegree.set(node.node_id, indegree.get(node.node_id) + 1);
      children.get(p).push(node.node_id);
    }
  }
  const queue = [...indegree.entries()].filter(([, d]) => d === 0).map(([id]) => id);
  let visited = 0;
  while (queue.length > 0) {
    const id = queue.shift();
    visited += 1;
    for (const c of children.get(id)) {
      indegree.set(c, indegree.get(c) - 1);
      if (indegree.get(c) === 0) queue.push(c);
    }
  }
  if (visited !== manifest.nodes.length) {
    const cyclic = manifest.nodes.filter((n) => indegree.get(n.node_id) > 0).map((n) => n.node_id).join(', ');
    wrapperError(WRAPPER_EXIT.MANIFEST, `DAG 存在环（fail-closed），涉及节点: ${cyclic}`);
  }
  return { manifest, skillRoot };
}

function findNode(manifest, nodeId) {
  const node = manifest.nodes.find((n) => n.node_id === nodeId);
  if (!node) {
    wrapperError(WRAPPER_EXIT.NODE, `node_id 不存在于当前版本 manifest: ${nodeId}`);
  }
  return node;
}

function isPathLike(entry) {
  return entry.includes('/') || entry.includes('\\') || entry === '..';
}

class BoundaryError extends Error {}

function resolveWithinSkillRoot(entry, skillRoot) {
  if (path.isAbsolute(entry)) {
    throw new BoundaryError(`command 条目不允许绝对路径: ${entry}`);
  }
  const resolved = path.resolve(skillRoot, entry);
  if (resolved !== skillRoot && !resolved.startsWith(skillRoot + path.sep)) {
    throw new BoundaryError(`command 条目越过 skill 包目录: ${entry}`);
  }
  return resolved;
}

function validateNodeCommand(node, skillRoot, checkExists) {
  const scriptIndex = node.node_type === 'script' ? node.command.length - 1 : -1;
  return node.command.map((entry, index) => {
    if (index !== scriptIndex || !isPathLike(entry)) return entry;
    const resolved = resolveWithinSkillRoot(entry, skillRoot);
    if (checkExists && !existsSync(resolved)) {
      throw new Error(`command 指向的脚本不存在: ${entry}`);
    }
    return resolved;
  });
}

let child = null;

function installSignalForwarding() {
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT']) {
    process.on(sig, () => {
      if (child) {
        child.kill(sig);
      } else {
        process.exit(128 + (SIGNAL_NUM[sig] ?? 15));
      }
    });
  }
}

function runTransparently(command, args, cwd) {
  installSignalForwarding();
  child = spawn(command, args, {
    stdio: 'inherit',
    env: process.env,
    cwd,
  });
  child.on('error', (err) => {
    wrapperError(WRAPPER_EXIT.SPAWN, `目标命令无法启动: ${err.message}`);
  });
  child.on('close', (code, signal) => {
    if (code !== null && code !== undefined) {
      process.exit(code);
    }
    if (code === null && signal === null) {
      wrapperError(WRAPPER_EXIT.SPAWN, '子进程退出但无退出码与信号');
    }
    process.exit(128 + (SIGNAL_NUM[signal] ?? 15));
  });
}

function main() {
  const argv = process.argv.slice(2);
  const mode = argv[0];

  if (mode === 'run') {
    const nodeId = argv[1];
    if (!nodeId || nodeId === '--') usageError('run 模式缺少 <node_id>');
    let separatorIndex = 2;
    let interpreter = null;
    if (argv[2] === '--interpreter') {
      interpreter = argv[3];
      if (interpreter === undefined || interpreter === '--' || interpreter.trim() === '') {
        usageError('--interpreter 缺少取值');
      }
      if (/^<[^>]*>$/.test(interpreter)) {
        usageError(`解释器占位未替换: ${interpreter}（请替换为实际解释器命令或路径）`);
      }
      separatorIndex = 4;
    }
    if (argv[separatorIndex] !== '--') usageError('run 模式要求使用 -- 分隔业务参数');
    const bizArgs = argv.slice(separatorIndex + 1);

    const { manifest, skillRoot } = loadManifest();
    const drift = skillVersionMismatch(manifest, skillRoot);
    if (drift) {
      process.stderr.write(
        `[bsm-obs-run warn] manifest skill_version "${manifest.skill_version}" 与 SKILL.md metadata.version "${drift.skillVersion ?? '(缺失)'}" 不一致（不阻断执行）\n`,
      );
    }
    const node = findNode(manifest, nodeId);
    let resolvedCommand;
    try {
      resolvedCommand = validateNodeCommand(node, skillRoot);
    } catch (err) {
      wrapperError(WRAPPER_EXIT.BOUNDARY, err.message);
    }
    if (interpreter !== null) {
      if (node.node_type !== 'script') usageError(`cli 节点不接受 --interpreter: ${nodeId}`);
      resolvedCommand[0] = interpreter;
    }

    runTransparently(resolvedCommand[0], [...resolvedCommand.slice(1), ...bizArgs], process.cwd());
    return;
  }

  if (mode === 'validate') {
    const { manifest, skillRoot } = loadManifest();
    const drift = skillVersionMismatch(manifest, skillRoot);
    if (drift) {
      if (drift.missing) {
        wrapperError(
          WRAPPER_EXIT.MANIFEST,
          'validate 未通过：SKILL.md 缺少 frontmatter metadata.version（无法与 manifest skill_version 比对，不静默跳过）',
        );
      }
      wrapperError(
        WRAPPER_EXIT.MANIFEST,
        `validate 未通过：manifest skill_version "${manifest.skill_version}" 与 SKILL.md metadata.version "${drift.skillVersion}" 不一致`,
      );
    }
    let failed = 0;
    for (const node of manifest.nodes) {
      try {
        validateNodeCommand(node, skillRoot, true);
        process.stdout.write(`ok: ${node.node_id}\n`);
      } catch (err) {
        failed += 1;
        process.stdout.write(`ng: ${node.node_id} — ${err.message}\n`);
      }
    }
    if (failed > 0) {
      wrapperError(WRAPPER_EXIT.MANIFEST, `validate 未通过：${failed}/${manifest.nodes.length} 个节点命令无法解析`);
    }
    process.stdout.write(`manifest 校验通过：${manifest.nodes.length} 个节点，未执行任何业务命令\n`);
    return;
  }

  if (mode === 'external') {
    if (argv[1] !== '--') usageError('external 模式要求使用 -- 分隔目标命令');
    const target = argv.slice(2);
    if (target.length === 0) usageError('external 模式缺少目标命令');
    runTransparently(target[0], target.slice(1), process.cwd());
    return;
  }

  usageError(`未知模式: ${mode ?? '(空)'}`);
}

main();
