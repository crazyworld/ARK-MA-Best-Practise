"""平台标签唯一事实源（FIX-17）。

原版依据：
  - KS_Data_Processing.md:44-55「站点名称 → 平台标签」映射表（9 类标准平台）
  - KS_Exception_Handling.md:139-145「数据类型/站点出现映射表外的新值」：
    打印样本 → 归「其他」→ 纳入声量但不单独出饼图 → 超 10% 告知用户
  - KS_Metadata_Structure.md:58「无法确定的平台记录异常，不默认归抖音」

设计原则：
  - 平台标签是**中文标准名**（与客户报告用词一致），SSJH 平台键单独维护。
  - 映射不靠模糊包含匹配：客户明确禁止「仅凭模糊命中」把短视频默认归抖音。
  - 每个模块引用本文件，不允许各自维护局部映射表。
"""
from __future__ import annotations

# 标准平台中文名 → SSJH 平台键（取数用）。顺序即卡片展示顺序。
CANON: dict[str, str] = {
    "小红书": "xiaohongshu",
    "抖音": "douyin",
    "快手": "kuaishou",
    "视频号": "weixin_channels",
    "微博": "weibo",
    "微信": "weixin",
    "知乎": "zhihu",
    "B站": "bilibili",
    "新闻": "news",
    "论坛": "forum",
    "电商": "ecom",
}

# 归入「其他」的平台标签（计入声量，不出单独饼图）。
OTHER = "其他"
# 「其他」的内部 slug（行内 _platform 字段统一用 slug，展示层再转中文）。
OTHER_SLUG = "other"
SLUG_TO_CANON = {v: k for k, v in CANON.items()}

# 站点名/别名 → 标准平台标签。
# 客户 KS 只列到「新闻（已核实为新闻站点）」这一级，具体媒体名（UC大鱼号、
# 今日头条、网易新闻…）属新闻站点，在此显式登记，避免落到「其他」。
ALIASES: dict[str, str] = {
    # 小红书
    "小红书": "小红书",
    "xiaohongshu": "小红书",
    "电商笔记": "小红书",
    "xhs": "小红书",
    # 抖音
    "抖音": "抖音",
    "douyin": "抖音",
    "抖音短视频": "抖音",
    # 快手
    "快手": "快手",
    "kuaishou": "快手",
    "快手短视频": "快手",
    # 视频号
    "视频号": "视频号",
    "微信视频号": "视频号",
    "weixin_channels": "视频号",
    "channels": "视频号",
    # 微博
    "微博": "微博",
    "weibo": "微博",
    "新浪微博": "微博",
    # 微信
    "微信": "微信",
    "微信公众号": "微信",
    "weixin": "微信",
    "wechat": "微信",
    "公众号": "微信",
    # 知乎
    "知乎": "知乎",
    "zhihu": "知乎",
    # B站
    "B站": "B站",
    "哔哩哔哩": "B站",
    "bilibili": "B站",
    "b站": "B站",
    # 新闻（含具体媒体名）
    "新闻": "新闻",
    "news": "新闻",
    "UC大鱼号": "新闻",
    "大鱼号": "新闻",
    "今日头条": "新闻",
    "头条": "新闻",
    "网易新闻": "新闻",
    "腾讯新闻": "新闻",
    "新浪新闻": "新闻",
    "搜狐": "新闻",
    "搜狐新闻": "新闻",
    "凤凰网": "新闻",
    "澎湃新闻": "新闻",
    "人民网": "新闻",
    "新华网": "新闻",
    "中国新闻网": "新闻",
    "界面新闻": "新闻",
    "第一财经": "新闻",
    "36氪": "新闻",
    "虎嗅": "新闻",
    "钛媒体": "新闻",
    # 论坛
    "论坛": "论坛",
    "forum": "论坛",
    "贴吧": "论坛",
    "百度贴吧": "论坛",
    "豆瓣": "论坛",
    "虎扑": "论坛",
    "知乎论坛": "论坛",
    # 电商
    "电商": "电商",
    "ecom": "电商",
    "淘宝": "电商",
    "天猫": "电商",
    "京东": "电商",
    "拼多多": "电商",
    "抖音商城": "电商",
}

# 展示顺序（报告与卡片统一）
ORDER: tuple[str, ...] = tuple(CANON)

# 未知站点占比超过该比例时提示用户（原版：> 10% 告知用户）。
UNKNOWN_SITE_ALERT_RATIO = 0.10


def normalize(site_name: str) -> tuple[str, bool]:
    """站点名 → (标准平台标签, 是否已核实映射)。

    未命中返回 (OTHER, False)。调用方必须把 False 的情况记入 unknown_sites，
    不得按模糊包含关系就近归类（原版明确禁止默认归抖音）。
    """
    text = str(site_name or "").strip()
    if not text:
        return OTHER, False
    hit = ALIASES.get(text)
    if hit:
        return hit, True
    lowered = text.lower()
    if lowered in ALIASES:
        return ALIASES[lowered], True
    return OTHER, False


def to_slug(canon: str) -> str:
    """标准平台标签 / slug / 别名 → 内部 slug；未知返回 other。"""
    text = str(canon).strip()
    if text in CANON:
        return CANON[text]
    if text in SLUG_TO_CANON:
        return text
    lowered = text.lower()
    alias = ALIASES.get(text)
    if alias is None:
        alias = ALIASES.get(lowered)
    if alias is not None:
        return CANON.get(alias, OTHER_SLUG)
    return OTHER_SLUG


def normalize_slug(site_name: str) -> tuple[str, bool]:
    """站点名 → (内部 slug, 是否已核实映射)。

    行内 `_platform` 一律用 slug（下游 platform_dist / 词云 / 标签抽取都按 slug 聚合），
    未映射的站点落到 other，不再把原始站点名当平台。
    """
    canon, matched = normalize(site_name)
    return to_slug(canon), matched


def display(value: str) -> str:
    """slug 或中文名 → 展示用标准中文名；未知原样返回。"""
    text = str(value or "").strip()
    if text in CANON:
        return text
    if text == OTHER_SLUG:
        return OTHER
    if text in SLUG_TO_CANON:
        return SLUG_TO_CANON[text]
    hit = ALIASES.get(text)
    return hit if hit else text


def to_ssjh_key(canon: str) -> str | None:
    """标准平台标签 → SSJH 平台键；「其他」返回 None（不可检索）。"""
    return CANON.get(canon)


def ssjh_keys(canon_list) -> list[str]:
    """平台列表（中文标准名 / slug / 别名）→ SSJH 平台键列表。

    丢弃「其他」与无法映射的值——SSJH 侧无法检索「其他」。
    """
    out: list[str] = []
    for item in canon_list or []:
        text = str(item).strip()
        key = CANON.get(text)
        if key is None:
            canon = from_ssjh_key(text)
            key = CANON.get(canon) if canon else None
        if key and key not in out:
            out.append(key)
    return out


def from_ssjh_key(key: str) -> str | None:
    """SSJH 平台键 → 标准中文名（兼容旧 Run 里的 slug）。"""
    text = str(key or "").strip()
    if text in CANON:
        return text
    for canon, ssjh_key in CANON.items():
        if ssjh_key == text:
            return canon
    hit = ALIASES.get(text)
    return hit if hit else None


def unknown_ratio(unknown_count: int, total: int) -> float:
    return (unknown_count / total) if total else 0.0


def should_alert_unknown(unknown_count: int, total: int) -> bool:
    """未知站点是否达到需要告知用户的阈值。"""
    return unknown_ratio(unknown_count, total) > UNKNOWN_SITE_ALERT_RATIO
