"""Black-box CLI E2E audit against loopback HTTP. Never uses supplier credentials.

Runs this Skill unchanged in separate Python processes. Output must be a new external directory. All records, prices,
tokens, job identifiers and acknowledgements are synthetic test fixtures.
"""
import copy, csv, datetime, hashlib, http.server, json, os, pathlib, subprocess, sys, threading, time, traceback, urllib.parse
import yaml

import argparse
SOURCE=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from version import VERSION
SAMPLE_DAY=(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()-datetime.timedelta(days=1)).isoformat()
parser=argparse.ArgumentParser(description='Offline synthetic CLI acceptance; takes about 2-4 minutes, no supplier calls.')
parser.add_argument('--output',required=True,help='New directory outside the Skill; existing directories are rejected')
args=parser.parse_args()
OUT=pathlib.Path(args.output).expanduser().resolve()
if OUT.is_relative_to(SOURCE) or OUT==SOURCE or OUT.exists():
    parser.error('Use a new output directory outside the Skill')

def digest(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(pathlib.Path(p).read_text(encoding='utf-8-sig'))
def write(p,value):
    p=pathlib.Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def yaml_write(p,value):pathlib.Path(p).write_text(yaml.safe_dump(value,allow_unicode=True),encoding='utf-8')

class FakeSupplier(http.server.BaseHTTPRequestHandler):
    calls=[];jobs={};ready=False;transient_status_failures=0
    rows=[dict(id=f'synthetic-{i}',content='SYNTHETIC_CONTENT_SENTINEL',publish_time=SAMPLE_DAY+'T04:00:00Z',like_cnt=i if i%3==0 else None) for i in range(125)]
    def log_message(self,*args):pass
    def reply(self,data,status=200):
        raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def track(self,path):
        self.calls.append(dict(method=self.command,path=path,at=time.time()))
        write(OUT/'http-calls.json',self.calls)
    def do_POST(self):
        path=urllib.parse.urlsplit(self.path).path;self.track(path)
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        if path=='/api/socialmedia/totalVolume':self.reply(dict(success=True,code=200,data=dict(total=len(self.rows))))
        elif path=='/auth/obtain':self.reply(dict(success=True,data='SYNTHETIC_DYNAMIC_TOKEN'))
        elif path=='/project/job/add':
            fields=urllib.parse.parse_qs(body.decode());job=900001+len(self.jobs)
            self.jobs[job]=dict(id=job,name=fields['name'][0])
            self.reply(dict(success=True,data=job))
        else:self.reply(dict(success=False,code=404),404)
    def do_GET(self):
        parsed=urllib.parse.urlsplit(self.path);path=parsed.path;self.track(path)
        query=urllib.parse.parse_qs(parsed.query)
        if path=='/project/job/status/detail':
            if self.transient_status_failures:
                type(self).transient_status_failures-=1;self.reply(dict(success=False,code=503),503);return
            job=int(query['id'][0]);data=dict(self.jobs[job],status=1,
                jobInstStatusReport=[dict(id=1,version=0,errCode=0,jobId=job,status=2 if self.ready else 1,dataCount=len(self.rows) if self.ready else 0)])
            self.reply(dict(success=True,data=data))
        elif path=='/api/count':self.reply(dict(success=True,data=dict(total=len(self.rows))))
        elif path=='/api/search':
            start=int(query.get('scrollId',['0'])[0]);n=int(query.get('size',['100'])[0]);batch=self.rows[start:start+n]
            cursor=str(start+len(batch)) if start+len(batch)<len(self.rows) else None
            self.reply(dict(success=True,data=dict(docs=batch,scrollId=cursor)))
        else:self.reply(dict(success=False,code=404),404)

def main():
    OUT.mkdir(parents=True)
    CTRL=OUT/'harness';CTRL.mkdir()
    source=SOURCE;entry=source/'ssjh_social_task.py'
    frozen={str(p.relative_to(source)):digest(p) for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    cfg=yaml.safe_load((source/'config.yaml').read_text(encoding='utf-8-sig'))
    cfg['task']['request_id']='';cfg['profiler']['keywords']='SYNTHETIC_QUERY_SENTINEL'
    cfg['search']['source']='weibo';cfg['search']['weibo']['msg_type']=1
    cfg['search']['start_date']=cfg['search']['end_date']=SAMPLE_DAY
    cfg['precheck']={'uncertainty_acceptance_ref':'SYNTHETIC_ACCEPTANCE'}
    cfg['observability']=dict(delivery_mode='intermediate',sample_kind='test')
    cfg['output']=dict(file=str(OUT/'records.csv'),template=str(source/'template.yaml'))
    config=OUT/'task.yaml';yaml_write(config,cfg)
    api=yaml.safe_load((source/'api_config.yaml').read_text(encoding='utf-8-sig'))
    api['credentials']=dict(username='synthetic-user',password='[REDACTED_FOR_ARCHIVE]',fixed_token='SYNTHETIC_FIXED_TOKEN')
    api['es']=dict(host='synthetic-local',cluster='synthetic',index='synthetic')
    api['runtime'].update(cache_token=False,poll_interval=120)
    private=CTRL/'synthetic-private';private.mkdir(exist_ok=True)
    write(private/'prices.json',dict(schema_version=1,currency='CNY',stat_success='0.01',record_rates={p:'0.01' for p in ['weibo','xiaohongshu','news','forum','kuaishou','shipinhao']}))
    env={k:v for k,v in os.environ.items() if not k.startswith('SSJH_')}
    env.update(SSJH_PRICING_FILE=str(private/'prices.json'),SSJH_BUDGET_DIR=str(private),NO_PROXY='127.0.0.1,localhost')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),FakeSupplier)
    endpoint=f'http://127.0.0.1:{server.server_port}'
    api['api']=dict(base_url=endpoint,count_base_url=endpoint+'/api');api_file=OUT/'api-synthetic.yaml';yaml_write(api_file,api)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    cases=[];commands=[];sequence=0;run=None
    def case(name,condition,detail=None):
        result=dict(name=name,status='PASS' if condition else 'FAIL',detail=detail)
        cases.append(result);write(OUT/'checks-live.json',cases)
        if not condition:raise AssertionError(name)
    def call(action,*extra,expected=0,run_dir=None,config_file=None,cache=False):
        nonlocal sequence
        sequence+=1
        cmd=[sys.executable,'-B','-X','utf8',str(entry),'--action',action,'--config',str(config_file or config),'--api-config',str(api_file),'--registry',str(OUT/'requests.sqlite3')]
        cmd+=['--cache-root',str(OUT/'cache')] if cache else ['--run-dir',str(run_dir or run or OUT/'plan-run')]
        cmd+=list(map(str,extra));started=time.time()
        p=subprocess.run(cmd,cwd=OUT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=50)
        prefix=OUT/'commands'/f'{sequence:02d}-{action}';prefix.parent.mkdir(exist_ok=True)
        prefix.with_suffix('.jsonl').write_text(p.stdout,encoding='utf-8');prefix.with_suffix('.stderr.txt').write_text(p.stderr,encoding='utf-8')
        items=[json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        commands.append(dict(sequence=sequence,action=action,exit_code=p.returncode,elapsed_seconds=time.time()-started,stdout=str(prefix.with_suffix('.jsonl'))))
        write(OUT/'commands.json',commands)
        assert p.returncode==expected,(action,p.returncode,items[-1] if items else p.stderr)
        assert items and all(x['skill_version']==VERSION for x in items)
        return items[-1]
    def snapshot(name):
        for rel in ['state.json','logs/timing.json','logs/progress.json','logs/deliveries.json']:
            write(OUT/'snapshots'/name/rel,read(run/rel))
    try:
        call('plan');case('plan_no_http',not FakeSupplier.calls)
        first=call('execute',cache=True);run=next((OUT/'cache').glob('*/state.json')).parent
        case('precheck_waits_for_confirmation',first['state']=='AWAITING_QUANTITY_CONFIRMATION' and len(FakeSupplier.jobs)==0)
        state=read(run/'state.json');rid=state['request_id'];assert rid
        event=call('notifications')['data']['events'][0]
        call('notifications');case('notification_read_does_not_ack',read(run/'state.json')['observability']['notifications'][-1]['status']=='pending')
        call('ack-notification','--event-id',event['event_id'],'--delivery-ref','SYNTHETIC_CONSUMED_PRECHECK')
        case('notification_ack_persists_across_processes',not call('notifications')['data']['events'])
        created=call('execute','--max-records','125','--confirmation-ref','SYNTHETIC_SCOPE_CONFIRMATION',cache=True)
        created_at=time.time();case('created_once_and_returned_job',created['job_id'] is not None and len(FakeSupplier.jobs)==1)
        before=len(FakeSupplier.calls);call('execute',cache=True)
        case('execute_respects_120s_poll_interval',len(FakeSupplier.calls)==before)
        FakeSupplier.transient_status_failures=1
        call('status','--refresh')
        case('transient_status_retry_keeps_same_job',len([x for x in FakeSupplier.calls if x['path']=='/project/job/status/detail'])==2 and len(FakeSupplier.jobs)==1)
        notice=call('notifications')['data']['events'][0]
        call('ack-notification','--event-id',notice['event_id'],'--delivery-ref','SYNTHETIC_CONSUMED_RUNNING')
        call('status','--refresh');case('identical_status_does_not_repeat_notice',not call('notifications')['data']['events'])
        snapshot('running')
        # Exercise the real interval without changing the clock, source or state.
        target=dt_epoch(read(run/'state.json')['last_poll_attempt_at'])+120.2
        print(json.dumps(dict(progress='waiting_real_poll_interval',seconds=round(max(0,target-time.time()),1))),flush=True)
        while time.time()<target:time.sleep(min(.5,target-time.time()))
        FakeSupplier.ready=True
        done=call('execute',cache=True)
        case('whole_execute_flow_completed',done['state']=='COMPLETED' and done['ok'])
        result=read(run/'result.json');case('125_records_and_two_pages',result['downloaded']==125 and len(read(run/'pages/manifest.json')['pages'])==2)
        with (OUT/'records.csv').open(encoding='utf-8-sig',newline='') as f:table=list(csv.reader(f))
        case('csv_export_verified',len(table)==126 and all(digest(a['target'])==a['sha256'] for a in result['artifacts']))
        timing=read(run/'logs/timing.json');case('timing_creation_to_verified_delivery_recorded',timing['creation_started_epoch'] is not None and timing['completed_at'] is not None and timing['create_to_verified_delivery_seconds']>=120)
        case('test_sample_excluded_from_future_estimator',not timing['eligible_for_future_estimation'] and 'NON_NORMAL_SAMPLE' in timing['exclusion_reasons'])
        case('initial_estimate_recorded_without_retraining',timing['initial_estimate']['max_seconds']==1200 and timing['estimation_updated'] is False)
        case('intermediate_delivery_not_forced_to_user',done['audience']=='workflow' and not done['user_notification_required'])
        delivery=read(run/'logs/deliveries.json')['records'];case('verified_intermediate_delivery_logged',len(delivery)==1 and delivery[0]['export_verified'] and delivery[0]['outputs']['downloaded']==125)
        pending=call('notifications')['data']['events'];case('latest_completion_notice_only',len(pending)==1 and pending[0]['state']=='COMPLETED')
        snapshot('completed')
        count=len(FakeSupplier.calls);call('execute',cache=True)
        after=read(run/'logs/timing.json');d2=read(run/'logs/deliveries.json')['records']
        case('cache_reuse_no_http_no_duplicate_timing',len(FakeSupplier.calls)==count and after['completed_at']==timing['completed_at'] and after['sample_id']==timing['sample_id'])
        case('cache_reuse_updates_same_delivery',len(d2)==1 and d2[0]['verification_count']==2)
        cfg2=copy.deepcopy(cfg);cfg2['output']['file']=str(OUT/'records.xlsx');conf2=OUT/'task-xlsx.yaml';yaml_write(conf2,cfg2)
        call('export',config_file=conf2)
        case('xlsx_reexport_no_http_and_separate_delivery',len(FakeSupplier.calls)==count and len(read(run/'logs/deliveries.json')['records'])==2 and (OUT/'records.xlsx').is_file())
        current_event=call('notifications')['data']['events'][0]
        ack=['--event-id',current_event['event_id'],'--delivery-ref','SYNTHETIC_CONSUMED_COMPLETED']
        call('ack-notification',*ack);before=digest(run/'state.json');call('ack-notification',*ack)
        case('ack_is_idempotent',digest(run/'state.json')==before)
        call('ack-notification','--event-id','unknown','--delivery-ref','SYNTHETIC_BAD_ACK',expected=2)
        case('bad_ack_does_not_pollute_acquisition',digest(run/'state.json')==before)
        (run/'logs/timing.json').write_text('INTENTIONAL_TEST_CORRUPTION',encoding='utf-8')
        call('plan');case('log_view_restored_from_state',read(run/'logs/timing.json')['completed_at']==timing['completed_at'])
        original=(OUT/'records.csv').read_bytes();(OUT/'records.csv').write_text('INTENTIONAL_TEST_CORRUPTION',encoding='utf-8')
        rows_before=read(run/'logs/deliveries.json')['records'];failed=call('execute',cache=True,expected=2)
        case('bad_cache_blocks_without_new_delivery_or_http',failed['code']=='CACHE_FILE_HASH_MISMATCH' and len(FakeSupplier.calls)==count and read(run/'logs/deliveries.json')['records']==rows_before)
        (OUT/'records.csv').write_bytes(original);call('execute',cache=True)
        case('cache_reverification_supersedes_error_notice',call('notifications')['data']['events'][0]['kind']=='recovered')
        logs=''.join(p.read_text(encoding='utf-8') for p in (run/'logs').glob('*.json'))
        case('three_logs_exclude_content_query_credentials_and_costs',all(x not in logs for x in ['SYNTHETIC_QUERY_SENTINEL','SYNTHETIC_CONTENT_SENTINEL','SYNTHETIC_PASSWORD','SYNTHETIC_FIXED_TOKEN','actual_cost','record_rates','confirmation_ref']))
        # A distinct synthetic request starts in user mode to verify routing.
        usercfg=copy.deepcopy(cfg);usercfg['task']['request_id']='e2e-user-mode';usercfg['observability']['delivery_mode']='user';usercfg['output']['file']=str(OUT/'user.csv')
        userconf=OUT/'task-user.yaml';yaml_write(userconf,usercfg);userrun=OUT/'user-run'
        call('execute',config_file=userconf,run_dir=userrun)
        usernotice=call('notifications',run_dir=userrun)
        case('explicit_user_mode_routes_to_user',usernotice['user_notification_required'] and usernotice['data']['events'][0]['audience']=='user')
        call('execute','--max-records','125','--confirmation-ref','SYNTHETIC_USER_CONFIRMATION',config_file=userconf,run_dir=userrun)
        call('download',config_file=userconf,run_dir=userrun)
        case('explicit_user_delivery_completed',read(userrun/'logs/deliveries.json')['records'][0]['audience']=='user')
        snapshot('final')
        case('source_package_unchanged',all(digest(source/p)==sha for p,sha in frozen.items()))
        case('main_request_never_recreated',len(FakeSupplier.jobs)==2 and read(run/'state.json')['request_id']==rid)
    except Exception as exc:
        write(OUT/'runner-error.json',dict(type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc()))
        cases.append(dict(name='runner_completion',status='FAIL',detail=str(exc)))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        summary=dict(version=VERSION,source_sha256=frozen,
            mode='unmodified CLI subprocesses, loopback HTTP synthetic service',
            checks=cases,passed=sum(x['status']=='PASS' for x in cases),failed=sum(x['status']=='FAIL' for x in cases),
            commands=len(commands),synthetic_jobs=len(FakeSupplier.jobs),real_supplier_calls=0,paid_calls=0,
            notifications_sent=0,host_runtime=sys.platform+' Python CLI',agent_runtime='NOT_TESTED',server_stopped=not thread.is_alive())
        write(OUT/'RESULTS.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
    return int(bool(summary['failed']))

def dt_epoch(value):
    import datetime
    return datetime.datetime.fromisoformat(value).timestamp()

if __name__=='__main__':sys.exit(main())
