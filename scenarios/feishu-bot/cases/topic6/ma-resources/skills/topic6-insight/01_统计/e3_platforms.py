#!/usr/bin/env python3
"""
01_统计 · E3 平台新鲜事
统计口径唯一权威。02_洞察/E3_平台新鲜事/v6.md 不重复。
综合热度指数公式来源：references/综合热度指数算法说明.md（用户提供的原始算法说明副本）。
"""

from __future__ import annotations

import math
import sys

import pandas as pd

from _common import fmt_score, top_titles_with_links_str, top_titles_relevant_str

EXPECTED_PLATFORMS = ["微博", "抖音", "B站", "知乎"]


def _check_platform_coverage(df_y: pd.DataFrame) -> None:
    """防御性检查（2026-09-15新增）：宽表"平台"字段的实际取值跟EXPECTED_PLATFORMS
    不完全对应时打印warning。风险场景：宽表平台字段拼写漂移（如"哔哩哔哩"vs"B站"），
    会导致该平台被 df_y[df_y["平台"]==pname] 过滤成空，静默走"上周XX无营销可用热点"
    分支，看起来像"这平台真的没热点"而不是"平台名没匹配上"——只打印，不中断执行。"""
    actual = set(df_y["平台"].dropna().unique())
    expected = set(EXPECTED_PLATFORMS)
    unexpected = actual - expected
    missing = expected - actual
    if unexpected:
        print(f"[e3_platforms] ⚠️ 宽表出现预期外的平台名：{unexpected}"
              f"（这些数据不会被任何版块统计，检查是否是平台字段拼写漂移）", file=sys.stderr)
    if missing:
        print(f"[e3_platforms] ⚠️ 预期平台缺失：{missing}"
              f"（本轮宽表里完全没有这个平台的数据，若非真实情况请检查平台字段拼写）", file=sys.stderr)


def _check_driving_words_coverage(p: pd.DataFrame, pname: str) -> None:
    """防御性检查（2026-09-15新增）："热点驱动词"字段大面积为空时打印warning。
    风险场景：这种情况下"平台热点词TOP3"会静默判定成"候选<2个不输出"，但真实原因
    可能是C1标注管道异常（那一轮忘了产出驱动词字段），不是真的没有驱动词候选——
    两种情况从最终报告上看不出区别，需要靠这条warning在执行日志里区分。"""
    if "热点驱动词" not in p.columns or p.empty:
        return
    non_empty_ratio = p["热点驱动词"].fillna("").str.strip().ne("").mean()
    if non_empty_ratio < 0.05:
        print(f"[e3_platforms] ⚠️ {pname}的热点驱动词字段非空率仅{non_empty_ratio:.1%}，"
              f"「平台热点词TOP3」可能因此空缺——请确认C1标注是否正确产出该字段"
              f"（而不是真的没有驱动词候选）", file=sys.stderr)


def _collect_word_candidates(p: pd.DataFrame) -> list:
    """对单个平台的数据做"热点驱动词"拆分，返回未打分的候选列表（词频>1的驱动词，
    每项：{热点驱动词, 词频, 均分, 具体热搜}）。

    2026-09-20从 _score_driving_words() 抽出（见HC3反馈3.2）：候选采集（拆分/过滤词频）
    与打分排序（见 _score_word_candidates()）拆成两步，中间插入一次轻量LLM"打标"步骤
    （pipeline_e.py::build_e3_task()）过滤"主体不明的动作/结果词"（如"胜利""大胜"——
    这类词是C0标注Prompt"非具名驱动"类目下产出的既有字段，本身不区分具名实体/网络梗
    还是纯动作结果描述，脚本层此前也没有任何过滤，词频>1就直接进入候选池打分排序）。
    不修改C0标注层（用户明确要求"不动C0标注，在写洞察时滤出去"），过滤逻辑完全落在
    这一层下游。

    返回的每一项都是原生python dict（可直接json序列化，供 e3_word_candidates.json
    导出+打标步骤round-trip），不含打分字段——打分留给 _score_word_candidates()，
    因为打分要用到"平台内最高词频"这类需要看到全部候选才能算的统计量，过滤发生在
    打分之前，过滤后候选集合变了，打分必须用过滤后的集合重新算，不能先打分再删。
    """
    words = p.copy()
    words["_驱动词"] = words["热点驱动词"].fillna("").str.split("|")
    words = words.explode("_驱动词")
    words["_驱动词"] = words["_驱动词"].str.strip()
    words = words[words["_驱动词"] != ""]

    word_rows = []
    for word, wgrp in words.groupby("_驱动词"):
        词频 = len(wgrp)
        if 词频 <= 1:
            continue  # 单条样本噪音风险高，剔除（综合热度指数算法说明.md Step1）
        word_rows.append({
            "热点驱动词": word,
            "词频": 词频,
            "均分": wgrp["标准化热度分"].mean(),
            "具体热搜": top_titles_with_links_str(wgrp, n=3, show_platform=False),
        })
    return word_rows


