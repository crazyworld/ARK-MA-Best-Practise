"""topic6 数据节点 mock 回归测试（数据来源：~/work/mock_data/ada_topic6_mock_data）。

全流程里只有「吃上游数据、产下游数据」的确定性脚本节点才需要数据；外部调用
（MCP 取数、DataHub、C2 LLM 链、E 洞察 LLM、飞书/妙搭发布）不在本测试范围。
每个节点：用 mock 中它的上游输入单独起一个项目目录 → 跑真实脚本 → 与 mock 中
同一次线上运行的产物逐单元格比对。不一致 = 该节点行为变了（预期内就更新 mock）。

节点覆盖（两轮：sample=500 条样本 / full=3677 条全量）：
  A  fetch_hot_topics --skip-fetch     raw.json → hot_topics_raw.xlsx
  B  log1p_p1p99_normalize             raw.xlsx → normalized.xlsx
  抽样 sample_500 --size 500            normalized → sample_500.xlsx
  C后处理 datahub_annotate._postprocess result_raw → postprocess（7 任务；线上失败行允许被救回）
  筛选 c0_merge_phase1 + c0_filter_usable  postprocess → phase1_merged / usable_subset
  D  merge_annotations                 postprocess + C2 → wide_table
  E数据 run_stats                      wide_table → e1~e4 data / flags / candidates
  F  pipeline_f                        e1~e4_v1.md + run_config → 报告 md

运行：cases/topic6/run_tests.sh -k mock_nodes
mock 目录不存在时整体 skip（CI / 他人机器无该数据）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

TOPIC6 = Path(__file__).resolve().parents[1] / "cases" / "topic6"
SKILLS = TOPIC6 / "ma-resources" / "skills"
ANNOTATION = SKILLS / "topic6-annotation" / "scripts"
FETCH = SKILLS / "topic6-fetch-normalize"
INSIGHT = SKILLS / "topic6-insight"
MOCK_DIR = Path(os.environ.get(
    "TOPIC6_MOCK_DIR", Path.home() / "work" / "mock_data" / "ada_topic6_mock_data"))

sys.path.insert(0, str(TOPIC6 / "tools"))
from mock_project import load_tree  # noqa: E402

pytestmark = pytest.mark.skipif(
    not MOCK_DIR.is_dir(), reason=f"mock 数据目录不存在: {MOCK_DIR}")

ROUND_MODE = {"sample": "full", "full": "skip_sampling"}
TASKS = [
    ("c0", "C0_基础事实"), ("c3", "C3_节点标注"),
    ("r1", "R1_平台借势"), ("r2", "R2_商业合作"), ("r3", "R3_风险预警"),
    ("r4", "R4_创意借鉴"), ("r5", "R5_消费者行为"),
]
# W41 周报的发布日（报告「本周节点预告」窗口 2026-10-12 ~ 10-18）
PUBLISH_DATE = "2026-10-12"


@pytest.fixture(scope="module", params=sorted(ROUND_MODE))
def mock_round(request):
    project, tree = load_tree(MOCK_DIR, request.param)
    return request.param, project, tree


def _seed(tmp_path: Path, project: str, tree: dict, rels: list[str]) -> Path:
    proj = tmp_path / project
    for rel in rels:
        dest = proj / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree[rel], dest)
    return proj


def _run(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, **(env or {})},
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return result


def _frame(path: Path) -> pd.DataFrame:
    return pd.read_excel(path)


def _assert_same_xlsx(actual: Path, expected: Path) -> None:
    got, want = _frame(actual), _frame(expected)
    assert got.shape == want.shape
    pd.testing.assert_frame_equal(got, want, check_dtype=False)


# ---------------------------------------------------------------------------
# A / B / 抽样（两轮共用同一份数据，只跑一次）
# ---------------------------------------------------------------------------
class TestFetchNormalizeSample:
    @pytest.fixture(scope="class")
    def tree(self):
        return load_tree(MOCK_DIR, "full")

    def test_a_clean_from_raw_json(self, tmp_path, tree):
        project, files = tree
        proj = _seed(tmp_path, project, files, ["01_原始数据/hot_topics_skill_raw.json"])
        _run(FETCH / "scripts" / "fetch_hot_topics.py", "--skip-fetch",
             "--start", "2026-09-28", "--end", "2026-10-12", "--project-dir", str(proj))
        got = _frame(proj / "01_原始数据" / "hot_topics_raw.xlsx")
        want = _frame(files["01_原始数据/hot_topics_raw.xlsx"])
        # 清洗按 hot_index 降序后编号；同分行的先后（及 row_id）不稳定，按内容比对
        key = ["platform", "title"]
        assert got.shape == want.shape
        pd.testing.assert_frame_equal(
            got.drop(columns="row_id").sort_values(key).reset_index(drop=True),
            want.drop(columns="row_id").sort_values(key).reset_index(drop=True),
            check_dtype=False,
        )
        assert got["row_id"].is_unique

    def test_b_normalize(self, tmp_path, tree):
        project, files = tree
        proj = _seed(tmp_path, project, files, ["01_原始数据/hot_topics_raw.xlsx"])
        _run(FETCH / "scripts" / "log1p_p1p99_normalize.py", "--project-dir", str(proj),
             "--benchmark", str(FETCH / "references" / "平台热度基准_2026.json"))
        _assert_same_xlsx(proj / "02_标准化" / "hot_topics_normalized.xlsx",
                          files["02_标准化/hot_topics_normalized.xlsx"])

    def test_sample_500(self, tmp_path, tree):
        project, files = tree
        proj = _seed(tmp_path, project, files, ["02_标准化/hot_topics_normalized.xlsx"])
        _run(FETCH / "scripts" / "sample_500.py", "--size", "500", "--project-dir", str(proj))
        _assert_same_xlsx(proj / "03_抽样" / "sample_500.xlsx",
                          files["03_抽样/sample_500.xlsx"])


# ---------------------------------------------------------------------------
# C 后处理：result_raw → postprocess（不含 DataHub 调用与失败行重试）
# ---------------------------------------------------------------------------
_POSTPROCESS_RUNNER = """
import sys, importlib.util, pandas as pd
from pathlib import Path
scripts, task, raw, out = sys.argv[1:5]
sys.path.insert(0, scripts)
spec = importlib.util.spec_from_file_location("dh", Path(scripts) / "datahub_annotate.py")
dh = importlib.util.module_from_spec(spec); spec.loader.exec_module(dh)
dh.REFERENCES_DIR = Path(scripts).parent / "references" / "marketing_calendar"
post, _ = dh._postprocess(pd.read_excel(raw), task)
post.to_excel(out, index=False)
"""


@pytest.mark.parametrize("task,subdir", TASKS)
def test_c_postprocess(tmp_path, mock_round, task, subdir):
    round_name, _project, files = mock_round
    meta = json.loads(files[f"04_标注/{subdir}/{task}_completion_meta.json"].read_text("utf-8"))
    if (meta.get("retry") or {}).get("retried_rows"):
        pytest.skip("线上做过失败行重试，postprocess 含重试回填，raw 不能单独复现")
    if task == "c3":
        pytest.importorskip("lunardate", reason="C3 时效窗口依赖 lunardate（沙箱已装）")
    runner = tmp_path / "pp.py"
    runner.write_text(_POSTPROCESS_RUNNER, encoding="utf-8")
    out = tmp_path / "post.xlsx"
    _run(runner, str(ANNOTATION), task,
         str(files[f"04_标注/{subdir}/{task}_result_raw_r1.xlsx"]), str(out))
    got = _frame(out)
    want = _frame(files[f"04_标注/{subdir}/{task}_postprocess_r1.xlsx"])
    assert list(got.columns) == list(want.columns)
    assert got["row_id"].tolist() == want["row_id"].tolist()
    err = f"{task}_parse_error"
    # 线上解析成功的行必须逐字段一致；线上失败的行允许被新版解析器救回（1→0），不允许反向
    ok = want[err] == 0
    pd.testing.assert_frame_equal(
        got[ok].reset_index(drop=True), want[ok].reset_index(drop=True), check_dtype=False)
    assert not ((want[err] == 0) & (got[err] == 1)).any()


# ---------------------------------------------------------------------------
# 筛选 + D 合并
# ---------------------------------------------------------------------------
def _annotation_inputs(files: dict) -> list[str]:
    rels = [rel for rel in files if "_postprocess_r1" in rel or "c2_event_result" in rel]
    return rels + ["03_抽样/sample_500.xlsx", "02_标准化/hot_topics_normalized.xlsx"]


def test_filter_and_merge(tmp_path, mock_round):
    round_name, project, files = mock_round
    mode = ROUND_MODE[round_name]
    proj = _seed(tmp_path, project, files, _annotation_inputs(files))
    (proj / "run_config.yaml").write_text("mode: full\nstatus: {}\n", encoding="utf-8")
    args = ("--project-dir", str(proj), "--mode", mode, "--run-id", "1")
    _run(ANNOTATION / "c0_merge_phase1.py", *args)
    _run(ANNOTATION / "c0_filter_usable.py", *args)
    _run(ANNOTATION / "merge_annotations.py", *args)
    for rel in (f"04_合并/phase1_merged_{mode}_r1.xlsx",
                f"04_标注/_可用子集/usable_subset_{mode}_r1.xlsx",
                f"05_合并/wide_table_{mode}_r1.xlsx"):
        _assert_same_xlsx(proj / rel, files[rel])


# ---------------------------------------------------------------------------
# E 数据准备 + F 报告合并（仅全量轮有 06/07 产物）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def full_tree():
    return load_tree(MOCK_DIR, "full")


def test_e_run_stats(tmp_path, full_tree):
    project, files = full_tree
    proj = _seed(tmp_path, project, files, ["05_合并/wide_table_skip_sampling_r1.xlsx"])
    out_dir = proj / "06_洞察" / "v1"
    out_dir.mkdir(parents=True)
    _run(INSIGHT / "01_统计" / "run_stats.py", "--project-dir", str(proj),
         "--mode", "skip_sampling", "--run-id", "1", "--publish-date", PUBLISH_DATE,
         "--out-dir", str(out_dir),
         env={"MARKETING_CALENDAR_PATH": str(FETCH / "references" / "marketing_calendar_2026.csv")})

    def read(name: str) -> str:
        return (out_dir / name).read_text("utf-8")

    def want(name: str) -> str:
        return files[f"06_洞察/v1/{name}"].read_text("utf-8")

    # 以下差异为参照 CC V2.0 的预期口径变化(Obsidian《Topic6 MA 提效优化清单》),mock 是线上快照不改:
    # E1 无上期时不再输出「环比数据」块;其余内容与快照一致
    assert read("e1_data.md").rstrip() == want("e1_data.md").replace(
        "\n\n**环比数据**：Projects/ 下未找到对应上一自然周日期区间的项目目录，本期是该系列的首次归档"
        "（或上一期尚未跑到 E 阶段）。无可比上期：省略第三句环比，不允许编造任何变化。", "").rstrip()
    # E2 上周节点改用全量 df:营销不可用的寒露热搜也纳入回顾,列名为「涉及品牌IP候选」
    e2 = json.loads(read("e2_flags.json"))
    assert e2["last_week"]["has_data"] is True
    assert "| 寒露 | 1 |" in e2["last_week"]["data_md"]
    assert "涉及品牌IP候选" in e2["last_week"]["data_md"]
    assert e2["next_week"] == json.loads(want("e2_flags.json"))["next_week"]
    # E3「平台热点词 TOP3」移到 pipeline_e 打标后拼接;热门事件表保留
    assert "平台热点词" not in read("e3_data.md")
    assert read("e3_data.md").count("热门事件") == want("e3_data.md").count("热门事件")
    for name in ("e3_word_candidates.json", "e3_event_fulltext.json"):
        assert set(json.loads(read(name))) >= {"微博", "抖音", "B站", "知乎"}, name
    # E4 候选新增营销观察/消费洞察两个键,原两个键不变
    e4_got, e4_want = json.loads(read("e4_candidates.json")), json.loads(want("e4_candidates.json"))
    assert {k: e4_got[k] for k in e4_want} == e4_want
    assert e4_got["营销观察"] and e4_got["消费洞察"]
    # e4_data 表格中同热度分的行顺序不稳定，按行集合比对
    assert sorted(read("e4_data.md").splitlines()) == sorted(want("e4_data.md").splitlines())


def test_f_merge_report(tmp_path, full_tree):
    project, files = full_tree
    rels = [f"06_洞察/v1/{name}" for name in ("e1_v1.md", "e2_v1.md", "e3_v1.md", "e4_v1.md")]
    proj = _seed(tmp_path, project, files, rels)
    (proj / "run_config.yaml").write_text(
        "mode: full\nperiod:\n  period_label: W41 热点周报\n"
        "  date_start: '2026-10-05'\n  date_end: '2026-10-11'\nstatus: {}\n",
        encoding="utf-8",
    )
    _run(INSIGHT / "scripts" / "pipeline_f.py", "--project-dir", str(proj), "--version", "1")
    report = "07_报告/热点报告_W41 热点周报_v1.md"
    assert (proj / report).read_text("utf-8") == files[report].read_text("utf-8")


def test_mock_tree_complete():
    """两轮的关键产物都能按 sha256 在 mock 目录里找到（load_tree 缺文件会直接抛错）。"""
    _p, sample = load_tree(MOCK_DIR, "sample")
    _p, full = load_tree(MOCK_DIR, "full")
    assert "05_合并/wide_table_full_r1.xlsx" in sample
    assert "07_报告/热点报告_W41 热点周报_v1.md" in full
    assert len(_frame(sample["03_抽样/sample_500.xlsx"])) == 500
