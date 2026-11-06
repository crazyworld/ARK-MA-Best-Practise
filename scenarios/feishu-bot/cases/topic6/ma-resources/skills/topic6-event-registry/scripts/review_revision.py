"""Audited, model-free final review revision; never rewrite source run receipts."""
import argparse
import csv
from pathlib import Path
import shutil
import time
import uuid
from c2_runtime import atomic_json, exclusive_lock, file_hash
from c2_quality import inspect_ownership
from relay import read_jsonl, write_jsonl
from retain_original import retain_original


def validate_plan(plan, request, events):
    if plan.get('source_review_id') != request['id']:
        raise ValueError('Review plan does not match source evidence')
    if not str(plan.get('reviewer','')).strip() or not str(plan.get('note','')).strip():
        raise ValueError('Actual reviewer and review scope required')
    by_id={e['event_id']:e for e in events}; seen=set(); forced={}
    for row in plan.get('retain_events',[]):
        eid=row['event_id']; ids=row.get('member_record_ids',[])
        if eid not in by_id or eid in seen or not row.get('reason','').strip():
            raise ValueError('Unknown/duplicate event or empty retention reason')
        if len(ids)!=len(set(ids)) or set(ids)!=set(by_id[eid]['member_record_ids']):
            raise ValueError('Retention plan must bind the complete event membership')
        seen.add(eid)
        for rid in ids:forced[rid]=row['reason']
    if not forced:
        raise ValueError('A revision requires concrete retention decisions')
    return forced


