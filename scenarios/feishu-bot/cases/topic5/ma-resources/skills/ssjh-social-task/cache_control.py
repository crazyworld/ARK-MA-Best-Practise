"""Verified local reuse. This module never calls a supplier endpoint."""
import contextlib
from pathlib import Path
import sqlite3
import durable as d
from errors import SkillError
from transfer import PageStore, export_artifacts, fingerprint
from data_validation import validate_records

from xlsx_text import TABLE_CONTRACT, LEGACY_TABLE_CONTRACT

def require(value, code):
    if not value:
        raise SkillError(code, 'cache', next_action='保留缓存并核对完整性；重新导出只使用通过校验的原始数据，不重新取数。')

def checked_file(path, digest):
    path=Path(path)
    require(path.is_file(), 'CACHE_FILE_MISSING')
    require(d.digest(path)==digest, 'CACHE_FILE_HASH_MISMATCH')

def spec_for(store, output, template):
    return fingerprint(dict(binding=store.binding,pages=store.m['pages'],output=str(Path(output).resolve()),
                            template=template,table_contract=TABLE_CONTRACT))

def verify(run, contract, table=True):
    require(run.state.get('state')=='COMPLETED', 'CACHE_NOT_COMPLETED')
    require(run.state.get('contract')==contract, 'CACHE_QUERY_OR_PROVIDER_CHANGED')
    require((run.path/'pages/manifest.json').is_file(), 'CACHE_PAGE_MANIFEST_MISSING')
    store=PageStore(run.path,run.state['job_id'],run.state['task_count'],run.state['max_records'],contract['provider'])
    require(store.m.get('complete') is True and store.m.get('pending') is None, 'CACHE_PAGES_NOT_COMPLETE')
    with contextlib.closing(sqlite3.connect(':memory:')) as conn: store.rebuild_index(conn)
    from quantity_control import review
    review(run,store.m['count'])
    result=d.read_json(run.path/'result.json')
    require(result==run.state.get('result'), 'CACHE_RESULT_BINDING_MISMATCH')
    require(result.get('job_id')==run.state['job_id'] and result.get('task_count')==store.m['count']
            and result.get('downloaded')==store.m['count'], 'CACHE_COUNT_BINDING_MISMATCH')
    quality=result['quality'];quality_path=run.path/'validation.json'
    require(Path(quality['report']).resolve()==quality_path, 'CACHE_QUALITY_PATH_MISMATCH')
    checked_file(quality_path,quality['sha256'])
    report=d.read_json(quality_path)
    require(report.get('status') in {'PASS','PASS_WITH_WARNINGS'} and report['status']==quality['status'], 'CACHE_QUALITY_FAILED')
    require(report.get('binding')==dict(job_id=run.state['job_id'],pages_fingerprint=fingerprint(store.m['pages']),
        query_fingerprint=fingerprint(contract['canonical_query'])), 'CACHE_QUALITY_BINDING_MISMATCH')
    manifest=d.read_json(run.path/'export.json')
    require(manifest.get('state')=='PUBLISHED', 'CACHE_EXPORT_NOT_PUBLISHED')
    artifacts=manifest.get('artifacts',[])
    require(len(artifacts)==2 and [{k:v for k,v in a.items() if k!='temp'} for a in artifacts]==result.get('artifacts'), 'CACHE_ARTIFACT_BINDING_MISMATCH')
    require(Path(result['raw_data']).resolve()==run.path/'records.json'
            and [Path(a['target']).resolve() for a in artifacts]==[run.path/'records.json',Path(result['output']).resolve()], 'CACHE_ARTIFACT_PATH_MISMATCH')
    for artifact in artifacts if table else artifacts[:1]:
        require(artifact['count']==store.m['count'],'CACHE_ARTIFACT_COUNT_MISMATCH')
        checked_file(artifact['target'],artifact['sha256'])
    return store, result, manifest

def reuse_or_export(run, contract, output, template, explicit_export=False):
    store,result,original=verify(run,contract,table=False)
    spec=spec_for(store,output,template)
    legacy=fingerprint(dict(binding=store.binding,pages=store.m['pages'],output=str(Path(output).resolve()),template=template,table_contract=LEGACY_TABLE_CONTRACT))
    if original['spec'] in (spec,legacy):
        verify(run,contract,table=True)
        return dict(result,quantity_review=run.state.get('quantity_review'),cache_hit=True,cache_verified=True,local_export=False)
    # A different export spec can rebuild a missing old table, but never replace it.
    require(Path(output).resolve()!=Path(result['output']).resolve(), 'EXPORT_CHANGED_REQUIRES_NEW_OUTPUT_PATH')
    folder=run.path/'exports'/spec
    manifest_path=folder/'export.json'
    hit=manifest_path.is_file() and d.read_json(manifest_path).get('state')=='PUBLISHED'
    quality_report=validate_records(store.iter_rows(),run.state['task_count'],run.state['estimated_volume'],
        contract['canonical_query'],[x[0] for x in template] if template else None,
        deviation_policy=run.state.get('quantity_policy'),estimate_relation=run.state.get('estimate',{}).get('relation','MATCHED'))
    require(quality_report['status']!='FAIL','CACHE_REEXPORT_VALIDATION_FAILED')
    quality_report['binding']=dict(job_id=run.state['job_id'],pages_fingerprint=fingerprint(store.m['pages']),
                                   query_fingerprint=fingerprint(contract['canonical_query']))
    if hit:
        quality_path=folder/'validation.json'
        require(quality_path.is_file(),'CACHE_DERIVED_QUALITY_MISMATCH')
        previous=d.read_json(quality_path)
        def comparable(report):
            value={k:v for k,v in report.items() if k!='checked_at'}
            value['issues']=sorted(value['issues'],key=lambda x:(x['code'],str(x.get('field'))))
            return value
        require(comparable(previous)==comparable(quality_report),'CACHE_DERIVED_QUALITY_MISMATCH')
        quality_report=previous
    else:
        d.write_json(folder/'validation.json',quality_report)
    exports=export_artifacts(folder,store,output,template)
    quality=dict(status=quality_report['status'],report=str(folder/'validation.json'),sha256=d.digest(folder/'validation.json'),
        dictionary_version=quality_report['dictionary_version'],error_occurrences=quality_report['error_occurrences'],warning_occurrences=quality_report['warning_occurrences'])
    return dict(result,quantity_review=run.state.get('quantity_review'),output=str(output),artifacts=[{k:v for k,v in a.items() if k!='temp'} for a in exports['artifacts']],
                quality=quality,cache_hit=hit,cache_verified=True,local_export=True)
