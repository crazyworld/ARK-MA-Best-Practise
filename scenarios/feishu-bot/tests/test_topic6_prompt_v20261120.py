"""2026-11-20 客户标注 Prompt 原样替换(C0 v17 / R1 v7 / R2 v6 / R3 v5 / R4 v7 / R5 v8)回归。

锁定三件事:
1. 活跃 Prompt 的占位符与输出键 == 脚本 schema(正式 DataHub / demo 两条路径);
2. R5 v8 六字段在 datahub 后处理 → merge 宽表 → E4 统计全链路可用,宽表仍 28 列、下游列名不变;
3. demo 批量包装不再写死 40 字,且能切掉新版「【待标注数据】」单条输入段。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pandas as pd

TOPIC6 = Path(__file__).resolve().parents[1] / "cases" / "topic6"
ANN = TOPIC6 / "ma-resources" / "skills" / "topic6-annotation"
SCRIPTS = ANN / "scripts"
STATS = TOPIC6 / "ma-resources" / "skills" / "topic6-insight" / "01_统计"

ACTIVE = {
    "c0": "C0_基础事实/v17.md",
    "r1": "R1_平台借势/v7.md",
    "r2": "R2_商业合作/v6.md",
    "r3": "R3_风险预警/v5.md",
    "r4": "R4_创意借鉴/v7.md",
    "r5": "R5_消费者行为/v8.md",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


def _prompt(task: str) -> str:
    return (ANN / "prompts" / ACTIVE[task]).read_text(encoding="utf-8")


def test_active_prompts_only_use_supported_placeholders():
    """DataHub 只按 platform/title/desc 三列渲染;多一个占位符就会原样漏进 Prompt。"""
    for task in ACTIVE:
        found = set(re.findall(r"{{(\w+)}}", _prompt(task)))
        assert found == {"platform", "title", "desc"}, (task, found)


def test_datahub_schema_matches_active_prompt_output_keys():
    mod = _load("datahub_annotate_v1120", SCRIPTS / "datahub_annotate.py")
    for task in ("c0", "r1", "r2", "r3", "r4", "r5"):
        text = _prompt(task)
        schema = mod.TASK_SCHEMAS[task]
        for field in schema["fields"]:
            assert f"“{field}”" in text or f'"{field}"' in text, (task, field)
    assert mod.TASK_SCHEMAS["r5"]["primary_field"] == "是否消费洞察"


def test_r5_v8_postprocess_merge_and_e4_stats(tmp_path):
    """R5 v8 结果 → postprocess 六列 → 宽表映射回 是否消费者行为/消费者行为说明 → E4 消费洞察候选可取。"""
    dh = _load("datahub_annotate_v1120b", SCRIPTS / "datahub_annotate.py")
    raw = pd.DataFrame({
        "row_id": [1, 2],
        "llm_result": [
            json.dumps({"是否消费洞察": "是", "适用消费人群": "养犬人群", "观点情绪": "将宠物视为家庭成员",
                        "消费场景": "养犬与家庭陪伴", "消费需求": "",
                        "洞察线索": "表达宠物家庭成员化认知"}, ensure_ascii=False),
            json.dumps({"是否消费洞察": "否", "适用消费人群": "", "观点情绪": "", "消费场景": "",
                        "消费需求": "", "洞察线索": "仅泛可爱感叹"}, ensure_ascii=False),
        ],
    })
    post, stats = dh._postprocess(raw, "r5")
    assert stats["valid"] == 2 and stats["empty_primary_count"] == 0
    assert post.loc[0, "r5_适用消费人群"] == "养犬人群"
    assert list(post["r5_是否消费洞察"]) == ["是", "否"]

    merge = _load("merge_annotations_v1120", SCRIPTS / "merge_annotations.py")
    spec = merge.TASK_MERGE_SPECS["r5"]
    assert all(col in post.columns for col in spec["raw_cols"])
    wide = post[["row_id"] + spec["raw_cols"]].rename(columns=spec["rename"])
    assert list(wide["是否消费者行为"]) == ["是", "否"]
    assert wide.loc[0, "消费者行为说明"] == "表达宠物家庭成员化认知"
    # 宽表列数不变:R1~R5 各 2 列
    assert sum(len(merge.TASK_MERGE_SPECS[t]["rename"]) for t in ("r1", "r2", "r3", "r4", "r5")) == 10

    e4 = _load("e4_marketing_v1120", STATS / "e4_marketing.py")
    df_y = wide.assign(事件簇名=["德牧家人", "德牧可爱"], 标准化热度分=[90.0, 80.0])
    picked = e4._select_consumer_events(df_y)
    assert list(picked["事件簇名"]) == ["德牧家人"]


def test_demo_routes_follow_active_prompts_and_r5_fields():
    mod = _load("run_demo_routes_v1120", SCRIPTS / "run_demo_routes.py")
    for name, route in mod.ROUTES.items():
        assert route["prompt"] == f"prompts/{ACTIVE[name]}"
        system = mod._batch_system_prompt(route)
        assert "{{title}}" not in system and "【待标注数据】" not in system, name
        assert "40 字" not in system and "40字" not in system.split("## Demo 批量模式")[1]
        user = json.loads(mod._batch_user_prompt(route, [{"row_id": "a"}]))
        keys = set(user["required_output"]["results"][0])
        assert route["label"] in keys
    r5 = mod.ROUTES["r5"]
    assert r5["label"] == "是否消费洞察"
    rows = [{"row_id": "a"}, {"row_id": "b"}]
    normalized = mod._normalize_results("r5", rows, [
        {"row_id": "a", "是否消费洞察": "是", "适用消费人群": "亲子家庭", "观点情绪": "",
         "消费场景": "秋假亲子出游", "消费需求": "选择遛娃目的地", "洞察线索": "呈现亲子目的地选择需求"},
        {"row_id": "b", "是否消费洞察": "否", "洞察线索": "泛bug建议征集"},
    ])
    assert normalized[0]["r5_消费场景"] == "秋假亲子出游"
    assert normalized[1]["r5_适用消费人群"] == ""
    # R5 每行输出预算大于默认 160
    assert r5["row_tokens"] > mod.DEFAULT_ROW_TOKENS


def test_e1_wow_maps_legacy_industry_names():
    e1 = _load("e1_industry_v1120", STATS / "e1_industry.py")
    text = ("| TOP10行业 | 热点总计 |\n|---|---|\n| 影视综艺 | 10 |\n"
            "| 食品零食 | 8 |\n| 餐饮连锁 | 5 |\n")
    assert e1.parse_top10_from_e1_data(text) == ["影视综艺", "食品", "餐饮"]
