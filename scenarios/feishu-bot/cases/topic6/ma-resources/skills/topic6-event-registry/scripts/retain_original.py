"""After semantic stages succeed, preserve unresolved topics without invented identity."""
import csv
import json
from c2_runtime import atomic_json, digest, unique_ids
from c2_quality import inspect_ownership
from f1_extract import unknown_frame
from relay import read_jsonl, write_jsonl


def retain_original(root, window, forced=None, allow_degraded=False):
    forced = forced or {}
    previous_path = root / 'out/original_retained.json'
    previous = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else {}
    prior_rows = {r['record_id']: r for r in previous.get('records', [])}
    with (root / 'work/source.csv').open(encoding='utf-8-sig', newline='') as handle:
        source = list(csv.DictReader(handle))
    events = list(read_jsonl(root / 'work/block_events.jsonl'))
    eventness = list(read_jsonl(root / 'work/eventness.jsonl'))
    quality = inspect_ownership(source, events, eventness)
    # An incomplete execution cannot be relabelled as semantic uncertainty.
    # MA degraded-policy=accept：技术降级记录在 c2_quality，不在此阻断（降级行不会被标为未决保留）
    if not allow_degraded and (quality['technical_missing_ids'] or quality['degraded_ids']):
        raise ValueError('Original retention requires complete, non-degraded execution')
    by_id = {r['record_id']: r for r in source}
    judgments = {r['record_id']: r for r in eventness}
    if set(judgments) != set(by_id):
        raise ValueError('Original retention requires complete eventness coverage')
    if set(forced)-set(by_id) or any(judgments[r]['eventness']=='NON_EVENT' for r in forced):
        raise ValueError('Manual retention cannot create records or reverse excluded records')
    pending = {r['record_id']: r for r in eventness
               if r.get('needs_evidence') or r['eventness'] == 'UNCERTAIN'}
    if any(r['eventness'] == 'NON_EVENT' for r in pending.values()):
        raise ValueError('Unresolved NON_EVENT must be reconsidered before exclusion')
    result, audit, removed = [], [], set()
    for event in events:
        if event.get('retention_status')=='ORIGINAL_RETAINED' and len(event['member_record_ids'])==1:
            rid=event['member_record_ids'][0]
            if rid not in prior_rows:
                raise ValueError('Retained original missing prior audit')
            result.append(event); audit.append(prior_rows[rid]); continue
        affected = sorted(set(event['member_record_ids']) & (set(pending)|set(forced)))
        if not affected:
            result.append(event)
            continue
        # The group's synthesized name/state cannot safely be inherited by a
        # subset. Keep every member verbatim, leaving unrelated groups unchanged.
        removed.add(event['event_id'])
        for rid in event['member_record_ids']:
            row = by_id[rid]
            reason = (pending[rid].get('front_evidence_reason') or pending[rid].get('reason_code')
                      or 'INSUFFICIENT_INFO') if rid in pending else 'GROUP_DEPENDS_ON_UNRESOLVED_MEMBER'
            if rid in forced: reason='MANUAL_REVIEW_RETENTION: '+forced[rid]
            note = '归档复核后保留原热点，未确认合并关系；' + reason
            eid = 'RAW_' + digest({'window': window, 'record_id': rid})[:24]
            result.append(dict(event_id=eid, block_id=event.get('block_id', ''),
                event_name=row['title'], anchor_title=row['title'], event_description='',
                canonical_anchor_record_id=rid, member_record_ids=[rid], member_count=1,
                singleton=True, origin='original_retained', retention_status='ORIGINAL_RETAINED',
                anchor='', anchor_type='', series_instance=None, facets=[], identity_core={},
                confidence=None, member_confidence={rid: None}, member_notes={rid: note},
                name_check={'status': 'SINGLETON_VERBATIM'},
                state={'latest_stage': None, 'member_count': 1, 'key_facts': {},
                       'platforms': [row['platform']]}))
            audit.append(dict(record_id=rid, title=row['title'], platform=row['platform'],
                original_event_id=event['event_id'], reason=reason, unresolved=rid in pending,
                related_unresolved_ids=affected, eventness=judgments[rid]['eventness'],
                manual_review_retained=rid in forced,
                evidence_ids=judgments[rid].get('front_evidence_ids', [])))
    unique_ids(result, 'event_id')
    inspect_ownership(source, result, eventness)
    retained = {r['record_id'] for r in audit}
    for filename in ('frames.jsonl', 'frames_resolved.jsonl'):
        path = root / 'work' / filename
        if path.exists():
            frames = list(read_jsonl(path))
            # Keep heat and source metadata; clear unverified semantic assertions.
            frames = [{**unknown_frame(), **{k: f[k] for k in
                       ('record_id', 'platform', 'heat', 'front_evidence_ids') if k in f},
                       'title': by_id[f['record_id']]['title'],
                       'raw_title': by_id[f['record_id']]['title'], 'information_score': 0,
                       'needs_evidence': True} if f['record_id'] in retained else f for f in frames]
            write_jsonl(path, frames)
    relations_path = root / 'work/block_relations.jsonl'
    relations = list(read_jsonl(relations_path)) if relations_path.exists() else []
    dropped = [r for r in relations if r.get('event_a') in removed or r.get('event_b') in removed]
    write_jsonl(relations_path, [r for r in relations if r not in dropped])
    write_jsonl(root / 'work/block_events.jsonl', result)
    atomic_json(root / 'out/original_retained.json', dict(schema='c2-original-retention/1',
        policy='preserve-original-after-review', unresolved_count=len(pending),
        retained_count=len(audit), records=audit, removed_relations=previous.get('removed_relations',[])+dropped,
        manual_retained_count=sum(bool(r.get('manual_review_retained')) for r in audit),
        related_records_split_count=sum(not r['unresolved'] and not r.get('manual_review_retained') for r in audit),
        semantic_accuracy='NOT_MEASURED'))
    return audit
