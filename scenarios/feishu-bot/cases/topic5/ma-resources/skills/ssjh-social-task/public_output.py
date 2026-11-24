"""Allowlisted public envelope; never serialize complete state/config/receipts."""
import json
from query_contract import has_filters, resolved_filters

from version import VERSION
BUDGET_MESSAGE = '您的取数任务涉及数据费用成本过高，超出数据费费控阈值1000元限制，请联系智蓝数据部负责人袁杰松。感谢理解和配合！~'
FIELDS = ('estimated_volume', 'task_count', 'downloaded', 'unique_ids',
          'delta_task_minus_estimate', 'elapsed_seconds', 'updated_at',
          'last_poll_attempt_at', 'last_successful_poll_at', 'last_progress_at',
          'observation_age_seconds', 'observation_stale', 'download_percent',
          'job_status', 'subtask_status', 'output', 'raw_data', 'cache_hit',
          'local_export', 'cache_verified', 'run_dir', 'remote_result_conflict')

def public_data(value):
    result = {k: value[k] for k in FIELDS if k in value and type(value[k]) in (str,int,float,bool,type(None))}
    estimate=value.get('estimate')
    if isinstance(estimate,dict):result['estimate']={k:estimate[k] for k in ('version','relation','reasons','adjustments') if k in estimate}
    quality = value.get('quality')
    if isinstance(quality, dict):
        result['quality'] = {k: quality[k] for k in ('status','report','sha256','dictionary_version','error_occurrences','warning_occurrences') if k in quality}
    artifacts = value.get('artifacts')
    if isinstance(artifacts, list):
        result['artifacts'] = [{k: a[k] for k in ('target','sha256','count') if k in a} for a in artifacts if isinstance(a,dict)]
    event=value.get('transport_event')
    if isinstance(event,dict) and event.get('stage')==value.get('state') and event.get('stage') is not None and not value.get('cache_hit') and not value.get('local_export'):
        result['transport_event']={k:event[k] for k in ('operation','event','attempt','max_attempts','retry_after_seconds','wait_seconds','stage','observed_at') if k in event}
    review=value.get('quantity_review')
    if isinstance(review,dict):
        fields=('status','estimated','actual','delta','absolute_difference','relative_difference','reason')
        result['volume_comparison']={k:review[k] for k in ('status','delivery_allowed','ratio_denominator','notes','estimate_relation') if k in review}
        result['volume_comparison']['total']={k:v for k,v in review.get('total',{}).items() if k in fields}
        result['volume_comparison']['platforms']={p:{k:v for k,v in row.items() if k in fields} for p,row in review.get('platforms',{}).items()}
    return result

