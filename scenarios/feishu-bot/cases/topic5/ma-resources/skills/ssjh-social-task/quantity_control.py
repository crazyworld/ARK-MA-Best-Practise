"""Volume estimates tolerate drift; pagination and approved ceilings never do."""
import datetime as dt
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from errors import SkillError

DEFAULT = dict(normal_absolute=20, normal_ratio='0.05', major_absolute=100, major_ratio='0.20')
ORDER = {'PASS':0, 'WARNING':1, 'UNVERIFIED':2, 'BLOCKED':3}


def require(ok, code):
    if not ok:raise SkillError(code,'quantity')


def policy(value=None):
    if value is None:value={}
    require(isinstance(value,dict) and set(value)<=set(DEFAULT),'QUANTITY_POLICY_INVALID')
    result=dict(DEFAULT,**value)
    for key in ('normal_absolute','major_absolute'):
        require(type(result[key]) is int and result[key]>=0,'QUANTITY_POLICY_INVALID')
    for key in ('normal_ratio','major_ratio'):
        try:
            require(type(result[key]) in (int,float,str),'QUANTITY_POLICY_INVALID')
            number=Decimal(str(result[key]))
            require(number.is_finite() and 0<=number<=1,'QUANTITY_POLICY_INVALID')
        except (InvalidOperation,ValueError):raise SkillError('QUANTITY_POLICY_INVALID','quantity') from None
        result[key]=str(number.normalize())
    require(result['normal_absolute']<=result['major_absolute'] and Decimal(result['normal_ratio'])<=Decimal(result['major_ratio']),'QUANTITY_POLICY_INVALID')
    return result


def configured(cfg):
    risk=cfg.get('risk',{})
    require(isinstance(risk,dict),'QUANTITY_POLICY_INVALID')
    if 'volume_deviation' in risk:require(isinstance(risk['volume_deviation'],dict),'QUANTITY_POLICY_INVALID')
    return policy(risk.get('volume_deviation',{}))


def resolve_policy(run,cfg):
    current=configured(cfg)
    saved=run.state.get('quantity_policy')
    # Legacy paid runs adopt defaults; cannot silently choose looser thresholds.
    if saved is None and run.state.get('job_id'):saved=policy({})
    require(saved is None or policy(saved)==current,'QUANTITY_POLICY_CHANGED_FOR_EXISTING_RUN')
    return current


def bind_policy(run,cfg):
    current=resolve_policy(run,cfg)
    if run.state.get('quantity_policy')!=current:run.update(_progress=False,quantity_policy=current)
    return current


def compare(estimated,actual,rules=None,relation='MATCHED'):
    rules=policy(rules)
    require(relation in ('MATCHED','UPPER_BOUND','UNCERTAIN'),'ESTIMATE_RELATION_INVALID')
    require(type(actual) is int and actual>=0,'QUANTITY_ACTUAL_INVALID')
    require(estimated is None or type(estimated) is int and estimated>=0,'QUANTITY_ESTIMATE_INVALID')
    if estimated is None:return dict(status='UNVERIFIED',estimated=None,actual=actual,reason='ESTIMATE_UNAVAILABLE',absolute_difference=None,relative_difference=None)
    delta=actual-estimated;difference=abs(delta)
    ratio=float(Decimal(difference)/estimated) if estimated else None
    if estimated==0:
        status='PASS' if actual==0 else 'BLOCKED' if actual>rules['major_absolute'] else 'WARNING'
        reason='ZERO_BASELINE' if actual else 'BOTH_ZERO'
    elif difference<=rules['normal_absolute'] or Decimal(difference)<=estimated*Decimal(rules['normal_ratio']):
        status='PASS';reason='WITHIN_NORMAL_TOLERANCE'
    elif difference>rules['major_absolute'] and Decimal(difference)>estimated*Decimal(rules['major_ratio']):
        status='BLOCKED';reason='MAJOR_VOLUME_DEVIATION'
    else:status='WARNING';reason='VOLUME_DEVIATION_WARNING'
    if relation=='UPPER_BOUND' and actual<=estimated:status='PASS';reason='WITHIN_CONDITIONAL_UPPER_BOUND'
    if relation=='UNCERTAIN':status='WARNING';reason='ESTIMATE_NOT_COMPARABLE_NO_SHORTFALL_INFERENCE'
    return dict(status=status,estimated=estimated,actual=actual,delta=delta,absolute_difference=difference,
                relative_difference=ratio,reason=reason)


