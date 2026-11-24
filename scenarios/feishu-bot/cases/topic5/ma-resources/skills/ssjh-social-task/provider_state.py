"""Select an unambiguous offline execution before interpreting result readiness.

Instances are executions/retries, not platform rows; never sum their dataCount.
"""
from errors import SkillError


def require(ok, code):
    if not ok:raise SkillError(code)


def select(data, job_id, search):
    require(isinstance(data,dict) and data.get('id')==job_id, 'STATUS_JOB_ID_MISMATCH')
    main=data.get('status');rows=data.get('jobInstStatusReport')
    require(type(main) is int and main in (0,1,2,3) and isinstance(rows,list), 'STATUS_SCHEMA_OR_UNKNOWN_STATE')
    # 3 is not an official main state; retain conservative legacy failure handling.
    if main==3:return dict(main=main,sub=None,failed=True,ready=False,instance=None)
    candidates=[];seen=set();start,end=search['startTime'],search['endTime']
    for row in rows:
        require(isinstance(row,dict),'SUBTASK_SCHEMA_ERROR')
        require(type(row.get('jobId')) is int and row['jobId']==job_id,'READINESS_SUBTASK_JOB_MISMATCH')
        identity=row.get('id')
        require(type(identity) in (str,int) and str(identity).isdigit() and int(identity)>0,'SUBTASK_ID_REQUIRED')
        require(str(identity) not in seen,'DUPLICATE_SUBTASK_ID');seen.add(str(identity))
        require(row.get('isCrawl',0) in (0,'0'), 'CRAWL_INSTANCE_REQUIRES_RECONCILIATION')
        low,high=row.get('schedStart'),row.get('schedEnd')
        if low is not None or high is not None:
            require(type(low) is int and type(high) is int and low<=high,'SUBTASK_PERIOD_INVALID')
            if high<start or low>end:continue
            # Only exact, documented scope is adopted; partial periods are not guessed.
            require((low,high)==(start,end),'SUBTASK_PERIOD_REQUIRES_RECONCILIATION')
        version=row.get('version')
        require(type(version) is int and version>=0,'SUBTASK_VERSION_REQUIRED')
        candidates.append(row)
    if not candidates:
        require(main!=2,'INCONSISTENT_TERMINAL_STATUS')
        require(not rows,'NO_CURRENT_SUBTASK_REQUIRES_RECONCILIATION')
        return dict(main=main,sub=None,failed=False,ready=False,instance=None)
    periods={(r.get('schedStart'),r.get('schedEnd')) for r in candidates}
    require(len(periods)==1,'MULTIPLE_PERIODS_REQUIRE_RECONCILIATION')
    latest=max(r['version'] for r in candidates)
    current=[r for r in candidates if r['version']==latest]
    require(len(current)==1,'AMBIGUOUS_CURRENT_SUBTASK_REQUIRES_RECONCILIATION')
    row=current[0];sub=row.get('status');error=row.get('errCode')
    require(type(sub) is int and sub in (0,1,2,3),'SUBTASK_SCHEMA_ERROR')
    require(error is None and sub in (0,1) or type(error) is int,'SUBTASK_ERROR_CODE_INVALID')
    failed=sub==3 or error not in (None,0)
    require(failed or main!=2 or sub==2,'INCONSISTENT_TERMINAL_STATUS')
    require(failed or main!=0 or sub==0,'INCONSISTENT_INITIAL_STATUS')
    ready=not failed and sub==2
    count=row.get('dataCount') if ready else None
    if ready:require(type(count) is int and count>=0,'TASK_COUNT_MISSING_OR_INVALID')
    return dict(main=main,sub=sub,failed=failed,ready=ready,expected=count,
                instance=dict(id=str(row['id']),version=latest,schedStart=row.get('schedStart'),schedEnd=row.get('schedEnd')))
