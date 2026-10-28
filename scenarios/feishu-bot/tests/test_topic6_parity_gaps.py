"""topic6 MA 适配 vs 原 topic6 对比报告中补齐的功能点回归。

对应 obsidian review/topic6_MA适配vs原topic6_原Validator对比.md 的 🔴缺 / ⚠️矛盾 条目。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1] / "cases/topic6"
MA = ROOT / "ma-resources"
ANN_SCRIPTS = MA / "skills/topic6-annotation/scripts"
COORDINATOR = MA / "agents/coordinator.system.md"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # dataclass 解析需要模块已注册
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dh():
    module = _load("parity_datahub_annotate", ANN_SCRIPTS / "datahub_annotate.py")
    module._c3_timewindow = lambda df: df
    return module


@pytest.fixture(scope="module")
def wait_batch():
    return _load("parity_wait_datahub_batch", ANN_SCRIPTS / "wait_datahub_batch.py")


# ---------------------------------------------------------------- 节点 C 第一批

@pytest.mark.parametrize(
    "envs, expected",
    [
        ({"DATAHUB_API_KEY_OFFICE": "o", "DATAHUB_API_KEY_HOME": "h", "DATAHUB_API_KEY": "d"}, "o"),
        ({"DATAHUB_API_KEY_HOME": "h", "DATAHUB_API_KEY": "d"}, "h"),
        ({"DATAHUB_API_KEY": "d"}, "d"),
    ],
)
def test_datahub_key_three_level_fallback(dh, monkeypatch, envs, expected):
    for env in dh.API_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    for env, value in envs.items():
        monkeypatch.setenv(env, value)
    assert dh._api_key() == expected


def test_datahub_key_missing_raises(dh, monkeypatch):
    for env in dh.API_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    with pytest.raises(RuntimeError, match="DATAHUB_API_KEY_OFFICE"):
        dh._api_key()


def _prepare_annotate(tmp_path, dh, monkeypatch, poll_results, result_frame=None):
    project = tmp_path / "proj"
    input_path = project / "03_抽样" / "sample_500.xlsx"
    input_path.parent.mkdir(parents=True)
    pd.DataFrame({"row_id": ["T1", "T2"], "title": ["a", "b"]}).to_excel(input_path, index=False)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt", encoding="utf-8")
    created: list[int] = []
    polled: list[int] = []

    monkeypatch.setenv("DATAHUB_API_KEY", "k")
    monkeypatch.setattr(dh, "_list_models", lambda _k: [
        {"model_id": "M", "platform": "ByteDance", "input_price": 0, "output_price": 0}])
    monkeypatch.setattr(dh, "_upload_file", lambda *_a: "ds")

    def fake_create(*_a):
        created.append(1000 + len(created))
        return created[-1]

    def fake_poll(_k, task_id):
        polled.append(task_id)
        outcome = poll_results[len(polled) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    frame = result_frame if result_frame is not None else pd.DataFrame({
        "row_id": ["T1", "T2"],
        "llm_result": ['{"是否营销可用":"是"}', '{"是否营销可用":"否"}'],
    })
    monkeypatch.setattr(dh, "_create_task", fake_create)
    monkeypatch.setattr(dh, "_poll_until_done", fake_poll)
    monkeypatch.setattr(dh, "_download_result", lambda _u, out: frame.to_excel(out, index=False))
    monkeypatch.setattr(dh, "_postprocess", lambda f, _t: (f, {
        "status": "pass", "total": len(f), "valid": len(f), "invalid": 0, "valid_rate": 1.0}))
    monkeypatch.setattr(dh, "_failed_row_ids", lambda *_a: [])
    return project, input_path, prompt, created, polled


def test_task_failure_resubmits_once_then_succeeds(tmp_path, dh, monkeypatch):
    project, input_path, prompt, created, polled = _prepare_annotate(
        tmp_path, dh, monkeypatch,
        [dh.DataHubTaskFailed("任务 1000 终止于失败态"),
         {"result_url": "u", "total_tokens": 400}],
    )
    meta = dh.annotate("c0", str(project), str(input_path), str(prompt), "M", run_id=1)
    assert created == [1000, 1001]
    assert polled == [1000, 1001]
    assert meta["task_id"] == 1001
    assert meta["resubmits"] == 1
    assert meta["failed_task_ids"] == [1000]


def test_task_failure_after_resubmit_limit_raises(tmp_path, dh, monkeypatch):
    project, input_path, prompt, created, _ = _prepare_annotate(
        tmp_path, dh, monkeypatch,
        [dh.DataHubTaskFailed("f1"), dh.DataHubTaskFailed("f2")],
    )
    with pytest.raises(dh.DataHubTaskFailed):
        dh.annotate("c0", str(project), str(input_path), str(prompt), "M", run_id=1)
    assert created == [1000, 1001]
    submit = json.loads((project / "04_标注/C0_基础事实/c0_submit_meta.json").read_text("utf-8"))
    assert submit["resubmits"] == 1  # 落盘计数,重启 worker 不会再重提


def test_timeout_is_not_resubmitted(tmp_path, dh, monkeypatch):
    project, input_path, prompt, created, _ = _prepare_annotate(
        tmp_path, dh, monkeypatch, [TimeoutError("poll timeout")])
    with pytest.raises(TimeoutError):
        dh.annotate("c0", str(project), str(input_path), str(prompt), "M", run_id=1)
    assert created == [1000]


def test_token_fallback_splits_total_three_to_one(tmp_path, dh, monkeypatch):
    project, input_path, prompt, _, _ = _prepare_annotate(
        tmp_path, dh, monkeypatch, [{"result_url": "u", "total_tokens": 400}])
    meta = dh.annotate("c0", str(project), str(input_path), str(prompt), "M", run_id=1)
    assert (meta["input_tokens"], meta["output_tokens"]) == (300, 100)
    assert meta["token_estimated"] is True


def test_token_exact_columns_take_precedence(tmp_path, dh, monkeypatch):
    frame = pd.DataFrame({
        "row_id": ["T1", "T2"], "llm_result": ["{}", "{}"],
        "llm_input_tokens": [10, 20], "llm_output_tokens": [1, 2],
    })
    project, input_path, prompt, _, _ = _prepare_annotate(
        tmp_path, dh, monkeypatch, [{"result_url": "u", "total_tokens": 400}], frame)
    meta = dh.annotate("c0", str(project), str(input_path), str(prompt), "M", run_id=1)
    assert (meta["input_tokens"], meta["output_tokens"]) == (30, 3)
    assert meta["token_estimated"] is False


# ---------------------------------------------------------------- 批次熔断口径

BATCH_TASKS = {
    "c0": "C0_基础事实", "c3": "C3_节点标注", "r1": "R1_平台借势", "r2": "R2_商业合作",
    "r3": "R3_风险预警", "r4": "R4_创意借鉴", "r5": "R5_消费者行为",
}
BATCH_TASKS = {k: (v, None) for k, v in BATCH_TASKS.items()}

def _write_done(project: Path, task: str, valid_rate: float, run_id: int = 1):
    subdir, _ = BATCH_TASKS[task]
    d = project / "04_标注" / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task}_completion_meta.json").write_text(
        json.dumps({"run_id": run_id, "total": 10, "valid_rate": valid_rate}), "utf-8")


def _write_dead(project: Path, task: str, run_id: int = 1):
    subdir, _ = BATCH_TASKS[task]
    d = project / "04_标注" / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task}_worker_r{run_id}.pid").write_text("99999999", "utf-8")


def _write_running(project: Path, task: str, run_id: int = 1):
    subdir, _ = BATCH_TASKS[task]
    d = project / "04_标注" / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task}_worker_r{run_id}.pid").write_text(str(os.getpid()), "utf-8")


def test_batch_single_r_failure_degrades(tmp_path, wait_batch):
    for t in ("r1", "r2", "r3", "r4"):
        _write_done(tmp_path, t, 0.99)
    _write_dead(tmp_path, "r5")
    result = wait_batch.inspect_batch(tmp_path, ["r1", "r2", "r3", "r4", "r5"], 1)
    assert result["status"] == "degraded"
    assert result["missing_tasks"] == ["r5"]


def test_batch_two_failures_are_systemic(tmp_path, wait_batch):
    for t in ("r1", "r2", "r3"):
        _write_done(tmp_path, t, 0.99)
    _write_dead(tmp_path, "r4")
    _write_dead(tmp_path, "r5")
    result = wait_batch.inspect_batch(tmp_path, ["r1", "r2", "r3", "r4", "r5"], 1)
    assert result["status"] == "failed"
    assert "系统性" in result["reason"]


def test_batch_two_failures_stop_even_while_others_run(tmp_path, wait_batch):
    _write_running(tmp_path, "r1")
    _write_dead(tmp_path, "r2")
    _write_dead(tmp_path, "r3")
    result = wait_batch.inspect_batch(tmp_path, ["r1", "r2", "r3"], 1)
    assert result["status"] == "failed"


def test_batch_c0_failure_is_fatal(tmp_path, wait_batch):
    _write_done(tmp_path, "c3", 0.99)
    _write_dead(tmp_path, "c0")
    result = wait_batch.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert result["status"] == "failed"
    assert "c0" in result["reason"]


def test_batch_single_failure_waits_for_running_peers(tmp_path, wait_batch):
    _write_running(tmp_path, "r1")
    _write_dead(tmp_path, "r2")
    result = wait_batch.inspect_batch(tmp_path, ["r1", "r2"], 1)
    assert result["status"] == "running"


def test_batch_quality_gate(tmp_path, wait_batch):
    _write_done(tmp_path, "c0", 0.79)
    _write_done(tmp_path, "c3", 0.85)
    result = wait_batch.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert result["status"] == "quality_fail"
    assert result["quality_fail_tasks"] == ["c0"]
    assert result["quality_warn_tasks"] == ["c3"]


def test_batch_warn_only_is_complete(tmp_path, wait_batch):
    _write_done(tmp_path, "c0", 0.95)
    _write_done(tmp_path, "c3", 0.85)
    result = wait_batch.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert result["status"] == "complete"
    assert result["quality_warn_tasks"] == ["c3"]


def test_coordinator_batch_contract_matches_script():
    text = COORDINATOR.read_text(encoding="utf-8")
    for status in ("`degraded`", "`quality_fail`", "`failed`", "`complete`"):
        assert status in text
    assert "同一批内 ≥ 2 个任务失败 → RuntimeError" not in text


# ---------------------------------------------------------------- C2

C2_SCRIPT = MA / "skills/topic6-event-registry/scripts/run_topic6_c2.py"


@pytest.fixture(scope="module")
def c2():
    module = _load("parity_run_topic6_c2", C2_SCRIPT)
    module.STAGE_RETRY_BACKOFF_SECONDS = 0
    return module


def test_c2_plan_includes_rank_and_registry_with_window(c2, tmp_path):
    plan = dict(c2.build_stage_plan(tmp_path, tmp_path / "i.csv", "c", "e", window="2026W43"))
    assert "08_rank" in plan and "09_registry" in plan
    assert plan["09_registry"][0][-2:] == ["--window", "2026W43"]


@pytest.mark.parametrize("label, expected", [
    ("2026-W43", "2026W43"), ("2026W05", "2026W05"), ("2026-W5", "2026W05"), ("bad", ""),
])
def test_c2_window_from_run_config(c2, tmp_path, label, expected):
    import yaml
    (tmp_path / "run_config.yaml").write_text(
        yaml.safe_dump({"period": {"period_label": label}}), "utf-8")
    assert c2.resolve_window(tmp_path) == expected


def test_c2_stage_retry_once_for_04_and_07(c2, tmp_path, monkeypatch):
    import subprocess
    calls = []

    def flaky(command, log_path):
        calls.append(command[0])
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(c2, "_run_command", flaky)
    c2._run_stage("07_archive", [["x"]], tmp_path)
    assert calls == ["x", "x"]

    calls.clear()
    monkeypatch.setattr(c2, "_run_command", lambda cmd, _p: (calls.append(1), (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, cmd))))
    with pytest.raises(subprocess.CalledProcessError):
        c2._run_stage("05_recall", [["y"]], tmp_path)
    assert calls == [1]  # 无重试配置的阶段不重试


def _c2_project(tmp_path, c2, monkeypatch, failing_stage):
    import subprocess
    project = tmp_path / "proj"
    source = project / "src.xlsx"
    project.mkdir()
    (project / "run_config.yaml").write_text("period:\n  period_label: 2026-W43\n", "utf-8")

    def prepare(_s, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("x", "utf-8")
        return 1

    executed = []

    def run_stage(stage, _cmds, _log):
        executed.append(stage)
        if stage == failing_stage:
            raise subprocess.CalledProcessError(1, [stage])

    monkeypatch.setattr(c2, "_prepare_input", prepare)
    monkeypatch.setattr(c2, "_input_platforms", lambda _p: ("weibo",))
    monkeypatch.setattr(c2, "_run_stage", run_stage)
    monkeypatch.setattr(c2, "_write_result", lambda _r, dest, _n: dest.write_text("r", "utf-8"))
    args = c2.argparse.Namespace(
        project_dir=str(project), mode="full", run_id=1, input=str(source),
        chat_model="c", embedding_model="e", x2_verdict="off", demo_fast=False)
    return project, args, executed


def test_c2_registry_failure_does_not_fail_c2(c2, tmp_path, monkeypatch):
    project, args, executed = _c2_project(tmp_path, c2, monkeypatch, "09_registry")
    result = c2.run(args)
    assert result.exists()
    status = json.loads((project / "04_标注/C2_事件归档/c2_run/c2_status.json").read_text("utf-8"))
    assert status["status"] == "done"
    assert status["registry"]["status"] == "failed"
    assert status["registry"]["window"] == "2026W43"
    assert "09_registry" in status["registry"]["errors"]


def test_c2_core_stage_failure_still_fails(c2, tmp_path, monkeypatch):
    import subprocess
    project, args, _ = _c2_project(tmp_path, c2, monkeypatch, "07_archive")
    with pytest.raises(subprocess.CalledProcessError):
        c2.run(args)
    status = json.loads((project / "04_标注/C2_事件归档/c2_run/c2_status.json").read_text("utf-8"))
    assert status["status"] == "failed"


# ---------------------------------------------------------------- D 综合质量验证 / HC 展示

@pytest.fixture(scope="module")
def merge():
    return _load("parity_merge_annotations", ANN_SCRIPTS / "merge_annotations.py")


def _wide(rows: int = 100, usable: int = 56, clustered: int = 40, big: int = 5,
          platforms=("微博", "抖音", "B站", "知乎")):
    plat = [platforms[i % len(platforms)] for i in range(rows)]
    usable_col = ["是"] * usable + ["否"] * (rows - usable)
    cluster = [""] * rows
    for i in range(clustered):
        cluster[i] = "大事件" if i < big else f"事件{i}"
    nodes = ["双11"] * 10 + [""] * (rows - 10)
    return pd.DataFrame({"平台": plat, "是否营销可用": usable_col, "事件簇名": cluster, "提取节点": nodes})


def _rates(v=0.99):
    return {t: v for t in ("c0", "r1", "r2", "r3", "r4", "r5", "c3")}


def test_quality_report_pass_and_metrics(merge):
    q = merge.quality_report(_wide(), missing={}, valid_rates=_rates(), unusable_reasons={"纯八卦": 30})
    assert q["status"] == "pass", q["checks"]
    m = q["metrics"]
    assert m["r_subset_rows"] == 56
    assert m["c2_coverage"] == round(40 / 56, 4)
    assert m["c3_coverage"] == 0.1
    assert m["max_cluster_rows"] == 5 and m["max_cluster_name"] == "大事件"
    assert set(m["platform_distribution"]) == {"微博", "抖音", "B站", "知乎"}
    assert m["unusable_reasons"] == {"纯八卦": 30}


def test_quality_report_warning_tier(merge):
    df = _wide(usable=40, clustered=25, big=12, platforms=("微博", "抖音", "知乎"))
    rates = _rates()
    rates["r3"] = 0.85
    q = merge.quality_report(df, missing={}, valid_rates=rates, unusable_reasons={})
    assert q["status"] == "warn"
    text = " ".join(q["warnings"])
    assert "R3 有效率" in text
    assert "C0 营销可用率" in text
    assert "平台行数异常偏少" in text and "B站" in text
    assert "聚类规模极端" in text


def test_quality_report_c2_low_coverage_and_missing_route(merge):
    q = merge.quality_report(_wide(clustered=20), missing={"r2": True}, valid_rates={**_rates(), "r2": None},
                             unusable_reasons={})
    # 恰 1 路整批缺失 = degraded,与 wait_datahub_batch 同口径(按缺失继续),不是 fail
    assert q["status"] == "degraded"
    items = {c["item"]: c["status"] for c in q["checks"]}
    assert items["C2 事件簇非空率"] == "warn"
    assert items["R2 有效率"] == "missing"
    assert any("R2 整批缺失" in w for w in q["warnings"])


@pytest.mark.parametrize("missing, expected", [
    ({"r2": True, "r3": True}, "fail"),   # >=2 路缺失=系统性
    ({"c0": True}, "fail"),               # C0 缺失不可降级
    ({"c3": True}, "degraded"),
    ({"c2": True}, "degraded"),
])
def test_quality_report_missing_tiers(merge, missing, expected):
    rates = {**_rates(), **{k: None for k in missing}}
    df = _wide()
    if missing.get("c2"):
        df["事件簇名"] = ""
    q = merge.quality_report(df, missing=missing, valid_rates=rates, unusable_reasons={})
    assert q["status"] == expected


def test_quality_report_low_valid_rate_still_fails(merge):
    q = merge.quality_report(_wide(), missing={}, valid_rates={**_rates(), "r4": 0.7},
                             unusable_reasons={})
    assert q["status"] == "fail"


def test_hc_card_renders_distribution_table():
    hitl = _load("topic6_hitl", ROOT / "topic6_hitl.py")
    summary = {
        "rows": 500, "c0_valid_rate": 0.98, "r1_r5_valid_rates": [1.0, 0.99, 1.0, 1.0, 1.0],
        "marketing_hit_rate": 0.56, "c2_coverage": 0.6, "max_cluster_rows": 12,
        "max_cluster_name": "WTT", "platform_distribution": {"微博": {"rows": 300, "share": 0.6}},
        "unusable_reasons": {"纯八卦": 50},
    }
    elements = hitl._payload_summary_elements({"hc": "HC1", "distribution_summary": summary})
    content = next(e["content"] for e in elements if "分布摘要" in e.get("content", ""))
    for label in ("C0 有效率", "98.0%", "C0 营销可用占比", "C2 聚类覆盖率", "12 条（WTT）",
                  "微博 300（60.0%）", "纯八卦 50"):
        assert label in content
    # 旧 payload 仍兼容
    legacy = hitl._payload_summary_elements({"hc": "HC1", "distribution_summary": "文本摘要"})
    assert any("文本摘要" in e.get("content", "") for e in legacy)


def test_coordinator_hc_payload_uses_script_metrics():
    text = COORDINATOR.read_text(encoding="utf-8")
    assert "hc_metrics" in text and "quality_warnings" in text
    assert "health_summary_{mode}_r{N}.md" in text
    assert "phase1_merged_{mode}_r{N}.xlsx" in text
    assert "`HC1 reject: <原因>`" in text
    assert "HC1 打回:xxx" not in text


# ---------------------------------------------------------------- 启动:日期解析 / 取数窗口 / 项目模板

from datetime import date as _date  # noqa: E402


@pytest.fixture(scope="module")
def init_mod():
    return _load("parity_init_project", ANN_SCRIPTS / "init_project.py")


TODAY = _date(2026, 10, 28)  # 周三,上一个完整周 = W43(10-19 ~ 10-25)


@pytest.mark.parametrize("text, label, start, end, ptype", [
    ("热点周报", "2026-W43", "2026-10-19", "2026-10-25", "weekly"),
    ("热点周报 W41", "2026-W41", "2026-10-05", "2026-10-11", "weekly"),
    ("热点报告 第40周", "2026-W40", "2026-09-28", "2026-10-04", "weekly"),
    ("热点报告 09-01到09-14", "2026-W36", "2026-09-01", "2026-09-14", "custom"),
    ("热点周报 双周", "2026-W42", "2026-10-12", "2026-10-25", "biweekly"),
    ("热点周报 W40 双周", "2026-W40", "2026-09-28", "2026-10-11", "biweekly"),
])
def test_parse_period(init_mod, text, label, start, end, ptype):
    period = init_mod.parse_period(text, TODAY)
    assert (period["period_label"], period["date_start"], period["date_end"], period["period_type"]) \
        == (label, start, end, ptype)


@pytest.mark.parametrize("text", [
    "热点周报 W41 09-01到09-14",   # 周次 + 区间冲突
    "热点周报 W44",                 # 未结束
    "热点报告 02-30到03-02",        # 非法日期
    "热点报告 09-01到09-14 双周",   # 区间 + 双周冲突
])
def test_parse_period_ambiguous(init_mod, text):
    with pytest.raises(init_mod.Ambiguous) as info:
        init_mod.parse_period(text, TODAY)
    assert len(info.value.options) == 3


def test_project_dir_names_start_with_week(init_mod):
    for text, expected in [
        ("W41", "W41热点周报_20261005-20261011"),
        ("09-01到09-14", "W36自定义_20260901-20260914"),
        ("W40 双周", "W40双周报_20260928-20261011"),
    ]:
        assert init_mod.project_dir_name(init_mod.parse_period(text, TODAY)) == expected


def test_fetch_window_derivation(init_mod):
    period = init_mod.parse_period("W41", TODAY)
    assert init_mod.fetch_window(period) == {"fetch_start": "2026-09-28", "fetch_end": "2026-10-12"}


def test_init_creates_template_then_resumes(init_mod, tmp_path):
    import yaml
    created = init_mod.run("热点周报 W41", "full", TODAY, tmp_path)
    assert created["status"] == "created"
    cfg = yaml.safe_load((Path(created["project_dir"]) / "run_config.yaml").read_text("utf-8"))
    assert set(cfg) >= {"mode", "period", "fetch", "cost", "status"} and "annotation" not in cfg
    assert cfg["status"]["current_phase"] == "init"
    assert cfg["cost"]["usd_to_cny"] == 7.2

    again = init_mod.run("热点周报 W41", "full", TODAY, tmp_path)
    assert again["status"] == "resumed"
    resumed = init_mod.run("继续W41", "demo", TODAY, tmp_path)
    assert resumed["status"] == "resumed" and resumed["mode"] == "full"

    with pytest.raises(init_mod.Ambiguous):
        init_mod.run("热点周报 W41", "demo", TODAY, tmp_path)  # 同目录不同 mode
    with pytest.raises(init_mod.Ambiguous):
        init_mod.run("继续W39", "full", TODAY, tmp_path)


def _resolve(init_mod, tmp_path, text, mode, letter):
    """模拟 Coordinator:先拿 ambiguous,再把 resolutions[letter] 原样追加参数重调。"""
    with pytest.raises(init_mod.Ambiguous) as info:
        init_mod.run(text, mode, TODAY, tmp_path)
    args = info.value.resolutions[letter]
    assert args, f"{letter} 不应为 null"
    kw = {}
    it = iter(args)
    for flag in it:
        key = flag.lstrip("-").replace("-", "_")
        kw[key] = True if key in {"allow_incomplete", "overwrite"} else next(it)
    if "new_week" in kw:
        kw["new_week"] = int(kw["new_week"])
    return init_mod.run(text, mode, TODAY, tmp_path, **kw)


def test_init_ambiguous_resolutions_terminate(init_mod, tmp_path):
    """每个可执行选项按 resolutions 重调后必须结束 ambiguous(此前把选项文字拼进 --text 会死循环)。"""
    import yaml
    r = _resolve(init_mod, tmp_path, "继续W41", "full", "A")
    assert r["status"] == "created" and r["period"]["period_id"] == "W41"
    r = _resolve(init_mod, tmp_path, "热点周报 W41", "smoke", "A")
    assert r["status"] == "resumed" and r["mode"] == "full"
    r = _resolve(init_mod, tmp_path, "热点周报 W41", "smoke", "B")
    assert r["status"] == "created" and r["mode"] == "smoke"
    cfg = yaml.safe_load((Path(r["project_dir"]) / "run_config.yaml").read_text("utf-8"))
    assert cfg["mode"] == "smoke"
    r = _resolve(init_mod, tmp_path, "W40 10-06到10-12", "full", "A")
    assert r["period"]["period_id"] == "W40" and r["period"]["period_type"] == "weekly"
    r = _resolve(init_mod, tmp_path, "W40 10-06到10-12", "full", "B")
    assert r["period"]["period_type"] == "custom" and r["period"]["date_start"].endswith("10-06")
    r = _resolve(init_mod, tmp_path, "双周 09-22到10-05", "full", "B")
    assert r["period"]["period_type"] == "biweekly"
    with pytest.raises(init_mod.Ambiguous) as info:
        init_mod.run("W44", "full", TODAY, tmp_path)
    assert info.value.resolutions["C"] is None  # 需用户补信息的选项不给参数


def test_init_resume_week_does_not_prefix_match(init_mod, tmp_path):
    """继续W4 不得命中 W43/W41 项目。"""
    init_mod.run("热点周报 W41", "full", TODAY, tmp_path)
    init_mod.run("热点周报 W4", "full", TODAY, tmp_path)
    r = init_mod.run("继续W4", "full", TODAY, tmp_path)
    assert r["status"] == "resumed" and Path(r["project_dir"]).name.startswith("W4热点周报")


# ---------------------------------------------------------------- A+B

FETCH_SCRIPTS = MA / "skills/topic6-fetch-normalize/scripts"


def test_fetch_window_read_from_run_config(tmp_path):
    fetch = _load("parity_fetch_hot_topics", FETCH_SCRIPTS / "fetch_hot_topics.py")
    (tmp_path / "run_config.yaml").write_text(
        "fetch:\n  fetch_start: '2026-09-28'\n  fetch_end: '2026-10-12'\n", "utf-8")
    assert fetch.fetch_window_from_config(tmp_path) == ("2026-09-28", "2026-10-12")


def _norm_frame():
    return pd.DataFrame({
        "platform": ["微博", "微博", "抖音"],
        "hot_index": [1.0, 2.0, 3.0],
        "heat_score": [80.0, 85.0, 95.0],
    })


def test_low_max_score_exits_2_with_options(tmp_path, capsys):
    norm = _load("parity_normalize", FETCH_SCRIPTS / "log1p_p1p99_normalize.py")
    bench = {"微博": {"p1": 0, "p99": 1}, "抖音": {"p1": 0, "p99": 1}}
    with pytest.raises(SystemExit) as info:
        norm.validate_b(_norm_frame(), bench)
    assert info.value.code == norm.EXIT_LOW_MAX
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "low_max_score"
    assert len(payload["options"]) == 3
    assert norm.validate_b(_norm_frame(), bench, accept_low_max=True) == ["微博 最高分 85.0"]


def test_nan_score_is_hard_failure(tmp_path):
    norm = _load("parity_normalize", FETCH_SCRIPTS / "log1p_p1p99_normalize.py")
    frame = _norm_frame()
    frame.loc[2, "heat_score"] = float("nan")
    with pytest.raises(SystemExit) as info:
        norm.validate_b(frame, {"微博": {}, "抖音": {}})
    assert info.value.code == 1


def test_ab_status_writes_only_contract_fields():
    """status.ab 只写契约字段:A 不写 a_completed/路径/分布;B 只写 completed(+放行审计)。"""
    fetch_src = (FETCH_SCRIPTS / "fetch_hot_topics.py").read_text("utf-8")
    norm_src = (FETCH_SCRIPTS / "log1p_p1p99_normalize.py").read_text("utf-8")
    for dead in ('"a_completed"', '"platform_rows"', '"raw_json": str', '"raw_xlsx": str'):
        assert dead not in fetch_src, dead
    for dead in ('"platform_scores"', '"normalized_xlsx"', '"normalized_rows"', '"completed_at"'):
        assert dead not in norm_src, dead
    assert '{"completed": True}' in norm_src and '"low_max_accepted"' in norm_src
    sample_gate = (MA / "skills/topic6-annotation/prompts/03_阶段_抽样.md").read_text("utf-8")
    assert "status.ab.completed == true" in sample_gate


# ---------------------------------------------------------------- E

INSIGHT_SCRIPTS = MA / "skills/topic6-insight/scripts"


@pytest.fixture(scope="module")
def pipe_e():
    # 复用 test_topic6_pipeline_optimizations 的加载方式(stub openai + 隔离 stdout 包装)
    from test_topic6_pipeline_optimizations import _load_pipeline_e_module
    return _load_pipeline_e_module()


def test_e_requires_hc2_passed(pipe_e, tmp_path):
    (tmp_path / "run_config.yaml").write_text("status:\n  hc2_passed: false\n", "utf-8")
    assert pipe_e.check_hc2_gate(tmp_path, "skip_sampling")
    assert pipe_e.check_hc2_gate(tmp_path, "demo") is None
    (tmp_path / "run_config.yaml").write_text("status:\n  hc2_passed: true\n", "utf-8")
    assert pipe_e.check_hc2_gate(tmp_path, "skip_sampling") is None


def test_e_small_section_detected(pipe_e, tmp_path):
    (tmp_path / "e1_v1.md").write_text("x" * 200, "utf-8")
    (tmp_path / "e2_v1.md").write_text("# 二\n", "utf-8")
    secs = [{"id": "e1", "output_file": "e1_v1.md"}, {"id": "e2", "output_file": "e2_v1.md"},
            {"id": "e3", "output_file": "e3_v1.md"}]
    assert pipe_e.too_small_sections(tmp_path, secs) == ["e2", "e3"]


def test_e2_fixed_text_is_marked_exempt_from_size_gate(pipe_e, tmp_path):
    """E2 上/下周均无节点走固定话术(约 109 字节),须带 fixed_text 标记,不被 100B 门槛误判失败。"""
    import asyncio
    flags = {"last_week": {"has_data": False, "data_md": None, "empty_text": "上周无节点。"},
             "next_week": {"has_data": False, "data_md": None, "empty_text": "本周无节点。"}}
    (tmp_path / "e2_flags.json").write_text(json.dumps(flags, ensure_ascii=False), "utf-8")
    sec = {"id": "e2", "name": "营销节点", "data_file": "e2_flags.json"}

    async def no_llm(*_a, **_k):
        raise AssertionError("固定话术分支不应调用 LLM")

    result = asyncio.run(pipe_e.build_e2_task(no_llm, sec, tmp_path))
    assert result["status"] == "ok" and result["fixed_text"] is True
    assert "上周无节点" in result["content"]


# ---------------------------------------------------------------- F 自校验

@pytest.fixture(scope="module")
def selfcheck():
    return _load("parity_report_self_check", INSIGHT_SCRIPTS / "report_self_check.py")


GOOD_REPORT = """# 社媒热点周刊 · 2026-W41

