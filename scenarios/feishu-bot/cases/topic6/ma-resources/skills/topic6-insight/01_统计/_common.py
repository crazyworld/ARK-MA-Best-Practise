#!/usr/bin/env python3
"""
01_统计 · 共享工具函数
跨版块复用的基础设施。专属逻辑放对应的 e{N}_xxx.py，这里只放≥2个版块共用的函数。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


_LINK_TITLE_BREAK_RE = re.compile(r"<br\s*/?>|\r\n|\r|\n", re.IGNORECASE)


def md_link_title(title) -> str:
    """热搜标题 → 可安全放进 markdown 链接文字与表格单元格的文本。

    - 换行 / `<br>` → 空格:原标题含换行时链接文字带 `<br>`,渲染断开(W44「不烧心」)
    - `[` `]` → 全角 `［` `］`:半角方括号打断 `[文字](url)`(W44 B站「【[AI全民制作人】」)。
      不用反斜杠转义:md2source 的 LINK_RE 不识别 `\\[`,会让链接整体失效
    - `|` → `\\|`:表格单元格内保持原有转义
    只用于链接文字;事件簇名/说明等其他单元格仍走各自原逻辑,不改正文字形。"""
    text = _LINK_TITLE_BREAK_RE.sub(" ", str(title))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text.replace("[", "［").replace("]", "］").replace("|", "\\|")


def fmt_score(v, suffix: str = " 分") -> str:
    """格式化标准化分：保留 1 位小数 + 可选后缀（默认" 分"）。
    suffix="" 用于不带"分"字的场景（如E3热点事件/平台热点词的热度值列，2026-09-15新增）。"""
    try:
        f = float(v)
        return f"{f:.1f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def top_titles_str(grp: pd.DataFrame, n: int = 5) -> str:
    """取 grp 中标准化分最高的 top-n title，用 <br> 分隔"""
    top = grp.nlargest(min(n, len(grp)), "标准化热度分")["热点标题"].tolist()
    return "<br>".join(top)


def _render_titles_with_links(top: pd.DataFrame, show_platform: bool) -> str:
    """把已经选好的候选行渲染成 "- [标题](链接) (平台)" 格式，<br>分隔。
    从 top_titles_with_links_str 抽出，供多个"先筛选/排序、再渲染"的变体复用。"""
    lines = []
    for _, r in top.iterrows():
        title = md_link_title(safe_field(r, "热点标题", ""))
        link = safe_field(r, "链接", "")
        platform = safe_field(r, "平台", "")
        text = f"[{title}]({link})" if (link and link != "—") else title
        if show_platform:
            text += f" ({platform})"
        lines.append(f"- {text}")
    return "<br>".join(lines)


def top_titles_with_links_str(grp: pd.DataFrame, n: int = 5, show_platform: bool = True) -> str:
    """取 grp 中标准化分最高的 top-n 条，格式化为 "- [标题](链接) (平台)"，<br>分隔。
    链接/平台取自宽表原始列（不由 LLM 编造/转写时改写——LLM 只负责原样转写这段文本）。
    链接缺失时退化为 "- 标题 (平台)"（不留空的 markdown 链接语法）。
    show_platform=False 用于调用方已按平台分组、平台名冗余的场景（如E3，2026-09-15新增）。
    标题原样保留时会转义"|"为"\\|"（部分B站标题自带竖线，否则会把表格撑破一列）。"""
    top = grp.nlargest(min(n, len(grp)), "标准化热度分")
    return _render_titles_with_links(top, show_platform)


_RELEVANCE_STOPWORDS = {
    "播出", "讨论", "发布", "事件", "话题", "热议", "争议", "上市", "报道",
    "现象", "口碑", "进展", "动态", "风波", "系列", "相关", "综合", "近期",
}


def _extract_relevance_keywords(event_name: str, min_len: int = 2) -> list[str]:
    """从事件簇名里提取用于"标题是否体现事件名称"判断的关键词片段：
    连续中文片段（>=min_len字）+ 连续英数字片段，去掉常见模板/结果词
    （这些词本身不是事件的"识别信息"，含它们不代表标题真的关联到这个具体事件）。"""
    tokens = re.findall(rf"[一-龥]{{{min_len},}}|[A-Za-z0-9]+", event_name or "")
    return [t for t in tokens if t not in _RELEVANCE_STOPWORDS and len(t) >= min_len]


def top_titles_relevant_str(grp: pd.DataFrame, event_name: str, n: int = 3,
                            show_platform: bool = True) -> str:
    """两步筛选后取TopN，格式同 top_titles_with_links_str：
    ①候选内先筛出"标题包含事件簇名关键词片段"的子集（能让读者一眼看出这条热搜属于该事件）；
    ②在符合条件的子集里按标准化热度分取前N条；不足N条时，用剩余候选里分数最高的补齐
    （补齐的排在后面，不覆盖前面已确认关联的结果）。

    跟 top_titles_with_links_str 的区别：后者是单条件排序（纯按分），只看分数不看标题跟
    事件名称是否对得上，容易选出"标题跟事件簇名字面无关、只是恰好这条热搜分数最高"的热搜
    （HC3 反馈3.1的实测案例：事件"剧集《早春晴朗》播出及口碑讨论"混入不含"早春晴朗"字样的
    "井柏然孙千空降"）。event_name 提取不出关键词（比如纯模板词组成）时退化为原有单条件排序。
    """
    keywords = _extract_relevance_keywords(event_name)
    if not keywords:
        return top_titles_with_links_str(grp, n=n, show_platform=show_platform)

    titles = grp["热点标题"].fillna("")
    mask = titles.apply(lambda t: any(k in t for k in keywords))
    relevant, rest = grp[mask], grp[~mask]

    top_relevant = relevant.nlargest(min(n, len(relevant)), "标准化热度分")
    remain = n - len(top_relevant)
    if remain > 0 and len(rest) > 0:
        top_fill = rest.nlargest(min(remain, len(rest)), "标准化热度分")
        top = pd.concat([top_relevant, top_fill])
    else:
        top = top_relevant
    return _render_titles_with_links(top, show_platform)


def safe_field(r, key: str, default: str = "—") -> str:
    """安全取值：key不存在或值为NaN/空字符串时返回default（`x or default`对NaN不生效）"""
    v = r.get(key)
    if v is None or (isinstance(v, float) and pd.isna(v)) or not str(v).strip():
        return default
    return v


def explode_pipe(series: pd.Series) -> pd.Series:
    """将 | 分隔的多值字段展开为单值 Series"""
    return series.fillna("").str.split("|").explode().str.strip()


def explode_aligned_pipe_cols(df: pd.DataFrame, anchor_col: str, aligned_cols: list) -> pd.DataFrame:
    """按 anchor_col（如"提取节点"）用 | 拆分行数为基准，把 aligned_cols 里的其他字段也按位置对齐拆开。

    对应 c3_v6_flatten.py 的广播存储惯例：某字段若对该行所有节点取值相同（含全空），
    只存一个值不加 |；段数少于 anchor 段数时视为广播值，重复填充到每个展开位置，
    不是"这个字段缺了几段"。段数相等则按位置一一对应拆开。

    anchor_col 为空/NaN 的行直接跳过（无命中，不产出任何展开行）。
    返回值：行数 = 原表按 anchor 段数展开后的总数，其余原始列原样重复填充到每个展开行。
    """
    records = []
    for _, row in df.iterrows():
        anchor_val = row.get(anchor_col)
        if pd.isna(anchor_val) or str(anchor_val).strip() == "":
            continue
        names = [n.strip() for n in str(anchor_val).split("|")]
        n = len(names)
        col_parts = {}
        for col in aligned_cols:
            val = row.get(col)
            parts = [] if pd.isna(val) else [p.strip() for p in str(val).split("|")]
            if len(parts) == n:
                col_parts[col] = parts
            elif len(parts) <= 1:
                col_parts[col] = [(parts[0] if parts else "")] * n
            else:
                # 段数既不等于 n 也不是广播（≤1），异常兜底：重复最后一段填满，不报错中断
                col_parts[col] = (parts + [parts[-1]] * n)[:n]
        for i, name in enumerate(names):
            new_row = row.to_dict()
            new_row[anchor_col] = name
            for col in aligned_cols:
                new_row[col] = col_parts[col][i]
            records.append(new_row)
    return pd.DataFrame(records)


def _read_period_from_config(proj: Path) -> tuple[date, date, str] | None:
    """优先读 run_config.yaml 的 period 字段（date_start/date_end/period_type），
    比目录名正则更权威——目录被手动改名/命名不规范时目录名会失效，run_config.yaml不受影响。
    读不到/解析失败返回 None，调用方回退到目录名正则。"""
    cfg_path = proj / "run_config.yaml"
    if not cfg_path.exists():
        return None
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        period = cfg.get("period", {})
        start_s, end_s = period.get("date_start"), period.get("date_end")
        period_type = period.get("period_type", "weekly")
        if not start_s or not end_s:
            return None
        return date.fromisoformat(str(start_s)), date.fromisoformat(str(end_s)), period_type
    except Exception:
        return None


def find_prev_project_dir(proj: Path) -> Path | None:
    """定位"上一个完整周期"对应的项目目录，找不到返回None（由调用方走兜底文案，不报错）。

    只对 period_type="weekly" 做环比——custom/biweekly 等非weekly周期没有明确的"上一周"
    语义，硬减7天算出的区间要么对不上任何真实项目、要么恰好撞上一个日期长度不同的项目产生
    看似正常但语义错误的"环比"，比"不环比"更危险，所以直接跳过不匹配。

    日期来源优先级：run_config.yaml 的 period.date_start/date_end/period_type
                  → 目录名里的 \\d{8}-\\d{8} 正则（run_config.yaml缺失/字段不全时的fallback）
    """
    cfg = _read_period_from_config(proj)
    if cfg is not None:
        start, end, period_type = cfg
    else:
        m = re.search(r"(\d{8})-(\d{8})", proj.name)
        if not m:
            return None
        start = date.fromisoformat(m.group(1)[:4] + "-" + m.group(1)[4:6] + "-" + m.group(1)[6:])
        end = date.fromisoformat(m.group(2)[:4] + "-" + m.group(2)[4:6] + "-" + m.group(2)[6:])
        period_type = "weekly"  # 目录名正则场景没有period_type来源，只在明确是weekly时才启用过，维持原行为

    if period_type != "weekly":
        return None

    prev_start = start - timedelta(days=7)
    prev_end = end - timedelta(days=7)
    prev_range = f"{prev_start:%Y%m%d}-{prev_end:%Y%m%d}"

    candidates = sorted(d for d in proj.parent.glob(f"*{prev_range}*") if d.is_dir())
    return candidates[0] if candidates else None
