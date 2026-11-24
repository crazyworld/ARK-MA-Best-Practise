"""Local quality audit; never infer supplier semantics or modify source records."""
import collections
import datetime as dt
import hashlib
import json
import math
import pathlib
import sqlite3
from urllib.parse import urlsplit

from errors import SkillError
from dictionary_platform import identify
from platform_contract import PLATFORMS

HERE = pathlib.Path(__file__).resolve().parent
CN = dt.timezone(dt.timedelta(hours=8))


def load_dictionary():
    path = HERE / 'data_dictionary.json'
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        raise SkillError('DATA_DICTIONARY_UNAVAILABLE_OR_INVALID') from None
    if not isinstance(data, dict):
        raise SkillError('DATA_DICTIONARY_INVALID')
    fields = data.get('fields')
    if data.get('schema_version') != '1.1' or not isinstance(fields, list):
        raise SkillError('DATA_DICTIONARY_INVALID')
    allowed = {'identifier','text','url','timestamp','count','sentiment','opaque_enum','identifier_list','text_list',
               'nonnegative_number','text_or_text_list','json_text_or_object'}
    if not isinstance(data.get('version'), str) or any(not isinstance(f, dict) or not isinstance(f.get('field'), str)
            or not f['field'] or f.get('kind') not in allowed or type(f.get('required')) is not bool for f in fields):
        raise SkillError('DATA_DICTIONARY_INVALID')
    names = [f['field'] for f in fields]
    if len(names) != len(set(names)) or 'id' not in names:
        raise SkillError('DATA_DICTIONARY_INVALID')
    identity = next(f for f in fields if f['field']=='id')
    if identity['kind']!='identifier' or not identity['required']:
        raise SkillError('DATA_DICTIONARY_INVALID')
    profiles=data.get('platforms');evidence=data.get('evidence_catalog')
    if not isinstance(profiles,dict) or set(profiles)!=set(PLATFORMS) or not isinstance(evidence,dict) or not evidence:
        raise SkillError('DATA_DICTIONARY_INVALID')
    for profile in profiles.values():
        if not isinstance(profile,dict) or type(profile.get('sample_records')) is not int or profile['sample_records']<0 or profile.get('evidence_status') not in {'observed_structure_only','no_nonzero_sample'}:
            raise SkillError('DATA_DICTIONARY_INVALID')
        if (profile['sample_records']==0)!=(profile['evidence_status']=='no_nonzero_sample'):
            raise SkillError('DATA_DICTIONARY_INVALID')
    for record in evidence.values():
        if not isinstance(record,dict) or not isinstance(record.get('kind'),str) or not isinstance(record.get('sha256'),str) or len(record['sha256'])!=64 or any(c not in '0123456789abcdef' for c in record['sha256']):
            raise SkillError('DATA_DICTIONARY_INVALID')
    for field in fields:
        refs=field.get('evidence_refs');observations=field.get('platform_evidence')
        if field.get('missing_policy') not in {'block','observe_only','report_when_selected','report_when_selected; preserve missing/null/empty distinction in validation report'} or not isinstance(refs,list) or not refs or any(not isinstance(ref,str) or ref not in evidence for ref in refs) or not isinstance(observations,dict) or not set(observations)<=set(profiles):
            raise SkillError('DATA_DICTIONARY_INVALID')
        if field['required'] and field['field']!='id':
            raise SkillError('DATA_DICTIONARY_INVALID')
        for platform,record in observations.items():
            if not isinstance(record,dict) or record.get('source') not in refs or record.get('status')!='observed_structure_only' or record.get('sample_records')!=profiles[platform]['sample_records'] or type(record.get('present')) is not int or not 0<record['present']<=record['sample_records'] or not isinstance(record.get('observed_types'),dict):
                raise SkillError('DATA_DICTIONARY_INVALID')
            if any(type(n) is not int or n<0 for n in record['observed_types'].values()) or sum(record['observed_types'].values())!=record['present']:
                raise SkillError('DATA_DICTIONARY_INVALID')
    return data, hashlib.sha256(raw).hexdigest()


def timestamp(value):
    """Only explicit epoch seconds/milliseconds or timezone-qualified ISO values."""
    if type(value) in (int, float) and math.isfinite(value):
        if 1e9 <= value < 1e10:
            return dt.datetime.fromtimestamp(value, dt.timezone.utc)
        if 1e12 <= value < 1e13:
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
        raise ValueError('timestamp format')
    if isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            return parsed
    raise ValueError('timestamp format or timezone')