# ---------------------------------------------------------------------------
# 综合热度指数打分：v1_legacy（HC3修复前原始公式）/ v2（2026-09-20新增，见HC3反馈3.3）
# ---------------------------------------------------------------------------
# 本仓库目录不是git仓库（无版本控制兜底），Prompt文件靠"只增不改+新建v{N+1}.md"
# 做到可回溯；Python打分逻辑没有对应机制，所以这里照搬同样的精神：旧公式原样保留
# 不删除，新公式另起一个函数，用 ACTIVE_SCORING_VERSION 这一个开关决定当前生效
# 哪个版本——效果不好只需要把下面这一行改回"v1_legacy"，不需要凭记忆重写旧代码。
ACTIVE_SCORING_VERSION = "v2"

# v2引入的"可借势"加成系数（乘法模型，非DeepSeek讨论稿的线性加权模型，理由见
# HC3反馈3.3方案讨论：乘法让"可借势"的影响跟词本身基础质量成比例，一个基础分
# 很低的词即使被误判"可借势=是"也不会凭空跳到及格线附近，比线性加权更能抵抗
# 这个全新判断维度可能存在的打标噪音）。具体数值待真实数据验证后调整，不是定论。
E3_BORROW_BOOST_BETA = 0.5


def _score_word_candidates_v1_legacy(word_rows: list) -> pd.DataFrame:
    """HC3修复前的原始打分公式，均分70%基础权重+词频log尺度最多30%加成，
    组内相对min-max映射到[40,100]。2026-09-20起不再是默认路径（见
    ACTIVE_SCORING_VERSION），原样保留仅供快速回滚/跟v2并排对比，不再主动维护
    ——3.3之后如果还要调整打分逻辑，请在v2或新建v3上改，不要改这个函数。

    口径（对应 references/综合热度指数算法说明.md Step1-5，Step1"词频=1剔除"已在
    _collect_word_candidates()完成，这里假定传入的都已经是词频>1的候选）：
      - freq_norm = log2(词频) / log2(平台内最高词频+1)
      - raw = 均分 × (0.7 + 0.3 × freq_norm)（均分占70%基础权重，词频最多贡献30%加成）
      - 综合热度指数 = 40 + 60 × (raw-raw_min)/(raw_max-raw_min)，min/max只在传入的
        候选集合内部算（raw_max==raw_min退化情况：全部记为100.0，避免除零）

    候选为空时返回空DataFrame（带这几列，调用方直接判断.empty/len）。
    """
    cols = ["热点驱动词", "词频", "均分", "综合热度指数", "具体热搜"]
    if not word_rows:
        return pd.DataFrame(columns=cols)

    wdf = pd.DataFrame(word_rows)
    max_freq = wdf["词频"].max()
    wdf["freq_norm"] = wdf["词频"].apply(lambda f: math.log2(f) / math.log2(max_freq + 1))
    wdf["raw"] = wdf["均分"] * (0.7 + 0.3 * wdf["freq_norm"])
    raw_min, raw_max = wdf["raw"].min(), wdf["raw"].max()
    if raw_max > raw_min:
        wdf["综合热度指数"] = 40 + 60 * (wdf["raw"] - raw_min) / (raw_max - raw_min)
    else:
        wdf["综合热度指数"] = 100.0
    return wdf.sort_values("综合热度指数", ascending=False).reset_index(drop=True)[cols]


