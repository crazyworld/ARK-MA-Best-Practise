"""smoke 第二轮基础表回归测试。

smoke 第二轮按约定传 --mode skip_sampling，但 C0/C3/R1~R5 只标注了 30 条样本。
合并脚本必须读 run_config.yaml 顶层 mode=smoke，以 03_抽样/sample_500.xlsx 为基础表，
否则会以全量 normalized 表 LEFT JOIN，缺失率 ~99% 触发筛选熔断。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "cases" / "topic6" / "ma-resources" / "skills" / "topic6-annotation" / "scripts"
)
FULL_ROWS = 300
SAMPLE_ROWS = 30
RUN_ID = 2


def _run(script: str, proj: Path, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script),
         "--project-dir", str(proj), "--mode", mode, "--run-id", str(RUN_ID)],
        capture_output=True, text=True, timeout=120,
    )


def _make_project(tmp_path: Path, top_mode: str | None, with_sample: bool = True) -> Path:
    proj = tmp_path / "W43热点周报_20261019-20261025"
    full = pd.DataFrame({
        "row_id": range(1, FULL_ROWS + 1),
        "platform": ["抖音"] * FULL_ROWS,
        "title": [f"t{i}" for i in range(1, FULL_ROWS + 1)],
    })
    sample_ids = list(range(1, FULL_ROWS + 1, FULL_ROWS // SAMPLE_ROWS))[:SAMPLE_ROWS]
    sample = full[full["row_id"].isin(sample_ids)]

    (proj / "02_标准化").mkdir(parents=True)
    full.to_excel(proj / "02_标准化" / "hot_topics_normalized.xlsx", index=False)
    if with_sample:
        (proj / "03_抽样").mkdir()
        sample.to_excel(proj / "03_抽样" / "sample_500.xlsx", index=False)

    # 标注结果只覆盖样本行（与 smoke 实际一致）
    c0_dir = proj / "04_标注" / "C0_基础事实"
    c0_dir.mkdir(parents=True)
    c0 = pd.DataFrame({"row_id": sample_ids})
    for col in ["c0_商业实体", "c0_热点驱动词", "c0_行业归属", "c0_营销触发方式",
                "c0_营销维度", "c0_平台原生形式", "c0_不可用原因", "c0_判断说明"]:
        c0[col] = "x"
    c0["c0_是否营销可用"] = "是"
    c0["c0_parse_error"] = 0
    c0.to_excel(c0_dir / f"c0_postprocess_r{RUN_ID}.xlsx", index=False)

    if top_mode is not None:
        (proj / "run_config.yaml").write_text(
            yaml.safe_dump({"mode": top_mode, "status": {}}, allow_unicode=True),
            encoding="utf-8",
        )
    return proj


def _phase1(proj: Path, mode: str) -> pd.DataFrame:
    return pd.read_excel(proj / "04_合并" / f"phase1_merged_{mode}_r{RUN_ID}.xlsx")


def _wide(proj: Path, mode: str) -> pd.DataFrame:
    return pd.read_excel(proj / "05_合并" / f"wide_table_{mode}_r{RUN_ID}.xlsx")


class TestSmokeSecondRound:
    def test_c0_merge_uses_sample_base(self, tmp_path):
        proj = _make_project(tmp_path, "smoke")
        r = _run("c0_merge_phase1.py", proj, "skip_sampling")
        assert r.returncode == 0, r.stderr
        df = _phase1(proj, "skip_sampling")
        assert len(df) == SAMPLE_ROWS
        assert int(df["是否营销可用"].isna().sum()) == 0

    def test_filter_does_not_trip(self, tmp_path):
        proj = _make_project(tmp_path, "smoke")
        assert _run("c0_merge_phase1.py", proj, "skip_sampling").returncode == 0
        r = _run("c0_filter_usable.py", proj, "skip_sampling")
        assert r.returncode == 0, r.stderr + r.stdout
        usable = pd.read_excel(
            proj / "04_标注" / "_可用子集" / f"usable_subset_skip_sampling_r{RUN_ID}.xlsx")
        assert len(usable) == SAMPLE_ROWS

    def test_merge_annotations_uses_sample_base(self, tmp_path):
        proj = _make_project(tmp_path, "smoke")
        r = _run("merge_annotations.py", proj, "skip_sampling")
        assert r.returncode == 0, r.stderr
        df = _wide(proj, "skip_sampling")
        assert len(df) == SAMPLE_ROWS

    def test_merge_annotations_smoke_missing_sample_fails_loudly(self, tmp_path):
        proj = _make_project(tmp_path, "smoke", with_sample=False)
        r = _run("merge_annotations.py", proj, "skip_sampling")
        assert r.returncode != 0
        assert "smoke 模式样本表不存在" in (r.stderr + r.stdout)


class TestCleanness:
    """非 smoke 组合行为不变。"""

    @pytest.mark.parametrize("top_mode", ["full", "skip_sampling", None])
    def test_skip_sampling_still_uses_full_base(self, tmp_path, top_mode):
        proj = _make_project(tmp_path, top_mode)
        assert _run("c0_merge_phase1.py", proj, "skip_sampling").returncode == 0
        assert len(_phase1(proj, "skip_sampling")) == FULL_ROWS
        assert _run("merge_annotations.py", proj, "skip_sampling").returncode == 0
        assert len(_wide(proj, "skip_sampling")) == FULL_ROWS

    @pytest.mark.parametrize("mode", ["full", "demo"])
    def test_full_demo_use_sample_base(self, tmp_path, mode):
        proj = _make_project(tmp_path, mode)
        assert _run("c0_merge_phase1.py", proj, mode).returncode == 0
        assert len(_phase1(proj, mode)) == SAMPLE_ROWS
        assert _run("merge_annotations.py", proj, mode).returncode == 0
        assert len(_wide(proj, mode)) == SAMPLE_ROWS

    def test_smoke_first_round_full_unchanged(self, tmp_path):
        proj = _make_project(tmp_path, "smoke")
        assert _run("c0_merge_phase1.py", proj, "full").returncode == 0
        assert len(_phase1(proj, "full")) == SAMPLE_ROWS


# ---------------------------------------------------------------------------
# datahub_annotate 复用判断:同路径不同内容(smoke 第二轮重抽样)不得复用
# ---------------------------------------------------------------------------
import importlib.util
import json
import types


def _load_annotate():
    spec = importlib.util.spec_from_file_location(
        "topic6_datahub_annotate_fp", SCRIPTS / "datahub_annotate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launch_args(proj: Path, sample: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        task="c0", project_dir=str(proj), input=str(sample),
        prompt_file=str(proj / "prompt.md"), model_id="DeepSeek-V4-Flash",
        run_id=RUN_ID, mode="skip_sampling",
    )


def _write_completion(proj: Path, sample: Path, fingerprint: str | None) -> Path:
    task_dir = proj / "04_标注" / "C0_基础事实"
    task_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": RUN_ID, "source_input_file": str(sample.resolve())}
    if fingerprint is not None:
        meta["source_input_fingerprint"] = fingerprint
    path = task_dir / "c0_completion_meta.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    return path


class TestAnnotateReuse:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        module = _load_annotate()
        proj = tmp_path / "proj"
        sample = proj / "03_抽样" / "sample_500.xlsx"
        sample.parent.mkdir(parents=True)
        pd.DataFrame({"row_id": [1, 2, 3]}).to_excel(sample, index=False)
        calls: list = []

        class FakeProcess:
            pid = __import__("os").getpid()

        monkeypatch.setattr(module.subprocess, "Popen",
                            lambda cmd, **_kw: calls.append(cmd) or FakeProcess())
        return module, proj, sample, calls

    def test_fingerprint_stable_across_rewrite(self, env):
        module, _proj, sample, _calls = env
        before = module._input_fingerprint(sample)
        pd.read_excel(sample).to_excel(sample, index=False)  # 同内容重写,字节会变
        assert module._input_fingerprint(sample) == before

    def test_same_path_new_content_relaunches(self, env):
        module, proj, sample, calls = env
        completion = _write_completion(proj, sample, module._input_fingerprint(sample))
        pd.DataFrame({"row_id": [7, 8, 9]}).to_excel(sample, index=False)  # 重抽样覆盖
        result = module.launch_background(_launch_args(proj, sample))
        assert result["status"] == "submitted"
        assert len(calls) == 1
        assert not completion.exists()  # 防止 wait_datahub_batch 误判 done

    def test_same_content_reuses(self, env):
        module, proj, sample, calls = env
        _write_completion(proj, sample, module._input_fingerprint(sample))
        result = module.launch_background(_launch_args(proj, sample))
        assert result["status"] == "done"
        assert calls == []

    def test_legacy_meta_without_fingerprint_reuses(self, env):
        module, proj, sample, calls = env
        _write_completion(proj, sample, None)
        result = module.launch_background(_launch_args(proj, sample))
        assert result["status"] == "done"
        assert calls == []

    def test_submission_rejects_changed_content(self, env):
        module, proj, sample, _calls = env
        prompt = proj / "prompt.md"
        prompt.write_text("p", encoding="utf-8")
        submission = {
            "task": "c0", "run_id": RUN_ID, "task_id": 1, "model_id": "m",
            "prompt_file": str(prompt), "source_input_file": str(sample.resolve()),
            "source_input_fingerprint": module._input_fingerprint(sample),
        }
        kw = dict(task="c0", run_id=RUN_ID, input_path=sample,
                  prompt_file=str(prompt), model_id="m")
        assert module._submission_matches_request(submission, **kw)
        pd.DataFrame({"row_id": [7, 8, 9]}).to_excel(sample, index=False)
        assert not module._submission_matches_request(submission, **kw)