def valid_value(value, rule):
    kind = rule['kind']
    if kind == 'identifier':
        return type(value) in (str, int) and (not isinstance(value, str) or bool(value.strip()))
    if kind == 'text':
        return isinstance(value, str)
    if kind == 'url':
        try:
            url = urlsplit(value) if isinstance(value, str) else None
            return url is not None and url.scheme in ('http', 'https') and bool(url.hostname)
        except ValueError:
            return False
    if kind == 'timestamp':
        try:
            timestamp(value)
            return True
        except (ValueError, OverflowError, OSError):
            return False
    if kind == 'count':
        return type(value) is int and value >= 0
    if kind == 'nonnegative_number':
        return type(value) is int and value>=0 or type(value) is float and math.isfinite(value) and value>=0
    if kind == 'text_or_text_list':
        return isinstance(value,str) or isinstance(value,list) and all(isinstance(v,str) for v in value)
    if kind == 'json_text_or_object':
        try:
            parsed=json.loads(value) if isinstance(value,str) else value
            if not isinstance(parsed,dict):return False
            json.dumps(parsed,allow_nan=False)
            return True
        except (ValueError,TypeError):return False
    if kind == 'sentiment':
        return type(value) in (str, int) and str(value) in ('-1', '0', '1')
    if kind == 'opaque_enum':
        return type(value) in (str, int)
    if kind == 'identifier_list':
        return isinstance(value, list) and all(valid_value(v, {'kind': 'identifier'}) for v in value)
    if kind == 'text_list':
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    raise SkillError('DATA_DICTIONARY_RULE_UNKNOWN')