def _score_word_candidates_v2(word_rows: list) -> pd.DataFrame:
    """2026-09-20新增（HC3反馈3.3，引入"是否可借势"第三维度）。三个信号：
      - H = 均分/100：内容质量/受关注程度，理论范围[0,1]（均分本身就是0-100分）
      - F = 词频在候选池内部做log+min-max归一化，范围[0,1]（词频没有全局固定
        上限，只能相对候选池算，跟v1一样是"组内相对"）
      - B = 可借势（word_rows每项的"可借势"key，bool；取不到/未打标时默认False，
        即"打标之前/打标失败兜底"统一按"暂不加成"处理，不虚构借势信号）

    公式（乘法加成，"可借势"是放大器而非独立加分项，理由见模块顶部注释）：
      基础分 = 0.6×H + 0.4×F（均分60%+词频40%，词频权重比v1的log尺度30%上限更高，
                              专门解决"词频差21倍排名反而更低"的反直觉问题——真实
                              W37数据验证过，见HC3修复3.3讨论记录）
      raw    = 基础分 × (1 + E3_BORROW_BOOST_BETA × B)
      综合热度指数 = 40 + 60 × raw / (1 + E3_BORROW_BOOST_BETA)
        （分母把raw的理论上限重新归一到1，保证结果严格落在[40,100]，不依赖
        候选集合内部的实际min/max——跟v1"组内相对映射"是不同的设计哲学，好处
        是分数有绝对语义，代价是候选池整体偏弱的某一期可能没有词能拿到100分，
        这是设计取舍不是bug）

    候选为空时返回空DataFrame（带这几列，调用方直接判断.empty/len）。
    """
    cols = ["热点驱动词", "词频", "均分", "综合热度指数", "具体热搜"]
    if not word_rows:
        return pd.DataFrame(columns=cols)

    freqs = [r["词频"] for r in word_rows]
    freq_min, freq_max = min(freqs), max(freqs)

    out_rows = []
    for r in word_rows:
        H = r["均分"] / 100.0
        if freq_max > freq_min:
            F = ((math.log2(r["词频"] + 1) - math.log2(freq_min + 1))
                 / (math.log2(freq_max + 1) - math.log2(freq_min + 1)))
        else:
            F = 1.0  # 候选池内词频全相同（退化情况），不做区分，避免除零
        B = 1 if r.get("可借势") else 0  # 未打标/打标失败时r里没这个key，get()默认None→0
        base = 0.6 * H + 0.4 * F
        raw = base * (1 + E3_BORROW_BOOST_BETA * B)
        idx = 40 + 60 * raw / (1 + E3_BORROW_BOOST_BETA)
        out_rows.append({
            "热点驱动词": r["热点驱动词"],
            "词频": r["词频"],
            "均分": r["均分"],
            "综合热度指数": idx,
            "具体热搜": r["具体热搜"],
        })
    wdf = pd.DataFrame(out_rows)
    return wdf.sort_values("综合热度指数", ascending=False).reset_index(drop=True)[cols]


def _score_word_candidates(word_rows: list) -> pd.DataFrame:
    """打分调度入口，所有调用方（pipeline_e.py、本文件内其他函数）统一只调这一个
    函数，不直接调 _v1_legacy/_v2——回滚只改上面的 ACTIVE_SCORING_VERSION 常量，
    不用改任何调用点代码。

    v2需要每项候选自带"可借势"key才能生效加成；调用方若还没跑打标步骤（比如
    prep_e3_word_candidates()的预排序、prep_e3_word_freq_audit()的打标前审计），
    传入的word_rows不含这个key，v2会统一按B=0处理——效果等价于只用"0.6H+0.4F"
    基础分排序，不会因为缺这个字段报错或产出错误结果。
    """
    if ACTIVE_SCORING_VERSION == "v2":
        return _score_word_candidates_v2(word_rows)
    return _score_word_candidates_v1_legacy(word_rows)


