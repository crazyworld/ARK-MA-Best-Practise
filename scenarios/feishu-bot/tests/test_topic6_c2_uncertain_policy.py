"""Q2(W45 job_fe11fa6b2761):C2 「不确定」成员导致整簇拆回原标题 → 与客户原版对齐。

根因:MA 用 --review-policy record 无人值守跑 rc.14,non_events 门禁不停等、无人复核,
UNCERTAIN/needs_evidence 记录永远未决;retain_original 把含未决成员的整组拆回原标题
(本期 280 条,《后会友期》36 条全部成单条)。客户原版 ada_topic6_v2_261110 的
c2_run_all_platforms.py::_run_stages_once 逐平台直接调 vendor 00→09,不经 run_c2.py、不调用
retain_original(W43 实跑产物 149 条 UNCERTAIN 中 78 条正常进入多成员簇)。

修复:run_c2 新增 --uncertain-policy keep(MA 封装传入;默认 split=上游语义不变)。
keep 时合并组不因未决成员拆散,只把未决成员记入 original_retained.json 的
uncertain_kept 审计;NON_EVENT 排除、技术降级、人工强制保留(forced)规则不变。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = (Path(__file__).resolve().parents[1] / "cases" / "topic6" / "ma-resources"
           / "skills" / "topic6-event-registry" / "scripts")
sys.path.insert(0, str(SCRIPTS))

from retain_original import retain_original  # noqa: E402


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _event(eid: str, name: str, members: list[str]) -> dict:
    return {"event_id": eid, "block_id": "B1", "event_name": name, "anchor_title": name,
            "event_description": "d", "member_record_ids": members, "member_count": len(members),
            "singleton": len(members) == 1, "facets": [], "identity_core": {"k": "v"},
            "member_confidence": {m: 90 for m in members}, "member_notes": {},
            "name_check": {"status": "OK"}, "state": {"member_count": len(members)}}


@pytest.fixture()
def run_dir(tmp_path):
    root = tmp_path / "run"
    (root / "work").mkdir(parents=True)
    (root / "out").mkdir()
    source = [("R1", "后会友期结局"), ("R2", "后会友期最后一期"), ("R3", "如何看待后会友期为什么能火"),
              ("R4", "三甲医院医生推荐患者去三乙医院"), ("R5", "做饭教程")]
    (root / "work" / "source.csv").write_text(
        "record_id,platform,title\n" + "".join(f"{r},微博,{t}\n" for r, t in source), encoding="utf-8")
    _jsonl(root / "work" / "eventness.jsonl", [
        {"record_id": "R1", "eventness": "EVENT"},
        {"record_id": "R2", "eventness": "EVENT"},
        {"record_id": "R3", "eventness": "UNCERTAIN", "needs_evidence": True},  # 观点类标题 → 未决
        {"record_id": "R4", "eventness": "EVENT"},
        {"record_id": "R5", "eventness": "NON_EVENT"},
    ])
    _jsonl(root / "work" / "block_events.jsonl", [
        _event("E1", "《后会友期》大结局及收官讨论", ["R1", "R2", "R3"]),
        _event("E2", "三甲医院推荐患者转院至三乙医院", ["R4"]),
    ])
    _jsonl(root / "work" / "frames.jsonl", [{"record_id": r, "title": t, "action": "x"} for r, t in
                                            [("R1", "a"), ("R2", "b"), ("R3", "c"), ("R4", "d")]])
    return root


def test_default_split_policy_is_upstream_semantics(run_dir):
    """默认(split)= rc.12/rc.14 上游行为:含未决成员的组拆回原标题。"""
    retain_original(run_dir, "2026W45")
    events = _read_jsonl(run_dir / "work" / "block_events.jsonl")
    names = sorted(e["event_name"] for e in events)
    assert "《后会友期》大结局及收官讨论" not in names
    assert {"后会友期结局", "后会友期最后一期", "如何看待后会友期为什么能火"} <= set(names)
    report = json.loads((run_dir / "out" / "original_retained.json").read_text(encoding="utf-8"))
    assert report["retained_count"] == 3


def test_keep_policy_keeps_group_and_audits_uncertain_members(run_dir):
    retain_original(run_dir, "2026W45", uncertain_policy="keep")
    events = _read_jsonl(run_dir / "work" / "block_events.jsonl")
    by_name = {e["event_name"]: e for e in events}
    assert by_name["《后会友期》大结局及收官讨论"]["member_record_ids"] == ["R1", "R2", "R3"]
    assert by_name["《后会友期》大结局及收官讨论"]["identity_core"] == {"k": "v"}  # 组身份不被清空
    assert len(events) == 2
    report = json.loads((run_dir / "out" / "original_retained.json").read_text(encoding="utf-8"))
    assert report["retained_count"] == 0 and report["records"] == []
    assert report["uncertain_policy"] == "keep"
    assert [r["record_id"] for r in report["uncertain_kept"]] == ["R3"]
    assert report["uncertain_kept"][0]["event_id"] == "E1"
    # 未决成员的 Frame 不被清空(组未拆,保留其抽取结果供下游命名/排名)
    frames = {f["record_id"]: f for f in _read_jsonl(run_dir / "work" / "frames.jsonl")}
    assert frames["R3"]["action"] == "x"


def test_keep_policy_still_honours_manual_forced_retention(run_dir):
    """人工审核强制保留(forced,review_revision 路径)不受 keep 影响,仍整组拆回。"""
    retain_original(run_dir, "2026W45", forced={"R1": "误合"}, uncertain_policy="keep")
    names = {e["event_name"] for e in _read_jsonl(run_dir / "work" / "block_events.jsonl")}
    assert "《后会友期》大结局及收官讨论" not in names and "后会友期结局" in names


def test_keep_policy_rejects_unknown_value(run_dir):
    with pytest.raises(ValueError, match="uncertain_policy"):
        retain_original(run_dir, "2026W45", uncertain_policy="maybe")


def test_run_c2_flag_default_split_and_ma_wrapper_passes_keep():
    run_c2 = (SCRIPTS / "run_c2.py").read_text(encoding="utf-8")
    assert 'choices=["split", "keep"], default="split"' in run_c2
    assert "uncertain_policy=cfg.get('uncertain_policy', 'split')" in run_c2
    wrapper = (SCRIPTS / "run_topic6_c2.py").read_text(encoding="utf-8")
    assert '"--uncertain-policy", "keep"' in wrapper


# ---------------------------------------------------------------- 端到端(离线 mock relay,零付费)

def test_ma_wrapper_end_to_end_keeps_uncertain_member_in_group(tmp_path):
    """MA 封装(keep):组内 1 条「如何看待」UNCERTAIN 成员不再把 8 条跨平台同组拆回原标题。"""
    import os, subprocess, time
    import pandas as pd
    import yaml  # noqa: F401
    mock = Path(__file__).resolve().parent / "fixtures" / "c2_mock_relay.py"
    port_file = tmp_path / "relay.port"
    relay = subprocess.Popen([sys.executable, str(mock), "--port-file", str(port_file)])
    try:
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.05)
        project = tmp_path / "proj"
        subset = project / "04_标注" / "_可用子集"
        subset.mkdir(parents=True)
        rows, n = [], 0
        for platform in ("微博", "抖音", "知乎", "B站"):
            for group in ("华为", "小米"):
                for k in range(2):
                    n += 1
                    title = f"#{group}#新品发布会第{k}场{platform}"
                    if group == "华为" and platform == "知乎" and k == 1:
                        title = f"如何看待#{group}#新品发布会{platform}"
                    rows.append({"row_id": f"r{n:04d}", "平台": platform, "热点标题": title,
                                 "标准化热度分": 100 - n})
        pd.DataFrame(rows).to_excel(subset / "usable_subset_full_r1.xlsx", index=False)
        (project / "run_config.yaml").write_text("mode: full\n", encoding="utf-8")
        env = {**os.environ, "LLM_RELAY_BASE_URL": f"http://127.0.0.1:{port_file.read_text().strip()}/v1",
               "LLM_RELAY_API_KEY": "test", "C2_CHAT_MODEL_ID": "DeepSeek-V4.1-Flash",
               "EMBEDDING_MODEL_ID": "Doubao-embedding"}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "run_topic6_c2.py"), "--project-dir", str(project),
                               "--mode", "full", "--run-id", "1"], env=env, capture_output=True, text=True,
                              timeout=300)
        assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
        c2_dir = project / "04_标注" / "C2_事件归档"
        result = pd.read_excel(c2_dir / "c2_event_result_r1.xlsx")
        counts = result["一级事件名"].value_counts()
        assert sorted(counts.tolist()) == [8, 8], counts.to_dict()   # 两组都完整,未拆回原标题
        engine_out = c2_dir / "c2_run" / "engine" / "out"
        retained = json.loads((engine_out / "original_retained.json").read_text(encoding="utf-8"))
        assert retained["uncertain_policy"] == "keep" and retained["retained_count"] == 0
        assert [r["title"] for r in retained["uncertain_kept"]] == ["如何看待#华为#新品发布会知乎"]
        quality = json.loads((engine_out / "c2_quality.json").read_text(encoding="utf-8"))
        assert quality["uncertain_policy"] == "keep" and quality["uncertain_kept_count"] == 1
        assert quality["related_records_split_count"] == 0
    finally:
        relay.kill()
        relay.wait()


def test_member_review_flag_default_on_and_ma_wrapper_passes_off():
    """原版 c2_run_all_platforms 只跑 00→09,无 x2 置信度筛查 / x3 双向复核。
    run_c2 默认 on = 上游 rc.14 语义;MA 封装传 off 对齐原版。"""
    run_c2 = (SCRIPTS / "run_c2.py").read_text(encoding="utf-8")
    assert 'choices=["on", "off"], default="on"' in run_c2
    assert "cfg.get('member_review', 'on') == 'on'" in run_c2
    wrapper = (SCRIPTS / "run_topic6_c2.py").read_text(encoding="utf-8")
    assert '"--frontend", "legacy"' in wrapper and '"--frontend", "fused"' not in wrapper
    assert '"--member-review", "off"' in wrapper