def validate_records(rows, expected_count, estimated_volume=None, queries=None, selected_fields=None, deviation_policy=None, estimate_relation='MATCHED'):
    dictionary, digest = load_dictionary()
    rules = {f['field']: f for f in dictionary['fields']}
    selected = set(selected_fields if selected_fields is not None else rules)
    issues = {}
    def issue(code, field, severity, row=None):
        key = (code, field, severity)
        item = issues.setdefault(key, dict(code=code, field=field, severity=severity, count=0, sample_row_numbers=[]))
        item['count'] += 1
        if row is not None and len(item['sample_row_numbers']) < 5:
            item['sample_row_numbers'].append(row)
    stats = {name: dict(present=0, missing=0, null=0, empty=0, invalid=0, observed_types={}) for name in rules}
    def platform_bucket():
        return dict(record_count=0,field_summary={name:dict(present=0,missing=0,null=0,empty=0,zero=0,invalid=0,observed_types={}) for name in rules},undocumented_fields={})
    platform_stats={p:platform_bucket() for p in PLATFORMS}
    platform_stats['unknown']=platform_bucket()
    unknown = collections.Counter()
    intervals = []
    for q in queries or []:
        if q.get('timezone') == 'Asia/Shanghai':
            try:
                start = dt.datetime.combine(dt.date.fromisoformat(q['start_date']), dt.time(), CN)
                end = dt.datetime.combine(dt.date.fromisoformat(q['end_date']) + dt.timedelta(days=1), dt.time(), CN)
                intervals.append((start, end))
            except (ValueError, KeyError):
                pass
    count = 0
    unique = 0
    # A temporary on-disk SQLite index avoids retaining all IDs in memory.
    index = sqlite3.connect('')
    try:
        index.execute('CREATE TABLE ids (id TEXT PRIMARY KEY)')
        for count, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                issue('RECORD_NOT_OBJECT', None, 'error', count)
                continue
            try:
                json.dumps(row, allow_nan=False)
            except (ValueError, TypeError):
                issue('RECORD_NOT_STRICT_JSON', None, 'error', count)
            platform,attribution=identify(row)
            platform_stat=platform_stats[platform];platform_stat['record_count']+=1
            if platform=='unknown' and (queries or 'site_id' in row or 'data_type' in row):issue(attribution,None,'warning',count)
            for name in row.keys() - rules.keys():
                unknown[name] += 1
                platform_stat['undocumented_fields'][name]=platform_stat['undocumented_fields'].get(name,0)+1
            for name, rule in rules.items():
                stat = stats[name]
                scoped=platform_stat['field_summary'][name]
                warn_missing=rule.get('required') or name in selected and rule.get('missing_policy')!='observe_only'
                if name not in row:
                    stat['missing'] += 1
                    scoped['missing']+=1
                    if warn_missing:
                        issue('FIELD_MISSING', name, 'error' if rule.get('required') else 'warning', count)
                    continue
                value = row[name]
                stat['present'] += 1
                scoped['present']+=1
                typ = type(value).__name__
                stat['observed_types'][typ] = stat['observed_types'].get(typ, 0) + 1
                scoped['observed_types'][typ]=scoped['observed_types'].get(typ,0)+1
                if type(value) in (int,float) and value==0:scoped['zero']+=1
                if value is None:
                    stat['null'] += 1
                    scoped['null']+=1
                elif value == '' or value == [] or value == {} or isinstance(value, str) and not value.strip():
                    stat['empty'] += 1
                    scoped['empty']+=1
                else:
                    if not valid_value(value, rule):
                        stat['invalid'] += 1
                        scoped['invalid']+=1
                        issue('FIELD_TYPE_OR_FORMAT_INVALID', name, 'error' if rule.get('required') else 'warning', count)
                    continue
                if warn_missing:
                    issue('FIELD_EMPTY', name, 'error' if rule.get('required') else 'warning', count)
            identity = row.get('id')
            if valid_value(identity, {'kind': 'identifier'}):
                try:
                    index.execute('INSERT INTO ids VALUES (?)', (str(identity),))
                    unique += 1
                except sqlite3.IntegrityError:
                    issue('DUPLICATE_RECORD_ID', 'id', 'error', count)
            if not any(isinstance(row.get(k), str) and row[k].strip() for k in ('title', 'content')):
                issue('NO_TITLE_OR_CONTENT', None, 'warning', count)
            if intervals and row.get('publish_time') is not None:
                try:
                    when = timestamp(row['publish_time'])
                    if not any(start <= when < end for start, end in intervals):
                        issue('PUBLISH_TIME_OUTSIDE_QUERY', 'publish_time', 'warning', count)
                except (ValueError, OverflowError, OSError):
                    pass  # Already reported by the field check; never guess timezone.
    finally:
        index.close()
    if type(expected_count) is not int or expected_count < 0 or count != expected_count:
        issue('RECORD_COUNT_MISMATCH', None, 'error')
    from quantity_control import compare
    deviation=compare(estimated_volume,expected_count,deviation_policy,estimate_relation) if type(expected_count) is int and expected_count>=0 else None
    if deviation and deviation['status']=='WARNING':issue('VOLUME_DEVIATION_WARNING',None,'warning')
    elif deviation and deviation['status']=='BLOCKED':issue('VOLUME_DEVIATION_MAJOR',None,'error')
    for name in selected - rules.keys():
        issue('SELECTED_FIELD_NOT_IN_DICTIONARY', name, 'warning')
    errors = sum(i['count'] for i in issues.values() if i['severity'] == 'error')
    warnings = sum(i['count'] for i in issues.values() if i['severity'] == 'warning')
    for stat in stats.values():
        stat['nonempty_rate'] = (stat['present'] - stat['null'] - stat['empty']) / count if count else None
    for platform,bucket in platform_stats.items():
        n=bucket['record_count']
        bucket['attribution_basis']='registered_site_id_or_unique_data_type; conflicts_are_unknown'
        for name,stat in bucket['field_summary'].items():
            stat['nonempty_rate']=(stat['present']-stat['null']-stat['empty'])/n if n else None
            stat['availability']='no_records' if not n else 'not_observed' if not stat['present'] else 'present_but_empty' if stat['present']==stat['null']+stat['empty'] else 'observed'
            stat['baseline_evidence']='observed_structure_only' if platform in rules[name]['platform_evidence'] else 'unknown'
    return dict(schema_version='1.0',dictionary_version=dictionary['version'],dictionary_sha256=digest,
        status='FAIL' if errors else 'PASS_WITH_WARNINGS' if warnings else 'PASS',
        checked_at=dt.datetime.now(CN).isoformat(),record_count=count,unique_ids=unique,expected_count=expected_count,
        estimated_volume=estimated_volume,error_occurrences=errors,warning_occurrences=warnings,
        volume_deviation=deviation,
        field_summary=stats,by_platform=platform_stats,issues=list(issues.values()),undocumented_fields=dict(sorted(unknown.items())),
        checks_not_performed=['official supplier semantics certification','cross-platform metric equivalence','keyword relevance reclassification','billing reconciliation'],
        semantics_status='provisional_dictionary_not_supplier_certified',
        publish_time_scope_check='union_of_query_date_ranges' if intervals else 'not_tested',
        policy='No source mutation, alias inference, missing-to-zero conversion, record deletion or automatic deduplication. Optional field defects are warnings; transport integrity and ID defects are blocking.')
