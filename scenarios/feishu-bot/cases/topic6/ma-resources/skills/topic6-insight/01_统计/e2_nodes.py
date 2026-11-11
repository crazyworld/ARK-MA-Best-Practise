#!/usr/bin/env python3
"""
01_统计 · E2 营销节点
统计口径唯一权威。02_洞察/E2_营销节点/v8.md 不重复。
"""

from __future__ import annotations

import csv
import math
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from _common import _render_titles_with_links, explode_pipe, fmt_score

# 默认复用 fetch-normalize skill 的全年节点 CSV，避免在多个 skill 里维护副本。
# MARKETING_CALENDAR_PATH 仍可覆盖，且兼容旧 Markdown 表格格式。
_DEFAULT_CALENDAR = (
    "/mnt/skills/topic6-fetch-normalize/references/marketing_calendar_2026.csv"
)
_calendar_env = os.environ.get("MARKETING_CALENDAR_PATH", _DEFAULT_CALENDAR)
CALENDAR_PATH = Path(_calendar_env) if _calendar_env else None

# 节点类型 → 是否需要长周期筹备（对应 marketing-node-tagging/SKILL.md 候选节点窗口规则：
# 电商大促14天/大众节日7天 vs 小众节点2天/二十四节气1天，7天为分界）。
# "本周节点预告"密度判断不能只看节点数量——节点数量多但全是短筹备类型时，
# 实际筹备工作量是可控的；只有出现长筹备类型才真正意味着需要提前启动准备。
LONG_PREP_TYPES = {"电商大促", "大众节日"}


# "补充信号"（dist>=0，还没发生的节点折叠进本周预告末尾）的远距离上限（天，2026-10-09新增）。
# 此前 dist 没有上限，可能把三个月后才发生的节点也当"本周预告的补充信号"带出来；
# 加30天上限是为了覆盖"同月相邻大节点会被一起提前讨论"这类场景（如中秋+国庆同月但
# 相隔两周，国庆早期讨论可能在中秋那周就已经出现，但国庆本身不在"下周"窗口内），
# 又不至于扯出跟"本周"毫无关系的远期节点。
UPCOMING_SIGNAL_MAX_DIST = 30

# "具体热搜"挑选算法参数（2026-10-13新增，见下方 _pick_representative_titles() docstring）。
# ALPHA：综合分里"热度"的权重，1-ALPHA是"簇大小"的权重——无历史数据支撑偏向哪边，
# 0.5/0.5起步，后续按真实数据观察调整（跟e3_platforms.py::E3_BORROW_BOOST_BETA
# 当年定0.5起步同一个思路，不是定论）。
E2_CLUSTER_SCORE_ALPHA = 0.5
# 候选池里"事件簇大小>=2"的簇数量少于这个值时，判定"不存在真实的簇竞争"，直接
# 退化为纯标准化热度分排序——这是结构性判断，不是数量阈值：要凑够2个"簇大小>=2"
# 的簇，候选池至少要有4条，所以"热点总计<4"的节点必然会退化，不需要额外设一个
# "总条数"阈值去保护小节点（真实数据验证：21条的中秋节已证明"总条数不大"≠"不会
# 出现簇竞争"，按总数设阈值反而会漏判这类场景，结构判断更准）。
E2_MIN_COMPETING_CLUSTERS = 2


