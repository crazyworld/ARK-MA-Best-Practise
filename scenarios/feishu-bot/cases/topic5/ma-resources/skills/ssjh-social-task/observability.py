"""Local telemetry and notification outbox. No network or message transport.

Authoritative records live with state.json in the same atomic write; logs are
regenerable views. Acknowledgements are caller reports, not delivery proofs.
"""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import uuid
import durable
from errors import SkillError
from public_output import VERSION, envelope, public_data
from query_contract import resolved_filters

VISIBLE = {'AWAITING_QUANTITY_CONFIRMATION','SUPPLIER_RUNNING','SUPPLIER_COMPLETED',
           'READY_FOR_DOWNLOAD','DOWNLOADING','VALIDATING','EXPORTING','COMPLETED',
           'PRECHECK_FAILED','BUDGET_BLOCKED','CREATE_OUTCOME_UNKNOWN','WAIT_TIMEOUT_RESUMABLE',
           'DOWNLOAD_FAILED_RESUMABLE','EXPORT_FAILED_RESUMABLE','DATA_VALIDATION_FAILED',
           'SUPPLIER_FAILED','TASK_COUNT_EXCEEDS_LIMIT','QUANTITY_REVIEW_REQUIRED'}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def epoch(value):
    if not value:return None
    try:return dt.datetime.fromisoformat(value).timestamp()
    except (TypeError,ValueError):return None

def duration(start,end):
    if start is None or end is None or end<start:return None
    return round(end-start,6)

def settings(cfg, state):
    value=cfg.get('observability',{})
    if not isinstance(value,dict) or set(value)-{'delivery_mode','sample_kind'}:
        raise SkillError('OBSERVABILITY_CONFIG_INVALID')
    mode=value.get('delivery_mode','intermediate')
    kind=value.get('sample_kind','normal')
    if mode not in {'intermediate','user'} or kind not in {'normal','diagnostic','test'}:
        raise SkillError('OBSERVABILITY_CONFIG_INVALID')
    saved=state.get('observability',{})
    if saved and kind!=saved['sample_kind']:
        raise SkillError('TIMING_SAMPLE_KIND_IMMUTABLE')
    return mode,kind

