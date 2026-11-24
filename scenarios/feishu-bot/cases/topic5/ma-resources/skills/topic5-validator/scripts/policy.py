"""Topic5 口径唯一事实源（Single source of truth）。

所有跨模块共用的口径常量集中在本文件，禁止在 handlers.py / scan_report.py /
validator.py / REPORT_CONTRACT.md / pipeline.v0.2.yaml 里各写一份。改口径只改这里。

加载方式：
  - scan_report.py 与本文件同目录，直接 import。
  - pipeline-orchestrator 侧通过 validator.load_policy() 按同一套目录探测规则惰性加载。
    加载失败时调用方保留本地兼容常量，并由 test_policy_single_source 断言两侧一致，
    避免模块导入期就因缺文件而崩溃。
"""
from __future__ import annotations

import re

# ── 版本 ──────────────────────────────────────────────────────────────
# 口径或规则语义变化时必须递增；pipeline.v0.2.yaml 的 validator_rules_version 必须与之一致，
# run_init 会校验（见 FIX-57）。
RULES_VERSION = "topic5-validator-qc-v1.9"
RULES_SOURCE = "客户 KS 检查清单 v1.22"

# ── 指标白名单 ────────────────────────────────────────────────────────
# 报告与方案只允许出现这些指标名（FIX-19 增补“声量贡献参考”）。
METRIC_WHITELIST = (
    "条均互动量",
    "条均互动倍数",
    "品牌提及率",
    "品牌主导讨论占比",
    "产品提及占比",
    "声量贡献参考",
    "NSR",
)

# ── NSR 口径（FIX-13）────────────────────────────────────────────────
# 客户 KS_Metrics_Calculation v0.12：分母恒为正+负，不含中性。
# 中性占 73.6% 的历史事故把 +57.6% 压成 +15.2%，客户脚本 aggregate.py:129 直接写死公式。
NSR_DEFINITION = "(正面内容数 - 负面内容数) / (正面内容数 + 负面内容数)"
# 客户脚本硬编码，不提供开关（KS_Metrics_Calculation v0.12 / aggregate.py:129）。
NSR_DENOMINATOR_IS_FIXED = True
NSR_MIN_POSITIVE_NEGATIVE = 30
NSR_DENOMINATOR_LABEL = "正面+负面（不含中性）"
# 报告正文出现下列说法即视为把旧口径写回了报告（P0）。
NSR_WRONG_DENOMINATOR_PATTERNS = (
    re.compile(r"分母(?:中)?(?:包含|含|包括)中性"),
    re.compile(r"含中性(?:内容)?(?:的)?分母"),
    re.compile(r"中性(?:内容)?(?:也)?计入分母"),
)

# ── 禁用措辞（FIX-20）────────────────────────────────────────────────
# 无条件禁用：指标名/解释里出现即越界。
FORBIDDEN_METRIC_TERMS = (
    "人均",          # 人均互动量等，统一改“条均互动量”
    # KS_Metrics_Calculation.md:212-219 禁用/替换词表原文只列「产品转化率」与
    # 「购买转化」；不收单字「转化」，避免误伤用户原话引用与合法描述。
    "转化率",        # 覆盖 产品转化率
    "购买转化",
    "品牌关联率",
    "高意向",
    "高黏性",
    "高粘性",
    "成本效率",
    "ROI",
    "互动效率",
    "覆盖效率",
    "效率倍数",
)
# 条件禁用：只在指标/达人/结构语境下违规，产品讨论里的“性价比”“渗透”属合法表达。
_CTX_SUBJECT = r"(?:达人|KOL|博主|层级|头部|腰部|尾部|素人|内容类型|PGC|UGC|品牌|活动|指标|声量|互动)"
FORBIDDEN_METRIC_PATTERNS = (
    # 达人/层级/内容类型的“性价比”属于对人的性价比评价 → 越界
    re.compile(_CTX_SUBJECT + r"[^。！？\n]{0,8}性价比"),
    re.compile(r"性价比[^。！？\n]{0,6}" + _CTX_SUBJECT),
    # “渗透率”指活动/品牌总声量时越界；该场景只能叫“声量贡献参考”
    re.compile(r"(?:活动|品牌|声量)[^。！？\n]{0,6}渗透率"),
    re.compile(r"渗透率[^。！？\n]{0,6}(?:声量|活动|品牌)"),
)

