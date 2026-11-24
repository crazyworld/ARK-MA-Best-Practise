"""Precheck -> explicit quantity approval -> one creation -> status/resume/download.

State and confirmation references are local audit records, not identity authentication.
Only the Agent/user can attest that a confirmation reference is genuine.
"""
import argparse
import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import uuid

import ssjh_social_task as core
import query_contract as query
from errors import SkillError, describe
from transport import call_json
import durable
from request_registry import Registry
from transfer import PageStore, export_artifacts
from data_validation import validate_records, load_dictionary
from budget import Budget
from action_policy import guard, ACTIVE
from public_output import VERSION, envelope, emit, public_data
from cache_control import reuse_or_export
import observability
import quantity_control as quantity
import precheck_policy as estimation

HERE = pathlib.Path(__file__).resolve().parent
CN = dt.timezone(dt.timedelta(hours=8))


def require(condition, code):
    if not condition:
        raise SkillError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def read(path):
    value = json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), "STATE_MAPPING_REQUIRED")
    return value


def write(path, value):
    durable.write_json(path,value)


class Run:
    def __init__(self, path, registry=None, create=True):
        self.path = pathlib.Path(path).resolve()
        if create: self.path.mkdir(parents=True, exist_ok=True)
        self.registry = pathlib.Path(registry).expanduser().resolve() if registry else pathlib.Path.home()/".ssjh-social-task"/"requests.sqlite3"
        self.file = self.path / "state.json"
        self.state = read(self.file) if self.file.exists() else {}
        if self.state.get("registry_path"):
            saved = pathlib.Path(self.state["registry_path"]).resolve()
            require(not registry or self.registry == saved,"RUN_REGISTRY_PATH_MISMATCH")
            self.registry = saved

    @contextlib.contextmanager
    def lock(self, record_errors=True):
        lock = self.path / "run.lock"
        try:
            with lock.open("x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except FileExistsError:
            raise SkillError("RUN_LOCKED_VERIFY_OWNER_BEFORE_RECOVERY") from None
        try:
            self.state = read(self.file) if self.file.exists() else {}
            observability.materialize(self.path,self.state)
            yield
        except Exception as exc:
            if not record_errors:raise
            detail = describe(exc)
            state = self.state.get("state", "NOT_STARTED")
            state = {"DOWNLOADING": "DOWNLOAD_FAILED_RESUMABLE", "VALIDATING": "DATA_VALIDATION_FAILED", "EXPORTING": "EXPORT_FAILED_RESUMABLE", "PRECHECK_RUNNING": "PRECHECK_FAILED"}.get(state, state)
            if detail['category'] == 'budget':
                state = 'BUDGET_BLOCKED'
            try: self.update(_progress=False, state=state, last_error=detail["code"], error_detail=detail)
            except Exception: pass  # Preserve the original failure if recording also fails.
            raise
        finally:
            try: lock.unlink()
            except OSError: pass

    def update(self, **values):
        before=copy.deepcopy(self.state)
        delivery=values.pop('_delivery',None)
        operation=values.pop('_delivery_operation','acquisition')
        progress=values.pop('_progress',True)
        now=dt.datetime.now(CN).isoformat(timespec="microseconds")
        # Only stage/count changes represent progress; logging and identical polls do not.
        progress_keys=("state","job_id","task_count","downloaded","job_status","subtask_status")
        if progress and any(k in values and values[k]!=self.state.get(k) for k in progress_keys):
            values['last_progress_at']=now
        if 'last_error' in values and values['last_error'] is None: values['error_detail']=None
        if values.get('state',self.state.get('state'))!=self.state.get('state') or delivery is not None:
            values.setdefault('transport_event',None)
        self.state.update(values, updated_at=now)
        observability.observe(before,self.state,now,delivery_verified=delivery is not None)
        if delivery is not None:observability.record_delivery(self.state,delivery,now,operation)
        write(self.file, self.state)
        observability.materialize(self.path,self.state)
        event = envelope(self.state, 'status' if self.state.get('state')=='COMPLETED' and delivery is None else 'event')
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def configure(self,cfg):
        estimation.settings(cfg)
        rules=quantity.resolve_policy(self,cfg)
        mode,kind=observability.settings(cfg,self.state)
        if self.state.get('delivery_mode')!=mode or self.state.get('sample_kind')!=kind or self.state.get('quantity_policy')!=rules:
            self.update(_progress=False,delivery_mode=mode,sample_kind=kind,quantity_policy=rules)


    def transport_event(self,event):
        values=dict(transport_event=dict(event,stage=self.state.get('state','NOT_STARTED'),observed_at=dt.datetime.now(CN).isoformat(timespec='microseconds')))
        if event.get('operation')=='status' and event.get('event')=='request_attempt':
            values['last_poll_attempt_at']=dt.datetime.now(CN).isoformat(timespec='microseconds')
        self.update(**values)


def status_summary(state, now=None):
    result=public_data(state)
    result.update(state=state.get('state','NOT_STARTED'),request_id=state.get('request_id'),job_id=state.get('job_id'))
    now=now or dt.datetime.now(CN)
    observed=state.get('last_successful_poll_at')
    age=None
    if observed:
        try: age=max(0.,(now-dt.datetime.fromisoformat(observed)).total_seconds())
        except (TypeError,ValueError): pass
    result.update(observation_age_seconds=age,observation_stale=age is None or age>300,
                  freshness_basis='last_successful_poll_at',overall_percent=None,eta_seconds=None)
    total=state.get('task_count');downloaded=state.get('downloaded')
    result['download_percent']=min(100.,100.*downloaded/total) if type(total) is int and total>0 and type(downloaded) is int else None
    stage=state.get('state','NOT_STARTED')
    result['stage_label']={'NOT_STARTED':'尚未开始','PRECHECK_RUNNING':'查询声量',
        'AWAITING_QUANTITY_CONFIRMATION':'等待数量确认','APPROVED_NOT_SUBMITTED':'已确认，尚未创建',
        'CREATE_OUTCOME_UNKNOWN':'创建结果待核对','SUPPLIER_RUNNING':'供应商处理中',
        'SUPPLIER_COMPLETED':'供应商任务完成','READY_FOR_DOWNLOAD':'数据已就绪','VERIFYING_DATA_READY':'核对数据就绪情况','DOWNLOADING':'下载中','EXPORTING':'导出中',
        'VALIDATING':'校验返回数据','DATA_VALIDATION_FAILED':'数据校验失败，需核对',
        'COMPLETED':'本地产物完成','BUDGET_BLOCKED':'费用门禁停止，等待上游处理','WAIT_TIMEOUT_RESUMABLE':'等待超时，可恢复',
        'DOWNLOAD_FAILED_RESUMABLE':'下载中断，可恢复','EXPORT_FAILED_RESUMABLE':'导出中断，可恢复',
        'SUPPLIER_FAILED':'供应商任务失败','TASK_COUNT_EXCEEDS_LIMIT':'实际数量超过确认上限'}.get(stage,stage)
    result['next_action']=('查看已有产物；无需再次取数。' if stage=='COMPLETED' else
        '在当前会话确认条件、声量与数量上限。' if stage=='AWAITING_QUANTITY_CONFIRMATION' else
        '核对供应商任务，使用 recover/reconcile；禁止重建。' if stage=='CREATE_OUTCOME_UNKNOWN' else
        '使用原目录与请求ID恢复；状态过期时查询供应商。' if state.get('job_id') else
        '检查配置并执行 plan/precheck。')
    if state.get('error_detail'): result['next_action']=state['error_detail'].get('next_action',result['next_action'])
    if stage=='DATA_VALIDATION_FAILED':
        result['next_action']='读取validation.json核对错误，保留原页面；未处理前不交付、不重建计费任务。'
    elif stage=='COMPLETED' and state.get('quality',{}).get('status')=='PASS_WITH_WARNINGS':
        result['next_action']='交付产物时同时说明validation.json中的质量告警；不能称全部数据质量合格。'
    return result


def fetch_status(api, job_id, token=None, emit=None):
    holder=[token or core.get_dynamic_token(api)]
    def refresh(remaining): holder[0]=core.get_dynamic_token(api,force_refresh=True,budget=remaining)
    return call_json('status',lambda timeout:core.requests.get(api['api']['base_url']+'/project/job/status/detail',
        headers={'Authorization':holder[0]},params={'id':job_id},timeout=timeout,allow_redirects=False),api,emit=emit,refresh=refresh)


def build_queries(cfg):
    return query.normalize(cfg)


def task_contract(cfg, api):
    canonical = build_queries(cfg)
    compiled = query.task_payload(canonical, cfg["profiler"]["name"])
    # Account identity is hashed; tokens/passwords never enter the receipt or registry.
    scope = fingerprint({"username":api.get("credentials",{}).get("username", "")})
    return dict(**compiled, canonical_query=canonical,
        provider=dict(api=api["api"], es=api["es"], account_scope=scope),
        task={k:v for k,v in cfg.get("task",{}).items() if k not in {"request_id", "name"}}, provider_task=api.get("task", {}))


def budget_for(run, cfg, api):
    contract = task_contract(cfg, api)
    return Budget(run, request_id_for(run, cfg), contract['provider']['account_scope'], fingerprint(contract))


def precheck(run, cfg, api, request_id=None, config_path=HERE / 'config.yaml'):
    try:
        return _precheck(run,cfg,api,request_id,config_path)
    except Exception as exc:
        if run.state.get('state') in {'PREFLIGHT_RUNNING','PRECHECK_RUNNING'}:
            detail=describe(exc,'precheck')
            run.update(state='BUDGET_BLOCKED' if detail['category']=='budget' else 'PRECHECK_FAILED',
                       last_error=detail['code'],error_detail=detail)
        raise


def _precheck(run, cfg, api, request_id, config_path):
    load_dictionary()
    run.configure(cfg)
    require(not run.state.get("create_intent") and not run.state.get("job_id"), "EXISTING_CREATE_INTENT_DO_NOT_REPLAY")
    guard('precheck',run.state)
    # Invalidate a previous receipt even when the new local preflight fails.
    run.update(state='PREFLIGHT_RUNNING',approval=None,precheck_sha256=None,last_error=None)
    output_settings(cfg,config_path)
    contract = task_contract(cfg,api)
    estimate_plan=estimation.plan(contract['canonical_query'],cfg)
    estimate=estimation.public(estimate_plan)
    run.update(_progress=False,contract=contract,estimate=estimate)
    require(estimate['relation']!='UNAVAILABLE','STAT_ESTIMATE_UNAVAILABLE_USE_UPSTREAM_ESTIMATE')
    requests = estimate_plan['requests']
    token=api.get("credentials",{}).get("fixed_token")
    require(not requests or isinstance(token,str) and bool(token) and not token.startswith("${"), "STAT_TOKEN_REQUIRED")
    rid = request_id_for(run, cfg, request_id)
    run.update(_progress=False, request_id=rid)
    budget = budget_for(run, cfg, api)
    budget.begin_precheck(requests)
    run.update(state="PRECHECK_RUNNING", approval=None, precheck_sha256=None, last_error=None)
    earliest=time.time();results=[];total=0
    folder=run.path/("volume-"+uuid.uuid4().hex);folder.mkdir()
    if estimate_plan['manual'] is not None:
        for platform,count in sorted(estimate_plan['manual']['counts'].items()):
            path=folder/(platform+'.json')
            write(path,dict(source='upstream_estimate',platform=platform,count=count,reference_sha256=fingerprint(estimate_plan['manual']['reference'])))
            results.append(dict(platform=platform,count=count,path=str(path),sha256=file_hash(path)))
            total+=count
        budget.check_estimate(results)
    for i,item in enumerate(requests):
        started=time.time()
        payload=call_json('volume',lambda timeout:budget.stat_call(lambda t:core.requests.post(api["api"]["count_base_url"].rstrip("/")+"/socialmedia/totalVolume",
            json=dict(item["body"],token=token),timeout=t,allow_redirects=False),timeout),api,emit=run.transport_event)
        require(isinstance(payload,dict) and payload.get("success") is True and payload.get("code")==200,"STAT_API_ERROR")
        data=payload.get("data")
        require(isinstance(data,dict) and type(data.get("total")) is int and data["total"]>=0,"STAT_VOLUME_MISSING_OR_INVALID")
        count=data["total"]
        evidence=dict(request=item, count=count, started_epoch=started,observed_epoch=time.time())
        path=folder/(str(i)+".json");write(path,evidence)
        results.append(dict(platform=item["platform"],count=count,path=str(path),sha256=file_hash(path)))
        total+=count
        budget.check_estimate(results)
    receipt=dict(schema_version=2,created_epoch=earliest,query_fingerprint=fingerprint(contract),contract=contract,
        results=results,estimated_volume=total,adapter="task-first-estimation-v2",contract_policy_version=4,request_count=len(requests),
        estimate=estimate,estimate_plan_sha256=estimation.binding(estimate_plan),
        budget_ref=budget.record_quote(results))
    write(run.path/"precheck.json",receipt)
    run.update(state="AWAITING_QUANTITY_CONFIRMATION",contract=contract,estimated_volume=total,precheck_sha256=file_hash(run.path/"precheck.json"))
    return receipt


def validate_precheck(run, cfg, api):
    build_queries(cfg)
    p = run.path / "precheck.json"
    require(p.exists() and file_hash(p) == run.state.get("precheck_sha256"), "PRECHECK_MISSING_OR_CHANGED")
    receipt = read(p)
    require(receipt.get('contract_policy_version')==4, 'PRECHECK_POLICY_CHANGED_RERUN')
    require(receipt['query_fingerprint']==fingerprint(task_contract(cfg,api)),'QUERY_CHANGED_RERUN_PRECHECK')
    current_plan=estimation.plan(build_queries(cfg),cfg)
    require(receipt.get('estimate_plan_sha256')==estimation.binding(current_plan) and receipt.get('estimate')==estimation.public(current_plan),'ESTIMATE_PLAN_CHANGED_RERUN')
    require(receipt["query_fingerprint"] == fingerprint(task_contract(cfg, api)), "QUERY_CHANGED_RERUN_PRECHECK")
    max_age = cfg.get("risk", {}).get("precheck_max_age_seconds", 1800)
    require(type(max_age) is int and 0 < max_age <= 86400, "INVALID_PRECHECK_MAX_AGE")
    require(0 <= time.time() - receipt["created_epoch"] <= max_age, "PRECHECK_EXPIRED_RERUN")
    for item in receipt["results"]:
        require(file_hash(item["path"]) == item["sha256"], "STAT_EVIDENCE_CHANGED")
    return receipt


def approve(run, cfg, api, maximum, reference):
    require(not run.state.get("create_intent"), "EXISTING_CREATE_INTENT_DO_NOT_REPLAY")
    guard('approve',run.state)
    receipt = validate_precheck(run, cfg, api)
    estimate_acceptance=estimation.acceptance(cfg,receipt['estimate'])
    require(type(maximum) is int and maximum > 0 and bool(reference and reference.strip()), "EXPLICIT_LIMIT_AND_CONFIRMATION_REQUIRED")
    require(0 < receipt["estimated_volume"] <= maximum, "ESTIMATE_ZERO_OR_EXCEEDS_LIMIT")
    budget_for(run, cfg, api).approve(receipt, maximum)
    approval = dict(precheck_sha256=run.state["precheck_sha256"], max_records=maximum, confirmation_ref=reference,estimate_acceptance=estimate_acceptance,
        confirmed_at=dt.datetime.now(CN).isoformat(timespec="seconds"))
    run.update(state="APPROVED_NOT_SUBMITTED", approval=approval,last_error=None)


def output_settings(cfg, config_path, allow_recovery=False):
    output = pathlib.Path(cfg["output"]["file"])
    if not output.is_absolute():
        output = pathlib.Path(config_path).resolve().parent / output
    require(output.suffix in {".csv", ".xlsx"}, "OUTPUT_FORMAT_INVALID")
    require(allow_recovery or not output.exists(), "OUTPUT_ALREADY_EXISTS_RECONCILE_OR_CHOOSE_NEW_PATH")
    raw = cfg["output"].get("template", "template.yaml")
    require(isinstance(raw,str),'TEMPLATE_PATH_MUST_BE_STRING')
    template = core.load_template(str(pathlib.Path(config_path).resolve().parent / raw)) if raw else None
    if output.suffix == ".xlsx":
        import openpyxl
    return output, template


def bind_record(run, record):
    current=run.state.get("job_id")
    require(current is None or current==record.get("job_id"),"RUN_ALREADY_BOUND_TO_ANOTHER_JOB")
    if current is not None and current==record.get("job_id"):
        return
    run.update(state=record["state"],request_id=record["request_id"],registry_path=str(run.registry),create_intent=True,
        task_name=record["task_name"],contract=record["contract"],max_records=record["max_records"],
        estimated_volume=record["estimated_volume"],create_started_epoch=record["created_epoch"],job_id=record.get("job_id"),last_error=None)
    if record.get('estimate') is not None:run.update(_progress=False,estimate=record['estimate'])


def request_id_for(run,cfg,request_id=None):
    from request_registry import valid_id
    values=[v for v in [request_id,cfg.get("task",{}).get("request_id"),run.state.get("request_id")] if v]
    require(len(set(values))<=1,"RUN_REQUEST_ID_MISMATCH")
    value=valid_id(request_id or cfg.get("task",{}).get("request_id") or run.state.get("request_id"))
    require(run.state.get("request_id") in (None,value),"RUN_REQUEST_ID_MISMATCH")
    return value


def sync_known_creation_budget(run, cfg, api, record):
    if record.get('budget_policy_version') != 1:
        return  # Historical jobs remain recoverable without invented past costs.
    try:
        budget_for(run,cfg,api).mark_known_creation()
        result='RECONCILED'
    except Exception:
        # Keep the existing reservation and allow recovery of already paid data.
        result='PENDING_INTERNAL_CHECK'
    run.update(_progress=False,budget_reconciliation=result)


def submit(run, cfg, api, config_path=HERE / "config.yaml", request_id=None):
    load_dictionary()
    request_id=request_id_for(run,cfg,request_id)
    contract=task_contract(cfg,api);contract_hash=fingerprint(contract);registry=Registry(run.registry)
    existing=registry.get(request_id,contract_hash)
    if existing:
        bind_record(run,existing)
        require(existing.get("job_id") is not None,"CREATE_OUTCOME_UNKNOWN_RECONCILE_ONLY")
        sync_known_creation_budget(run,cfg,api,existing)
        return existing["job_id"]
    require(not run.state.get("create_intent") and not run.state.get("job_id"),"EXISTING_CREATE_INTENT_DO_NOT_REPLAY")
    guard('create',run.state)
    receipt=validate_precheck(run,cfg,api);approval=run.state.get("approval") or {}
    require(approval.get('estimate_acceptance')==estimation.acceptance(cfg,receipt['estimate']),'ESTIMATE_ACCEPTANCE_CHANGED')
    require(approval.get("precheck_sha256")==run.state.get("precheck_sha256") and bool(approval.get("confirmation_ref")),"QUANTITY_CONFIRMATION_REQUIRED")
    require(type(approval.get("max_records")) is int and 0<receipt["estimated_volume"]<=approval["max_records"],"ESTIMATE_EXCEEDS_APPROVED_LIMIT")
    core.validate_configs(cfg,api);output_settings(cfg,config_path)
    budget=budget_for(run,cfg,api)
    budget.approve(receipt,approval['max_records'])
    token=core.get_dynamic_token(api);validate_precheck(run,cfg,api)
    budget=budget_for(run,cfg,api)  # Recheck price revision after login and before claim.
    record,won,ticket=budget.claim_create(receipt,approval['max_records'],lambda:registry.claim(request_id,contract_hash,
        dict(contract=contract,max_records=approval["max_records"],estimated_volume=receipt["estimated_volume"],
             approval=approval,estimate=receipt['estimate'],original_run_dir=str(run.path),budget_policy_version=1)))
    bind_record(run,record)
    if not won:
        require(record.get("job_id") is not None,"CREATE_OUTCOME_UNKNOWN_RECONCILE_ONLY")
        return record["job_id"]
    durable.fault("after_create_intent")
    task_cfg=json.loads(json.dumps(cfg));task_cfg.setdefault("task",{})["name"]=record["task_name"]
    job=core.create_task(token,task_cfg,api,prepared=contract)
    require(type(job) is int and job>0,"CREATE_RESPONSE_INVALID_DO_NOT_REPLAY")
    budget.settle(ticket,'incurred')
    durable.fault("after_remote_create")
    record=registry.attach(request_id,contract_hash,job)
    durable.fault("after_registry_job")
    bind_record(run,record)
    return job


def recover_request(run,cfg,api,request_id,job_id=None,reference=None):
    request_id=request_id_for(run,cfg,request_id);contract_hash=fingerprint(task_contract(cfg,api));registry=Registry(run.registry)
    record=registry.get(request_id,contract_hash)
    require(record is not None,"REQUEST_NOT_FOUND_IN_SHARED_REGISTRY")
    if job_id is not None:
        require(bool(reference and reference.strip()),"RECONCILIATION_REFERENCE_REQUIRED")
        require(type(job_id) is int and job_id>0,"INVALID_JOB_ID")
        payload=fetch_status(api,job_id,emit=run.transport_event)
        data=payload.get("data",{})
        require(isinstance(data,dict) and data.get("id")==job_id and data.get("name")==record["task_name"],"RECONCILIATION_JOB_NAME_MISMATCH")
        record=registry.attach(request_id,contract_hash,job_id,reference)
    bind_record(run,record)
    require(record.get("job_id") is not None,"CREATE_OUTCOME_UNKNOWN_RECONCILE_ONLY")
    sync_known_creation_budget(run,cfg,api,record)
    return record["job_id"]


def provider_status(run, api, token=None):
    from provider_state import select
    require(type(run.state.get("job_id")) is int, "JOB_ID_REQUIRED_RECONCILE_UNKNOWN_CREATE")
    require(run.state.get("contract", {}).get("provider") == dict(api=api["api"], es=api["es"], account_scope=fingerprint({"username":api.get("credentials",{}).get("username", "")})), "PROVIDER_CHANGED")
    run.update(last_poll_attempt_at=dt.datetime.now(CN).isoformat(timespec='microseconds'))
    payload=fetch_status(api,run.state['job_id'],token,run.transport_event)
    decision=select(payload.get('data'),run.state['job_id'],run.state['contract']['searchCondition'])
    terminal=run.state.get('state')=='COMPLETED'
    values=dict(job_status=decision['main'],subtask_status=decision['sub'],
        elapsed_seconds=int(time.time()-run.state.get('create_started_epoch',time.time())),
        last_successful_poll_at=dt.datetime.now(CN).isoformat(timespec='microseconds'),last_error=None,error_detail=None,
        selected_instance=decision['instance'])
    if not decision['ready']:
        if terminal:values['remote_result_conflict']=True
        else:values['state']='SUPPLIER_FAILED' if decision['failed'] else 'SUPPLIER_RUNNING'
        run.update(**values)
        if decision['failed']:raise SkillError('SUPPLIER_TASK_FAILED')
        return None
    expected=decision['expected']
    if expected>run.state['max_records']:
        if not terminal:values['state']='TASK_COUNT_EXCEEDS_LIMIT'
        else:values['remote_result_conflict']=True
        run.update(**values)
        raise SkillError('TASK_COUNT_EXCEEDS_APPROVED_LIMIT_ALREADY_SUBMITTED')
    if not terminal:values['state']='VERIFYING_DATA_READY'
    run.update(**values)
    fixed=api.get('credentials',{}).get('fixed_token')
    require(isinstance(fixed,str) and bool(fixed) and not fixed.startswith('${'),'STAT_TOKEN_REQUIRED')
    # No dataType/site/time overrides: count the full result scope of this job,
    # exactly as /search does. Selected instance counts are never summed.
    payload=call_json('count',lambda timeout:core.requests.get(api['api']['count_base_url'].rstrip('/')+'/count',
        params=dict(jobId=run.state['job_id'],token=fixed,pLimit=1),timeout=timeout,allow_redirects=False),api,emit=run.transport_event)
    counted=payload.get('data')
    require(isinstance(counted,dict) and type(counted.get('total')) is int and counted['total']>=0,'READY_COUNT_INVALID')
    actual=counted['total']
    if actual>run.state['max_records']:
        run.update(**({'remote_result_conflict':True} if terminal else {'state':'TASK_COUNT_EXCEEDS_LIMIT'}))
        raise SkillError('TASK_COUNT_EXCEEDS_APPROVED_LIMIT_ALREADY_SUBMITTED')
    readiness=dict(basis='current_offline_instance_complete_and_full_job_count_match',expected=expected,
        available=actual,verified=actual==expected,instance=decision['instance'],checked_at=dt.datetime.now(CN).isoformat())
    if terminal:
        prior=run.state.get('readiness',{}).get('instance')
        run.update(readiness=readiness,remote_task_count=expected,
            remote_result_conflict=actual!=expected or expected!=run.state.get('task_count') or prior is not None and prior!=decision['instance'])
    else:
        run.update(state='READY_FOR_DOWNLOAD' if actual==expected else 'VERIFYING_DATA_READY',task_count=expected,readiness=readiness)
    return expected if actual==expected else None


def download(run, cfg, api, config_path, observed_count=None):
    guard('download',run.state)
    quantity.bind_policy(run,cfg)
    provider=task_contract(cfg,api)["provider"]
    require(run.state.get("contract",{}).get("provider")==provider,"PROVIDER_CHANGED")
    require(run.state.get('contract')==task_contract(cfg,api),'CACHE_QUERY_OR_PROVIDER_CHANGED')
    manifest_path=run.path/"pages/manifest.json"
    complete=manifest_path.exists() and read(manifest_path).get("complete") is True
    count=run.state.get("task_count") if complete else (provider_status(run,api) if observed_count is None else observed_count)
    require(type(count) is int,"TASK_NOT_COMPLETE")
    require(0<=count<=run.state['max_records'],'TASK_COUNT_EXCEEDS_APPROVED_LIMIT_ALREADY_SUBMITTED')
    run.update(_progress=False,task_count=count)
    quantity.review(run,count,api,refresh=not complete)
    output,template=output_settings(cfg,config_path,allow_recovery=(run.path/"export.json").exists())
    store=PageStore(run.path,run.state["job_id"],count,run.state["max_records"],provider)
    run.update(state="DOWNLOADING",downloaded=store.m["count"],task_count=count)
    downloaded=store.download(api,core.requests.get,on_progress=lambda n:run.update(downloaded=n))
    run.update(state="VALIDATING",downloaded=downloaded)
    quality_report=validate_records(store.iter_rows(),count,run.state["estimated_volume"],
        run.state.get('contract',{}).get('canonical_query'),[x[0] for x in template] if template else None,
        deviation_policy=run.state.get('quantity_policy'),estimate_relation=run.state.get('estimate',{}).get('relation','MATCHED'))
    quality_report['binding']=dict(job_id=run.state['job_id'],pages_fingerprint=fingerprint(store.m['pages']),
        query_fingerprint=fingerprint(run.state.get('contract',{}).get('canonical_query')))
    quality_path=run.path/'validation.json'
    write(quality_path,quality_report)
    quality=dict(status=quality_report['status'],report=str(quality_path),sha256=file_hash(quality_path),
        dictionary_version=quality_report['dictionary_version'],error_occurrences=quality_report['error_occurrences'],
        warning_occurrences=quality_report['warning_occurrences'])
    run.update(quality=quality)
    require(quality_report['status']!='FAIL','DATA_VALIDATION_FAILED')
    run.update(state="EXPORTING",downloaded=downloaded)
    exports=export_artifacts(run.path,store,output,template)
    result=dict(job_id=run.state["job_id"],request_id=run.state.get("request_id"),estimated_volume=run.state["estimated_volume"],
        task_count=count,downloaded=downloaded,unique_ids=downloaded,delta_task_minus_estimate=count-run.state["estimated_volume"],
        output=str(output),raw_data=str(run.path/"records.json"),artifacts=[{k:v for k,v in item.items() if k!='temp'} for item in exports["artifacts"]],quality=quality,quantity_review=run.state.get('quantity_review'),actual_billed_count=None,actual_cost=None)
    write(run.path/"result.json",result);durable.fault("after_result_file")
    run.update(state="COMPLETED",result=result,last_error=None,_delivery=result)
    return result


def completed_output(run,cfg,api,config_path,explicit_export=False):
    output,template=output_settings(cfg,config_path,allow_recovery=True)
    result=reuse_or_export(run,task_contract(cfg,api),output,template,explicit_export)
    # Do not replace the original completed acquisition/result with a derivative.
    run.update(_progress=False,last_error=None,_delivery=result,
               _delivery_operation='cache_reuse' if result.get('cache_hit') else 'local_reexport')
    emit(run.state,extra=result)
    return result


def execute(run,cfg,api,args):
    """One bounded advancement; precheck and user confirmation require separate calls."""
    guard('execute',run.state)
    stage=run.state.get('state','NOT_STARTED')
    contract=task_contract(cfg,api)
    if run.state.get('contract'):
        require(run.state['contract']==contract,'CACHE_QUERY_OR_PROVIDER_CHANGED')
    if run.state.get('request_id'):
        request_id_for(run,cfg,args.request_id)
    if stage=='COMPLETED':
        return completed_output(run,cfg,api,args.config)
    if stage=='NOT_STARTED':
        require(not run.state.get('create_intent') and not run.state.get('job_id'),'EXISTING_CREATE_INTENT_DO_NOT_REPLAY')
        rid=args.request_id or cfg.get('task',{}).get('request_id') or uuid.uuid4().hex
        run.update(_progress=False,request_id=rid,contract=contract)
        request_id_for(run,cfg,args.request_id)
        precheck(run,cfg,api,rid,args.config)
    elif stage=='AWAITING_QUANTITY_CONFIRMATION':
        validate_precheck(run,cfg,api)
        if args.max_records is not None or args.confirmation_ref:
            approve(run,cfg,api,args.max_records,args.confirmation_ref)
            submit(run,cfg,api,args.config,args.request_id)
    elif stage=='APPROVED_NOT_SUBMITTED':
        submit(run,cfg,api,args.config,args.request_id)
    elif stage in ACTIVE-{'CREATE_OUTCOME_UNKNOWN'}:
        if (run.path/'pages/manifest.json').exists() and read(run.path/'pages/manifest.json').get('complete'):
            download(run,cfg,api,args.config)
        else:
            interval=api.get('runtime',{}).get('poll_interval',120)
            require(type(interval) is int and interval>=120,'INVALID_POLL_INTERVAL_OR_TIMEOUT')
            last=run.state.get('last_poll_attempt_at')
            epoch=dt.datetime.fromisoformat(last).timestamp() if last else run.state.get('create_started_epoch',0)
            if time.time()-epoch<interval:
                emit(dict(run.state,**status_summary(run.state)),'status')
                return
            count=provider_status(run,api)
            if count is not None:download(run,cfg,api,args.config,observed_count=count)
    else:
        if stage=='BUDGET_BLOCKED' and (run.state.get('error_detail') or {}).get('code')=='BUDGET_LIMIT_EXCEEDED':
            from budget import fail
            fail('BUDGET_LIMIT_EXCEEDED')
        raise SkillError('EXECUTE_STOPPED_EXPLICIT_RECOVERY_REQUIRED','state',
            next_action='保留原请求，明确核对失败原因；未知创建使用recover/reconcile，不自动重放。')
    emit(run.state,extra=dict(run_dir=str(run.path)))


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise SkillError('CLI_ARGUMENTS_INVALID','validation',next_action='检查命令参数和--help。')
    parser = Parser(description=f"ssjh-social-task {VERSION}: lossless export and scoped telemetry")
    parser.add_argument("--action", choices=["preflight", "review-quantity", "notifications", "ack-notification", "execute", "export", "plan", "precheck", "approve", "create", "status", "resume", "download", "recover", "reconcile", "doctor"], default="plan")
    parser.add_argument("--config", default=str(HERE / "config.yaml"))
    parser.add_argument("--api-config", default=str(HERE / "api_config.yaml"))
    parser.add_argument("--run-dir")
    parser.add_argument("--cache-root", help="Caller-selected workspace cache; execute only; mutually exclusive with run-dir")
    parser.add_argument("--request-id")
    parser.add_argument("--registry", help="Shared local registry; all runs must use the same file")
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--confirmation-ref")
    parser.add_argument("--event-id",help="Progress notification ID returned by notifications")
    parser.add_argument("--delivery-ref",help="Caller delivery/consumption reference; only its hash is stored")
    parser.add_argument("--refresh", action="store_true", help="status: refresh from provider; default reads local status without locking")
    try: args = parser.parse_args(argv)
    except SkillError as exc:
        emit({},'error',describe(exc,'arguments'))
        return 2
    run=None
    try:
        if args.action=='preflight':
            from readiness import preflight
            report=preflight(args.config,args.api_config,args.run_dir,args.registry)
            value=envelope({},'diagnostic')
            value.update(ok=report['ok'],data=report,message='本地配置与无凭证连接检查完成。',next_action='START_PRECHECK' if report['ok'] else 'FIX_DEPLOYMENT')
            print(json.dumps(value,ensure_ascii=False))
            return 0 if report['ok'] else 2
        if args.action=='doctor':
            from diagnostics import doctor
            report=doctor(args.config,args.api_config,args.run_dir,args.registry)
            value=envelope({},'diagnostic')
            value.update(ok=report['ready_for_local_checks'],data=report,message='本地环境诊断完成。',next_action='REVIEW_DIAGNOSTIC')
            print(json.dumps(value,ensure_ascii=False))
            return 0 if report['ready_for_local_checks'] else 2
        require(not (args.run_dir and args.cache_root),'RUN_DIR_AND_CACHE_ROOT_MUTUALLY_EXCLUSIVE')
        loaded=None
        if args.cache_root:
            require(args.action=='execute','CACHE_ROOT_REQUIRES_EXECUTE')
            loaded=core.load_configs(args.config,args.api_config)
            args.run_dir=str(pathlib.Path(args.cache_root).expanduser().resolve()/fingerprint(task_contract(*loaded)))
        require(bool(args.run_dir),'RUN_DIR_REQUIRED')
        if args.action=='notifications':
            run=Run(args.run_dir,args.registry,create=False)
            value=envelope(run.state,'notifications')
            value['data']=dict(events=observability.pending(run.state))
            print(json.dumps(value,ensure_ascii=False),flush=True)
            return 0
        if args.action=='ack-notification':
            run=Run(args.run_dir,args.registry,create=False)
            require(run.file.is_file(),'NOTIFICATION_RUN_NOT_FOUND')
            with run.lock(record_errors=False):
                observability.acknowledge(run.state,args.event_id,args.delivery_ref,dt.datetime.now(CN).isoformat())
                write(run.file,run.state)
                observability.materialize(run.path,run.state)
                value=envelope(run.state,'notification_ack')
                value['data']=dict(event_id=args.event_id,status='acknowledged',delivery_evidence='caller_report_not_independently_verified')
                print(json.dumps(value,ensure_ascii=False),flush=True)
            return 0
        if args.action=='status' and not args.refresh:
            run=Run(args.run_dir,args.registry,create=False)
            emit(dict(run.state,**status_summary(run.state)),'status')
            return 0
        run=Run(args.run_dir,args.registry)
        with run.lock():
            cfg, api = loaded or core.load_configs(args.config, args.api_config)
            run.configure(cfg)
            guard(args.action,run.state) if args.action not in {'download','resume','create','precheck','approve','export'} else None
            if args.action == "plan":
                build_queries(cfg);output_settings(cfg,args.config,allow_recovery=True)
                run.update(_progress=False,estimate=estimation.public(estimation.plan(build_queries(cfg),cfg)))
                emit(run.state,extra=dict(run_dir=str(run.path)))
            elif args.action == 'execute':
                execute(run,cfg,api,args)
            elif args.action == 'export':
                guard('export',run.state)
                completed_output(run,cfg,api,args.config,True)
            elif args.action == 'review-quantity':
                require(run.state.get('contract')==task_contract(cfg,api),'CACHE_QUERY_OR_PROVIDER_CHANGED')
                count=provider_status(run,api)
                if count is not None:quantity.review(run,count,api,refresh=True)
                emit(run.state)
            elif args.action == "precheck":
                precheck(run, cfg, api, args.request_id, args.config)
                emit(run.state)
            elif args.action == "approve":
                approve(run, cfg, api, args.max_records, args.confirmation_ref)
                emit(run.state)
            elif args.action == "create":
                submit(run, cfg, api, args.config,args.request_id)
                emit(run.state)
            elif args.action in {"recover", "reconcile"}:
                require(args.action != "reconcile" or args.job_id is not None,"RECONCILIATION_JOB_ID_REQUIRED")
                recover_request(run,cfg,api,args.request_id,args.job_id if args.action=="reconcile" else None,args.confirmation_ref)
                emit(run.state)
            elif args.action == "status":
                provider_status(run, api)
                emit(dict(run.state,**status_summary(run.state)),'status')
            elif args.action == "download":
                if run.state.get("state") == "COMPLETED":
                    completed_output(run,cfg,api,args.config)
                else:
                    download(run, cfg, api, args.config)
            else:
                if run.state.get("state") == "COMPLETED":
                    completed_output(run,cfg,api,args.config)
                    return 0
                guard('resume',run.state)
                if (run.path/"pages/manifest.json").exists() and read(run.path/"pages/manifest.json").get("complete"):
                    download(run,cfg,api,args.config)
                    return 0
                start = time.monotonic()
                interval = api.get("runtime", {}).get("poll_interval", 120)
                timeout = api.get("runtime", {}).get("timeout", 3600)
                require(type(interval) is int and interval >= 120 and type(timeout) is int and timeout > 0, "INVALID_POLL_INTERVAL_OR_TIMEOUT")
                while True:
                    count = provider_status(run, api)
                    if count is not None:
                        break
                    if time.monotonic() - start >= timeout:
                        run.update(state="WAIT_TIMEOUT_RESUMABLE")
                        raise SkillError("WAIT_TIMEOUT_RESUMABLE_SAME_JOB")
                    time.sleep(min(interval, max(0, timeout-(time.monotonic()-start))))
                download(run, cfg, api, args.config, observed_count=count)
        return 0
    except Exception as exc:
        detail=describe(exc,args.action)
        state=run.state if run else {}
        emit(state,'error',detail)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
