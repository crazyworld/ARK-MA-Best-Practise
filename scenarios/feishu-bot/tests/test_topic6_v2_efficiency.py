"""Topic6 MA 提效优化回归测试(参照 CC V2.0,见 Obsidian《Topic6 MA 提效优化清单》)。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


TOPIC6_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
SKILLS_DIR = TOPIC6_DIR / "ma-resources" / "skills"
PIPELINE_E_SCRIPT = SKILLS_DIR / "topic6-insight" / "scripts" / "pipeline_e.py"


def _load_pipeline_e_module():
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = object
    old_openai = sys.modules.get("openai")
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.modules["openai"] = openai_stub
    try:
        sys.stdout = types.SimpleNamespace(buffer=__import__("io").BytesIO())
        sys.stderr = types.SimpleNamespace(buffer=__import__("io").BytesIO())
        spec = importlib.util.spec_from_file_location("topic6_pipeline_e_eff", PIPELINE_E_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        if old_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old_openai


def _section(sid="e1", max_tokens=1000, **extra):
    return {
        "id": sid,
        "name": sid.upper(),
        "prompt_dir": sid.upper(),
        "data_file": f"{sid}_data.md",
        "section_title": f"# {sid.upper()}",
        "max_tokens": max_tokens,
        "output_file": f"{sid}_v1.md",
        **extra,
    }


def _checkpoint(module):
    return {
        "checkpoint_version": module.CHECKPOINT_VERSION,
        "run_signature": "sig",
        "model": "m",
        "mode": "demo",
        "version": "v1",
        "sections": {},
    }


# ---------------------------------------------------------------------------
# P1-3 Prompt 版本数字排序
# ---------------------------------------------------------------------------

def test_prompt_version_sorts_numerically_v10_after_v9(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    prompt_dir = tmp_path / "E4"
    prompt_dir.mkdir()
    for v in ("v2", "v9", "v10"):
        (prompt_dir / f"{v}.md").write_text(f"{v} {{{{data}}}}", encoding="utf-8")
    monkeypatch.setattr(module, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(module, "SHARED_ROLE_STYLE", tmp_path / "missing.md")

    assert module.load_prompt_template("E4").startswith("v10 ")


# ---------------------------------------------------------------------------
# P0-1 截断自动升档
# ---------------------------------------------------------------------------

def test_length_truncation_doubles_max_tokens_and_counts_discarded_tokens(monkeypatch):
    module = _load_pipeline_e_module()
    budgets = []

    async def fake_stream(_key, _url, section, _prompt, _model):
        budgets.append(section["max_tokens"])
        if len(budgets) < 3:
            return {"content": "半截", "input_tokens": 10,
                    "output_tokens": section["max_tokens"], "stop_reason": "length"}
        return {"content": "完整", "input_tokens": 10, "output_tokens": 50, "stop_reason": "stop"}

    monkeypatch.setattr(module, "_stream_completion", fake_stream)
    result = asyncio.run(module.call_llm_async("k", "u", _section(max_tokens=1000), "p", "m"))

    assert budgets == [1000, 2000, 4000]
    assert result["status"] == "ok"
    assert result["content"] == "完整"
    assert result["stop_reason"] == "stop"
    assert result["max_tokens_upgrades"] == 2
    assert result["effective_output_tokens"] == 50
    assert result["output_tokens"] == 1000 + 2000 + 50
    assert result["input_tokens"] == 30


def test_length_upgrade_respects_retry_limit_and_ceiling(monkeypatch):
    module = _load_pipeline_e_module()
    budgets = []

    async def always_length(_key, _url, section, _prompt, _model):
        budgets.append(section["max_tokens"])
        return {"content": "半截", "input_tokens": 1, "output_tokens": 1, "stop_reason": "length"}

    monkeypatch.setattr(module, "_stream_completion", always_length)
    result = asyncio.run(module.call_llm_async("k", "u", _section(max_tokens=20000), "p", "m"))

    assert budgets == [20000, module.MAX_TOKENS_CEILING]
    assert result["stop_reason"] == "length"


def test_empty_content_with_length_is_upgraded_not_network_retried(monkeypatch):
    """推理模型把预算花在思考上 → 正文为空 + length,应升档而不是当网络错误。"""
    module = _load_pipeline_e_module()
    budgets = []

    async def fake_create(**kwargs):
        budgets.append(kwargs["max_tokens"])
        content = "" if len(budgets) == 1 else "正文"

        class _It:
            def __init__(self):
                self._done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._done:
                    raise StopAsyncIteration
                self._done = True
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(
                        delta=types.SimpleNamespace(content=content),
                        finish_reason="length" if not content else "stop",
                    )],
                    usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=7),
                )
        return _It()

    class _Client:
        def __init__(self, **_kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))

        async def close(self):
            return None

    monkeypatch.setattr(module, "AsyncOpenAI", _Client)
    result = asyncio.run(module.call_llm_async("k", "u", _section(max_tokens=500), "p", "m"))

    assert budgets == [500, 1000]
    assert result["status"] == "ok"
    assert result["content"] == "正文"


# ---------------------------------------------------------------------------
# P0-2 完整性闸 + P0-3 异常隔离
# ---------------------------------------------------------------------------

def test_validate_section_content_flags_table_column_mismatch_and_ignores_escaped_pipe():
    module = _load_pipeline_e_module()
    good = "| a | b |\n|---|---|\n| 游戏\\|音乐 | 2 |"
    bad = "| a | b |\n|---|---|\n| 1 | 2 | 3 |"

    assert module.validate_section_content("e1", good) == []
    assert any("表格行单元格数(3)≠表头(2)" in i for i in module.validate_section_content("e1", bad))
    assert module.validate_section_content("e1", "   ") == ["[e1] 正文为空"]
    assert module.validate_section_content("e1", "ok", "length")


def _run_sections(module, tmp_path, monkeypatch, sections, fake_call):
    insight_dir = tmp_path / "v1"
    insight_dir.mkdir(exist_ok=True)
    for sec in sections:
        (insight_dir / sec["data_file"]).write_text("data", encoding="utf-8")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "k")
    monkeypatch.setenv("LLM_RELAY_BASE_URL", "u")
    monkeypatch.setattr(module, "load_prompt_template", lambda _d: "{{data}}")
    monkeypatch.setattr(module, "call_llm_async", fake_call)
    checkpoint = _checkpoint(module)
    path = insight_dir / "ckpt.json"
    results = asyncio.run(module.run_all_sections(
        tmp_path, insight_dir, "m", sections, checkpoint=checkpoint, checkpoint_path=path,
    ))
    return results, checkpoint, insight_dir


def _ok(sec, content, out=10):
    return {"id": sec["id"], "name": sec["name"], "status": "ok", "content": content,
            "input_tokens": 1, "output_tokens": out, "elapsed_s": 0.1, "model": "m",
            "stop_reason": "stop"}


def test_broken_table_reruns_section_once_then_passes(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    calls = []

    async def fake_call(_k, _u, sec, _p, _m, *, max_attempts):
        calls.append(sec["id"])
        if len(calls) == 1:
            return _ok(sec, "| a | b |\n|---|---|\n| 1 | 2 | 3 |", out=10)
        return _ok(sec, "| a | b |\n|---|---|\n| 1 | 2 |", out=20)

    results, checkpoint, insight_dir = _run_sections(
        module, tmp_path, monkeypatch, [_section("e1")], fake_call)

    assert calls == ["e1", "e1"]
    assert results[0]["status"] == "ok"
    assert results[0]["output_tokens"] == 30  # 被丢弃那次也计入成本
    assert checkpoint["sections"]["e1"]["status"] == "ok"
    assert (insight_dir / "e1_v1.md").exists()


def test_section_failing_validation_twice_is_error_and_not_checkpointed(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    calls = []

    async def fake_call(_k, _u, sec, _p, _m, *, max_attempts):
        calls.append(sec["id"])
        return _ok(sec, "| a | b |\n|---|---|\n| 1 |")

    results, checkpoint, insight_dir = _run_sections(
        module, tmp_path, monkeypatch, [_section("e1")], fake_call)

    assert calls == ["e1", "e1"]
    assert results[0]["status"] == "error"
    assert "完整性校验未通过" in results[0]["error"]
    assert "e1" not in checkpoint["sections"]
    assert not (insight_dir / "e1_v1.md").exists()


def test_one_section_exception_does_not_break_other_sections(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()

    async def fake_call(_k, _u, sec, _p, _m, *, max_attempts):
        if sec["id"] == "ex":
            raise RuntimeError("boom")
        return _ok(sec, "正文足够长" * 30)

    results, checkpoint, _ = _run_sections(
        module, tmp_path, monkeypatch, [_section("e1"), _section("ex")], fake_call)

    assert [r["status"] for r in results] == ["ok", "error"]
    assert "boom" in results[1]["error"]
    assert "e1" in checkpoint["sections"]


def test_fixed_text_section_skips_validation(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    sec = _section("e2")

    async def fake_e2(_llm, s, _dir):
        return {**_ok(s, "固定话术"), "fixed_text": True}

    monkeypatch.setattr(module, "build_e2_task", fake_e2)
    monkeypatch.setattr(module, "validate_section_content",
                        lambda *_a, **_k: pytest.fail("fixed_text 不应进完整性闸"))

    async def never(*_a, **_k):
        raise AssertionError

    results, _, _ = _run_sections(module, tmp_path, monkeypatch, [sec], never)
    assert results[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# P0-6 A 阶段数据质量校验
# ---------------------------------------------------------------------------

FETCH_SCRIPT = SKILLS_DIR / "topic6-fetch-normalize" / "scripts" / "fetch_hot_topics.py"
ANNOTATE_SCRIPT = SKILLS_DIR / "topic6-annotation" / "scripts" / "datahub_annotate.py"


def _load(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quality_frame(n=100, **overrides):
    import pandas as pd

    data = {
        "row_id": [f"T{i:04d}" for i in range(n)],
        "platform": ["微博"] * n,
        "title": [f"标题{i}" for i in range(n)],
        "hot_index": [float(i + 1) for i in range(n)],
        "hotpost_time": ["2026-10-20"] * n,
        "hottopic_desc": [None] * n,
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_data_quality_clean_frame_has_no_errors():
    fetch = _load(FETCH_SCRIPT, "fetch_hot_topics_eff")
    report = fetch.check_data_quality(_quality_frame(), raw_record_count=120)

    assert report["hard_errors"] == []
    assert report["soft_warnings"] == []
    assert report["field_null_rate"]["hottopic_desc"] == 1.0  # 可选字段全空只记录


def test_data_quality_soft_warns_on_null_rate_and_non_positive_hot_index():
    fetch = _load(FETCH_SCRIPT, "fetch_hot_topics_eff")
    hot = [0.0] * 10 + [float(i) for i in range(1, 91)]
    titles = [""] * 6 + [f"t{i}" for i in range(94)]
    report = fetch.check_data_quality(_quality_frame(hot_index=hot, title=titles), 100)

    assert report["hard_errors"] == []
    assert any("title" in w for w in report["soft_warnings"])
    assert any("hot_index≤0" in w for w in report["soft_warnings"])


def test_data_quality_hard_blocks_empty_column_and_duplicate_row_id():
    fetch = _load(FETCH_SCRIPT, "fetch_hot_topics_eff")
    frame = _quality_frame(hotpost_time=[None] * 100, row_id=["T0001"] * 100)
    report = fetch.check_data_quality(frame, 100)

    assert any("hotpost_time" in e for e in report["hard_errors"])
    assert any("重复 row_id" in e for e in report["hard_errors"])


# ---------------------------------------------------------------------------
# P0-7 R1~R5 主字段非空才算有效
# ---------------------------------------------------------------------------

def test_postprocess_counts_empty_primary_field_as_invalid():
    import pandas as pd

    annotate = _load(ANNOTATE_SCRIPT, "datahub_annotate_eff")
    df = pd.DataFrame({
        "row_id": ["T1", "T2", "T3", "T4"],
        "llm_result": [
            '{"是否商业合作":"是","判断说明":"联名"}',
            '{"是否商业合作":"","判断说明":""}',   # SUCCESS 但主字段空(W39 E5)
            '{"判断说明":"漏了主字段"}',
            "not json",
        ],
    })
    post, stats = annotate._postprocess(df, "r2")

    assert stats["valid"] == 1
    assert stats["status"] == "fail"
    assert stats["empty_primary_count"] == 2
    assert post["r2_parse_error"].tolist() == [0, 1, 1, 1]
    assert annotate._failed_row_ids(post, "r2") == ["T2", "T3", "T4"]
    assert "r2_empty_result" not in post.columns  # 产物列契约不变(mock_nodes 回放依赖)


# ---------------------------------------------------------------------------
# P1-1 节点日历补全 + fail-loud
# ---------------------------------------------------------------------------

E2_NODES_SCRIPT = SKILLS_DIR / "topic6-insight" / "01_统计" / "e2_nodes.py"
MARKETING_CALENDAR = SKILLS_DIR / "topic6-fetch-normalize" / "references" / "marketing_calendar_2026.csv"
SUPPLEMENT_NODES = ("冬奥会", "迎财神", "高考", "世界杯", "暑期档", "开学季", "亚运会")


def test_marketing_calendar_contains_supplement_nodes(monkeypatch):
    e2 = _load(E2_NODES_SCRIPT, "e2_nodes_eff")
    monkeypatch.setattr(e2, "CALENDAR_PATH", MARKETING_CALENDAR)
    calendar = {name: d.isoformat() for d, name, _t in e2._load_calendar_rows()}

    assert {n: calendar.get(n) for n in SUPPLEMENT_NODES} == {
        "冬奥会": "2026-02-06", "迎财神": "2026-02-21", "高考": "2026-06-07",
        "世界杯": "2026-06-11", "暑期档": "2026-07-15", "开学季": "2026-09-01",
        "亚运会": "2026-09-19",
    }
    raw = MARKETING_CALENDAR.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw  # 保持原文件编码与换行


def test_e2_calendar_that_parses_to_zero_rows_fails_loud(tmp_path, monkeypatch):
    e2 = _load(E2_NODES_SCRIPT, "e2_nodes_eff")
    bad = tmp_path / "calendar.csv"
    bad.write_text("日期,节点\n2026-10-01,国庆\n", encoding="utf-8")
    monkeypatch.setattr(e2, "CALENDAR_PATH", bad)

    with pytest.raises(ValueError, match="解析出 0 个节点"):
        e2._load_calendar_rows()


# ---------------------------------------------------------------------------
# P1-2 --sections 定点重跑落回最新轮次
# ---------------------------------------------------------------------------

def test_sections_rerun_without_version_reuses_latest_round(tmp_path):
    module = _load_pipeline_e_module()
    (tmp_path / "v1").mkdir()
    (tmp_path / "v2").mkdir()

    assert module.resolve_round_version(tmp_path, None, "e3") == 2
    assert module.resolve_round_version(tmp_path, None, None) == 3
    assert module.resolve_round_version(tmp_path, 5, "e3") == 5
    assert module.resolve_round_version(tmp_path / "empty", None, "e3") == 1



# ---------------------------------------------------------------------------
# P0-4 / P0-5 E3、E4 分步并发路由 + 伪表格解析
# ---------------------------------------------------------------------------

def test_parse_pseudo_rows_tolerates_missing_header_and_empty_placeholder():
    module = _load_pipeline_e_module()
    hdr = ["事件簇名", "保留候选", "理由"]
    # LLM 漏写表头:第一条数据不能被吞成表头
    rows = module._parse_pseudo_rows("A || A || x\nB || B || y", expected_header=hdr)
    assert [r["事件簇名"] for r in rows] == ["A", "B"]
    # "无重复事件"占位句不是数据行
    arb = ["涉及候选", "保留候选", "理由"]
    assert module._parse_pseudo_rows("涉及候选 || 保留候选 || 理由\n无重复事件", expected_header=arb) == []
    assert module._parse_pseudo_rows("无重复事件", expected_header=arb) == []


def _e3_fixture(insight_dir):
    data = "\n\n---\n\n".join(
        f"## {p}\n\n**热门事件 TOP 1**\n\n| 排名 | 事件 |\n|---|---|\n| 1 | {p}事件 |"
        for p in ("微博", "抖音", "B站", "知乎")
    )
    (insight_dir / "e3_data.md").write_text(data, encoding="utf-8")
    word = lambda w, s: {"热点驱动词": w, "词频": 3, "均分": s, "条数": 3, "最高分": s,
                         "具体热搜": f"- [{w}](https://x/{w})"}
    (insight_dir / "e3_word_candidates.json").write_text(json.dumps({
        "微博": [word("郑钦文", 90.0), word("大胜", 95.0), word("中网", 80.0)],
        "抖音": [], "B站": [], "知乎": [],
    }, ensure_ascii=False), encoding="utf-8")
    (insight_dir / "e3_event_fulltext.json").write_text(json.dumps({
        "微博": [{"事件簇名": "微博事件", "标题列表": ["t1", "t2"]}],
    }, ensure_ascii=False), encoding="utf-8")


def _llm_ok(sec, content):
    return {"id": sec["id"], "name": sec["name"], "status": "ok", "content": content,
            "input_tokens": 100, "output_tokens": 10, "elapsed_s": 0.1, "model": "m",
            "stop_reason": "stop"}


def test_e3_runs_tagging_and_draft_concurrently_then_writes_each_platform(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    _e3_fixture(tmp_path)
    monkeypatch.setattr(module, "load_prompt_template", lambda _d: "{{data}}")
    started, prompts = [], {}
    both_started = asyncio.Event()

    async def llm_call(sec, prompt):
        started.append(sec["id"])
        prompts[sec["id"]] = prompt
        if sec["id"] in ("e3_tagging", "e3_insight_draft"):
            if {"e3_tagging", "e3_insight_draft"} <= set(started):
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)  # 串行执行会在这里超时
        if sec["id"] == "e3_tagging":
            return _llm_ok(sec, "### 热点词打标\n\n热点驱动词 || 归类 || 可借势 || 理由\n"
                                "郑钦文 || 具体实体/热梗 || 否 || x\n大胜 || 主体不明的动作/结果词 || 否 || x\n"
                                "中网 || 具体实体/热梗 || 是 || x")
        if sec["id"] == "e3_insight_draft":
            return _llm_ok(sec, "### 洞察初稿\n\n平台 || 事件簇名 || 信号类型 || 判断依据\n"
                                "微博 || 微博事件 || 风险/舆情型 || 负面")
        return _llm_ok(sec, f"### {sec['id'].split(':')[1]}\n\n正文")

    sec = {"id": "e3", "name": "平台新鲜事", "prompt_dir": "E3", "data_file": "e3_data.md",
           "section_title": "# 三、平台新鲜事", "max_tokens": 18000}
    result = asyncio.run(module.build_e3_task(llm_call, sec, tmp_path, "m"))

    assert result["status"] == "ok"
    assert [s for s in started if s.startswith("e3:")] == ["e3:微博", "e3:抖音", "e3:B站", "e3:知乎"]
    assert result["content"].index("### 微博") < result["content"].index("### 知乎")
    weibo = prompts["e3:微博"]
    assert "平台热点词 TOP 2" in weibo and "大胜" not in weibo  # 主体不明词被过滤
    assert "洞察初稿参考" in weibo and "风险/舆情型" in weibo
    assert "平台热点词" not in prompts["e3:抖音"]
    assert result["input_tokens"] == 600  # 2 个子步骤 + 4 个平台
    assert (tmp_path / "e3_tagging_audit.md").exists()


def test_e3_tagging_failure_drops_word_table_but_section_succeeds(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    _e3_fixture(tmp_path)
    monkeypatch.setattr(module, "load_prompt_template", lambda _d: "{{data}}")
    prompts = {}

    async def llm_call(sec, prompt):
        prompts[sec["id"]] = prompt
        if sec["id"] == "e3_tagging":
            return {**_llm_ok(sec, ""), "status": "error", "error": "disconnect"}
        return _llm_ok(sec, "### 平台\n\n正文")

    sec = {"id": "e3", "name": "E3", "prompt_dir": "E3", "data_file": "e3_data.md",
           "section_title": "# 三", "max_tokens": 1}
    result = asyncio.run(module.build_e3_task(llm_call, sec, tmp_path, "m"))
    assert result["status"] == "ok"
    assert "平台热点词" not in prompts["e3:微博"]  # 不回退未过滤候选


def test_e3_any_platform_failure_fails_section(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    _e3_fixture(tmp_path)
    monkeypatch.setattr(module, "load_prompt_template", lambda _d: "{{data}}")

    async def llm_call(sec, prompt):
        if sec["id"] == "e3:B站":
            return {**_llm_ok(sec, ""), "status": "error", "error": "boom"}
        return _llm_ok(sec, "### x\n\n正文")

    sec = {"id": "e3", "name": "E3", "prompt_dir": "E3", "data_file": "e3_data.md",
           "section_title": "# 三", "max_tokens": 1}
    result = asyncio.run(module.build_e3_task(llm_call, sec, tmp_path, "m"))
    assert result["status"] == "error" and "e3:B站" in result["error"]
    assert result["input_tokens"] > 0  # 失败也保留真实花费


def _e4_candidates():
    risk = lambda n, s, link: {"事件簇名": n, "行业": "汽车出行|数码3C", "热度分": f"{s} 分",
                               "风险判断说明": "说明", "具体热搜": [
                                   {"标题": n, "链接": link, "平台": "微博", "热度分": f"{s} 分"}]}
    coop = lambda n, s, link: {"事件簇名": n, "商业实体": "A|B", "行业": "食品", "热度分": f"{s} 分",
                               "代表热搜标题": n, "代表热搜链接": link, "合作动态说明": "说明"}
    return {
        "舆情风险": [risk("刹车门", 90, "https://r/1"), risk("刹车门后续", 80, "https://r/2"),
                    risk("财务暴雷", 99, "https://r/3")],
        "合作动态": [coop("联名A", 70, "https://c/1"), coop("代言B", 95, "https://c/2"),
                    coop("刹车门", 60, "https://c/3")],
        "营销观察": [{"事件簇名": "玩法X", "平台": "抖音", "行业": "美妆", "热度分": "88 分",
                    "代表热搜标题": "玩法X", "代表热搜链接": "https://m/1", "营销发现说明": "新玩法"}],
        "消费洞察": [{"事件簇名": "换机潮", "平台": "知乎", "行业": "数码3C", "热度分": "77 分",
                    "代表热搜标题": "换机潮", "代表热搜链接": "https://k/1", "消费者行为说明": "换机"}],
    }


E4_TAGGING_OUT = """### 舆情风险打标