# ── 类别命名（FIX-32，检查清单 3.9 P0）──────────────────────────────
# 观点类别=讨论话题、参与方式=动作/形式，均不得带情感或评价倾向。
# 逐字照抄原版 blueai-opinion-discovery-skill / phase-ugc-discovery-skill run.py 的
# SENTIMENT_WORDS / EVAL_SUFFIX / CATCHALL_NAMES；编排器 label_review 引用这里。
TENDENCY_TERMS = (
    "好评", "差评", "吐槽", "避雷", "失望", "满意", "不满", "喜欢", "种草",
    "安利", "夸赞", "点赞", "批评", "抱怨", "推荐", "踩雷", "惊艳", "翻车",
    "正面", "负面", "积极", "消极",
)
TENDENCY_NAME_PATTERN = re.compile(r"(好|差|棒|赞|烂|高|低|贵|便宜)$")
CATCHALL_CATEGORY_NAMES = ("其他", "其它", "未分类", "杂项", "无法归类", "其余")

# ── 报告章节结构（FIX-42，KS_Report_Template §一 固定四章，唯一事实源）────
# 一品牌 / 二活动（每个活动 2.N.1~2.N.5，末节 2.{N+1} 活动拉升效果）/ 三经验总结 / 四总结与建议。
REPORT_CHAPTERS = ("一、品牌传播表现", "二、活动传播表现", "三、经验总结", "四、总结与建议")
BRAND_SECTIONS = (("1.1", "传播概况"), ("1.2", "口碑分析"), ("1.3", "内容分析"), ("1.4", "热门内容"))
CAMPAIGN_SECTIONS = (("1", "传播概况"), ("2", "提及漏斗分析"), ("3", "口碑分析"),
                     ("4", "内容分析"), ("5", "热门内容"))
LIFT_SECTION_TITLE = "活动拉升效果"
SUMMARY_SECTIONS = (("3.1", "成功经验"), ("3.2", "待改进问题"), ("3.3", "后续行动计划"))
# 「活动声量渗透」为旧标题，「渗透率」为禁用指标名（检查清单 3.8），不得再出现。
RETIRED_SECTION_TITLES = ("活动声量渗透", "综合表现定位")


def lift_prefix(campaign_count: int) -> str:
    """活动拉升节编号 2.{N+1}（单活动 = 2.2）。"""
    return f"2.{max(int(campaign_count), 1) + 1}"


# ── Callout 写作规则（FIX-46，KS_Report_Template §Callout / 检查清单 5.14、5.15）────
INSIGHT_MARK = "> [!insight]"
TIP_MARK = "> [!tip]"
CALLOUT_ADVICE_TERMS = ("建议", "应该", "应当", "需要")
CALLOUT_DEGREE_TERMS = ("非常", "极大", "显著", "明显", "大幅", "很少")
CALLOUT_METAPHOR_TERMS = ("买到", "换来", "跑赢", "撬动", "破圈")
CALLOUT_CAUSAL_TERMS = ("带来", "驱动", "拉动", "转化为", "说明用户缺乏兴趣")
CALLOUT_RULES = (
    "Callout 规则：整体洞察用 `> [!insight]` 开头的单个引用块，固定三句（判断句 → 数字证据 → 意味着什么），"
    "不写第四句、不给建议；1.1~1.4、2.N.1~2.N.5、2.{N+1} 各节在正文前用 `> [!tip]` 写一句"
    "（判断与数字证据在同一句），三、四章不写 callout。共用规则：结论在前、数字不作主语、不以数字开头；"
    "判断不超出数据（不写因果/动机，如" + "、".join(CALLOUT_CAUSAL_TERMS) + "）；"
    "禁用程度副词（" + "、".join(CALLOUT_DEGREE_TERMS) + "）与比喻（" + "、".join(CALLOUT_METAPHOR_TERMS) + "）；"
    "数据无差异写「无明显差异」或「样本不足，不下结论」；同一句的比例分母与主语口径一致。"
)

# 数据说明区禁止的方法论/版本/口径措辞（FIX-43，KS_Report_Template §零）。
DESCRIPTION_FORBIDDEN_TERMS = ("版本", "v0.", "口径", "采用", "聚合", "清洗", "抽样", "字段")