def evaluate(estimated,actual,expected_by_platform=None,actual_by_platform=None,rules=None,relation='MATCHED'):
    rules=policy(rules);total=compare(estimated,actual,rules,relation);rows={};notes=[]
    if expected_by_platform is not None:
        require(isinstance(expected_by_platform,dict),'PLATFORM_ESTIMATES_INVALID')
        require(actual_by_platform is None or isinstance(actual_by_platform,dict),'PLATFORM_COUNTS_INVALID')
        if actual_by_platform is None or set(expected_by_platform)!=set(actual_by_platform):
            notes.append('PLATFORM_COUNTS_UNAVAILABLE')
        else:
            rows={key:compare(n,actual_by_platform[key],rules,relation) for key,n in expected_by_platform.items()}
            if any(n is None for n in expected_by_platform.values()):notes.append('PLATFORM_ESTIMATES_UNAVAILABLE')
            elif sum(expected_by_platform.values())!=estimated:notes.append('PLATFORM_ESTIMATES_DO_NOT_SUM_TO_TOTAL')
            if sum(actual_by_platform.values())!=actual:notes.append('PLATFORM_COUNTS_DO_NOT_SUM_TO_TOTAL')
    grade=max([total['status']]+[row['status'] for row in rows.values()]+(['UNVERIFIED'] if notes else []),key=ORDER.get)
    return dict(schema_version=1,status=grade,policy=rules,total=total,platforms=rows,notes=notes,estimate_relation=relation,
                delivery_allowed=grade in ('PASS','WARNING'),ratio_denominator='precheck_volume',
                task_download_integrity='exact_match_required',root_cause='not_inferred')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def enforce(report):
    if report['delivery_allowed']:return
    code='VOLUME_DEVIATION_MAJOR' if report['status']=='BLOCKED' else 'QUANTITY_COMPARISON_UNVERIFIED'
    error=SkillError(code,'quantity',retryable=False,
        next_action='暂缓正常交付，向上游反馈并核对原任务；不得自动重新创建付费任务。')
    error.detail.update(route_to='upstream_agent',upstream_action='HOLD_DELIVERY_AND_REVIEW_ORIGINAL_TASK')
    raise error


def baselines(run):
    queries=run.state.get('contract',{}).get('canonical_query',[])
    platforms=sorted({q['platform'] for q in queries})
    if len(platforms)<=1:return None
    saved=run.state.get('estimated_by_platform')
    if saved is None:
        path=run.path/'precheck.json'
        if path.is_file():
            require(hashlib.sha256(path.read_bytes()).hexdigest()==run.state.get('precheck_sha256'),'QUANTITY_PRECHECK_HASH_MISMATCH')
            receipt=json.loads(path.read_text(encoding='utf-8-sig'))
            require(receipt.get('query_fingerprint')==fingerprint(run.state['contract']),'QUANTITY_PRECHECK_SCOPE_MISMATCH')
            saved={key:0 for key in platforms}
            for item in receipt['results']:
                require(item['platform'] in saved and type(item['count']) is int and item['count']>=0,'PLATFORM_ESTIMATES_INVALID')
                saved[item['platform']]+=item['count']
    require(saved is None or set(saved)==set(platforms),'PLATFORM_ESTIMATES_INVALID')
    return saved if saved is not None else {key:None for key in platforms}


def count_scope(platform):
    from platform_contract import count_scope as scope
    return scope(platform)


def review(run,actual,api=None,*,refresh=False):
    """Refresh uses existing-job count only. Local cache verification never networks."""
    rules=policy(run.state.get('quantity_policy'));expected=run.state.get('estimated_volume')
    relation=run.state.get('estimate',{}).get('relation','MATCHED')
    expected_map=baselines(run)
    key=dict(query_fingerprint=fingerprint(run.state.get('contract',{})),estimated=expected,actual=actual,
             expected_by_platform=expected_map,policy=rules,estimate_relation=relation,instance=run.state.get('readiness',{}).get('instance'))
    saved=run.state.get('quantity_review');actual_map=None
    if not refresh and saved:
        path=run.path/'quantity-review.json'
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==run.state.get('quantity_review_sha256'),'QUANTITY_REVIEW_HASH_MISMATCH')
        require(json.loads(path.read_text(encoding='utf-8-sig'))==saved,'QUANTITY_REVIEW_STATE_MISMATCH')
        if saved.get('binding')==key:actual_map=saved.get('actual_by_platform')
    missing=expected_map is not None and any(n is None for n in expected_map.values())
    if refresh and expected_map is not None and not missing:
        from transport import call_json
        import requests
        require(api is not None,'QUANTITY_API_REQUIRED')
        actual_map={}
        for platform in expected_map:
            params=dict(jobId=run.state['job_id'],token=api['credentials']['fixed_token'],pLimit=1,**count_scope(platform))
            payload=call_json('count',lambda timeout:requests.get(api['api']['count_base_url'].rstrip('/')+'/count',params=params,timeout=timeout,allow_redirects=False),api,emit=run.transport_event)
            data=payload.get('data');n=data.get('total') if isinstance(data,dict) else None
            require(type(n) is int and n>=0,'PLATFORM_COUNT_INVALID');actual_map[platform]=n
    report=evaluate(expected,actual,None if missing else expected_map,actual_map,rules,relation)
    if missing:
        report['notes'].append('PLATFORM_ESTIMATES_UNAVAILABLE')
        if report['status']!='BLOCKED':report.update(status='UNVERIFIED',delivery_allowed=False)
    report.update(binding=key,actual_by_platform=actual_map,checked_at=dt.datetime.now(dt.timezone.utc).isoformat())
    if api is not None or saved is None:
        import durable
        path=run.path/'quantity-review.json';durable.write_json(path,report)
        run.update(_progress=False,quantity_review=report,quantity_review_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    try:enforce(report)
    except SkillError as exc:
        run.update(state='QUANTITY_REVIEW_REQUIRED',last_error=exc.detail['code'],error_detail=exc.detail)
        raise
    return report