def main(argv=None):
    from run_c2 import Pipeline, VERSION, SCRIPTS, read, matches, hashes, copy_payload
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-run',type=Path,required=True)
    ap.add_argument('--run-dir',type=Path,required=True)
    ap.add_argument('--retention-plan',type=Path,required=True)
    a=ap.parse_args(argv);root=a.run_dir.resolve();source=a.source_run.resolve()
    if root==source or root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError('Revision must use a separate directory')
    if root.exists():
        raise ValueError('Revision directory already exists; preserve it and use a new directory')
    start=time.monotonic();root.mkdir(parents=True);state={'version':VERSION,'status':'RUNNING','trace':[]}
    atomic_json(root/'result.json',state)
    with exclusive_lock(root/'.c2.lock'):
        try:
            old=read(source/'config.json');old_state=read(source/'result.json')
            if old_state.get('status')!='WAITING_REVIEW' or old_state.get('gate')!='event_names':
                raise ValueError('Only a completed computation waiting for final review can be revised')
            if old.get('version') not in {'3.0.0-rc.13','3.0.0-rc.14'}:
                raise ValueError('Unsupported upstream version')
            if old.get('previous_registry'):
                raise ValueError('Final review revision currently supports current-window runs only')
            # Only the orchestration and final retention implementation changed.
            # Every other executed computation script must be byte-identical.
            for name,sha in old['code'].items():
                if name not in {'run_c2.py','retain_original.py','review_revision.py'} and file_hash(SCRIPTS/name)!=sha:
                    raise ValueError('Upstream computation code changed: '+name)
            req=read(source/'review_requests/event_names.json');parent=Path(req['directory'])
            receipt=read(parent/'receipt.json')
            if receipt['status']!='COMPLETE' or not matches(parent,receipt['outputs']) or not matches(parent,req['files']):
                raise ValueError('Source computation/review material changed')
            for gate in ('cleaning','non_events'):
                g=read(source/f'review_requests/{gate}.json');ack=read(source/'reviews'/(g['id']+'.json'))
                if not ack.get('approved') or not matches(Path(g['directory']),g['files']):
                    raise ValueError('Source front review incomplete or changed')
            plan=read(a.retention_plan);events=list(read_jsonl(parent/'work/block_events.jsonl'))
            forced=validate_plan(plan,req,events)
            with (parent/'work/source.csv').open(encoding='utf-8-sig',newline='') as h:rows=list(csv.DictReader(h))
            quality=inspect_ownership(rows,events,list(read_jsonl(parent/'work/eventness.jsonl')))
            if quality['technical_missing_ids'] or quality['degraded_ids']:
                raise ValueError('Technical failures cannot be settled by semantic review')
            cfg={**old,'version':VERSION,'run_dir':str(root),
                 'code':{p.name:file_hash(p) for p in SCRIPTS.glob('*.py')},
                 'references':{p.relative_to(SCRIPTS.parent).as_posix():file_hash(p) for p in (SCRIPTS.parent/'references').rglob('*') if p.is_file()},
                 'review_revision':{'source_run':str(source),'source_config_sha256':file_hash(source/'config.json'),
                                   'source_review_id':req['id'],'plan_sha256':file_hash(a.retention_plan),
                                   'upstream_version':old['version'],'upstream_reused_not_cold_start':True}}
            atomic_json(root/'config.json',cfg);initial=root/'input_snapshot';initial.mkdir()
            copy_payload(parent,initial);atomic_json(initial/'receipt.json',hashes(initial))
            atomic_json(root/'review-retention-plan.json',plan)
            pipe=Pipeline(root,cfg)
            current=pipe.node('review_retention',initial,action=lambda d:retain_original(d,cfg['window'],forced))
            if cfg['mode']=='single':current=pipe.script('ranking',current,'08_rank_events.py')
            current=pipe.script('registry',current,'09_commit_registry.py','--window',cfg['window'])
            if cfg.get('previous_registry'):
                def history(d):
                    previous=list(read_jsonl(parent/'out/registry_snapshot.jsonl'))
                    now=list(read_jsonl(d/'out/registry_snapshot.jsonl'));ids={e['event_id'] for e in now}
                    source_events={e['event_id'] for e in events}
                    now.extend(e for e in previous if e['event_id'] not in source_events and e['event_id'] not in ids)
                    write_jsonl(d/'out/registry_snapshot.jsonl',now)
                current=pipe.node('carry_history',current,action=history)
            current=pipe.script('detail',current,'x4_detail_table.py','--source',initial/'work/source.csv','--strict-quality')
            with (current/'out/human_review.csv').open(encoding='utf-8-sig',newline='') as h:
                if list(csv.DictReader(h)):raise ValueError('Remaining flagged names require actual review/retention')
            def finalize(d):
                q=read(d/'out/c2_quality.json');ret=read(d/'out/original_retained.json')
                current_events={e['event_id']:e for e in read_jsonl(d/'work/block_events.jsonl')}
                snapshot=list(read_jsonl(d/'out/registry_snapshot.jsonl'))
                for row in snapshot:
                    if row['event_id'] in current_events:
                        row['identity_core']=current_events[row['event_id']].get('identity_core') or {}
                        row['embedding_model']=cfg['embedding_model'];row['embedding_revision']=cfg['embedding_revision']
                write_jsonl(d/'out/registry_snapshot.jsonl',snapshot)
                q.update(review_completion='COMPLETED_FOR_THIS_RUN',semantic_quality='NOT_BENCHMARKED',
                    business_disposition='ACCEPTED_WITH_ORIGINALS',retention_policy='preserve-original-after-review',
                    original_retained_count=ret['retained_count'],unresolved_count=ret['unresolved_count'],
                    related_records_split_count=ret['related_records_split_count'],manual_retained_count=ret['manual_retained_count'])
                q['evidence_pending_ids']=[r['record_id'] for r in read_jsonl(d/'work/front_evidence_pending.jsonl')]
                atomic_json(d/'out/c2_quality.json',q)
                atomic_json(d/'out/review_revision_audit.json',{'plan':plan,'source_review':req,'revision':cfg['review_revision']})
                atomic_json(d/'out/c2_provenance.json',{'version':VERSION,'review_revision':cfg['review_revision'],
                    'input_sha256':cfg['source_sha256'],'mode':cfg['mode'],'upstream_stage_receipts':old_state['trace'],
                    'stage_receipts':pipe.trace,'model_calls_in_revision':0})
                if (d/'run_manifest.json').exists():shutil.copyfile(d/'run_manifest.json',d/'out/c2_run_metrics.json')
            current=pipe.node('finalize',current,action=finalize)
            output_hashes={p.relative_to(current/'out').as_posix():file_hash(p) for p in (current/'out').rglob('*') if p.is_file()}
            shutil.copytree(current/'out',root/'out')
            state.update(status='COMPLETE',outputs=output_hashes,output_dir=str(root/'out'),final_stage=str(current),
                input_records=len(rows),review_gates='COMPLETED',review_revision=cfg['review_revision'],
                semantic_evaluation='NOT_A_BENCHMARK',performance_evaluation='REVIEW_REVISION_REUSES_UPSTREAM_COMPUTATION')
        except Exception as exc:
            state.update(status='FAILED',error_type=type(exc).__name__,error=str(exc))
        finally:
            state['trace']=pipe.trace if 'pipe' in locals() else []
            state['wall_seconds']=time.monotonic()-start
            atomic_json(root/'history'/(uuid.uuid4().hex+'.json'),state);atomic_json(root/'result.json',state)
    print(__import__('json').dumps({k:v for k,v in state.items() if k not in {'trace','outputs'}},ensure_ascii=False))
    return 0 if state['status']=='COMPLETE' else 2


if __name__=='__main__':raise SystemExit(main())