# ── 热门内容表固定字段（FIX-22，KS_Report_Template 1.4/2.N.5 P0）──────
# PGC 表含标题/内容类型/作者；UGC 表不设标题、内容类型、作者（隐私）。
# 「内容」截断 ≤50 字，超出加省略号。报告组装（report_contract）与质检共用。
HOT_PGC_COLUMNS = ("排名", "标题", "内容", "内容类型", "站点名称", "作者", "链接", "总互动量")
HOT_UGC_COLUMNS = ("排名", "内容", "站点名称", "链接", "总互动量")
HOT_EXCERPT_CHARS = 50


def tendency_hits(name: str) -> list[str]:
    """类别名命中的倾向词；以评价词结尾记为「评价后缀」。只判断类别名，不判原文。"""
    text = str(name or "").strip()
    hits = [word for word in TENDENCY_TERMS if word in text]
    if TENDENCY_NAME_PATTERN.search(text):
        hits.append("评价后缀")
    return hits

# ── 达人层级阈值（FIX-16）────────────────────────────────────────────
# 平台 → (头部下限, 腰部下限, 尾部下限)，单位：粉丝数。None 表示该平台不参与层级统计。
KOL_THRESHOLDS = {
    "抖音": (1_000_000, 100_000, 10_000),
    "微博": (1_000_000, 100_000, 10_000),
    "B站": (1_000_000, 100_000, 10_000),
    "快手": (1_000_000, 100_000, 10_000),
    "小红书": (500_000, 50_000, 10_000),
    "视频号": (500_000, 50_000, 10_000),
    "知乎": (100_000, 10_000, 1_000),
    "新闻": None,
    "微信": None,
    "其他": None,
    "未知": None,
}
# 内部层级键（数据层契约，禁止改成中文；中文只用于展示）
KOL_LEVELS = ("head", "waist", "tail", "ordinary", "unknown", "n/a")
KOL_LEVEL_LABELS = {
    "head": "头部", "waist": "腰部", "tail": "尾部",
    "ordinary": "素人", "unknown": "未知", "n/a": "不适用",
}
# 达人层级效能只统计这三层（排除素人/未知）
KOL_EFFECTIVENESS_LEVELS = ("head", "waist", "tail")

# ── 分维 NSR / 讨论对象（FIX-14）───────────────────────────────────
DISCUSSION_DIMENSIONS = ("品牌", "产品", "活动")
DISCUSSION_DIMENSION_LABELS = {
    "品牌": "品牌主导讨论",
    "产品": "产品主导讨论",
    "活动": "活动流程与获取主导讨论",
}
DIMENSION_NSR_MIN_POSITIVE_NEGATIVE = 30

# ── 拉升口径（FIX-15）───────────────────────────────────────────────
LIFT_BASE_MIN_VOLUME = 100          # 基准期总声量 ≤100 不输出变化率
LIFT_BASE_MIN_RATIO = 1 / 10        # 或不足活动期 1/10
LIFT_DAYS_RATIO_WARN = 3            # 两期天数比 >3 倍提示“仅供参考”
HOLIDAYS = {
    "元旦": ((1, 1), (1, 1)),
    "春节": None,                    # 农历，按年列在 HOLIDAY_BY_YEAR
    "五一": ((5, 1), (5, 5)),
    "618": ((6, 16), (6, 20)),
    "国庆": ((10, 1), (10, 7)),
    "双十一": ((11, 1), (11, 11)),
}
# 需要精确到日期的年份表（春节按当年国务院安排）
HOLIDAY_BY_YEAR = {
    2024: [("春节", "2024-02-10", "2024-02-17")],
    2025: [("春节", "2025-01-28", "2025-02-04")],
    2026: [("春节", "2026-02-17", "2026-02-23")],
    2027: [("春节", "2027-02-06", "2027-02-12")],
}

# ── 合并/统计阈值 ───────────────────────────────────────────────────
AGGREGATE_SUM_PATTERNS = ("合计声量", "总声量", "声量总和", "累计声量", "声量合计")
AGGREGATE_DISCLAIMER_MARKERS = ("不相加", "不合计", "逐个", "分别统计", "不得相加", "勿")