def _pick_representative_titles(grp: pd.DataFrame, n: int) -> pd.DataFrame:
    """按"热度+话题广度"综合挑n行代表性候选（2026-10-13新增，见
    `2.0版本优化记录.md`"需求7"）。

    背景：此前"具体热搜"纯按标准化热度分取TopN，W40真实数据复现2次同类问题——
    国庆节（116条）TOP3里2条来自同一个事件簇（"国庆黄金周旅游消费热潮"），另一个
    规模相当的簇（"国庆节庆祝活动与文旅热潮"）完全没被展示；中秋节（21条）同样
    2条来自同一簇（"Tiffany月饼事件及后续舆论发酵"）。根因：纯按单条分数排序，
    选出的多条可能是同一个话题反复刷屏，没有照顾到"话题广度"。

    修复分两步，不是单纯线性加权（线性加权在国庆节案例实测会导致另一个问题——
    见下方"固定保留全局最热"的说明）：

    1. **固定保留1个"全局热度最高"的名额**，不管它是否被C2聚类——这一步是必须的：
       若只按"簇大小+热度"综合分排序，一个真正的爆款单条（如国庆节"青春没有售价，
       带我妈从重庆打出租车到北京"，100分但C2未聚类，簇大小=1）会被几个大簇
       （簇大小6-7）系统性压过去，排到TOP3之外——这反而丢了"最热"这个维度本身，
       不是我们要的效果。固定1个名额保证"最热的单条"永远有展示机会。
    2. 排除掉该名额所属的整个簇（避免剩余名额又选到同簇另一条，重新引入"同簇
       重复"问题），对剩余候选做"结构性判断+综合排序"选够剩下n-1个名额：
       - 结构性判断：剩余候选里"簇大小>=2"的簇不足 E2_MIN_COMPETING_CLUSTERS 个时，
         说明不存在真实的簇竞争，直接按标准化热度分排序补齐（跟改动前行为一致）
       - 否则按综合分 `ALPHA×H + (1-ALPHA)×F` 排序取不同簇的代表（H=该簇最高
         标准化分/100，F=该簇大小做log2+候选池内min-max归一化，仿
         e3_platforms.py::_score_word_candidates_v1_legacy() 给词频做归一化的
         同一手法），每簇只取簇内最高分的1条，不展示同簇多条
       - 选出的簇数不足时，用剩余候选里标准化热度分最高的行补齐（兜底逻辑仿
         _common.py::top_titles_relevant_str() 的fill模式）

    事件簇名为空（C2未聚类）的行视为"簇大小=1"的独立候选，不排除、不降级跟有
    簇名的行同场竞争——W40真实数据国庆节57%未聚类，不能简单忽略这部分。

    展示顺序（2026-10-13新增）：最终选出的几条按"是否有事件簇名"分组展示——有
    事件簇名的排前面，事件簇名为空（未聚类）的排最后，组内再按标准化热度分降序；
    不是单纯按分数降序排列。理由：固定保留的"全局最热"名额如果恰好是未聚类的
    候选，单纯按分数排列会让它排在最前面，但它代表的是"单条爆款"而不是"一个
    被识别出来的话题"，展示优先级上应该让"确实被识别成一个话题"的簇代表更靠前，
    未聚类的爆款作为补充放在后面。

    `len(grp) <= n` 时直接全给，不需要选择（跟调用方 `n_rep` 短路逻辑衔接，
    这里是双重保险，调用方已经做过这个判断）。
    """
    if len(grp) <= n:
        return grp

    grp = grp.copy()
    _single_ids = pd.Series("__single_" + grp.index.astype(str), index=grp.index)
    grp["_ck"] = grp["事件簇名"].fillna(_single_ids)

    def _order_for_display(df: pd.DataFrame) -> pd.DataFrame:
        """展示顺序：有事件簇名的排前面，未聚类（事件簇名为空）排最后，
        组内再按标准化热度分降序——见函数docstring"展示顺序"说明。"""
        df = df.copy()
        df["_has_cluster"] = df["事件簇名"].notna()
        return (
            df.sort_values(["_has_cluster", "标准化热度分"], ascending=[False, False])
            .drop(columns=["_ck", "_has_cluster"])
        )

    hottest = grp.nlargest(1, "标准化热度分")
    hottest_ck = hottest.iloc[0]["_ck"]
    rest = grp[grp["_ck"] != hottest_ck]

    remaining_n = n - 1
    if remaining_n <= 0 or rest.empty:
        return hottest.drop(columns="_ck")

    sizes = rest.groupby("_ck").size()
    if (sizes >= 2).sum() < E2_MIN_COMPETING_CLUSTERS:
        fill = rest.nlargest(remaining_n, "标准化热度分")
        return _order_for_display(pd.concat([hottest, fill]))

    stats = rest.groupby("_ck").agg(
        簇大小=("标准化热度分", "size"), 簇最高分=("标准化热度分", "max")
    )
    max_size = stats["簇大小"].max()
    stats["综合分"] = E2_CLUSTER_SCORE_ALPHA * (stats["簇最高分"] / 100) + (
        1 - E2_CLUSTER_SCORE_ALPHA
    ) * (stats["簇大小"].apply(lambda s: math.log2(s + 1)) / math.log2(max_size + 1))

    top_cks = stats.sort_values("综合分", ascending=False).head(remaining_n).index
    picked = pd.concat(
        rest[rest["_ck"] == ck].nlargest(1, "标准化热度分") for ck in top_cks
    )

    if len(picked) < remaining_n:
        fill = rest[~rest.index.isin(picked.index)].nlargest(
            remaining_n - len(picked), "标准化热度分"
        )
        picked = pd.concat([picked, fill])

    return _order_for_display(pd.concat([hottest, picked]))


