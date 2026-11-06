"""C2 输出预算与 03 实体归一截断兜底回归（W44 full：03 首批 50 实体撞 max_tokens=16000 阻断 C2）。

- relay：表内模型按原生输出上限请求（不被上游按 Sonnet 调校的 16000/24000 压低），截断报错带推理拆分；
- 03：截断先原样重试一次，仍截断对半拆、串行累积注册表，名字集合不丢不重。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "cases" / "topic6" / "ma-resources" / "skills" / "topic6-event-registry" / "scripts"
sys.path.insert(0, str(SCRIPTS))


class _Relay:
    """最小 OpenAI 兼容 SSE mock：名字数 > limit 的实体请求返回 finish_reason=length。"""

    def __init__(self, limit: int, truncate_first: int = 0):
        self.limit = limit
        self.truncate_first = truncate_first
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                user = body["messages"][-1]["content"]
                names = []
                if "待判定的实体名：" in user:
                    part = user.split("待判定的实体名：", 1)[1].split("输出格式：", 1)[0]
                    names = [l.split("\t")[0] for l in part.strip().splitlines() if l.strip()]
                truncated = len(names) > outer.limit or len(outer.requests) <= outer.truncate_first
                text = json.dumps({"resolutions": [
                    {"name": n, "decision": "NEW_ENTITY", "entity_id": None, "canonical_name": n,
                     "type": "ORG", "parent_entity_name": None, "reason": "t"} for n in names]},
                    ensure_ascii=False)
                chunks = [{"choices": [{"delta": {"content": text[:20] if truncated else text},
                                        "finish_reason": "length" if truncated else "stop"}]},
                          {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 99,
                                                    "completion_tokens_details": {"reasoning_tokens": 80}}}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def relay_env(monkeypatch):
    servers = []

    def make(limit: int, truncate_first: int = 0) -> _Relay:
        srv = _Relay(limit, truncate_first)
        servers.append(srv)
        monkeypatch.setenv("LLM_RELAY_BASE_URL", srv.base)
        monkeypatch.setenv("LLM_RELAY_API_KEY", "test")
        return srv

    yield make
    for s in servers:
        s.close()


def test_relay_uses_native_output_budget_for_known_models(monkeypatch):
    import relay
    assert relay.effective_max_tokens("DeepSeek-V4.1-Flash", 16000) == 384000
    assert relay.effective_max_tokens("deepseek-v4.1-flash", 24000) == 384000
    # 脚本显式给更大值时尊重脚本；表外模型(如上游默认 Sonnet)保持脚本传入值
    assert relay.effective_max_tokens("DeepSeek-V4.1-Flash", 500000) == 500000
    assert relay.effective_max_tokens("claude-sonnet-4-6", 16000) == 16000
    monkeypatch.setenv("LLM_RELAY_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "k")
    assert relay.Relay("DeepSeek-V4.1-Flash", 16000, "s").max_tokens == 384000


def test_relay_truncation_error_reports_reasoning_split(relay_env):
    import relay
    srv = relay_env(limit=0)
    r = relay.Relay("claude-sonnet-4-6", 16000, "s")
    with pytest.raises(ValueError) as exc:
        r.call("待判定的实体名：\nA\tORG\n输出格式：")
    msg = str(exc.value)
    assert "max_tokens=16000" in msg and "reasoning_tokens=80" in msg and "completion_tokens=99" in msg
    assert r.reasoning_tokens == 80
    assert srv.requests[0]["max_tokens"] == 16000


def _frames(run_dir: Path, n: int) -> None:
    (run_dir / "work").mkdir(parents=True)
    with (run_dir / "work" / "frames.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"record_id": f"T{i:04d}",
                                 "actor": [{"name": f"实体{i:03d}", "type": "ORG"}]},
                                ensure_ascii=False) + "\n")


def _run03(run_dir: Path, srv: _Relay, model: str = "claude-sonnet-4-6", batch: int = 50):
    env = {**os.environ, "LLM_RELAY_BASE_URL": srv.base, "LLM_RELAY_API_KEY": "test"}
    return subprocess.run([sys.executable, str(SCRIPTS / "03_normalize_entities.py"),
                           "--run-dir", str(run_dir), "--model", model, "--batch-size", str(batch)],
                          env=env, capture_output=True, text=True, timeout=120)


def _registry_names(run_dir: Path) -> list[str]:
    rows = [json.loads(l) for l in (run_dir / "work" / "entities.jsonl").read_text(encoding="utf-8").splitlines()]
    return sorted(n for e in rows for n in [e["canonical_name"], *e["aliases"]])


def test_03_splits_truncated_batch_and_keeps_every_name(tmp_path, relay_env):
    srv = relay_env(limit=12)  # 50→25→12/13→… 只有 ≤12 个名字的批才能通过
    _frames(tmp_path, 60)
    proc = _run03(tmp_path, srv)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "原样重试一次" in proc.stdout and "对半拆" in proc.stdout
    assert _registry_names(tmp_path) == sorted(f"实体{i:03d}" for i in range(60))
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["cost_ledger"][-1]
    assert entry["auto_split"] and entry["missing_names"] == 0
    assert entry["quality_status"] == "COMPLETE"


def test_03_transient_truncation_recovers_by_plain_retry(tmp_path, relay_env):
    srv = relay_env(limit=10_000, truncate_first=1)  # 仅第一次截断：原样重试即恢复，不拆批
    _frames(tmp_path, 30)
    proc = _run03(tmp_path, srv)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "原样重试一次" in proc.stdout and "对半拆" not in proc.stdout
    assert len(srv.requests) == 2
    assert _registry_names(tmp_path) == sorted(f"实体{i:03d}" for i in range(30))


def test_03_single_name_still_truncated_fails_loudly(tmp_path, relay_env):
    srv = relay_env(limit=0)
    _frames(tmp_path, 2)
    proc = _run03(tmp_path, srv)
    assert proc.returncode != 0 and "max_tokens" in proc.stderr


def test_03_requests_native_budget_for_deepseek(tmp_path, relay_env):
    srv = relay_env(limit=10_000)
    _frames(tmp_path, 3)
    proc = _run03(tmp_path, srv, model="DeepSeek-V4.1-Flash")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert {r["max_tokens"] for r in srv.requests} == {384000}
