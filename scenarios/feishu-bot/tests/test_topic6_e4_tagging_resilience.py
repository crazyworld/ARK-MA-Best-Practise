"""2026-11-13 E4 打标降级事故修复回归（P1/P3/P4/P5）。

事故链条（212 生产日志 + relay 实测坐实）：
  e4_tagging 起始预算 4000 → 8000 → 16000，MAX_TOKENS_RETRIES=2 用完放弃；
  真实需要约 16541，只差 500 被判"打标失败"；随后 _filter_e4_tagged 保守保留全部候选，
  每条打上「未打标（调用失败，保守保留）」占位类型；渲染时没有"变动类型/风险类型"列，
  撰写模型面对 40 条同一占位类型，自己概括成「品牌合作动态」「综合舆情风险」；
  打标本该过滤掉的候选（如广州商学院 vs 快递网点，双方都非面向消费者的品牌）全部留在合作动态里。

P1 起始预算：按 relay 实测需求抬高（打标 16541 / 枚举 11978），给足余量
P3 删静默降级：打标失败 → 重跑 1 次 → 仍失败则 E4 报错，不再默认打占位类型往下写
P4 小标题白名单：撰写稿自造小标题（品牌合作动态/综合舆情风险）必须被判失败
P5 e4_quality.json：打标覆盖率/占位类型数/类型分布/预算升档次数，供 HC 与人排查
"""

from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest

INSIGHT = (Path(__file__).resolve().parents[1] / "cases" / "topic6" / "ma-resources"
           / "skills" / "topic6-insight")
SCRIPTS = INSIGHT / "scripts"


def _load_pe(name: str = "t6_pe_quality"):
    """pipeline_e 顶部 import AsyncOpenAI（沙箱才有），本地按既有做法 stub 掉。"""
    if str(INSIGHT / "01_统计") not in sys.path:
        sys.path.insert(0, str(INSIGHT / "01_统计"))
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    old = sys.modules.get("openai")
    old_out, old_err = sys.stdout, sys.stderr
    stub = types.ModuleType("openai")
    stub.AsyncOpenAI = object
    sys.modules["openai"] = stub
    try:
        sys.stdout = types.SimpleNamespace(buffer=io.BytesIO())
        sys.stderr = types.SimpleNamespace(buffer=io.BytesIO())
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / "pipeline_e.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if old is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old


# ------------------------------------------------------------------ P1 起始预算

def test_e4_substep_budgets_cover_measured_peak():
    """打标/枚举实测峰值 16541 / 11978，起始预算必须显著高于它，否则每周撞线。"""
    pe = _load_pe("t6_pe_budget")
    assert pe.E4_TAGGING_MAX_TOKENS >= 64000
    assert pe.E4_CURATION_MAX_TOKENS >= 64000
    assert pe.E4_ARBITRATION_MAX_TOKENS >= 32000
    assert pe.E4_WRITING_MAX_TOKENS >= 64000


def test_e3_substep_budgets_raised():
    pe = _load_pe("t6_pe_budget_e3")
    assert pe.E3_TAGGING_MAX_TOKENS >= 64000
    assert pe.E3_PLATFORM_WRITE_MAX_TOKENS >= 32000


# ------------------------------------------------------------------ P3 删静默降级

def _candidates(risk_n: int = 3, coop_n: int = 3) -> dict:
    return {
        "舆情风险": [{"事件簇名": f"风险{i}", "行业": "数码3C", "热度分": "90.0 分",
                      "具体热搜": [], "风险判断说明": "x"} for i in range(risk_n)],
        "合作动态": [{"事件簇名": f"合作{i}", "商业实体": "某品牌", "行业": "食品零食",
                      "热度分": "90.0 分", "代表热搜标题": "t", "代表热搜链接": "",
                      "合作动态说明": "x"} for i in range(coop_n)],
        "营销观察": [], "消费洞察": [],
    }


def test_filter_no_longer_invents_fallback_type_when_tags_missing():
    """tags=None 不再等于"静默保留"：必须抛错，让调用方重跑或整体失败。"""
    pe = _load_pe("t6_pe_no_fallback")
    cand = _candidates()
    with pytest.raises(Exception):
        pe._filter_e4_tagged(cand, None, None, strict=True)


def test_filter_reports_missing_rows_instead_of_placeholder():
    """tags 存在但漏了某条 → strict 模式下抛错并指出漏了哪些，不再打「未分类（打标遗漏）」."""
    pe = _load_pe("t6_pe_missing")
    cand = _candidates(risk_n=3, coop_n=1)
    risk_tags = {
        "风险0": {"事件簇名": "风险0", "风险类型": "品牌舆情事件", "可接性": "接", "理由": "r"},
        "风险2": {"事件簇名": "风险2", "风险类型": "明星舆论风波", "可接性": "接", "理由": "r"},
    }
    coop_tags = {"合作0": {"事件簇名": "合作0", "变动类型": "合作争议",
                           "参考价值": "有用", "理由": "r"}}
    with pytest.raises(Exception) as exc:
        pe._filter_e4_tagged(cand, risk_tags, coop_tags, strict=True)
    assert "风险1" in str(exc.value)


def test_filter_strict_false_still_works_for_manual_override():
    """人工强制续跑开关：strict=False 保留旧兜底行为，但返回值必须带降级标记。"""
    pe = _load_pe("t6_pe_manual")
    cand = _candidates(risk_n=2, coop_n=0)
    risk_kept, coop_kept, degraded = pe._filter_e4_tagged(
        cand, None, {"合作0": {}}, strict=False)
    assert degraded is True
    assert all(t.startswith("未分类") for t, _ in risk_kept)


