"""Topic6 C2 rc.14 升级回归测试（离线 mock relay，零付费调用）。

覆盖：
- MA 封装 run_topic6_c2.py → run_c2.py（cross-platform + fused）端到端跑通，结果契约不变；
- F1 融合：每平台 1 次调用覆盖 00/01/02（提速核心）；
- 审核门禁 record 模式不停等，待审项进 c2_status.quality.review_pending；
- 同签名重跑零模型调用（checkpoint + 内容寻址缓存）；
- relay.py 保留 MA 传输层（LLM_RELAY_*，不读 ANTHROPIC_*），合入 rc.14 校验；
- 上游默认语义（strict/block）不被 MA 开关改变。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "cases" / "topic6" / "ma-resources" / "skills" / "topic6-event-registry" / "scripts"
SKILL_DIR = SCRIPTS.parent
MOCK = Path(__file__).resolve().parent / "fixtures" / "c2_mock_relay.py"
PLATFORMS = ["微博", "抖音", "知乎", "B站"]
GROUPS = ["华为", "小米", "苹果", "比亚迪", "茅台"]


@pytest.fixture()
def relay_server(tmp_path):
    port_file = tmp_path / "relay.port"
    proc = subprocess.Popen([sys.executable, str(MOCK), "--port-file", str(port_file)])
    for _ in range(100):
        if port_file.exists() and port_file.read_text().strip():
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("mock relay did not start")
    yield {"base": f"http://127.0.0.1:{port_file.read_text().strip()}/v1",
           "stats": port_file.with_suffix(".stats.json")}
    proc.kill()
    proc.wait()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    subset = project / "04_标注" / "_可用子集"
    subset.mkdir(parents=True)
    rows, n = [], 0
    for platform in PLATFORMS:
        for group in GROUPS:
            for k in range(2):
                n += 1
                rows.append({"row_id": f"r{n:04d}", "平台": platform,
                             "热点标题": f"#{group}#新品发布会第{k}场{platform}",
                             "标准化热度分": 100 - n})
        n += 1
        rows.append({"row_id": f"r{n:04d}", "平台": platform,
                     "热点标题": f"做饭教程{platform}", "标准化热度分": 1})
    pd.DataFrame(rows).to_excel(subset / "usable_subset_full_r1.xlsx", index=False)
    (project / "run_config.yaml").write_text("mode: full\n", encoding="utf-8")
    return project


def _run(project: Path, relay: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "LLM_RELAY_BASE_URL": relay["base"], "LLM_RELAY_API_KEY": "test",
           "C2_CHAT_MODEL_ID": "DeepSeek-V4.1-Flash", "EMBEDDING_MODEL_ID": "Doubao-embedding"}
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "run_topic6_c2.py"), "--project-dir", str(project),
         "--mode", "full", "--run-id", "1"],
        env=env, capture_output=True, text=True, timeout=300)


def _stats(relay: dict) -> dict:
    return json.loads(relay["stats"].read_text()) if relay["stats"].exists() else {}


def test_rc14_cross_platform_end_to_end_keeps_result_contract(tmp_path, relay_server):
    project = _project(tmp_path)
    proc = _run(project, relay_server)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

    c2_dir = project / "04_标注" / "C2_事件归档"
    result = pd.read_excel(c2_dir / "c2_event_result_r1.xlsx")
    assert list(result.columns) == ["row_id", "一级事件名"]
    assert len(result) == 44
    # 非事件（教程）不归属事件；同组跨平台 8 条归入同一一级事件
    named = result.dropna(subset=["一级事件名"])
    assert len(named) == 40
    assert sorted(named["一级事件名"].value_counts().tolist()) == [8] * 5

    stats = _stats(relay_server)
    assert stats.get("f1") == 4, stats          # F1 融合：每平台一次，覆盖 00/01/02
    assert stats.get("entities") == 1 and stats.get("embeddings") == 1
    assert stats.get("unknown", 0) == 0

    status = json.loads((c2_dir / "c2_run" / "c2_status.json").read_text())
    assert status["status"] == "done"
    assert status["llm_calls"] >= 6 and status["input_tokens"] > 0 and status["cost_usd"] > 0
    assert status["quality"]["coverage_status"] == "COMPLETE"
    assert status["quality"]["degraded_count"] == 0
    gates = {item["gate"] for item in status["quality"]["review_pending"]}
    assert {"cleaning", "non_events"} <= gates   # 门禁记录而非停等
    assert {"F1_微博", "EV", "archive", "finalize"} <= set(status["node_seconds"])

    quality = json.loads((c2_dir / "c2_run" / "engine" / "out" / "c2_quality.json").read_text())
    assert quality["review_completion"] == "DEFERRED_TO_CALLER"
    assert quality["review_policy"] == "record" and quality["degraded_policy"] == "accept"

    import yaml
    run_config = yaml.safe_load((project / "run_config.yaml").read_text(encoding="utf-8"))
    assert run_config["status"]["c2_cluster"]["status"] == "done"
    assert run_config["status"]["c2_cluster"]["engine"].endswith("3.0.0-rc.14")


def test_rc14_same_signature_rerun_makes_zero_model_calls(tmp_path, relay_server):
    project = _project(tmp_path)
    assert _run(project, relay_server).returncode == 0
    before = _stats(relay_server)
    status_path = project / "04_标注" / "C2_事件归档" / "c2_run" / "c2_status.json"
    status = json.loads(status_path.read_text())
    status["status"] = "failed"          # 模拟中断后续跑
    status_path.write_text(json.dumps(status, ensure_ascii=False))
    proc = _run(project, relay_server)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert _stats(relay_server) == before


def test_relay_keeps_ma_transport_and_merges_rc14_validation():
    text = (SCRIPTS / "relay.py").read_text(encoding="utf-8")
    assert "LLM_RELAY_BASE_URL" in text and "ANTHROPIC" not in text
    assert '"stream": True' in text and "watchdog" in text.lower()
    assert "C2_CACHE_CONTEXT" in text and "def valid_vector" in text
    assert "atomic_json(self.cache_path" in text
    for name in ("run_c2.py", "run_ev.py", "check_environment.py", "04_build_embeddings.py"):
        assert "ANTHROPIC" not in (SCRIPTS / name).read_text(encoding="utf-8"), name


def test_run_c2_ma_switches_default_to_upstream_semantics():
    text = (SCRIPTS / "run_c2.py").read_text(encoding="utf-8")
    assert 'choices=["strict", "record"], default="strict"' in text
    assert 'choices=["block", "accept"], default="block"' in text
    wrapper = (SCRIPTS / "run_topic6_c2.py").read_text(encoding="utf-8")
    for flag in ('"--mode", "cross-platform"', '"--frontend", "fused"',
                 '"--review-policy", "record"', '"--degraded-policy", "accept"'):
        assert flag in wrapper, flag
    assert 'or "DeepSeek-V4.1-Flash"' in wrapper


def test_skill_metadata_is_rc14_and_mount_name_unchanged():
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "name: topic6-event-registry" in skill
    assert "version: 3.0.0-rc.14" in skill
    assert "## Topic6 / MA 用法（先读）" in skill
    for name in ("run_c2.py", "f1_extract.py", "c2_runtime.py", "run_ev.py",
                 "c2_quality.py", "retain_original.py", "front_evidence.py"):
        assert (SCRIPTS / name).exists(), name


# ---------------------------------------------------------------- 封装单元行为（替代 2.2.0 逐阶段编排测试）

def _load_wrapper():
    import importlib.util
    spec = importlib.util.spec_from_file_location("topic6_c2_wrapper_rc14", SCRIPTS / "run_topic6_c2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.C2_RETRY_BACKOFF_SECONDS = 0
    return module


def test_build_c2_command_passes_window_models_and_fixed_params(tmp_path):
    c2 = _load_wrapper()
    cmd = c2.build_c2_command(tmp_path, tmp_path / "input.csv", "chat-x", "emb-y",
                              x2_verdict="off", window="2026W43")
    pairs = dict(zip(cmd[3::1], cmd[4::1]))
    assert cmd[2] == "run" and cmd[1].endswith("run_c2.py")
    assert pairs["--run-dir"] == str(tmp_path / "engine")
    assert pairs["--window"] == "2026W43" and pairs["--verdict"] == "off"
    assert pairs["--chat-model"] == "chat-x" and pairs["--embedding-model"] == "emb-y"
    assert pairs["--max-requests"] == str(c2.C2_MAX_REQUESTS)
    # 总并发 = front_workers × max_requests，控制在 relay 实测安全量级
    assert int(pairs["--front-workers"]) * c2.C2_MAX_REQUESTS <= 16
    assert cmd[-1] == "--execute"


def test_engine_failure_retries_once_then_raises(tmp_path, monkeypatch):
    import subprocess as sp
    c2 = _load_wrapper()
    calls = []

    def fail(cmd, log):
        calls.append(cmd)
        raise sp.CalledProcessError(2, cmd)

    monkeypatch.setattr(c2, "_run_command", fail)
    with pytest.raises(RuntimeError, match="run_c2 failed"):
        c2._run_engine(["x"], tmp_path)
    assert len(calls) == c2.C2_RETRIES + 1


def test_engine_retry_succeeds_on_checkpoint_resume(tmp_path, monkeypatch):
    import subprocess as sp
    c2 = _load_wrapper()
    (tmp_path / "engine").mkdir()
    calls = []

    def flaky(cmd, log):
        calls.append(cmd)
        if len(calls) == 1:
            raise sp.CalledProcessError(2, cmd)
        (tmp_path / "engine" / "result.json").write_text('{"status": "COMPLETE"}')

    monkeypatch.setattr(c2, "_run_command", flaky)
    assert c2._run_engine(["x"], tmp_path)["status"] == "COMPLETE"
    assert len(calls) == 2


def test_signature_changes_with_model_window_and_engine_code():
    c2 = _load_wrapper()
    base = c2._run_signature("sha", "m1", "e", "sonnet", window="W1", engine_digest="d1")
    assert base == c2._run_signature("sha", "m1", "e", "sonnet", window="W1", engine_digest="d1")
    assert base != c2._run_signature("sha", "m2", "e", "sonnet", window="W1", engine_digest="d1")
    assert base != c2._run_signature("sha", "m1", "e", "sonnet", window="W2", engine_digest="d1")
    assert base != c2._run_signature("sha", "m1", "e", "sonnet", window="W1", engine_digest="d2")


def test_model_change_resets_engine_dir(tmp_path, relay_server):
    project = _project(tmp_path)
    assert _run(project, relay_server).returncode == 0
    engine = project / "04_标注" / "C2_事件归档" / "c2_run" / "engine"
    stale = engine / "stale_marker"
    stale.write_text("old")
    env_model = os.environ.get("C2_CHAT_MODEL_ID")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_topic6_c2.py"), "--project-dir", str(project),
         "--mode", "full", "--run-id", "1", "--chat-model", "Other-Model"],
        env={**os.environ, "LLM_RELAY_BASE_URL": relay_server["base"], "LLM_RELAY_API_KEY": "t"},
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert not stale.exists()
    assert env_model == os.environ.get("C2_CHAT_MODEL_ID")


def test_engine_digest_tracks_references_and_endpoint(tmp_path, monkeypatch):
    """run_c2 config.json 含 references 哈希与端点哈希；任一变化封装签名必须变，否则永久 FAILED。"""
    c2 = _load_wrapper()
    base = c2._engine_digest()
    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://other.example/v1")
    assert c2._engine_digest() != base
    monkeypatch.undo()
    ref_dir = tmp_path / "skill"
    (ref_dir / "scripts").mkdir(parents=True)
    (ref_dir / "references").mkdir()
    (ref_dir / "references" / "a.md").write_text("v1")
    monkeypatch.setattr(c2, "SCRIPT_DIR", ref_dir / "scripts")
    first = c2._engine_digest()
    (ref_dir / "references" / "a.md").write_text("v2")
    assert c2._engine_digest() != first


def test_prepare_input_drops_blank_titles(tmp_path):
    c2 = _load_wrapper()
    src = tmp_path / "in.xlsx"
    pd.DataFrame({"row_id": ["a", "b", "c"], "平台": ["微博"] * 3,
                  "热点标题": ["标题A", "  ", None], "标准化热度分": [1, 2, 3]}).to_excel(src, index=False)
    assert c2._prepare_input(src, tmp_path / "out.csv") == 1
    assert pd.read_csv(tmp_path / "out.csv")["record_id"].tolist() == ["a"]


def test_prepare_input_all_blank_titles_fails(tmp_path):
    c2 = _load_wrapper()
    src = tmp_path / "in.xlsx"
    pd.DataFrame({"row_id": ["a"], "平台": ["微博"], "热点标题": [""], "标准化热度分": [1]}).to_excel(src, index=False)
    with pytest.raises(ValueError, match="non-empty title"):
        c2._prepare_input(src, tmp_path / "out.csv")


def test_blank_title_rows_get_empty_event_and_run_completes(tmp_path, relay_server):
    project = _project(tmp_path)
    xlsx = project / "04_标注" / "_可用子集" / "usable_subset_full_r1.xlsx"
    frame = pd.read_excel(xlsx)
    frame.loc[len(frame)] = {"row_id": "r9999", "平台": "微博", "热点标题": "", "标准化热度分": 1}
    frame.to_excel(xlsx, index=False)
    proc = _run(project, relay_server)
    assert proc.returncode == 0, proc.stdout[-2000:]
    result = pd.read_excel(project / "04_标注" / "C2_事件归档" / "c2_event_result_r1.xlsx")
    assert len(result) == 44 and "r9999" not in set(result["row_id"])


def test_engine_cost_sums_ledger_and_tolerates_bad_entries(tmp_path):
    c2 = _load_wrapper()
    out = tmp_path / "engine" / "out"
    out.mkdir(parents=True)
    (out / "c2_run_metrics.json").write_text(json.dumps({"cost_ledger": [
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.1, "calls": 1},
        {"input_tokens": 20, "output_tokens": None, "cost_usd": 0.2, "calls": 2},
        "junk", {"cost_usd": True},
    ]}))
    assert c2._engine_cost(tmp_path) == {"input_tokens": 30, "output_tokens": 5,
                                         "cost_usd": 0.3, "calls": 3}
    assert c2._engine_cost(tmp_path / "missing")["cost_usd"] == 0.0


def test_run_c2_front_workers_default_is_upstream_serial():
    text = (SCRIPTS / "run_c2.py").read_text(encoding="utf-8")
    assert '"--front-workers", type=int, default=1' in text
    assert "front_parallel" not in text


def test_all_chat_paths_default_to_deepseek_v41_flash():
    """除 DataHub(DeepSeek-V4-Flash) 与向量(Doubao-embedding) 外，所有对话模型统一 DeepSeek-V4.1-Flash。"""
    ma = ROOT / "cases" / "topic6" / "ma-resources"
    for name in ("coordinator", "annotator", "validator"):
        assert json.loads((ma / "agents" / f"{name}.json").read_text())["model"]["id"] == "DeepSeek-V4.1-Flash"
    env = json.loads((ma / "environment.json").read_text())["config"]["env"]
    assert env["C2_CHAT_MODEL_ID"] == "DeepSeek-V4.1-Flash"
    assert env["DATAHUB_MODEL_ID"] == "DeepSeek-V4-Flash"
    assert env["EMBEDDING_MODEL_ID"] == "Doubao-embedding"
    pipeline_e = (ma / "skills/topic6-insight/scripts/pipeline_e.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--model", default="DeepSeek-V4.1-Flash"' in pipeline_e
    demo = (ma / "skills/topic6-annotation/scripts/run_demo_routes.py").read_text(encoding="utf-8")
    assert 'or "DeepSeek-V4.1-Flash"' in demo
    scan = [p for p in ma.rglob("*") if p.suffix in {".py", ".json", ".md"} and p.is_file()
            and "__pycache__" not in p.parts]
    offenders = [str(p.relative_to(ma)) for p in scan
                 if re.search(r'default=["\']Doubao-Seed|"id": "Doubao-Seed|DeepSeek-V4-Flash-0731',
                              p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_demo_routes_price_deepseek_v41_flash():
    import importlib.util
    path = ROOT / "cases/topic6/ma-resources/skills/topic6-annotation/scripts/run_demo_routes.py"
    spec = importlib.util.spec_from_file_location("demo_routes_price", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._price_cny("DeepSeek-V4.1-Flash", 1_000_000, 1_000_000) == 17.5