def _load_calendar_rows() -> list[tuple[date, str, str]]:
    if not CALENDAR_PATH or not CALENDAR_PATH.exists():
        return []
    rows = _parse_calendar_file(CALENDAR_PATH)
    if not rows:
        # 文件存在却解析出 0 行=格式/列名不对(CC V2 E13:日历静默读空导致 E2 整段空白),
        # 必须报错而不是当成"本周暂无节点"
        raise ValueError(
            f"节点日历 {CALENDAR_PATH} 解析出 0 个节点:CSV 需含 date,name,type 列,"
            "Markdown 需为 | YYYY-MM-DD | 名称 | 类型 | 表格"
        )
    return rows


def _parse_calendar_file(path: Path) -> list[tuple[date, str, str]]:
    rows: list[tuple[date, str, str]] = []
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            for item in csv.DictReader(f):
                try:
                    node_date = date.fromisoformat((item.get("date") or "").strip())
                except ValueError:
                    continue
                name = (item.get("name") or "").strip()
                node_type = (item.get("type") or "").strip()
                if name:
                    rows.append((node_date, name, node_type))
        return rows

    with open(path, encoding="utf-8") as f:
        for line in f:
            match = re.match(
                r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|",
                line,
            )
            if not match:
                continue
            try:
                node_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            rows.append((node_date, match.group(2).strip(), match.group(3).strip()))
    return rows


def _load_node_calendar_full() -> dict[str, date]:
    """解析节点日历全年数据，返回{节点名称: 节点锚点日期}（不限时间窗口）。

    跟"本周节点预告"部分复用同一份日历文件，但那边只挑发布日~+6天窗口内的行；
    这里要拿到全年所有节点的锚点日期，供"提取节点"命中按锚点日期二次校验用
    （见 prep_e2_nodes() docstring）。文件不存在时返回空字典，调用方按
    "查不到锚点"分支统一兜底，不额外报错。"""
    if not CALENDAR_PATH or not CALENDAR_PATH.exists():
        return {}
    return {name: node_date for node_date, name, _node_type in _load_calendar_rows()}


