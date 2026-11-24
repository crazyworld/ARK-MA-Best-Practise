"""One normalized query compiles into both count and task requests (contract v1)."""
from errors import SkillError
import datetime as dt
import re
import calendar
from expression_contract import validate_for_provider
from platform_contract import GROUPS, ACCOUNT_FIELDS, POST_TYPES, source_of, site_of, task_fields
CN = dt.timezone(dt.timedelta(hours=8))

def require(ok, code):
    if not ok: raise SkillError(code)

def normalize_filters(search):
    """Missing means unrestricted; an explicit zero is a real filter, not a default."""
    value = search.get('filters', {})
    require(isinstance(value, dict), 'FILTERS_MAPPING_REQUIRED')
    require(set(value) <= {'ad', 'interaction_min', 'interaction_max'}, 'UNSUPPORTED_FILTER_FIELD')
    ad = value.get('ad', 'all')
    require(isinstance(ad, str) and ad in ('all', 'exclude', 'only'), 'INVALID_AD_FILTER')
    low, high = value.get('interaction_min'), value.get('interaction_max')
    for bound in (low, high):
        require(bound is None or type(bound) is int and bound >= 0, 'INVALID_INTERACTION_BOUND')
    require(low is None or high is None or low <= high, 'INTERACTION_RANGE_REVERSED')
    return dict(ad=ad, interaction_min=low, interaction_max=high)


def resolved_filters(query):
    """Read both the default v1 canonical receipt and new optional filter fields."""
    low, high = query.get('interaction_range', [None, None])
    return dict(ad=query.get('ad_filter', 'exclude' if query.get('filter_ad') else 'all'),
                interaction_min=low, interaction_max=high)


def has_filters(query):
    return resolved_filters(query) != dict(ad='all', interaction_min=None, interaction_max=None)


