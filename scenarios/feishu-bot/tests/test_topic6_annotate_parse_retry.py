"""datahub_annotate：LLM 返回的宽松 JSON 解析 + 失败行一次性重试回填。

历史问题（sesn-20261026032124-r6ac1 C3 首轮 28/30）：
- T1156 返回被 ```json 围栏包裹并附加文字 → 旧 _strip_fence 只处理"整段即围栏"，解析失败
- T3009 relay_empty_completion 空返回 → 无任何重试
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "cases/topic6/ma-resources/skills/topic6-annotation/scripts/datahub_annotate.py"
)


@pytest.fixture(scope="module")
def dh():
    import sys

    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("datahub_annotate_t", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # C3 时间窗依赖沙箱 /mnt/skills 下的营销日历，与解析/重试无关
    module._c3_timewindow = lambda df: df
    return module


C3_OK = '{"关联节点":[{"节点名称":"双十一"}]}'


@pytest.mark.parametrize(
    "raw",
    [
        C3_OK,
        f"```json\n{C3_OK}\n```",
        f"```\n{C3_OK}\n```",
        f"```json\n{C3_OK}\n```\n说明：命中一个节点",
        f"结果如下：\n```json\n{C3_OK}\n```",
        f"<think>先判断节点</think>\n{C3_OK}",
        f"{C3_OK}\n\n以上为判断结果",
        f"输出：{C3_OK} 完毕",
    ],
)
def test_parse_c3_tolerates_fence_and_extra_text(dh, raw):
    assert dh._parse_c3(raw) == [{"节点名称": "双十一"}]


def test_parse_c3_empty_list_and_failures(dh):
    assert dh._parse_c3('```json\n{"关联节点":[]}\n```\n无') == []
    assert dh._parse_c3("") is None
    assert dh._parse_c3(float("nan")) is None
    assert dh._parse_c3("模型拒绝回答") is None
    assert dh._parse_c3('{"其他":1}') is None


def test_parse_dict_braces_inside_strings(dh):
    raw = '前缀 {"判断说明":"含 } 和 { 的文本","是否营销可用":"是"} 后缀'
    assert dh._parse_dict(raw) == {"判断说明": "含 } 和 { 的文本", "是否营销可用": "是"}
    assert dh._parse_dict('```json\n{"a": 1}\n```\n附注') == {"a": 1}


def _setup_retry(tmp_path, dh, monkeypatch, retry_rows, *, fail=False):
    upload = tmp_path / "input.xlsx"
    pd.DataFrame({"row_id": ["T1", "T2", "T3"], "title": ["a", "b", "c"]}).to_excel(upload, index=False)
    calls = {"upload": 0, "create": 0}

    def fake_upload(_key, path):
        calls["upload"] += 1
        calls["retry_input"] = pd.read_excel(path)["row_id"].tolist()
        return "ds-retry"

    def fake_create(*_args):
        calls["create"] += 1
        if fail:
            raise RuntimeError("datahub 500")
        return 9001

    def fake_poll(_key, task_id):
        return {"result_list": retry_rows, "total_tokens": 50, "total_consume": 0.01}

    monkeypatch.setattr(dh, "_upload_file", fake_upload)
    monkeypatch.setattr(dh, "_create_task", fake_create)
    monkeypatch.setattr(dh, "_poll_until_done", fake_poll)
    monkeypatch.setattr(dh, "_get_task", lambda *a, **k: {"result_list": retry_rows})
    return upload, calls


def test_retry_only_failed_rows_and_backfill(tmp_path, dh, monkeypatch):
    df_raw = pd.DataFrame({
        "row_id": ["T1", "T2", "T3"],
        "title": ["a", "b", "c"],
        "llm_result": [C3_OK, "", "```json\n{\"关联节点\":[]}\n``` 附注"],
    })
    df_post, stats = dh._postprocess(df_raw.copy(), "c3")
    failed = dh._failed_row_ids(df_post, "c3")
    assert failed == ["T2"]  # T3 在新解析器下已能解析，只有空返回需要重试
    upload, calls = _setup_retry(
        tmp_path, dh, monkeypatch,
        [{"row_id": "T2", "title": "b", "llm_result": C3_OK}],
    )
    df_new, info = dh._retry_failed_rows(
        "k", task="c3", task_dir=tmp_path, run_id=1, upload_path=upload,
        prompt_text="p", model_id="m", failed_ids=failed, df_raw=df_raw,
    )
    assert calls["retry_input"] == ["T2"]
    assert info["retry_task_id"] == 9001 and info["retry_tokens"] == 50
    _, stats2 = dh._postprocess(df_new, "c3")
    assert stats2["invalid"] == 0
    assert df_new.loc[df_new.row_id == "T1", "llm_result"].item() == C3_OK

    # 重启续跑：同一 run_id + 同一失败集合复用已建的重试任务，不重复提交
    dh._retry_failed_rows(
        "k", task="c3", task_dir=tmp_path, run_id=1, upload_path=upload,
        prompt_text="p", model_id="m", failed_ids=failed, df_raw=df_raw,
    )
    assert calls["create"] == 1
    assert json.loads((tmp_path / "c3_retry_meta.json").read_text())["task_id"] == 9001


def test_retry_still_failing_keeps_invalid(tmp_path, dh, monkeypatch):
    df_raw = pd.DataFrame({"row_id": ["T1", "T2", "T3"], "title": list("abc"),
                           "llm_result": [C3_OK, "", C3_OK]})
    upload, _ = _setup_retry(
        tmp_path, dh, monkeypatch, [{"row_id": "T2", "title": "b", "llm_result": ""}],
    )
    df_new, _ = dh._retry_failed_rows(
        "k", task="c3", task_dir=tmp_path, run_id=1, upload_path=upload,
        prompt_text="p", model_id="m", failed_ids=["T2"], df_raw=df_raw,
    )
    _, stats = dh._postprocess(df_new, "c3")
    assert stats["invalid"] == 1 and stats["total"] == 3


def test_retry_works_for_dict_tasks(tmp_path, dh, monkeypatch):
    df_raw = pd.DataFrame({"row_id": ["T1", "T2"], "title": ["a", "b"],
                           "llm_result": ['{"是否营销可用":"是"}', None]})
    task = next(t for t in dh.TASK_SCHEMAS if t == "c0")
    df_post, _ = dh._postprocess(df_raw.copy(), task)
    assert dh._failed_row_ids(df_post, task) == ["T2"]
    upload, _ = _setup_retry(
        tmp_path, dh, monkeypatch,
        [{"row_id": "T2", "title": "b", "llm_result": '{"是否营销可用":"否"}'}],
    )
    df_new, _ = dh._retry_failed_rows(
        "k", task=task, task_dir=tmp_path, run_id=1, upload_path=upload,
        prompt_text="p", model_id="m", failed_ids=["T2"], df_raw=df_raw,
    )
    _, stats = dh._postprocess(df_new, task)
    assert stats["invalid"] == 0