def _build_last_week_table(bucket_df: pd.DataFrame) -> tuple[bool, str | None]:
    """按"提取节点"分组产出"上周节点回顾"汇总表markdown。
    返回 (has_data, data_md)；bucket_df 为空时 has_data=False。

    只接收已按锚点日期校验落在上周窗口的节点(2026-11-11 起日历查不到锚点的节点
    不进入报告，不再以「（锚点未核实）」标注展示，见 prep_e2_nodes 的 _classify)。

    "涉及品牌IP"列 2026-10-09改名"涉及品牌IP候选"：这一列此前直接当最终展示值用，
    但V1→V2把"涉及品牌"和"关键实体"（人物）两个字段合并成了"商业实体"一个字段后，
    这里的候选天然混着品牌/人物/IP/作品名，代码层没有类型信息可以过滤——交给Prompt
    在转写阶段判断过滤（见对应Prompt的「品牌筛选规则」），本函数只负责把候选算全，
    不做任何筛选，列名也要如实反映"这是候选，不是最终值"。
    """
    if bucket_df.empty:
        return False, None

    rows = []
    for node, grp in bucket_df.groupby("提取节点"):
        热搜条数 = len(grp)
        最高分 = grp["标准化热度分"].max()

        # 涉及品牌IP候选：C0"商业实体"字段（V2里C3"涉及品牌"+C1"关键实体"两个旧字段
        # 已收敛成这1个实体字段，不再分品牌/人物两列展示；混着品牌/人物/IP，交给
        # Prompt转写阶段筛选，见上方docstring）
        entities = explode_pipe(grp["商业实体"])
        entities = sorted(set(e for e in entities if e))
        涉及品牌IP候选 = " / ".join(entities) if entities else "—"

        # 涉及品类（行业归属 explode distinct）
        inds = explode_pipe(grp["行业归属"])
        inds = sorted(set(i for i in inds if i))
        涉及品类 = " / ".join(inds) if inds else "—"

        # 具体热搜（2026-10-13改为"热度+话题广度"综合挑选，见_pick_representative_titles()
        # docstring——此前纯按标准化分取TopN，W40真实数据复现"同一事件簇占多个名额、
        # 挤占话题广度"的问题；>3条给3条，≤3条全给，仍沿用这条既有短路规则）
        n_rep = 3 if 热搜条数 > 3 else 热搜条数
        rep_rows = _pick_representative_titles(grp, n=n_rep)
        具体热搜 = _render_titles_with_links(rep_rows, show_platform=True)

        rows.append({
            "节点": node,
            "热点总计": 热搜条数,
            "热度值": fmt_score(最高分),
            "涉及品牌IP候选": 涉及品牌IP候选,
            "涉及品类": 涉及品类,
            "具体热搜": 具体热搜,
        })

    node_df = pd.DataFrame(rows).sort_values("热点总计", ascending=False).reset_index(drop=True)
    col_order = ["节点", "热点总计", "热度值", "涉及品牌IP候选", "涉及品类", "具体热搜"]

    lines = [
        f"共命中 {len(node_df)} 个节点，{len(bucket_df)} 条节点-热搜对应关系",
        "",
        "| " + " | ".join(col_order) + " |",
        "|" + "---|" * len(col_order),
    ]
    for _, r in node_df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in col_order) + " |")
    return True, "\n".join(lines)


