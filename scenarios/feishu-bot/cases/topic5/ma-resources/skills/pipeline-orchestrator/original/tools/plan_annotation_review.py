"""Plan a capped, reproducible, special-label-prioritized semantic review."""
import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from check_annotation_structure import load_rows, ALIASES


def get(row, key):
    return next((str(row[name] or '').strip() for name in ALIASES[key] if name in row), '')


def stratum(row, stage, config=None):
    config = config or {}
    sentiment, relevant = get(row, 'sentiment'), get(row, 'relevant')
    opinion = get(row, 'opinion')
    special = []
    if relevant == '否':
        special.append('不相关')
    if sentiment == '负面':
        special.append('负面')
    if stage == 'round2' and '其他' in [x.strip() for x in opinion.split(',')]:
        special.append('其他')
    if relevant not in ['是', '否'] or sentiment not in ['正面', '中性', '负面'] or row.get('parse_error'):
        special.append('空值或非法')
    if stage == 'round2' and not opinion:
        special.append('空值或非法')
    if stage == 'round2':
        direction = {'正面': 'positive', '负面': 'negative'}.get(sentiment)
        allowed = config.get((direction or '') + '_labels')
        if allowed is not None and any(x.strip() not in allowed for x in opinion.split(',')):
            special.append('空值或非法')
    if stage == 'attributes' and get(row, 'type') == '未知':
        special.append('类型未知')
    if stage == 'attributes':
        ugc, dimension = get(row, 'ugc'), get(row, 'dimension')
        if get(row, 'type') == 'UGC' and ugc == '其他':
            special.append('其他')
        if (get(row, 'type') == 'UGC' and config.get('ugc_applicable', True)
                and ugc not in config.get('ugc_labels', [ugc])):
            special.append('空值或非法')
        if dimension not in config.get('dimension_labels', ['品牌', '产品', '活动']):
            special.append('空值或非法')
    if row.get('evidence_status') == 'unresolved':
        special.append('证据歧义')
    base = '|'.join([get(row, 'type'), sentiment, relevant])
    if stage == 'round2':
        base += '|观点:' + ','.join(sorted(set(x.strip() for x in opinion.split(',') if x.strip())))
    elif stage == 'attributes':
        base += '|参与:' + get(row, 'ugc') + '|主对象:' + get(row, 'dimension')
    return ('特殊:' + '+'.join(sorted(set(special))) if special else '正常') + '|' + base


