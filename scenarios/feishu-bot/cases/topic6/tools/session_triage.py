#!/usr/bin/env python3
"""Topic6 MA Session 一键排查：拉取事件 → 输出时间线 / 报错 / HC / 成本摘要。

用法（feishu-bot 目录下，topic6 conda 环境的 python）:
  python cases/topic6/tools/session_triage.py sesn-xxxx
  python cases/topic6/tools/session_triage.py sesn-xxxx --events-file events.json   # 复用已拉取的事件
  python cases/topic6/tools/session_triage.py sesn-xxxx --full                       # 报错不截断

凭据读取 ~/.arkagent/cases/topic6/config.env（可用 TOPIC6_CONFIG_FILE 覆盖），
不打印任何密钥。原始事件写入 --out-dir/{session}.events.json 供二次分析。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

FEISHU_BOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = Path(
    os.environ.get("TOPIC6_CONFIG_FILE")
    or Path(os.environ.get("ARKAGENT_HOME", Path.home() / ".arkagent")) / "cases/topic6/config.env"
)
DEFAULT_OUT_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "topic6_triage"

# 命中即视为可疑 tool_result（再结合 exit_code 判断）
ERROR_PATTERN = re.compile(
    r"Traceback|\bERROR\b|❌|\"ok\":\s*false|token_missing|token_invalid|token_expired|"
    r"strict mode|permission|no permission|denied|failed_precondition|熔断|超过 \d+% 阈值|"
    r"No such file|command not found|code\":\s*[1-9]\d{3,}",
    re.IGNORECASE,
)
EXIT_CODE_RE = re.compile(r"exit_code:\s*(\d+)")
PHASE_RE = re.compile(r"^\[phase\]\s*(\S+)", re.MULTILINE)
READ_ONLY_CMD = re.compile(r"^(cd [^;&|]+(&&|;)\s*)?(cat|sed|grep|head|tail|ls|find|wc|echo \"===)\b")
SECRET_RE = re.compile(r"(?i)(secret|token|api[_-]?key|password)([\"'=:\s]+)([A-Za-z0-9._\-]{12,})")


def load_env(path: Path) -> None:
    if not path.exists():
        sys.exit(f"配置文件不存在: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def fetch_events(session_id: str) -> list[dict]:
    sys.path.insert(0, str(FEISHU_BOT_DIR))
    from arkagent.ark import ArkClient  # noqa: PLC0415

    base = (os.environ.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    client = ArkClient(os.environ["ARK_API_KEY"], base)
    try:
        return await client.list_session_events(session_id)
    finally:
        await client.aclose()


def text_of(event: dict) -> str:
    content = event.get("content")
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


def redact(value: str) -> str:
    return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)


def ts(event: dict) -> str:
    return str(event.get("processed_at") or "")[11:19]


def one_line(value: str, limit: int) -> str:
    value = redact(value).replace("\n", " | ")
    return value if limit <= 0 or len(value) <= limit else value[:limit] + " …"


def triage(events: list[dict], limit: int) -> str:
    out: list[str] = []
    types = Counter(e.get("type") for e in events)
    out.append(f"# 事件总数 {len(events)}  " + ", ".join(f"{k}={v}" for k, v in types.most_common(8)))
    if events:
        out.append(f"# 时间 {events[0].get('processed_at', '')} → {events[-1].get('processed_at', '')}")

    # ---- 1. 时间线：用户消息 / 阶段 / 状态 / 子 Agent / 压缩 ----
    out.append("\n## 1. 时间线")
    agents = Counter()
    for i, e in enumerate(events):
        t = e.get("type", "")
        if t == "user.message":
            out.append(f"[{i}] {ts(e)} USER  {one_line(text_of(e), 160)}")
        elif t in ("session.status_idle", "session.status_failed", "session.status_terminated", "session.error", "user.interrupt"):
            reason = json.dumps(e.get("stop_reason") or e.get("error") or {}, ensure_ascii=False)
            out.append(f"[{i}] {ts(e)} {t}  {one_line(reason, 200)}")
        elif t == "agent.thread_context_compacted":
            out.append(f"[{i}] {ts(e)} ⚠️ 上下文压缩")
        elif t == "session.thread_created":
            agents[e.get("agent_name", "?")] += 1
        elif t == "agent.message":
            for phase in PHASE_RE.findall(text_of(e)):
                out.append(f"[{i}] {ts(e)} PHASE {phase}")
    if agents:
        out.append("子 Agent 线程: " + ", ".join(f"{k}×{v}" for k, v in agents.items()))

    # ---- 2. 报错的工具调用（附对应命令） ----
    out.append("\n## 2. 可疑工具结果")
    uses = {e.get("id"): e for e in events if e.get("type") == "agent.tool_use"}
    hits = 0
    for i, e in enumerate(events):
        if e.get("type") != "agent.tool_result":
            continue
        body = text_of(e)
        code = EXIT_CODE_RE.search(body)
        nonzero = bool(code and code.group(1) != "0")
        if not (e.get("is_error") or nonzero or ERROR_PATTERN.search(body)):
            continue
        hits += 1
        use = uses.get(e.get("tool_use_id")) or {}
        cmd = use.get("input") or {}
        command = str(cmd.get("command") or "")
        # 读源码/文档(cat/sed/grep/head 等)的输出天然含 ERROR/permission 字样，只在退出码非 0 时才算
        if not (e.get("is_error") or nonzero) and (
            READ_ONLY_CMD.match(command.strip()) or "path" in cmd or "name" in cmd
        ):
            hits -= 1
            continue
        cmd_text = cmd.get("description") or command or json.dumps(cmd, ensure_ascii=False)
        flag = "ERR" if e.get("is_error") else (f"exit={code.group(1)}" if code and nonzero else "match")
        match = ERROR_PATTERN.search(body)
        snippet = body[max(0, match.start() - 200):] if match else body
        out.append(f"[{i}] {ts(e)} {flag}  {one_line(str(cmd_text), 160)}\n      => {one_line(snippet, limit)}")
    out.append(f"共 {hits} 条")

    # ---- 3. 模型请求错误 ----
    model_errors = [(i, e) for i, e in enumerate(events) if e.get("type") == "span.model_request_end" and e.get("is_error")]
    out.append(f"\n## 3. 模型请求错误 {len(model_errors)} 条")
    for i, e in model_errors[:20]:
        out.append(f"[{i}] {ts(e)} {one_line(json.dumps(e.get('error') or e, ensure_ascii=False), limit)}")

    # ---- 4. HC payload 与 Agent 关键消息（idle 前最后一条） ----
    out.append("\n## 4. HC payload / 收尾消息")
    for i, e in enumerate(events):
        if e.get("type") == "session.status_idle":
            prev = next((events[j] for j in range(i - 1, -1, -1) if events[j].get("type") == "agent.message"), None)
            if prev:
                body = text_of(prev)
                kind = "HC" if body.lstrip().startswith("```json") else "MSG"
                out.append(f"[idle@{i}] {ts(e)} {kind}: {one_line(body, max(limit, 600))}")

    # ---- 5. 用量 ----
    usage = [e.get("usage") or {} for e in events if e.get("type") == "session.usage"]
    if usage:
        last = usage[-1]
        out.append(
            "\n## 5. 用量(最后一次 session.usage)\n"
            f"active_seconds={last.get('active_seconds')} input={last.get('input_tokens')} "
            f"cache_read={last.get('cache_read_input_tokens')} output={last.get('output_tokens')}"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_id")
    parser.add_argument("--events-file", help="复用已拉取的事件 JSON，跳过 API 调用")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--full", action="store_true", help="报错内容不截断")
    args = parser.parse_args()

    if args.events_file:
        events = json.loads(Path(args.events_file).read_text(encoding="utf-8"))
    else:
        load_env(Path(args.config))
        events = asyncio.run(fetch_events(args.session_id))
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / f"{args.session_id}.events.json"
        raw_path.write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"# 原始事件: {raw_path}")
    print(triage(events, 0 if args.full else 400))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