def prep_e2_nodes(df_y: pd.DataFrame, pub_date: date) -> dict:
    """
    返回结构化结果：

    {
      "last_week": {"has_data": bool, "data_md": str|None, "empty_text": str},
      "next_week": {"has_data": bool, "data_md": str|None, "empty_text": str},
    }

    has_data=False 时 data_md=None，empty_text 是无数据情况下的最终文案——
    这种情况 pipeline_e.py 会直接采用 empty_text 作为最终输出，不再调用 LLM
    （避免"无节点"这种确定性状态被交给 LLM 判断/转写导致输出不稳定）。
    has_data=True 时 data_md 是喂给 LLM 的表格片段（不含各子部分标题，
    标题由 pipeline_e.py 组装 Prompt 输入时统一加）。
    """
    result: dict = {}

    # ===== 提取节点展开 + 按锚点日期二次校验 =====
    exploded = df_y.assign(提取节点=df_y["提取节点"].fillna("").str.split("|")).explode("提取节点")
    exploded["提取节点"] = exploded["提取节点"].str.strip()
    exploded = exploded[exploded["提取节点"] != ""]

    signal_lines: list[str] = []  # dist>=0的节点，折叠进"本周节点预告"的补充信号文字

    if exploded.empty:
        result["last_week"] = {"has_data": False, "data_md": None, "empty_text": "上周无节点。"}
    else:
        calendar = _load_node_calendar_full()
        node_dist: dict[str, int | None] = {}
        for node in exploded["提取节点"].unique():
            node_date = calendar.get(node)
            node_dist[node] = (node_date - pub_date).days if node_date else None

        unknown_nodes = sorted(n for n, d in node_dist.items() if d is None)
        if unknown_nodes:
            print(
                f"[e2_nodes] ⚠️ 日历里查不到以下节点的锚点日期，不进入报告（只展示确定性节点；"
                f"若属命名对不上，请在 marketing_calendar_2026.csv 补一行）：{unknown_nodes}",
                file=sys.stderr,
            )

        def _classify(node: str) -> str | None:
            dist = node_dist[node]
            # 2026-11-11:查不到锚点日期的节点不再「保守归入上周」——报告只给确定性内容
            if dist is None:
                return None
            if -7 <= dist <= -1:
                return "last_week"
            if 0 <= dist <= UPCOMING_SIGNAL_MAX_DIST:
                return "upcoming"  # 还没发生、且在30天远距离上限内，折叠进预告补充信号
            return None

        exploded["_bucket"] = exploded["提取节点"].map(_classify)

        has_lw, md_lw = _build_last_week_table(exploded[exploded["_bucket"] == "last_week"])
        result["last_week"] = {"has_data": has_lw, "data_md": md_lw, "empty_text": "上周无节点。"}

        upcoming_df = exploded[exploded["_bucket"] == "upcoming"]
        if not upcoming_df.empty:
            for node, grp in upcoming_df.groupby("提取节点"):
                dist = node_dist[node]
                热搜条数 = len(grp)
                最高分 = grp["标准化热度分"].max()
                signal_lines.append(
                    f"{node}(还有{dist}天)已有{热搜条数}条提前讨论热搜,热度{fmt_score(最高分)}"
                )

    # ===== 本周节点预告 =====
    window_end = pub_date + timedelta(days=6)

    if not CALENDAR_PATH or not CALENDAR_PATH.exists():
        result["next_week"] = {
            "has_data": False,
            "data_md": None,
            "empty_text": (
                f"节点日历文件未找到（MARKETING_CALENDAR_PATH="
                f"{CALENDAR_PATH or '未设置'}），发布窗口：{pub_date} ~ {window_end}"
            ),
        }
    else:
        next_week = []
        for node_date, name, node_type in _load_calendar_rows():
            if pub_date <= node_date <= window_end:
                next_week.append({
                    "节点日期": node_date.isoformat(),
                    "节点名称": name,
                    "节点类型": node_type,
                })

        if not next_week:
            result["next_week"] = {
                "has_data": False,
                "data_md": None,
                "empty_text": "本周暂无节点。",
            }
        else:
            覆盖天数 = len({n["节点日期"] for n in next_week})
            窗口天数 = (window_end - pub_date).days + 1
            长筹备 = [n for n in next_week if n["节点类型"] in LONG_PREP_TYPES]
            短筹备 = [n for n in next_week if n["节点类型"] not in LONG_PREP_TYPES]
            lines = [
                f"发布窗口：{pub_date} ~ {window_end}（共{窗口天数}天），"
                f"共 {len(next_week)} 个节点，覆盖 {覆盖天数}/{窗口天数} 天；"
                f"长筹备节点（电商大促/大众节日，通常需提前7-14天启动）{len(长筹备)} 个，"
                f"短筹备节点（小众节点/二十四节气，1-2天即可）{len(短筹备)} 个",
                "",
                "| 节点日期 | 节点名称 | 节点类型 |",
                "|---|---|---|",
            ]
            for n in next_week:
                lines.append(f"| {n['节点日期']} | {n['节点名称']} | {n['节点类型']} |")

            if signal_lines:
                lines.append("")
                lines.append("补充信号（已有真实讨论数据支撑，供预告段落酌情提及，不需要单独出表格）：")
                for s in signal_lines:
                    lines.append(f"- {s}")

            result["next_week"] = {
                "has_data": True,
                "data_md": "\n".join(lines),
                "empty_text": "本周暂无节点。",
            }

    return result