def normalize(cfg):
    """Map only demonstrably equivalent conditions; never silently drop a filter."""
    s, p = cfg["search"], cfg["profiler"]
    require(set(s) <= {"start_date", "end_date", "source", "is_fast", "sentiments", "keyword_fields", "weibo", "weixin", "short_video", "xiaohongshu", "forum", "news", "video", "qa", "filters"}, "UNSUPPORTED_SEARCH_FIELD")
    filters = normalize_filters(s)
    account_fields=ACCOUNT_FIELDS
    require(set(p) <= {"name", "keywords", "filterwords", *ACCOUNT_FIELDS}, "UNSUPPORTED_PROFILER_FIELD")
    accounts={}
    for key,(platform,wire) in account_fields.items():
        value=p.get(key,'')
        require(isinstance(value,str),'ACCOUNT_IDS_STRING_REQUIRED')
        if value:
            require(all(x and x==x.strip() for x in value.split(',')),'ACCOUNT_IDS_INVALID')
            accounts[platform]=(wire,value)
    require(isinstance(p.get("keywords"), str) and bool(p["keywords"].strip() or accounts) and bool(p.get("name")), "KEYWORD_AND_NAME_REQUIRED")
    for field in ('keywords','filterwords'):
        try:validate_for_provider(p.get(field,''),allow_empty=field=='filterwords' or bool(accounts),limit=200)
        except SkillError as exc:
            exc.detail['parameter']='profiler.'+field
            raise
    mode=s.get('is_fast',0)
    require(type(mode) in (int,str) and str(mode) in ('0','1'),'TASK_COLLECTION_MODE_INVALID')
    sentiments=s.get('sentiments','0,1,-1');fields=s.get('keyword_fields','content,title')
    require(isinstance(sentiments,str) and bool(sentiments) and set(sentiments.split(','))<={'0','1','-1'} and len(sentiments.split(','))==len(set(sentiments.split(','))),'TASK_SENTIMENTS_INVALID')
    allowed_fields={'content','title','audio_asr_content','cover_ocr_content','video_highlight_content','video_content','commodity_title','src_title','src_content','src_cover_content','src_audio_content','src_highlight_content','src_video_content'}
    require(isinstance(fields,str) and bool(fields) and set(fields.split(','))<=allowed_fields and len(fields.split(','))==len(set(fields.split(','))),'TASK_MATCH_FIELDS_INVALID')
    dates = []
    for name in ("start_date", "end_date"):
        value = s[name]
        require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "FULL_DATES_REQUIRED")
        value = dt.datetime.strptime(value, "%Y-%m-%d")
        dates.append(value.date())
    require(dates[0] <= dates[1] < dt.datetime.now(CN).date(), "HISTORICAL_CLOSED_DATE_RANGE_REQUIRED")
    require(dates[1] < add_months(dates[0],12), 'TASK_DATE_RANGE_EXCEEDS_ONE_YEAR')
    require(isinstance(s['source'],str),'INVALID_SOURCE_LIST')
    sources = s["source"].split(",")
    require(len(set(sources)) == len(sources) and all(x == x.strip() and x for x in sources), "INVALID_SOURCE_LIST")
    queries = []
    for source in sources:
        require(source not in ('sohu_video','sohuVideo'),'SOHU_MARKET_CONTRACT_REQUIRED')
        require(source in {"weibo", "weixin", "xiaohongshu", "news", "forum", *GROUPS}, "PLATFORM_CONTRACT_NOT_ALIGNED")
        section = s.get("short_video" if source == "shortVideo" else source, {})
        require(set(section) <= {"site", "msg_type", "post_types"}, "UNSUPPORTED_PLATFORM_FIELD")
        if 'post_types' in section:require(isinstance(section['post_types'],(str,dict)),'TASK_POST_TYPES_INVALID')
        msg = section.get("msg_type", 1 if source in ("news","weixin") else 0)
        require(type(msg) is int and msg in (0, 1, 2), "INVALID_MESSAGE_TYPE")
        require(source!='weixin' or msg==1,'WEIXIN_ARTICLES_ONLY')
        if source in GROUPS:
            require(isinstance(section.get('site',''),str),'TASK_SITE_INVALID')
            sites = section.get("site", "").split(",")
            code='SHORTVIDEO_SITE_OR_POSTTYPES_NOT_ALIGNED' if source=='shortVideo' else 'PLATFORM_SITE_NOT_SUPPORTED'
            require(bool(sites) and len(set(sites)) == len(sites) and set(sites) <= set(GROUPS[source]), code)
            platforms = [GROUPS[source][x] for x in sites]
        else:
            require(not section.get('site') or source in ('news','forum'), 'TASK_SITE_NOT_SUPPORTED')
            if section.get('site'):require(isinstance(section['site'],str) and all(x.isdigit() for x in section['site'].split(',')),'TASK_SITE_INVALID')
            platforms = [source]
        post_types=section.get('post_types','')
        if isinstance(post_types,dict):
            require(set(post_types)<=set(platforms),'POST_TYPES_PLATFORM_NOT_SELECTED')
        for platform in platforms:
            require(set(fields.split(','))<=task_fields(platform),'TASK_FIELD_NOT_SUPPORTED_FOR_PLATFORM')
            require('commodity_title' not in fields.split(',') or msg==1,'COMMODITY_TITLE_REQUIRES_MAIN_POSTS')
            query = dict(keyword=p["keywords"], filterword=p.get("filterwords", ""), platform=platform,
                start_date=s["start_date"], end_date=s["end_date"], msg_type=str(msg), content_mode="ALL",
                filter_ad=filters['ad']=='exclude', keyword_fields="content,title", sentiments=["0", "1", "-1"], timezone="Asia/Shanghai")
            query['keyword_fields']=fields
            query['sentiments']=[x for x in ('0','1','-1') if x in sentiments.split(',')]
            if str(mode)!='0':query['collection_mode']=str(mode)
            if source in ('news','forum') and section.get('site'):query['site_ids']=section['site']
            if platform in accounts:query['account_filter']=dict([accounts[platform]])
            value=post_types.get(platform,'') if isinstance(post_types,dict) else post_types
            require(isinstance(value,str),'TASK_POST_TYPES_INVALID')
            if value:
                require(platform in POST_TYPES and set(value.split(','))<=POST_TYPES[platform][1] and len(value.split(','))==len(set(value.split(','))),'TASK_POST_TYPES_INVALID')
                query['post_types']=value
            # Preserve default receipts exactly; non-default filters are fingerprinted.
            if filters['ad'] != 'all': query['ad_filter'] = filters['ad']
            if filters['interaction_min'] is not None or filters['interaction_max'] is not None:
                query['interaction_range'] = [filters['interaction_min'], filters['interaction_max']]
            queries.append(query)
    require(set(accounts)<={q['platform'] for q in queries},'ACCOUNT_PLATFORM_NOT_SELECTED')
    require(all(q['keyword'] or q.get('account_filter') for q in queries),'KEYWORD_OR_ACCOUNT_REQUIRED_PER_PLATFORM')
    return sorted(queries, key=lambda q: q["platform"])