def _score_driving_words(p: pd.DataFrame) -> pd.DataFrame:
    """= _score_word_candidates(_collect_word_candidates(p))，向后兼容旧调用点
    （prep_e3_word_freq_audit()——审计表在run_stats.py执行期产出，那时打标步骤
    还没跑，只能展示"打标前"的候选打分情况，供人工核对"打标前后差异"用）。"""
    return _score_word_candidates(_collect_word_candidates(p))


def _rank_top5_events(p: pd.DataFrame) -> pd.DataFrame:
    """按事件簇名分组统计+排序，返回热点总计>=2的TOP5候选（2026-10-14从
    prep_e3_platforms()抽出，见"需求9"）。排序口径的唯一权威实现——
    prep_e3_platforms()的展示表格 和 prep_e3_event_fulltext()的洞察初稿全量数据
    都复用这个函数，保证两处拿到的是完全同一批TOP5事件，不会出现"初稿分析了
    表格里没有的事件"这种错位。

    返回DataFrame每行带"_grp"字段（原始分组的pd.DataFrame，未渲染），调用方按
    自己的需要从"_grp"取"具体热搜"（3条代表，见top_titles_relevant_str）或
    全量标题（见prep_e3_event_fulltext()），不在这里渲染成最终格式。
    """
    rows_agg = []
    for event_name, grp in p.groupby("事件簇名"):
        热点总计 = len(grp)
        # 「热度值」展示列仍为簇内最高分(口径不变);第二排序改为簇内平均分
        # (加和/热点数量,2026-11-11 业务口径),作为最后一列「平均热度值」展示排序锚点
        最高分 = grp["标准化热度分"].max()
        平均分 = grp["标准化热度分"].mean()
        行业 = grp["行业归属"].dropna().iloc[0] if grp["行业归属"].notna().any() else "—"
        # 行业归属是C0标注层明确定义的多值字段（"|"分隔，如"游戏|音乐"），直接拼进
        # markdown表格会被解析成列分隔符导致整行错位（2026-10-14真实数据实测命中：
        # "游戏|音乐"/"游戏|体育赛事"/"数码3C|科技/AI产业"），仿_common.py给标题转义
        # 的做法转义"|"，不是展示多个行业用"/"连接那种语义合并，只是markdown语法转义。
        行业 = str(行业).replace("|", "\\|")
        rows_agg.append({
            "事件簇名": event_name,
            "热点总计": 热点总计,
            "最高标准化分": 最高分,
            "平均标准化分": 平均分,
            "行业归属": 行业,
            "_grp": grp,
        })
    agg = pd.DataFrame(rows_agg) if rows_agg else pd.DataFrame(
        columns=["事件簇名", "热点总计", "最高标准化分", "平均标准化分", "行业归属", "_grp"]
    )

    # 过滤单条独立话题（热点总计<2），再按"热点总计降序，同数按平均标准化分降序"排序取TOP5
    agg = agg[agg["热点总计"] >= 2]
    agg = agg.sort_values(
        ["热点总计", "平均标准化分"], ascending=[False, False]
    ).head(5).reset_index(drop=True)
    return agg


