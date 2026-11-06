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


_C0_HEAD = ('{"商业实体":null,"热点驱动词":"吃蟹","行业归属":"食品零食","营销触发方式":"无主体潜在机会",'
            '"营销维度":"品类/行业相关","平台原生形式":"否","不可用原因":"无","是否营销可用":"是",'
            '"判断说明":"食品零食｜南北吃蟹差异讨论"')


@pytest.mark.parametrize("raw", [
    _C0_HEAD,                 # W43 实测:缺结尾 }
    _C0_HEAD + '}"',          # 多出 "
    _C0_HEAD + '}]',          # 多出 ]
    _C0_HEAD[:-1],            # 判断说明字符串未闭合
])
def test_parse_dict_repairs_truncated_tail_with_primary(dh, raw):
    obj = dh._parse_dict(raw, "是否营销可用")
    assert obj["是否营销可用"] == "是" and obj["行业归属"] == "食品零食"


def test_parse_dict_tail_repair_requires_primary_and_valid_value(dh):
    assert dh._parse_dict(_C0_HEAD) is None                       # 未传 primary 不补全
    half = '{"商业实体":null,"热点驱动词":"吃蟹","是否营销可用":"'   # 主字段被截断
    assert dh._parse_dict(half, "是否营销可用") is None
    assert dh._parse_dict('{"是否营销可用":"可能","判断说明":"x"', "是否营销可用") is None
    assert dh._parse_dict("模型拒绝回答", "是否营销可用") is None


def test_postprocess_counts_repaired_rows_as_valid(dh):
    df = pd.DataFrame({"row_id": ["T1", "T2"], "llm_result": [_C0_HEAD, None]})
    out, stats = dh._postprocess(df, "c0")
    assert out["c0_parse_error"].tolist() == [0, 1]
    assert out.loc[0, "c0_是否营销可用"] == "是"
    assert stats["valid"] == 1


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


def test_annotate_retries_failed_rows_up_to_three_rounds(tmp_path, dh, monkeypatch):
    """对齐 CC V2.0 补标最多 3 轮:每轮只提交上轮剩余失败行,第 2 轮起审计文件按轮分开。"""
    project = tmp_path / "proj"
    (project / "04_标注").mkdir(parents=True)
    upload = tmp_path / "in.xlsx"
    pd.DataFrame({"row_id": ["T1", "T2", "T3"], "title": list("abc")}).to_excel(upload, index=False)
    prompt = tmp_path / "p.md"
    prompt.write_text("p", encoding="utf-8")
    first = pd.DataFrame({"row_id": ["T1", "T2", "T3"], "title": list("abc"),
                          "llm_result": [C3_OK, "", ""]})
    # 第 1 轮修好 T2、T3 仍空;第 2 轮修好 T3
    rounds = [[{"row_id": "T2", "title": "b", "llm_result": C3_OK},
               {"row_id": "T3", "title": "c", "llm_result": ""}],
              [{"row_id": "T3", "title": "c", "llm_result": C3_OK}]]
    submitted = []
    monkeypatch.setenv("DATAHUB_API_KEY", "k")
    monkeypatch.setattr(dh, "_list_models", lambda _k: [
        {"model_id": "M", "platform": "ByteDance", "input_price": 0, "output_price": 0}])
    monkeypatch.setattr(dh, "_prepare_input", lambda _p: upload)

    def fake_upload(_k, path):
        submitted.append(pd.read_excel(path)["row_id"].tolist())
        return "ds"

    created = []

    def fake_create(*_a):
        created.append(2000 + len(created))
        return created[-1]

    def fake_poll(_k, task_id):
        if task_id == 2000:
            return {"result_url": "u", "total_tokens": 10}
        return {"result_list": rounds[task_id - 2001], "total_tokens": 5}

    monkeypatch.setattr(dh, "_upload_file", fake_upload)
    monkeypatch.setattr(dh, "_create_task", fake_create)
    monkeypatch.setattr(dh, "_poll_until_done", fake_poll)
    monkeypatch.setattr(dh, "_download_result", lambda _u, out: first.to_excel(out, index=False))
    monkeypatch.setattr(dh, "_get_task", lambda _k, tid, **_kw: {"result_list": rounds[tid - 2001]})
    meta = dh.annotate("c3", str(project), str(upload), str(prompt), "M", run_id=1)
    assert submitted[1:] == [["T2", "T3"], ["T3"]]
    assert meta["retry"]["rounds"] == 2 and meta["retry"]["recovered_rows"] == 2
    assert meta["retry"]["retry_tokens"] == 10
    task_dir = project / "04_标注" / dh.TASK_META["c3"]["subdir"]
    assert (task_dir / "c3_retry_meta.json").exists() and (task_dir / "c3_retry_2_meta.json").exists()
    assert dh.FAILED_ROW_RETRY_ROUNDS == 3
