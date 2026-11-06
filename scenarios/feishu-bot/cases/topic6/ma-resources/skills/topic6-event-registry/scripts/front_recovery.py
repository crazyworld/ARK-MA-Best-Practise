"""Import only a verified pre-exclusion snapshot into a new, separately audited run."""
import json
from pathlib import Path
import shutil
from c2_runtime import atomic_json, file_hash, unique_ids
from relay import read_jsonl

FILES = ('work/clean_titles.jsonl','work/eventness.jsonl','work/frames.jsonl',
         'work/f1_repairs.jsonl','out/00_clean_titles.csv','out/01_eventness.csv')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def inspect_seed(root, rows, config):
    root=Path(root).resolve()
    if root==Path(config['run_dir']).resolve() or Path(config['run_dir']).resolve().is_relative_to(root):
        raise ValueError('Revision must use a separate run directory outside the source run')
    previous=read(root/'config.json')
    keys=('source_sha256','frontend','mode','platforms','heat_col','chat_model','chat_revision','window','endpoint_hashes')
    if config['frontend']!='fused' or any(previous.get(k)!=config.get(k) for k in keys):
        raise ValueError('Front seed source/scope/model/endpoint differs from the revision')
    traces=[t for t in read(root/'result.json').get('trace',[]) if t['node'] in {'front_merge','front_reuse'}]
    if len(traces)!=1:
        raise ValueError('Source run has no unique completed front snapshot')
    receipt=Path(traces[0]['receipt']).resolve()
    if not receipt.is_relative_to(root/'stages'):
        raise ValueError('Front seed receipt escapes source run')
    meta=read(receipt); directory=Path(meta['directory']).resolve()
    if meta['status']!='COMPLETE' or not directory.is_relative_to(root/'stages'):
        raise ValueError('Front seed is incomplete or outside source run')
    outputs=meta.get('outputs',{})
    if not outputs or any(not (directory/p).resolve().is_relative_to(directory) or
            not (directory/p).is_file() or file_hash(directory/p)!=v for p,v in outputs.items()):
        raise ValueError('Front seed payload checksum mismatch')
    if any(p not in outputs for p in FILES):
        raise ValueError('Front seed required files missing')
    source={r['record_id']:r for r in rows}
    clean=list(read_jsonl(directory/'work/clean_titles.jsonl'))
    events=list(read_jsonl(directory/'work/eventness.jsonl'))
    frames=list(read_jsonl(directory/'work/frames.jsonl'))
    for records in (clean,events):
        if set(unique_ids(records))!=set(source) or any(r['title']!=source[r['record_id']]['title'] or
                r['clean_title']!=source[r['record_id']]['title'] for r in records):
            raise ValueError('Front seed input/title coverage mismatch')
    if set(unique_ids(frames))!={r['record_id'] for r in events if r['eventness']!='NON_EVENT'}:
        raise ValueError('Front seed Frame coverage mismatch')
    if any(f['title']!=source[f['record_id']]['title'] or f['raw_title']!=source[f['record_id']]['title'] for f in frames):
        raise ValueError('Front seed contains rewritten Frame titles')
    return dict(source_run=str(root),source_version=previous['version'],directory=str(directory),
                receipt=str(receipt),receipt_sha256=file_hash(receipt),config_sha256=file_hash(root/'config.json'),
                outputs=outputs, source_merge_seconds=meta.get('wall_seconds'),
                semantics='Imported original F1 judgments, not new-version F1 inference. No review approval is inherited.')


def copy_seed(seed, target):
    source=Path(seed['directory'])
    if file_hash(Path(seed['receipt']))!=seed['receipt_sha256']:
        raise ValueError('Front seed receipt changed')
    for name in FILES:
        if file_hash(source/name)!=seed['outputs'][name]:
            raise ValueError('Front seed changed before copy')
        (target/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source/name,target/name)
        if file_hash(target/name)!=seed['outputs'][name]:
            raise ValueError('Front seed changed during copy')
    if 'run_manifest.json' in seed['outputs']:
        if file_hash(source/'run_manifest.json')!=seed['outputs']['run_manifest.json']:
            raise ValueError('Front seed ledger changed')
        ledger=read(source/'run_manifest.json')
        for row in ledger.get('cost_ledger',[]):
            row.update(inherited=True,source_run=seed['source_run'])
        atomic_json(target/'run_manifest.json',ledger)
    atomic_json(target/'out/front_seed_provenance.json',seed)