def prep_e3_platforms(df_y: pd.DataFrame) -> str:
    """
    生成4个平台（微博/抖音/B站/知乎）各自的热门事件TOPN（Markdown）。
    df_y 入参已是全局"是否营销可用=是"过滤后的数据（见 run_stats.py::filter_usable()），
    本函数不重复做这层过滤，只按 平台 二次筛选。E3的"热度值"列不带"分"字后缀
    （fmt_score(v, suffix="")），跟E2/E4带"分"字的格式不同，对齐参考示例。

    热门事件TOPN（按事件簇名分组，2026-09-15更新；排序/过滤口径2026-10-14抽到
    _rank_top5_events()，本函数不重复实现）
       列：排名 | 事件名称 | 行业 | 热点总计 | 热度值 | 具体热搜 | 平均热度值
       口径：
         - 先过滤：热点总计（事件簇内热搜条数）≥2 的事件簇才纳入候选（单条独立话题不展示）
         - 排序：热点总计降序，相同再按簇内平均标准化分降序（2026-11-11 由最高分改为平均分）
         - 热度值：簇内最高标准化分（口径不变）
         - 平均热度值（最后一列，2026-11-11新增）：簇内标准化热度分平均值，即第二排序锚点
         - 取TOP5，候选不足5个则标题按实际数量写"TOP N"
         - 具体热搜：事件簇内标准化分最高的title，固定最多3条，
           格式"- [标题](链接)"（不带平台标签，本表已按平台分组），<br>换行
         - 候选全部被过滤（无热点总计≥2的事件簇）：不出表格，给出说明文案

    2026-09-20起（见HC3反馈3.2）：原"平台热点词TOP3"子版块已从本函数移出，改为
    pipeline_e.py::build_e3_task() 在打标过滤之后拼接（见该函数注释）。原因：热点词
    候选池混入了C0标注层"非具名驱动"类目下产出的主体不明动作/结果词（如"胜利""大胜"），
    需要先经一次轻量LLM打标过滤，本函数（run_stats.py执行期，打标还没跑）无法产出
    过滤后的最终版本，只能产出候选原始数据（见 prep_e3_word_candidates()）。
    候选打分口径本身（词频/均分/综合热度指数）见 _score_word_candidates()。

    完整候选明细（含TOP10全量打分，未经打标过滤，供人工/自动化流程核对打分算法本身）
    见 prep_e3_word_freq_audit()，该函数产出不进LLM Prompt，是独立的审计向导出。

    2026-10-14起（见"需求9"）：撰写"热门事件洞察"时此前只喂3条代表性标题容易让LLM
    "欠采样误判"（大事件簇如58条，光看3条猜不准整体在讨论什么），改成"洞察初稿"
    独立步骤先读全量标题判断（见 prep_e3_event_fulltext()），本函数产出的仍是原来
    这份"3条代表+表格"数据，定稿步骤读初稿判断+这份表格数据一起转写，输出格式不变。
    """
    platforms = EXPECTED_PLATFORMS
    sections = []

    _check_platform_coverage(df_y)

    for pname in platforms:
        p = df_y[df_y["平台"] == pname].copy()

        if p.empty:
            sections.append(f"## {pname}\n\n上周 {pname} 无营销可用热点。")
            continue

        _check_driving_words_coverage(p, pname)

        plines = [f"## {pname}", ""]

        # ---- 热门事件 TOPN ----
        agg = _rank_top5_events(p)

        plines.append(f"本平台营销可用热搜：{len(p)} 条 / 事件簇：{p['事件簇名'].nunique()} 个\n")
        if agg.empty:
            plines.append(f"{pname} 上周热门事件均为单条独立话题（热点总计<2），不展示热门事件榜单。")
        else:
            plines.append(f"### {pname} · 热门事件 TOP {len(agg)}\n")
            plines.append("| 排名 | 事件名称 | 行业 | 热点总计 | 热度值 | 具体热搜 | 平均热度值 |")
            plines.append("|---|---|---|---|---|---|---|")
            for i, r in agg.iterrows():
                具体热搜 = top_titles_relevant_str(r["_grp"], r["事件簇名"], n=3, show_platform=False)
                plines.append(
                    f"| {i+1} | {r['事件簇名']} | {r['行业归属']} | {int(r['热点总计'])} "
                    f"| {fmt_score(r['最高标准化分'], suffix='')} | {具体热搜} "
                    f"| {fmt_score(r['平均标准化分'], suffix='')} |"
                )

        # 平台热点词TOP3已移出本函数（2026-09-20，见函数docstring），
        # 由 pipeline_e.py::build_e3_task() 打标过滤后另行拼接。

        sections.append("\n".join(plines))

    return "\n\n---\n\n".join(sections)