def observe(before, state, now, delivery_verified=False):
    """Update telemetry in memory before workflow persists state atomically."""
    mode=state.get('delivery_mode','intermediate')
    kind=state.get('sample_kind','normal')
    prior=before.get('observability')
    if prior:
        data=copy.deepcopy(prior)
    else:
        data=dict(schema_version=1,run_id=uuid.uuid4().hex,skill_version=VERSION,
            sample_kind=kind,first_observed_at=now,last_observed_at=now,
            phases=[],notifications=[],deliveries=[],sequence=0,creation_observed=False,
            legacy_recovery=bool(before.get('create_intent') or before.get('job_id')),
            clock_anomaly=False,error_codes=[],delay_notified=False,observation_gaps=[])
    data.setdefault('origin_skill_version',data.get('skill_version'))
    data['skill_version']=VERSION
    data['delivery_mode']=mode
    at=epoch(now);last=epoch(data['last_observed_at'])
    if at is not None and last is not None and at<last and not data.get('completed_at'):data['clock_anomaly']=True
    if at is not None and last is not None and at-last>300 and data.get('creation_observed') and not data.get('completed_at'):
        data['observation_gaps'].append(dict(from_at=data['last_observed_at'],to_at=now,seconds=round(at-last,6)))
    data['last_observed_at']=now
    stage=state.get('state','NOT_STARTED')
    changed=not data['phases'] or data['phases'][-1]['state']!=stage
    if changed:
        if data['phases']:
            phase=data['phases'][-1];phase['exited_at']=now
            phase['wall_seconds']=duration(epoch(phase['entered_at']),at)
        data['phases'].append(dict(state=stage,entered_at=now,exited_at=None,wall_seconds=None))
    if not before.get('create_intent') and state.get('create_intent'):
        data['creation_observed']=True
        data['creation_started_epoch']=state.get('create_started_epoch')
    if state.get('job_id') and not data.get('creation_confirmed_at'):
        # In legacy recovery this is a local bind time, not remote creation time.
        data['creation_confirmed_at']=now
    if stage in {'READY_FOR_DOWNLOAD','SUPPLIER_COMPLETED'}:
        data.setdefault('ready_observed_at',now)
    error=(state.get('error_detail') or {}).get('code')
    previous_error=(before.get('error_detail') or {}).get('code')
    if error and error not in data['error_codes'] and not data.get('completed_at'):data['error_codes'].append(error)
    if stage=='COMPLETED' and not error and delivery_verified:
        data.setdefault('completed_at',now)
    result=envelope(state,'event')
    if result['data'].get('eta') and data.get('creation_observed'):
        data.setdefault('initial_estimate',dict(result['data']['eta'],generated_at=now))
    creation=data.get('creation_started_epoch')
    delayed=(stage in {'SUPPLIER_RUNNING','VERIFYING_DATA_READY','WAIT_TIMEOUT_RESUMABLE'}
        and not error and result['data'].get('eta') and creation is not None
        and at is not None and at-creation>result['data']['eta']['max_seconds'] and not data['delay_notified'])
    # Only meaningful changes, never identical polling or download page counts.
    event_kind=('error' if error and error!=previous_error else 'delay' if delayed else
                'recovered' if previous_error and not error and stage in VISIBLE else
                'stage' if changed and stage in VISIBLE else None)
    if stage=='COMPLETED' and not error:
        event_kind=('recovered' if delivery_verified and previous_error else
                    'stage' if delivery_verified and not (prior or {}).get('completion_notified') else None)
        if event_kind:data['completion_notified']=True
    if event_kind:
        if delayed:data['delay_notified']=True
        data['sequence']+=1
        for item in data['notifications']:
            if item['status']=='pending':item['status']='superseded';item['superseded_at']=now
        notice=dict(event_id=fingerprint([data['run_id'],data['sequence']]),sequence=data['sequence'],
            kind=event_kind,state=stage,created_at=now,status='pending',
            audience='workflow' if mode=='intermediate' else 'user',
            message='本次取数耗时已超出参考范围，将继续核对原任务状态。' if event_kind=='delay' else result['message'],
            code=error,next_action=result['next_action'],job_id=state.get('job_id'),
            request_id=state.get('request_id'),data=public_data(state),acknowledged_at=None,
            acknowledgement_ref_hash=None)
        data['notifications'].append(notice)
    state['observability']=data
    return data

def timing(state):
    data=state.get('observability') or {}
    contract=state.get('contract',{});queries=contract.get('canonical_query',[])
    start=data.get('creation_started_epoch');end=epoch(data.get('completed_at'))
    reasons=[]
    if data.get('sample_kind')!='normal':reasons.append('NON_NORMAL_SAMPLE')
    if not data.get('creation_observed'):reasons.append('CREATION_NOT_OBSERVED')
    if data.get('legacy_recovery'):reasons.append('LEGACY_RECOVERY')
    if not data.get('completed_at'):reasons.append('ACQUISITION_NOT_COMPLETED')
    if data.get('clock_anomaly') or duration(start,end) is None:reasons.append('INVALID_OR_MISSING_CLOCK_INTERVAL')
    if data.get('error_codes'):reasons.append('ERROR_OR_RECOVERY_OBSERVED')
    if data.get('observation_gaps'):reasons.append('OBSERVATION_GAP_OVER_300_SECONDS')
    identity=[contract.get('provider'),state.get('request_id'),state.get('job_id')]
    return dict(schema_version=1,skill_version=VERSION,origin_skill_version=data.get('origin_skill_version',data.get('skill_version')),
        sample_id=fingerprint(identity) if state.get('job_id') else None,
        run_id=data.get('run_id'),request_id=state.get('request_id'),job_id=state.get('job_id'),
        query_fingerprint=fingerprint(queries),provider_fingerprint=fingerprint(contract.get('provider')),
        sample_kind=data.get('sample_kind'),platforms=sorted(set(q['platform'] for q in queries)),
        query_filters=[dict(platform=q['platform'], **resolved_filters(q)) for q in queries],
        query_windows=[{k:q[k] for k in ('platform','start_date','end_date','timezone','msg_type') if k in q} for q in queries],
        estimated_volume=state.get('estimated_volume'),task_count=state.get('task_count'),downloaded=state.get('downloaded'),
        volume_comparison=public_data(state).get('volume_comparison'),estimate=public_data(state).get('estimate'),
        final_state=state.get('state','NOT_STARTED'),first_observed_at=data.get('first_observed_at'),
        last_observed_at=data.get('last_observed_at'),creation_started_epoch=start,
        creation_confirmed_at=data.get('creation_confirmed_at'),ready_observed_at=data.get('ready_observed_at'),
        completed_at=data.get('completed_at'),phases=data.get('phases',[]),
        create_to_ready_observation_seconds=duration(start,epoch(data.get('ready_observed_at'))),
        create_to_verified_delivery_seconds=duration(start,end),
        duration_basis='local_wall_clock_including_polling_and_resume_gaps_not_supplier_processing_time',
        clock_anomaly=data.get('clock_anomaly',False),error_codes=data.get('error_codes',[]),
        observation_gaps=data.get('observation_gaps',[]),initial_estimate=data.get('initial_estimate'),
        initial_estimate_hit=(data['initial_estimate']['min_seconds']<=duration(start,end)<=data['initial_estimate']['max_seconds'])
            if data.get('initial_estimate') and duration(start,end) is not None else None,
        eligible_for_future_estimation=not reasons,exclusion_reasons=reasons,
        estimation_updated=False,cache_and_reexports_are_separate_delivery_records=True)