def timestamp(value, end=False):
    day = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CN)
    if end: day = day.replace(hour=23, minute=59, second=59)
    return int(day.timestamp()*1000)


def add_months(day, months):
    """Calendar arithmetic, clamp a missing month-end day (also handles leap years)."""
    index=day.year*12+day.month-1+months
    year,month=divmod(index,12);month+=1
    return dt.date(year,month,min(day.day,calendar.monthrange(year,month)[1]))


def validate_lookback(queries, today=None):
    today=today or dt.datetime.now(CN).date()
    earliest=add_months(today,-24)
    for q in queries:
        require(dt.date.fromisoformat(q['start_date'])>=earliest, 'STAT_DATE_OUTSIDE_TWO_YEAR_WINDOW')


def task_payload(queries, name):
    require(bool(queries), "EMPTY_QUERY")
    first = queries[0]
    common = ("keyword","filterword","start_date","end_date","sentiments","keyword_fields","content_mode","filter_ad","timezone")
    require(all(all(q[k]==first[k] for k in common) for q in queries), "TASK_QUERY_SCOPE_CONFLICT")
    filters = resolved_filters(first)
    require(all(resolved_filters(q)==filters for q in queries), 'TASK_FILTER_SCOPE_CONFLICT')
    sc = dict(isFast="0", startTime=timestamp(first["start_date"]), endTime=timestamp(first["end_date"],True),
              sentiments=",".join(first["sentiments"]), keywordFields=first["keyword_fields"],
              source=",".join(sorted({source_of(q["platform"]) for q in queries})))
    require(all(q.get('collection_mode','0')==first.get('collection_mode','0') for q in queries),'TASK_QUERY_SCOPE_CONFLICT')
    sc['isFast']=first.get('collection_mode','0')
    for q in queries:
        source=source_of(q["platform"])
        key=source+"MsgType"
        require(key not in sc or sc[key]==int(q["msg_type"]), "PLATFORM_MESSAGE_TYPE_CONFLICT")
        if source!='weixin':sc[key]=int(q["msg_type"])
        if q.get('site_ids'):sc[source+'Site']=q['site_ids']
        if q.get('post_types'):sc[POST_TYPES[q['platform']][0]]=q['post_types']
    for group in GROUPS:
        sites=sorted(site_of(q['platform']) for q in queries if source_of(q['platform'])==group)
        if sites:sc[group+'Site']=','.join(sites)
    sources = sorted({source_of(q['platform']) for q in queries})
    if filters['ad'] != 'all':
        sc['isAdMap'] = {source: 0 if filters['ad']=='exclude' else 1 for source in sources}
    low, high = filters['interaction_min'], filters['interaction_max']
    if low is not None or high is not None:
        interval = f"{'' if low is None else low}-{'' if high is None else high}"
        sc['interactionRangeMap'] = {source: interval for source in sources}
    profiler=dict(name=name,keywords=first["keyword"])
    for q in queries:profiler.update(q.get('account_filter',{}))
    if first["filterword"]: profiler["filterwords"]=first["filterword"]
    return dict(searchCondition=sc,profilers=[profiler])


def volume_requests(queries):
    """Calendar ranges [start, start+3 months); wire precision is whole seconds."""
    validate_lookback(queries)
    requests=[]
    for q in queries:
        start=dt.date.fromisoformat(q["start_date"]); last=dt.date.fromisoformat(q["end_date"])
        while start<=last:
            end=min(last,add_months(start,3)-dt.timedelta(days=1));source=source_of(q["platform"])
            extra=dict(matchFields=q["keyword_fields"],msgType=q["msg_type"])
            site=site_of(q["platform"])
            if site: extra["siteIds"]=site
            if q.get('post_types') and q['platform'] in ('douyin','bilibili','zhihu'):extra['postTypes']=q['post_types']
            body=dict(keyword=q["keyword"],filterword=q["filterword"],sources=[source],sentiments=q["sentiments"],
                      startTime=str(timestamp(start.isoformat())),endTime=str(timestamp(end.isoformat(),True)),extra_condition={source:extra})
            filters = resolved_filters(q)
            terms = {}
            if filters['ad'] != 'all': terms['is_ad'] = ['0' if filters['ad']=='exclude' else '1']
            low, high = filters['interaction_min'], filters['interaction_max']
            if low is not None or high is not None: terms['interaction_cnt'] = [low, high]
            if terms: body['customConditions'] = [dict(sources=source, tConditions=[terms])]
            requests.append(dict(platform=q["platform"],body=body))
            start=end+dt.timedelta(days=1)
    return requests