事件簇名 || 风险类型 || 可接性 || 理由
刹车门 || 品牌舆情事件 || 接 || x
刹车门后续 || 品牌舆情事件 || 接 || x
财务暴雷 || 政策监管风险 || 不接 || 财务硬事实

### 合作动态打标

事件簇名 || 变动类型 || 参考价值 || 理由
联名A || 品牌联名/资源合作 || 有用 || x
代言B || 品牌×明星代言合作 || 有用 || x
刹车门 || 合作争议 || 有用 || x"""

E4_CURATION_OUT = """### 营销观察草稿

事件簇名 || 现象概括
玩法X || 可复用玩法

### 消费洞察草稿

候选组合 || 现象概括
换机潮 || 换机讨论"""

E4_ARBITRATION_OUT = """### 跨板块重复事件

涉及候选 || 最终归属 || 理由
合作动态:刹车门 + 舆情风险:刹车门 || 舆情风险 || 核心是负面

### 同池内部重复事件

涉及候选 || 保留候选 || 理由
舆情风险:刹车门 + 舆情风险:刹车门后续 || 刹车门 || 同一事件"""

E4_B_GOOD = """### 营销观察

#### 案例 1：玩法X（抖音 · 88 分）
- 原始热点：[玩法X](https://m/1)
- 营销发现：可复用
- 链接：[玩法X](https://m/1)
[配图：待补充]"""


def _run_e4(module, tmp_path, monkeypatch, responder):
    (tmp_path / "e4_candidates.json").write_text(
        json.dumps(_e4_candidates(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "load_prompt_template", lambda d: f"[{d}]\n{{{{data}}}}")
    calls, prompts = [], {}

    async def llm_call(sec, prompt):
        calls.append(sec["id"])
        prompts.setdefault(sec["id"], []).append(prompt)
        return await responder(sec, prompt, calls)

    sec = {"id": "e4", "name": "营销发现", "prompt_dir": "E4_营销发现", "data_file": "e4_data.md",
           "section_title": "# 四、营销发现", "max_tokens": 16000}
    return asyncio.run(module.build_e4_task(llm_call, sec, tmp_path, "m")), calls, prompts


def test_e4_full_chain_tags_arbitrates_sorts_and_writes_groups_concurrently(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    gate = asyncio.Event()
    seen: set = set()

    async def responder(sec, prompt, calls):
        sid = sec["id"]
        if sid in ("e4_tagging", "e4_curation", "e4:A", "e4:B"):
            seen.add(sid)
            pair = {"e4_tagging", "e4_curation"} if sid.startswith("e4_") else {"e4:A", "e4:B"}
            if pair <= seen:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)
        if sid == "e4_tagging":
            return _llm_ok(sec, E4_TAGGING_OUT)
        if sid == "e4_curation":
            return _llm_ok(sec, E4_CURATION_OUT)
        if sid == "e4_arbitration":
            gate.clear()
            return _llm_ok(sec, E4_ARBITRATION_OUT)
        if sid == "e4:A":
            return _llm_ok(sec, "### 合作动态\n\n[代言B](https://c/2)\n\n### 舆情风险\n\n[刹车门](https://r/1)")
        return _llm_ok(sec, E4_B_GOOD)

    result, calls, prompts = _run_e4(module, tmp_path, monkeypatch, responder)

    assert result["status"] == "ok", result.get("error")
    assert calls.index("e4_arbitration") > max(calls.index("e4_tagging"), calls.index("e4_curation"))
    data_a = prompts["e4:A"][0]
    assert "财务暴雷" not in data_a                 # 不接,过滤
    assert "| 合作争议 | 刹车门 |" not in data_a     # 仲裁归舆情风险
    assert "刹车门后续 |" not in data_a and "https://r/2" in data_a  # 同池合并,链接保留
    assert data_a.index("联名A") < data_a.index("代言B")  # 组间固定优先级先于热度
    assert "汽车出行、数码3C" in data_a              # 多值字段用顿号
    assert "_writing_coop_risk" in data_a and "_writing_marketing_consumer" in prompts["e4:B"][0]
    assert result["content"].index("### 合作动态") < result["content"].index("#### 案例 1")
    assert result["input_tokens"] == 500
    for name in ("e4_tagging_audit.md", "e4_curation_audit.md", "e4_arbitration_audit.md"):
        assert (tmp_path / name).exists()


def test_e4_group_with_fabricated_link_is_rewritten_once_then_fails(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()

    async def responder(sec, prompt, calls):
        if sec["id"] == "e4_tagging":
            return _llm_ok(sec, E4_TAGGING_OUT)
        if sec["id"] == "e4_curation":
            return _llm_ok(sec, E4_CURATION_OUT)
        if sec["id"] == "e4_arbitration":
            return _llm_ok(sec, "### 跨板块重复事件\n\n无重复事件\n\n### 同池内部重复事件\n\n无重复事件")
        if sec["id"] == "e4:A":
            return _llm_ok(sec, "### 合作动态\n\n[编造](https://fake/1)")
        return _llm_ok(sec, E4_B_GOOD)

    result, calls, _ = _run_e4(module, tmp_path, monkeypatch, responder)
    assert calls.count("e4:A") == 2 and calls.count("e4:B") == 1
    assert result["status"] == "error" and "https://fake/1" in result["error"]


def test_e4_b_group_free_prose_fails_card_format_check():
    module = _load_pipeline_e_module()
    prose = "**1. 某品牌联名**\n" + "这是一段很长的自由散文。" * 20
    assert module.validate_e4_group("b", prose, "")
    assert module.validate_e4_group("b", "上周未观察到符合条件的营销观察。", "") == []
    assert module.validate_e4_group("b", E4_B_GOOD, "[x](https://m/1)") == []


def test_e4_a_group_broken_table_fails_group_check():
    module = _load_pipeline_e_module()
    broken = "### 合作动态\n\n| 类型 | 事件 |\n|---|---|\n| 联名 | A | 多出一列 |"
    assert any("表格行单元格数" in i for i in module.validate_e4_group("a", broken, ""))
    ok = "### 合作动态\n\n| 类型 | 事件 |\n|---|---|\n| 联名 | 食品\\|饮料 |"
    assert module.validate_e4_group("a", ok, "") == []


def test_e4_tagging_failure_keeps_all_and_curation_failure_empties_b(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()

    async def responder(sec, prompt, calls):
        if sec["id"] in ("e4_tagging", "e4_curation"):
            return {**_llm_ok(sec, ""), "status": "error", "error": "disconnect"}
        if sec["id"] == "e4_arbitration":
            return _llm_ok(sec, "")
        if sec["id"] == "e4:A":
            return _llm_ok(sec, "### 合作动态\n\n正文")
        return _llm_ok(sec, "上周未观察到符合条件的营销观察。")

    result, calls, prompts = _run_e4(module, tmp_path, monkeypatch, responder)
    assert result["status"] == "ok"
    assert "财务暴雷" in prompts["e4:A"][0]             # 打标失败保守保留
    assert "上周未枚举出符合条件的营销观察" in prompts["e4:B"][0]  # 枚举失败不回退全量


def test_pipeline_signature_tracks_sub_step_prompts(tmp_path, monkeypatch):
    module = _load_pipeline_e_module()
    prompts = tmp_path / "prompts"
    sub = prompts / "E4_营销发现" / "_arbitration"
    sub.mkdir(parents=True)
    for sec in module.SECTIONS:
        (prompts / sec["prompt_dir"]).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "PROMPTS_DIR", prompts)
    (sub / "v1.md").write_text("a", encoding="utf-8")
    kw = dict(model="m", mode="full", data_run_id=1)
    before = module._pipeline_signature(tmp_path, **kw)
    (sub / "v1.md").write_text("b", encoding="utf-8")
    assert module._pipeline_signature(tmp_path, **kw) != before


def test_demo_routes_prompt_versions_match_version_status():
    """demo 路由硬编码的 R1~R5 Prompt 必须与 _版本状态.md 的活跃版本一致,否则 demo 与正式模式口径分叉。"""
    import importlib.util
    import re
    base = Path(__file__).resolve().parents[1] / "cases" / "topic6" / "ma-resources"
    spec = importlib.util.spec_from_file_location(
        "run_demo_routes_ver", base / "skills" / "topic6-annotation" / "scripts" / "run_demo_routes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    status = (base / "memory" / "topic6" / "_版本状态.md").read_text(encoding="utf-8")
    for route in mod.ROUTES.values():
        m = re.search(r"prompts/(R\d_[^/]+)/(v\d+)\.md", route["prompt"])
        assert m, route["prompt"]
        assert f"/mnt/skills/topic6-annotation/prompts/{m.group(1)}/{m.group(2)}.md" in status, route["prompt"]
        assert (base / "skills" / "topic6-annotation" / route["prompt"]).exists()