def plan(rows, stage, reviewed_ids=None, reserve=100, seed=42, config=None):
    config = config or {}
    if stage == 'round2' and not all(key in config for key in ['positive_labels', 'negative_labels']):
        raise ValueError('Round two requires approved direction labels/applicability config')
    if stage == 'attributes' and 'ugc_labels' not in config:
        raise ValueError('Attributes require approved UGC config')
    if reviewed_ids is not None and not isinstance(reviewed_ids, list):
        raise ValueError('Reviewed IDs must be a JSON array')
    if any(value is None or not str(value).strip() for value in (reviewed_ids or [])):
        raise ValueError('Reviewed IDs must be nonempty stable IDs')
    reviewed = {str(value) for value in (reviewed_ids or [])}
    if len(reviewed) > 500:
        raise ValueError('This round already exceeds the 500-distinct-post cap')
    if reserve < 0 or reserve > 500:
        raise ValueError('Reserve must be between 0 and 500')
    ids = [str(row.get('id') or '') for row in rows]
    if any(not x for x in ids) or len(ids) != len(set(ids)):
        raise ValueError('Stable unique IDs required; run the structural precheck')
    groups, populations = defaultdict(list), defaultdict(int)
    field_populations = defaultdict(int)
    for row in rows:
        # Round two opinions apply only to related positive/negative posts.
        if stage == 'round2' and not (get(row, 'relevant') == '是' and get(row, 'sentiment') in ['正面', '负面']):
            continue
        if stage == 'round2':
            direction = {'正面': 'positive', '负面': 'negative'}[get(row, 'sentiment')]
            if not config.get(direction + '_applicable', True):
                continue
        if stage == 'attributes' and get(row, 'relevant') != '是':
            continue
        if stage == 'round1':
            field_populations['sentiment'] += 1
            field_populations['relevant'] += 1
        elif stage == 'round2':
            field_populations['opinion'] += 1
        else:
            field_populations['dimension'] += 1
            if get(row, 'type') == 'UGC' and config.get('ugc_applicable', True):
                field_populations['ugc'] += 1
        key = stratum(row, stage, config)
        populations[key] += 1
        if str(row['id']) not in reviewed:
            groups[key].append(str(row['id']))
    population = sum(populations.values())
    base = min(math.ceil(population * .1), 500)
    remaining = 500 - len(reviewed)
    reserve_actual = min(reserve, max(0, remaining - 1)) if population else 0
    target = min(base, remaining - reserve_actual, sum(map(len, groups.values())))
    quotas = {key: 0 for key in groups}
    # Cover strata first, then oversample special strata. Layer-level rates are disclosed.
    order = sorted(groups, key=lambda key: (not key.startswith('特殊:'), key))
    normals = [key for key in order if key.startswith('正常')]
    if target >= 2 and normals:
        order.remove(normals[0])
        order.insert(1, normals[0])
    for key in order[:target]:
        quotas[key] += 1
    while sum(quotas.values()) < target:
        candidates = [key for key in groups if quotas[key] < len(groups[key])]
        key = max(candidates, key=lambda key: (
            (3 if key.startswith('特殊:') else 1) * math.sqrt(len(groups[key])) / (quotas[key] + 1), key))
        quotas[key] += 1
    rng = random.Random(seed)
    selected = []
    for key in sorted(groups):
        selected.extend({'id': row_id, 'stratum': key, 'sample_kind': 'stratified_random'}
                        for row_id in rng.sample(sorted(groups[key]), quotas[key]))
    return {'stage': stage, 'population': population, 'field_populations': dict(field_populations), 'base_sample_size': base,
            'sample_size': len(selected), 'distinct_reviewed_before': len(reviewed),
            'distinct_reviewed_after_plan': len(reviewed) + len(selected),
            'cap': 500, 'reserve_requested': reserve, 'remaining_after_plan': remaining - len(selected),
            'seed': seed, 'method': 'special-prioritized stratified random; unequal inclusion probabilities',
            'accuracy_is_population_truth': False,
            'strata': {key: {'population': populations[key], 'planned': quotas.get(key, 0)} for key in sorted(populations)},
            'uncovered_strata': [key for key in sorted(populations) if not quotas.get(key)],
            'samples': selected, 'llm_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--stage', choices=['round1', 'round2', 'attributes'], required=True)
    parser.add_argument('--reviewed-ids', help='JSON array: union of calibration, targeted and random IDs in this round')
    parser.add_argument('--config', help='Approved categories and per-direction applicability; required for round two/attributes')
    parser.add_argument('--reserve', type=int, default=100, help='Remaining slots reserved for targeted review/re-audit; configurable')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.resolve() in [Path(x).resolve() for x in [args.input, args.reviewed_ids, args.config] if x]:
        parser.error('Output must not overwrite an input')
    reviewed = json.loads(Path(args.reviewed_ids).read_text(encoding='utf-8-sig')) if args.reviewed_ids else []
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
    report = plan(load_rows(args.input), args.stage, reviewed, args.reserve, args.seed, config)
    if args.config:
        report['config_sha256'] = hashlib.sha256(Path(args.config).read_bytes()).hexdigest()
    report['source_sha256'] = hashlib.sha256(Path(args.input).read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ['population', 'sample_size', 'distinct_reviewed_after_plan', 'cap', 'remaining_after_plan']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