E3_EVENT_FULLTEXT_MAX_TITLES = 60  # 单事件簇全量标题封顶（2026-10-14新增，见"需求9"）。
# 防极端大事件簇（如春节期间可能上百条）让单次洞察初稿调用token失控；本次W40真实
# 数据实测最大事件簇（国乒）58条，60条封顶留了一点余量，超过的按标准化热度分
# 降序保留分数最高的那些（跟截断方向一致，不是随机丢弃）。


def prep_e3_event_fulltext(df_y: pd.DataFrame) -> dict:
    """每平台TOP5事件簇的全量原始标题（不含链接，纯文字），供"洞察初稿"步骤读
    （2026-10-14新增，见"需求9"）。

    背景：撰写"热门事件洞察"时，LLM此前只能看到每个事件簇3条代表性标题（见
    prep_e3_platforms()的"具体热搜"），对大事件簇（如本次W40真实数据国乒58条）
    容易"欠采样误判"——3条样本不足以准确理解这58条到底在讨论什么。改法：新增
    一个独立的"洞察初稿"步骤（pipeline_e.py::build_e3_task()），先读本函数产出
    的全量标题写一版初步判断，定稿步骤再基于初稿判断+原有的3条代表表格一起转写，
    最终输出格式不变。

    跟prep_e3_platforms()共用_rank_top5_events()的排序结果，保证"初稿看到的事件"
    跟"最终表格展示的事件"是完全同一批TOP5，不会错位。

    标题不含链接——链接是URL编码的中文字符串，单条经常上百token，初稿阶段只需要
    理解"这些标题在讨论什么"，不需要点击跳转，去掉链接能把一个58条的大事件簇
    塞进Prompt的token成本控制在远低于"带链接"的量级。

    返回：{平台名: [{事件簇名, 标题列表（最多E3_EVENT_FULLTEXT_MAX_TITLES条，按
    标准化热度分降序排列，超出封顶的从低分那头截断）}, ...]}
    平台无候选（TOP5为空）时对应值为空列表。
    """
    result: dict = {}
    for pname in EXPECTED_PLATFORMS:
        p = df_y[df_y["平台"] == pname].copy()
        if p.empty:
            result[pname] = []
            continue
        agg = _rank_top5_events(p)
        events = []
        for _, r in agg.iterrows():
            grp = r["_grp"]
            n = min(E3_EVENT_FULLTEXT_MAX_TITLES, len(grp))
            titles = grp.nlargest(n, "标准化热度分")["热点标题"].fillna("").tolist()
            titles = [t for t in titles if t]
            events.append({"事件簇名": r["事件簇名"], "标题列表": titles})
        result[pname] = events
    return result


E3_WORD_CANDIDATE_POOL_SIZE = 25  # 每平台候选池上限（打标前预截断，见 prep_e3_word_candidates() docstring）