# ------------------------------------------------------------------ P4 小标题白名单

LEGAL_TITLES = [
    "品牌联名/资源合作", "品牌×IP合作", "品牌×明星代言合作", "合作争议",
    "品牌舆情事件", "政策监管风险", "明星舆论风波", "文娱作品口碑", "机构/赛事争议",
    "未分类",
]


def test_illegal_subheading_fails_validation():
    """事故现场的两个自造小标题必须被判失败。"""
    pe = _load_pe("t6_pe_whitelist_bad")
    text = ("上周合作动作以代言为主。\n\n**品牌合作动态**\n\n本期代言密集。\n\n"
            "| 事件 | 代表热搜 | 行业 | 热度分 | 备注 |\n|---|---|---|---|---|\n"
            "| A | [t](https://a.com) | 食品 | 90.0 | 备注 |\n")
    issues = pe._validate_e4_group_subheadings(text)
    assert any("品牌合作动态" in i for i in issues)

    risk_text = ("本期风险集中。\n\n**综合舆情风险**\n\n"
                 "| 风险事件 | 具体热搜 | 风险影响 |\n|---|---|---|\n"
                 "| B | - [t](https://b.com) | 说明 |\n")
    assert any("综合舆情风险" in i for i in pe._validate_e4_group_subheadings(risk_text))


def test_legal_subheadings_pass():
    pe = _load_pe("t6_pe_whitelist_ok")
    text = "\n\n".join(f"**{t}**\n\n一句观察。\n" for t in
                       ("品牌联名/资源合作", "品牌×明星代言合作", "合作争议"))
    assert pe._validate_e4_group_subheadings(text) == []


def test_validate_e4_group_includes_subheading_check_for_group_a():
    pe = _load_pe("t6_pe_group_a")
    text = ("**品牌合作动态**\n\n| 事件 | 代表热搜 | 行业 | 热度分 | 备注 |\n"
            "|---|---|---|---|---|\n| A | t | 食品 | 90.0 | 备注 |\n")
    issues = pe.validate_e4_group("a", text, "")
    assert any("品牌合作动态" in i for i in issues)


# ------------------------------------------------------------------ P5 e4_quality.json

def test_quality_payload_shape():
    pe = _load_pe("t6_pe_quality_shape")
    cand = _candidates(risk_n=2, coop_n=2)
    risk_kept = [["品牌舆情事件", cand["舆情风险"][0]],
                 ["明星舆论风波", cand["舆情风险"][1]]]
    coop_kept = [["品牌×明星代言合作", cand["合作动态"][0]],
                 ["合作争议", cand["合作动态"][1]]]
    quality = pe.build_e4_quality(cand, risk_kept, coop_kept,
                                 tag_res={"max_tokens_used": 64000, "max_tokens_upgrades": 0,
                                          "output_tokens": 8895},
                                 parse_warnings=[])
    assert quality["tag_coverage"] == 1.0
    assert quality["fallback_type_count"] == 0
    assert quality["type_distribution"]["舆情风险"]["品牌舆情事件"] == 1
    assert quality["type_distribution"]["合作动态"]["合作争议"] == 1
    assert quality["budget"]["max_tokens_used"] == 64000
    assert quality["degraded"] is False


def test_quality_flags_degraded_and_fallback_count():
    pe = _load_pe("t6_pe_quality_degraded")
    cand = _candidates(risk_n=2, coop_n=0)
    risk_kept = [["未分类（打标遗漏，保守保留）", cand["舆情风险"][0]],
                 ["未分类（打标遗漏，保守保留）", cand["舆情风险"][1]]]
    quality = pe.build_e4_quality(cand, risk_kept, [], tag_res=None,
                                  parse_warnings=["E4候选打标: 输出不完整"])
    assert quality["tag_coverage"] < 1.0
    assert quality["fallback_type_count"] == 2
    assert quality["degraded"] is True
    assert quality["parse_warnings"]


# ------------------------------------------------------------------ P0 升档上限

def test_ceiling_reaches_model_hard_limit():
    """ceiling 必须等于 relay 实测硬上限 393216;旧值 32000 让升档形同虚设。"""
    pe = _load_pe("t6_pe_ceiling")
    assert pe.MAX_TOKENS_CEILING == 393216
    assert pe.MAX_TOKENS_TOTAL_ATTEMPTS >= 4


def test_upgrade_loop_not_capped_at_two_upgrades():
    """起始 4000 连撞线时,升档次数由总尝试次数决定,不再被"最多升 2 次"锁死。"""
    pe = _load_pe("t6_pe_upgrade")
    cur, upgrades = 4000, 0
    seen = [cur]
    for _ in range(pe.MAX_TOKENS_TOTAL_ATTEMPTS):
        if upgrades + 1 >= pe.MAX_TOKENS_TOTAL_ATTEMPTS or cur >= pe.MAX_TOKENS_CEILING:
            break
        cur = min(cur * 2, pe.MAX_TOKENS_CEILING)
        upgrades += 1
        seen.append(cur)
    # 4000→8000→16000→32000→64000:至少能升到 64000,覆盖实测峰值 16541 与 4 倍波动
    assert max(seen) >= 64000
    assert upgrades > pe.MAX_TOKENS_RETRIES
