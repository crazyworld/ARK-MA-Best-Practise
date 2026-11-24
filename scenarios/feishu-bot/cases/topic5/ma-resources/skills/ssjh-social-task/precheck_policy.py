"""Task-first estimation. Never rewrite the task to fit statistics."""
import copy
import hashlib
import json
from itertools import product
from errors import SkillError
from expression_contract import parse

VERSION=2

def require(ok,code):
    if not ok:raise SkillError(code,'estimate',next_action='保留任务条件，向上游提供估算限制和所需依据；不得重建已有任务。')

def settings(cfg):
    value=cfg.get('precheck',{})
    require(isinstance(value,dict) and set(value)<={'uncertainty_acceptance_ref','manual_estimate'},'PRECHECK_CONFIG_INVALID')
    ref=value.get('uncertainty_acceptance_ref','')
    require(isinstance(ref,str),'ESTIMATE_ACCEPTANCE_REFERENCE_INVALID')
    manual=value.get('manual_estimate')
    if manual is not None:
        require(isinstance(manual,dict) and set(manual)=={'counts','reference'},'MANUAL_ESTIMATE_INVALID')
        require(isinstance(manual['reference'],str) and bool(manual['reference'].strip()),'MANUAL_ESTIMATE_REFERENCE_REQUIRED')
        require(isinstance(manual['counts'],dict) and bool(manual['counts']) and all(type(n) is int and n>=0 for n in manual['counts'].values()),'MANUAL_ESTIMATE_COUNTS_INVALID')
    return value

def groups(node):
    if node is None:return []
    kind,value=node
    if kind=='term':return [[value]]
    children=[groups(child) for child in value]
    if kind=='|':return [g for child in children for g in child]
    return [sum(parts,[]) for parts in product(*children)]

def plan(queries,cfg=None):
    from query_contract import volume_requests
    cfg=cfg or {};options=settings(cfg);manual=options.get('manual_estimate')
    platforms=sorted(q['platform'] for q in queries)
    if manual is not None:
        require(set(manual['counts'])==set(platforms),'MANUAL_ESTIMATE_PLATFORM_MISMATCH')
        return dict(version=VERSION,relation='UNCERTAIN',reasons=['UPSTREAM_ESTIMATE_NOT_SUPPLIER_COUNT'],adjustments=[],requests=[],manual=manual)
    requests=[];reasons=[];adjustments=[];unavailable=[]
    for original in queries:
        q=copy.deepcopy(original);platform=q['platform']
        if platform in ('weixin','douyin','bilibili','zhihu','tencent_video','iqiyi','baidu_zhidao'):
            reasons.append(platform+':NEW_PLATFORM_SEMANTICS_UNVERIFIED')
        inc=parse(q['keyword'],allow_empty=True,limit=200);exc=parse(q['filterword'],allow_empty=True,limit=200)
        if q.get('account_filter') or not q['keyword']:
            unavailable.append(platform+':ACCOUNT_STAT_COMBINATION_UNVERIFIED');continue
        # Explicitly scoped fields whose statistics support is documented.
        supported={'content','title','cover_ocr_content'} if platform in ('news','forum') else {'content','title','cover_ocr_content','audio_asr_content','video_highlight_content','video_content'}
        if platform=='weibo':supported|={'src_content','src_cover_content','src_audio_content','src_highlight_content','src_video_content'}
        if platform=='bilibili':supported|={'src_title','src_content'}
        if platform=='douyin':supported.add('commodity_title')
        if set(q['keyword_fields'].split(','))-supported:
            unavailable.append(platform+':STAT_MATCH_FIELDS_UNAVAILABLE');continue
        if platform=='weibo' and 'title' in q['keyword_fields'].split(','):reasons.append(platform+':TITLE_SEMANTICS_UNVERIFIED')
        if '+' in q['keyword']+q['filterword'] and ',' in q['keyword_fields']:reasons.append(platform+':CROSS_FIELD_SEMANTICS_UNVERIFIED')
        if q['msg_type']!='1':reasons.append(platform+':COMMENT_MATCH_SEMANTICS_UNVERIFIED')
        if q.get('collection_mode','0')=='1':reasons.append(platform+':ONLINE_COLLECTION_CHANGES_POPULATION')
        elif platform not in ('weibo','weixin','news','forum'):reasons.append(platform+':STOCK_MODE_PLATFORM_UNVERIFIED')
        if q.get('interaction_range'):reasons.append(platform+':INTERACTION_BOUNDARY_UNVERIFIED')
        if q.get('site_ids'):adjustments.append(platform+':STAT_OMITS_TASK_SITE_FILTER')
        if q.get('post_types') and platform=='kuaishou':
            adjustments.append(platform+':STAT_OMITS_TASK_POST_TYPES');q.pop('post_types')
        if exc['groups']>50:
            q['filterword']='';adjustments.append(platform+':STAT_OMITS_EXCLUSIONS_OVER_50_GROUPS')
        variants=[q]
        if inc['groups']>50:
            dnf=groups(inc['ast']);variants=[]
            for offset in range(0,len(dnf),50):
                part=copy.deepcopy(q);part['keyword']='|'.join('('+'+'.join(g)+')' for g in dnf[offset:offset+50]);variants.append(part)
            adjustments.append(platform+':INCLUSION_BATCH_SUM_MAY_COUNT_OVERLAP')
        try:
            for item in variants:
                compiled=volume_requests([item])
                if len(compiled)>1:reasons.append(platform+':MULTI_WINDOW_SNAPSHOT_AND_PRECISION_UNVERIFIED')
                requests.extend(compiled)
        except SkillError as exc:
            if str(exc)!='STAT_DATE_OUTSIDE_TWO_YEAR_WINDOW':raise
            unavailable.append(platform+':STAT_HISTORY_UNAVAILABLE')
    if unavailable:requests=[];reasons+=unavailable
    return dict(version=VERSION,relation='UNAVAILABLE' if unavailable else 'UNCERTAIN' if reasons else 'UPPER_BOUND' if adjustments else 'MATCHED',
                reasons=sorted(set(reasons)),adjustments=sorted(set(adjustments)),requests=requests,manual=None)

def public(plan):
    return {k:plan[k] for k in ('version','relation','reasons','adjustments')}

def binding(plan):
    return hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def acceptance(cfg,estimate):
    if estimate['relation']!='UNCERTAIN':return None
    ref=settings(cfg).get('uncertainty_acceptance_ref','')
    require(bool(ref.strip()),'ESTIMATE_UNCERTAINTY_ACCEPTANCE_REQUIRED')
    return hashlib.sha256(ref.encode()).hexdigest()
