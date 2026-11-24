"""Reviewed data-search platform registry; marketplace IDs are never site IDs."""
from errors import SkillError

# Official task docs 3/43 (2026-10-31), search/getTagSiteList (2026-11-01).
PLATFORMS = {
    'weibo': ('weibo', '101993'),
    'weixin': ('weixin', '102027'),
    'xiaohongshu': ('xiaohongshu', '146510'),
    'news': ('news', None),
    'forum': ('forum', None),
    'douyin': ('shortVideo', '1003583'),
    'kuaishou': ('shortVideo', '1334510'),
    'shipinhao': ('shortVideo', '25047925'),
    'bilibili': ('video', '44'),
    'tencent_video': ('video', '35'),
    'iqiyi': ('video', '47'),
    'zhihu': ('qa', '101944'),
    'baidu_zhidao': ('qa', '102026'),
}
GROUPS = {source: {site: p for p, (s, site) in PLATFORMS.items() if s == source}
          for source in ('shortVideo', 'video', 'qa')}
ACCOUNT_FIELDS = {
    'weibo_uid': ('weibo', 'uid'), 'biz': ('weixin', 'biz'),
    'xhs_acc_id': ('xiaohongshu', 'xhsAccId'), 'sv_acc_id': ('douyin', 'svAccId'),
    'kuaishou_uid': ('kuaishou', 'kuaiShouAccId'),
    'shipinhao_uid': ('shipinhao', 'wechatVideoUid'),
    'bili_acc_id': ('bilibili', 'biliAccId'), 'zhihu_uid': ('zhihu', 'zhihuAccId'),
}
POST_TYPES = {
    'douyin': ('douyinPostType', {'video#0', 'image#0', 'video#1', 'image#1'}),
    'kuaishou': ('kuaishouPostType', {'video#0', 'image#0'}),
    'bilibili': ('bilibiliPostType', {'video', 'dynamic', 'article'}),
    'zhihu': ('zhihuPostType', {'questions', 'articles', 'pins'}),
}

def source_of(platform):
    return PLATFORMS[platform][0]

def site_of(platform):
    return PLATFORMS[platform][1] if source_of(platform) in GROUPS else None

def task_fields(platform):
    fields = {'content', 'title'}
    if platform in ('weibo', 'xiaohongshu', 'news', 'forum', 'douyin', 'kuaishou', 'shipinhao', 'bilibili'):
        fields.add('cover_ocr_content')
    if platform in ('weibo', 'xiaohongshu', 'douyin', 'kuaishou'):
        fields |= {'audio_asr_content', 'video_highlight_content', 'video_content'}
    if platform == 'weibo':
        fields |= {'src_content', 'src_cover_content', 'src_audio_content', 'src_highlight_content', 'src_video_content'}
    if platform == 'bilibili':
        fields |= {'src_title', 'src_content'}
    if platform == 'douyin':
        fields.add('commodity_title')
    return fields

def count_scope(platform):
    if platform in ('weibo', 'news', 'forum'):
        return dict(dataType={'weibo': '微博', 'news': '新闻', 'forum': '论坛'}[platform])
    if platform not in PLATFORMS:
        raise SkillError('PLATFORM_COUNT_SCOPE_UNSUPPORTED')
    return dict(siteId=PLATFORMS[platform][1])
