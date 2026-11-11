"""Deterministic OpenAI-compatible mock of bmc-llm-relay for offline C2 E2E tests.

只用于离线验证 rc.14 C2 编排在 MA relay 通道下能完整跑通（不评估语义质量）：
- /chat/completions（stream=True，SSE）：按 system/user 文本识别阶段，返回合法 JSON；
- /embeddings：按标题里的 #分组词 生成可区分向量，同组余弦相近，便于 05/06 成块。

用法：python c2_mock_relay.py --port 0 --port-file <path>（端口写入文件后阻塞服务）。
请求计数写 <port-file>.stats.json，供测试断言调用次数（如 F1 一次调用覆盖 00/01/02）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATS: dict[str, int] = {}
LOCK = threading.Lock()
STATS_PATH: Path | None = None
DIM = 16
RID = re.compile(r"\[([Rr]\d+)\]")


def bump(key: str) -> None:
    with LOCK:
        STATS[key] = STATS.get(key, 0) + 1
        if STATS_PATH:
            STATS_PATH.write_text(json.dumps(STATS, ensure_ascii=False), encoding="utf-8")


def group_of(text: str) -> str:
    m = re.search(r"#([^#\s]+)#", text)
    return m.group(1) if m else text[:4]


def vector(text: str) -> list[float]:
    g = hashlib.sha256(group_of(text).encode()).digest()
    t = hashlib.sha256(text.encode()).digest()
    return [1.0 + g[i] / 64.0 + t[i] / 4096.0 for i in range(DIM)]


def f1(user: str) -> dict:
    payload = json.loads(user)
    rows = []
    for rec in payload["records"]:
        title = rec.get("title") or rec.get("clean_title") or ""
        non_event = "教程" in title
        # 「如何看待」观点类标题:模拟真实 F1 常见的 UNCERTAIN + needs_evidence(W45 后会友期)
        uncertain = "如何看待" in title
        rows.append({
            "record_id": rec["record_id"],
            "eventness": "NON_EVENT" if non_event else ("UNCERTAIN" if uncertain else "EVENT"),
            "reason_code": ("GENERIC_KNOWLEDGE" if non_event else
                            "INSUFFICIENT_INFO" if uncertain else "SPECIFIC_STATE_CHANGE"),
            "confidence": 0.9,
            "exclusion_supported": non_event,
            "needs_evidence": uncertain,
            "frame": None if non_event else {
                "actor": [{"name": group_of(title), "type": "ORG"}],
                "action": "发布", "object": group_of(title), "event_type": "发布",
                "parent_hint": None, "stage": None, "time": None, "location": None,
                "key_facts": {},
            },
        })
    return {"records": rows}


def entities(user: str) -> dict:
    part = user.split("待判定的实体名：", 1)[1].split("输出格式：", 1)[0]
    names = [line.split("\t")[0] for line in part.strip().splitlines() if line.strip()]
    return {"resolutions": [{"name": n, "decision": "NEW_ENTITY", "entity_id": None,
                             "canonical_name": n, "type": "ORG", "parent_entity_name": None,
                             "reason": "mock"} for n in names]}


def archive(user: str) -> dict:
    groups: dict[str, list[str]] = {}
    for line in user.splitlines():
        m = re.match(r"\[([Rr]\d+)\] (.*)", line.strip())
        if m:
            groups.setdefault(group_of(m.group(2)), []).append(m.group(1))
    events, singletons = [], []
    for idx, (g, ids) in enumerate(sorted(groups.items()), 1):
        if len(ids) == 1:
            singletons.append(ids[0])
            continue
        events.append({
            "event_key": f"E{idx}", "event_name": f"{g}发布新品引发热议",
            "event_description": f"{g}相关事件", "facets": [{"angle": "热议", "member_record_ids": ids}],
            "anchor": g, "anchor_type": "ORG", "series_instance": None,
            "member_record_ids": ids, "member_confidence": {r: 95 for r in ids},
            "member_notes": {}, "name_evidence": {"主体": g, "数字": "", "时间": ""},
            "confidence": 0.9,
        })
    return {"events": events, "singletons": singletons, "relations": []}


def _lines(user: str) -> list[tuple[str, str]]:
    return [tuple(l.split("\t", 1)) for l in user.splitlines() if "\t" in l]


def legacy(user: str) -> dict | None:
    """legacy 前端(原版 00/01/02 分开调用)。判定口径与 f1() 一致,便于两种前端结果可比。"""
    if user.startswith("清洗以下"):
        bump("clean")
        return {"records": [{"record_id": r, "clean_title": t, "ops": ["NONE"]} for r, t in _lines(user)]}
    if user.startswith("判定以下"):
        bump("eventness")
        rows = []
        for r, t in _lines(user):
            ne, unc = "教程" in t, "如何看待" in t
            rows.append({"record_id": r, "eventness": "NON_EVENT" if ne else ("UNCERTAIN" if unc else "EVENT"),
                         "underlying_event": "", "confidence": 0.9,
                         "reason_code": "GENERIC_KNOWLEDGE" if ne else
                                        ("INSUFFICIENT_INFO" if unc else "SPECIFIC_STATE_CHANGE")})
        return {"records": rows}
    if user.startswith("抽取以下"):
        bump("frames")
        return {"frames": [{"record_id": r, "actor": [{"name": group_of(t), "type": "ORG"}], "action": "发布",
                            "object": group_of(t), "event_type": "发布", "parent_hint": None, "stage": None,
                            "time": None, "location": None, "key_facts": {}} for r, t in _lines(user)]}
    return None


def respond(system: str, user: str) -> dict:
    if user.lstrip().startswith("{") and '"records"' in user[:200]:
        bump("f1")
        return f1(user)
    old = legacy(user)
    if old is not None:
        return old
    if "待判定的实体名" in user:
        bump("entities")
        return entities(user)
    if "本批记录（共" in user:
        bump("archive")
        return archive(user)
    if user.startswith("裁定以下"):
        bump("x2")
        return {"results": [{"record_id": r, "verdict": "属于", "reason": "mock"}
                            for r in RID.findall(user)]}
    if user.startswith("复核以下") and "组事件" in user[:20]:
        bump("x3_merge")
        anchors = re.findall(r"## 共享锚点：(.*)", user)
        return {"results": [{"anchor": a.strip(), "merges": [], "keep_separate_reason": "mock"}
                            for a in anchors]}
    if user.startswith("复核以下"):
        bump("x3_split")
        ids = re.findall(r"### ([^｜\n]+)｜", user)
        return {"results": [{"event_id": i, "action": "KEEP", "new_name": "", "splits": [],
                             "reason": "mock"} for i in ids]}
    if '"event_name"' in system or "event_name" in user[-200:]:
        bump("rename")
        return {"event_name": None}
    bump("unknown")
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def _json(self, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/embeddings"):
            bump("embeddings")
            inputs = body.get("input") or []
            inputs = [inputs] if isinstance(inputs, str) else inputs
            self._json({"data": [{"index": i, "embedding": vector(t)} for i, t in enumerate(inputs)],
                        "usage": {"prompt_tokens": 1, "total_tokens": 1}})
            return
        msgs = body.get("messages") or []
        system = next((m["content"] for m in msgs if m.get("role") == "system"), "")
        user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
        text = json.dumps(respond(system, user), ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(0, len(text), 400):
            chunk = {"choices": [{"index": 0, "delta": {"content": text[i:i + 400]}}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
        tail = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        self.wfile.write(f"data: {json.dumps(tail)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()


def main() -> None:
    global STATS_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--port-file", type=Path, required=True)
    args = ap.parse_args()
    STATS_PATH = args.port_file.with_suffix(".stats.json")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    args.port_file.write_text(str(server.server_address[1]), encoding="utf-8")
    server.serve_forever()


if __name__ == "__main__":
    main()