def prep_e3_word_candidates(df_y: pd.DataFrame) -> dict:
    """"平台热点词"候选的结构化（JSON友好）版本，供 pipeline_e.py 的"打标"步骤使用
    （2026-09-20新增，见HC3反馈3.2）。跟E4的 prep_e4_candidates() 是同一种设计：
    这里导出的是"打标前"的候选（未经打标过滤），pipeline_e.py 读取后先做一次轻量
    LLM打标（①过滤"主体不明的动作/结果词"；②2026-09-20见HC3反馈3.3新增：判断
    "是否可借势/可复制形式"），再对过滤后的子集调用 _score_word_candidates() 重新
    打分排序取TOP3，拼回最终报告数据。

    候选口径：每平台 是否营销可用=是 的数据，"热点驱动词"字段explode拆分，词频>1
    （跟 _collect_word_candidates() 完全一致，本函数直接复用它），**每平台再按
    "打标前预打分"（即 _score_word_candidates() 在未过滤/未打标状态下的排序，此时
    v2公式里的"可借势"项统一按B=0处理，等价于纯按0.6均分+0.4词频排序）截断到TOP
    {E3_WORD_CANDIDATE_POOL_SIZE}**（2026-09-20实测发现：W37真实数据全平台词频>1的
    去重候选词高达423个，远超"几十个"的预期规模，会导致打标Prompt过长、单次LLM调用
    被max_tokens截断——跟E4"候选池先截断到TOP20再打标"是同一个道理：只有预打分
    靠前的候选才有机会进最终TOP3，长尾候选送进打标纯粹浪费token，不截断不会改变
    最终结果，只会让打标步骤更贵更慢甚至失败。3.3引入"可借势"加成后从15放宽到25，
    留出更宽的余量给"预打分较靠后但可能被可借势加成后反超"的候选，避免它们在
    还没被打标判断"可借势"之前就已经被预筛选环节淘汰）。

    返回：{平台名: [{热点驱动词, 词频, 均分, 具体热搜}, ...]}（已按预打分降序，
    每平台最多{E3_WORD_CANDIDATE_POOL_SIZE}条）
    平台本身无营销可用数据（p.empty）时对应值为空列表（区分"平台没数据"和"平台有数据
    但词频>1候选为0"两种情况，后者列表也是空的，pipeline_e.py不需要区分这两种情况，
    统一按"候选数<2不出此子版块"处理即可，不需要靠这个字段单独判断）。
    """
    result: dict = {}
    for pname in EXPECTED_PLATFORMS:
        p = df_y[df_y["平台"] == pname].copy()
        if p.empty:
            result[pname] = []
            continue
        raw_candidates = _collect_word_candidates(p)
        # 预打分只是为了确定截断顺序（打标后会用过滤后的子集重新打分，这里的
        # 综合热度指数/排名不是最终值），截断到TOP N 再转回原始候选dict（不含
        # 预打分字段，避免打标Prompt里出现容易被误解成"已经是最终排名"的数字）
        ranked = _score_word_candidates(raw_candidates)
        top_words = set(ranked.head(E3_WORD_CANDIDATE_POOL_SIZE)["热点驱动词"])
        result[pname] = [c for c in raw_candidates if c["热点驱动词"] in top_words]
    return result


def prep_e3_word_freq_audit(df_y: pd.DataFrame) -> str:
    """
    审计向导出，不进LLM Prompt——纯供人工/自动化流程核对"平台热点词TOP3"的打分算法本身。
    每个平台输出TOP10候选驱动词的完整打分明细：排名/热点驱动词/词频/均分/综合热度指数/具体热搜。

    2026-09-20起（见HC3反馈3.2）：本表展示的是**打标过滤前**的候选打分情况（跟最终报告
    实际展示的"平台热点词TOP3"不再完全一致——那份经过 pipeline_e.py::build_e3_task()
    的LLM打标过滤，会剔除"主体不明的动作/结果词"）。本表价值：①核对打分公式本身算得
    对不对；②对比"打标前TOP10"和最终报告"打标后TOP3"，能看出打标步骤具体过滤掉了哪些词。
    """
    platforms = EXPECTED_PLATFORMS
    sections = []
    for pname in platforms:
        p = df_y[df_y["平台"] == pname].copy()
        if p.empty:
            sections.append(f"## {pname}\n\n上周 {pname} 无营销可用热点。")
            continue

        wdf = _score_driving_words(p)
        if wdf.empty:
            sections.append(f"## {pname}\n\n无词频>1的驱动词候选。")
            continue

        top10 = wdf.head(10)
        lines = [
            f"## {pname}",
            "",
            "| 排名 | 热点驱动词 | 词频 | 均分 | 综合热度指数 | 具体热搜 |",
            "|---|---|---|---|---|---|",
        ]
        for i, r in top10.iterrows():
            lines.append(
                f"| {i+1} | {r['热点驱动词']} | {int(r['词频'])} "
                f"| {fmt_score(r['均分'], suffix='')} | {fmt_score(r['综合热度指数'], suffix='')} "
                f"| {r['具体热搜']} |"
            )
        sections.append("\n".join(lines))

    return "\n\n---\n\n".join(sections)
