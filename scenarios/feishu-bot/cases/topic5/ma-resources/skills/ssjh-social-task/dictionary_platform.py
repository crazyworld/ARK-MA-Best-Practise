"""Conservative attribution for field audits, independent of quantity acceptance."""
from platform_contract import PLATFORMS

DATA_TYPES = {
    'weibo': '微博', 'weixin': '微信', 'xiaohongshu': '电商笔记',
    'news': '新闻', 'forum': '论坛', 'douyin': '短视频', 'kuaishou': '短视频',
    'shipinhao': '短视频', 'bilibili': '视频', 'tencent_video': '视频',
    'iqiyi': '视频', 'zhihu': '问答', 'baidu_zhidao': '问答',
}
SITES = {site: platform for platform, (_, site) in PLATFORMS.items() if site is not None}
UNIQUE_TYPES = {DATA_TYPES[p]: p for p in ('weibo','weixin','xiaohongshu','news','forum')}


def identify(row):
    """Never infer platform from URL, display name, query order or metric fields."""
    raw_site = row.get('site_id')
    site = str(raw_site) if type(raw_site) in (int, str) else None
    data_type = row.get('data_type')
    platform = SITES.get(site)
    if platform:
        if data_type not in (None, '', DATA_TYPES[platform]):
            return 'unknown', 'PLATFORM_IDENTIFIERS_CONFLICT'
        return platform, 'registered_site_id'
    if isinstance(data_type, str) and data_type in UNIQUE_TYPES:
        platform = UNIQUE_TYPES[data_type]
        if site and platform not in ('news','forum'):
            return 'unknown', 'PLATFORM_IDENTIFIERS_CONFLICT'
        return platform, 'unique_data_type'
    return 'unknown', 'PLATFORM_UNRESOLVED'