def envelope(state, kind='result', detail=None, extra=None):
    stage = state.get('state', 'NOT_STARTED')
    detail = detail or state.get('error_detail')
    error = None
    if isinstance(detail, dict):
        error = {k: detail[k] for k in ('code','category','operation','retryable','http_status','attempt','max_attempts',
            'provider_code','retry_after_seconds','outcome_uncertain','route_to','upstream_action','parameter') if k in detail}
    if error is None and stage in {'PRECHECK_FAILED','DATA_VALIDATION_FAILED','SUPPLIER_FAILED',
        'TASK_COUNT_EXCEEDS_LIMIT','DOWNLOAD_FAILED_RESUMABLE','EXPORT_FAILED_RESUMABLE','BUDGET_BLOCKED','QUANTITY_REVIEW_REQUIRED'}:
        error=dict(code=stage,category='state',retryable=False)
    code = error.get('code') if error else None
    messages = {
        'NOT_STARTED': '任务尚未开始。', 'PREFLIGHT_RUNNING': '正在检查取数配置。', 'PRECHECK_RUNNING': '正在核对本次取数范围和声量。',
        'AWAITING_QUANTITY_CONFIRMATION': '声量已核对，请确认本次采集条件和数量上限。',
        'APPROVED_NOT_SUBMITTED': '采集范围已确认，准备创建任务。',
        'CREATE_OUTCOME_UNKNOWN': '任务提交结果待核对，请保留当前任务记录。',
        'SUPPLIER_RUNNING': '取数任务正在处理。', 'WAIT_TIMEOUT_RESUMABLE': '本次等待已结束，可继续查询原任务。',
        'VERIFYING_DATA_READY': '正在核对数据是否已可取回。',
        'READY_FOR_DOWNLOAD': '数据已就绪，准备取回。', 'SUPPLIER_COMPLETED': '数据已就绪，准备取回。',
        'DOWNLOADING': '正在取回数据。', 'VALIDATING': '正在校验返回数据。',
        'EXPORTING': '正在生成交付文件。', 'COMPLETED': '数据校验与导出已完成。',
    }
    message = messages.get(stage, '任务已停止，请核对状态后继续原任务。')
    next_action = ('CONFIRM_QUANTITY' if stage=='AWAITING_QUANTITY_CONFIRMATION' else
                   'USE_VERIFIED_CACHE' if stage=='COMPLETED' else
                   'RECONCILE_ORIGINAL_REQUEST' if stage=='CREATE_OUTCOME_UNKNOWN' else
                   'EXECUTE' if stage in {'NOT_STARTED','APPROVED_NOT_SUBMITTED'} else
                   'CHECK_OR_RESUME_ORIGINAL_TASK')
    if error:
        message = '本次操作未完成，请核对错误代码及原任务记录。'
        next_action = 'CHECK_ORIGINAL_TASK'
    if code=='BUDGET_LIMIT_EXCEEDED':
        message = BUDGET_MESSAGE; next_action = BUDGET_MESSAGE
        error.update(message=message, next_action=message)
    if stage=='QUANTITY_REVIEW_REQUIRED':
        message='取数数量存在重大偏差或分平台核对尚未完成，已暂缓交付，请上游核对原任务。'
        next_action='REVIEW_ORIGINAL_TASK_QUANTITY'
        if error:error.update(route_to='upstream_agent',upstream_action='HOLD_DELIVERY_AND_REVIEW_ORIGINAL_TASK',retryable=False)
    data = public_data(state)
    if not error and kind!='status':data.update(public_data(state.get('result') or {}))
    if extra: data.update(public_data(extra))
    if state.get('quantity_review'):data.update(public_data({'quantity_review':state['quantity_review']}))
    if stage=='COMPLETED' and kind=='status':
        data['cache_verified']=False
        message='本地记录显示任务已完成；交付文件前需复核缓存。'
        next_action='VERIFY_CACHE_WITH_EXECUTE'
    if stage=='COMPLETED' and not error and data.get('quality',{}).get('status')=='PASS_WITH_WARNINGS':
        message+='部分字段有质量告警，请随文件查看校验报告。'
    queries = state.get('contract', {}).get('canonical_query', [])
    if queries:
        data['filters'] = resolved_filters(queries[0])
    volume = state.get('estimated_volume')
    if (stage in {'SUPPLIER_RUNNING','VERIFYING_DATA_READY','WAIT_TIMEOUT_RESUMABLE'}
            and len(queries)==1 and queries[0].get('platform')=='weibo'
            and queries[0].get('msg_type')=='1' and type(volume) is int and 0<volume<=1000
            and queries[0].get('collection_mode','0')=='0' and not queries[0].get('account_filter')
            and queries[0].get('keyword_fields','content,title')=='content,title' and queries[0].get('sentiments',['0','1','-1'])==['0','1','-1']
            and not has_filters(queries[0])):
        data['eta'] = dict(min_seconds=300,max_seconds=1200,basis='similar_historical_tasks',from_stage='creation',is_sla=False)
        if not error:
            message += '相近任务通常约需5—20分钟。'
            if state.get('elapsed_seconds',0)>1200:
                message = '本次取数耗时已超出参考范围，将继续核对原任务状态。'
    audience='workflow' if state.get('delivery_mode','intermediate')=='intermediate' else 'user'
    if audience=='workflow' and stage=='COMPLETED' and not error:
        message='中间产物已就绪。' if kind!='status' else '中间产物已记录，使用前需复核缓存。'
    if not error and (state.get('quantity_review') or {}).get('status')=='WARNING':
        message+='预检与任务数量存在偏差，核对记录已随结果保留。'
    if data.get('estimate',{}).get('relation')=='UNCERTAIN':
        message+='本次为不确定估算，不代表同口径数量或费用保证。'
        if stage=='AWAITING_QUANTITY_CONFIRMATION':next_action='CONFIRM_QUANTITY_AND_ESTIMATE_UNCERTAINTY'
    if code in ('ESTIMATE_UNCERTAINTY_ACCEPTANCE_REQUIRED','STAT_ESTIMATE_UNAVAILABLE_USE_UPSTREAM_ESTIMATE'):
        error.update(route_to='upstream_agent',retryable=False)
        next_action='PROVIDE_ESTIMATE_AND_ACCEPTANCE_REFERENCE'
    return dict(schema_version='1.0', skill_version=VERSION, type=kind, ok=error is None,
                audience=audience,user_notification_required=audience=='user',
                state=stage, request_id=state.get('request_id'), job_id=state.get('job_id'),
                code=code, message=message, next_action=next_action, data=data, error_detail=error)

def emit(state, kind='result', detail=None, extra=None):
    value=envelope(state,kind,detail,extra)
    print(json.dumps(value,ensure_ascii=False),flush=True)
    return value