# 一、行业及热门话题

**行业洞察：**
- 上周营销可用热搜 1484 条，共覆盖 26 个行业，体育赛事热度最高，为 82.4 分。

| TOP10行业 | 热点总计 |
| --- | --- |
| 音乐 | [李玉刚《万疆》免费授权](https://www.zhihu.com/question/1) |

# 二、营销节点

本周 11 个节点覆盖 6 天，其中重阳节为长筹备节点，建议提前准备内容。

# 三、平台新鲜事

### 微博

微博本期热门话题以大众竞技、娱乐向内容为主，基调轻松，可作为轻量借势参考。

# 四、营销发现

### 营销观察
#### 案例 1：示例（B站 · 90.2分）
- 营销发现：将商业合作做成竞技内容，弱化硬广感知，其他品牌可复用同类玩法。
- 链接：https://www.bilibili.com/video/av1/

### 消费洞察
#### 洞察 1：泛消费者决策回归理性
- 行为反应：消费者对网红溢价产品追捧度下降，决策更务实，理性消费趋势明显。
- 营销发现：大众品类可强化实际价值传递。
"""


def test_self_check_clean_report_passes(selfcheck):
    result = selfcheck.check(GOOD_REPORT)
    assert result["status"] == "pass", result["issues"]


def test_self_check_ignores_hotsearch_titles_in_links(selfcheck):
    # 「万疆」「10万游客」等在链接文字里,不算热度单位/模糊数字
    text = GOOD_REPORT.replace("免费授权](", "免费授权 10万游客约 3 人](")
    assert selfcheck.check(text)["status"] == "pass"


@pytest.mark.parametrize("inject, section, check", [
    ("综上所述，本周体育领先。", "e1", "LLM 痕迹"),
    ("热度达 120万。", "e1", "热度单位"),
    ("本版块旨在梳理节点。", "e2", "版块意图泄露"),
    ("微博热度呈现年度趋势。", "e3", "越界推断"),
])
def test_self_check_fail_rules(selfcheck, inject, section, check):
    anchor = {"e1": "**行业洞察：**", "e2": "# 二、营销节点", "e3": "### 微博", "e4": "### 营销观察"}[section]
    result = selfcheck.check(GOOD_REPORT.replace(anchor, f"{anchor}\n\n{inject}\n", 1))
    assert result["status"] == "fail"
    assert section in result["failed_sections"]
    assert any(i["check"] == check for i in result["issues"])


def test_self_check_e4_internal_wording_and_missing_finding(selfcheck):
    text = GOOD_REPORT.replace("### 营销观察", "### 营销观察\n该事件乙方能不能接需评估。")
    text = text.replace("- 营销发现：大众品类可强化实际价值传递。\n", "")
    result = selfcheck.check(text)
    checks = {i["check"] for i in result["issues"]}
    assert {"打标内部措辞", "消费洞察启示段"} <= checks
    assert result["failed_sections"] == ["e4"]


def test_self_check_missing_section_and_bad_link(selfcheck):
    text = GOOD_REPORT.replace("# 二、营销节点", "# 二、节点").replace(
        "https://www.bilibili.com/video/av1/", "待补充")
    result = selfcheck.check(text)
    checks = {(i["section"], i["check"]) for i in result["issues"]}
    assert ("e2", "章节完整性") in checks
    assert ("e4", "超链接格式") in checks


def test_self_check_warn_does_not_block(selfcheck):
    text = GOOD_REPORT.replace("### 微博\n", "### 微博\n\n微博热度高于抖音，话题更集中，值得关注。\n", 1)
    result = selfcheck.check(text)
    assert result["status"] == "warn" and result["failed_sections"] == []


def test_coordinator_phase5_contract():
    text = COORDINATOR.read_text(encoding="utf-8")
    for needle in ("init_project.py", "`ambiguous`", "--accept-low-max", "low_max_score",
                   "report_self_check.py", "--skip-data-prep --sections", "HC3 打回",
                   "hc2_passed"):
        assert needle in text, needle