def record_delivery(state,result,now,operation):
    """Only called after actual export/cache verification succeeds."""
    data=state['observability'];contract=state.get('contract',{});queries=contract.get('canonical_query',[])
    identity=fingerprint([result.get('artifacts'),result.get('quality'),result.get('raw_data')])
    existing=next((x for x in data['deliveries'] if x['delivery_id']==identity),None)
    if existing:
        existing.update(last_executor_version=VERSION,last_verified_at=now,verification_count=existing['verification_count']+1,last_operation=operation)
        return existing
    record=dict(schema_version=1,delivery_id=identity,skill_version=VERSION,request_id=state.get('request_id'),
        job_id=state.get('job_id'),first_verified_at=now,last_verified_at=now,verification_count=1,
        operation=operation,last_operation=operation,audience='workflow' if data['delivery_mode']=='intermediate' else 'user',
        query_fingerprint=fingerprint(queries),query_windows=[{k:q[k] for k in ('platform','start_date','end_date','timezone','msg_type') if k in q} for q in queries],
        input_evidence='precheck.json and state.json contract (same run directory)',
        query_filters=[dict(platform=q['platform'], **resolved_filters(q)) for q in queries],
        outputs=public_data(result),export_verified=True,
        volume_comparison=public_data(state).get('volume_comparison'),estimate=public_data(state).get('estimate'),
        limitations=['Optional interaction metrics reflect supplier coverage; missing is not zero.',
                     'Dictionary is provisional; full official semantics and billing not verified.'])
    data['deliveries'].append(record)
    return record

def materialize(path,state):
    """Regenerate snapshots after a crash from authoritative persisted state."""
    if not state.get('observability'):return
    root=Path(path)/'logs';data=state['observability']
    durable.write_json(root/'timing.json',timing(state))
    durable.write_json(root/'progress.json',dict(schema_version=1,events=data['notifications']))
    durable.write_json(root/'deliveries.json',dict(schema_version=1,records=data['deliveries']))

def pending(state):
    return [copy.deepcopy(x) for x in state.get('observability',{}).get('notifications',[]) if x['status']=='pending']

def acknowledge(state,event_id,reference,now):
    if not isinstance(reference,str) or not reference.strip():raise SkillError('NOTIFICATION_ACK_REFERENCE_REQUIRED')
    record=next((x for x in state.get('observability',{}).get('notifications',[]) if x['event_id']==event_id),None)
    if not record:raise SkillError('NOTIFICATION_NOT_FOUND')
    digest=fingerprint(reference)
    if record['status']=='acknowledged':
        if record['acknowledgement_ref_hash']!=digest:raise SkillError('NOTIFICATION_ACK_CONFLICT')
        return record
    if record['status']!='pending':raise SkillError('NOTIFICATION_NO_LONGER_CURRENT')
    record.update(status='acknowledged',acknowledged_at=now,acknowledgement_ref_hash=digest,
                  delivery_evidence='caller_report_not_independently_verified')
    return record
