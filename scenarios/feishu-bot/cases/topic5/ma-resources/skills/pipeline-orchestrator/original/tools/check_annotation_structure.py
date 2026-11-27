"""Read-only structural precheck; dynamic labels come from the approved task config."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

ALIASES = {
    'sentiment': ['情感(AI)', 'sentiment'],
    'relevant': ['是否活动相关(AI)', '是否活动相关', 'campaign_relevant'],
    'opinion': ['观点类别(AI)', '观点(AI)', '观点'],
    'ugc': ['ugc_category'],
    'dimension': ['discussion_dimension'],
    'type': ['内容类型', 'content_type'],
}


def load_rows(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(data, list):
        rows = data
    else:
        rows = data['rows']
        if rows and isinstance(rows[0], list):
            columns = data['columns']
            if len(set(columns)) != len(columns):
                raise ValueError('Duplicate columns')
            if any(len(row) != len(columns) for row in rows):
                raise ValueError('Column/row length mismatch')
            rows = [dict(zip(columns, row)) for row in rows]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError('Rows must be objects or column-aligned arrays')
    return rows


def check(rows, config, stage):
    errors = []
    seen = set()
    def error(row_id, field, reason):
        errors.append({'id': row_id, 'field': field, 'reason': reason})
    def value(row, key, row_id):
        found = [(name, row[name]) for name in ALIASES[key] if name in row]
        vals = ['' if v is None else str(v).strip() for _, v in found]
        if len(set(vals)) > 1:
            error(row_id, key, 'conflicting_aliases')
        return vals[0] if vals else ''
    if stage in ['positive', 'negative', 'full']:
        required = ['positive_labels', 'negative_labels'] if stage == 'full' else [stage + '_labels']
        for key in required:
            if key not in config:
                raise ValueError('Missing approved config: ' + key)
    if stage in ['attributes', 'full'] and 'ugc_labels' not in config:
        raise ValueError('Missing approved config: ugc_labels')
    placeholders = set(config.get('opinion_not_applicable', ['N/A', '无', '']))
    limit = config.get('max_opinion_labels', 2)
    if not isinstance(limit, int) or limit < 1:
        raise ValueError('max_opinion_labels must be a positive integer')
    for index, row in enumerate(rows):
        row_id = str(row.get('id') or '')
        locator = row_id or 'row:' + str(index + 1)
        if not row_id:
            error(locator, 'id', 'missing_id')
        elif row_id in seen:
            error(locator, 'id', 'duplicate_id')
        seen.add(row_id)
        sentiment = value(row, 'sentiment', locator)
        relevant = value(row, 'relevant', locator)
        if sentiment not in ['正面', '中性', '负面']:
            error(locator, 'sentiment', 'illegal_or_empty')
        if relevant not in ['是', '否']:
            error(locator, 'relevant', 'illegal_or_empty')
        if row.get('parse_error'):
            error(locator, 'output', 'parse_error')
        if stage in ['positive', 'negative', 'full']:
            opinion = value(row, 'opinion', locator)
            direction = {'正面': 'positive', '负面': 'negative'}.get(sentiment)
            applicable = relevant == '是' and direction is not None
            if stage != 'full' and (not applicable or direction != stage):
                error(locator, 'opinion', 'row_outside_task_scope')
                continue
            if applicable and config.get(direction + '_applicable', True):
                labels = [x.strip() for x in opinion.split(',') if x.strip()]
                allowed = set(config[direction + '_labels'])
                if not labels or any(x not in allowed for x in labels):
                    error(locator, 'opinion', 'illegal_or_empty')
                if len(labels) != len(set(labels)):
                    error(locator, 'opinion', 'duplicate_labels')
                if len(labels) > limit:
                    error(locator, 'opinion', 'too_many_labels')
            elif opinion not in placeholders:
                error(locator, 'opinion', 'label_outside_applicable_scope')
        if stage in ['attributes', 'full']:
            content_type = value(row, 'type', locator)
            ugc = value(row, 'ugc', locator)
            dimension = value(row, 'dimension', locator)
            if content_type not in ['UGC', 'PGC', '未知']:
                error(locator, 'type', 'illegal_or_empty')
            if relevant == '是' and content_type == 'UGC' and config.get('ugc_applicable', True):
                if ugc not in config['ugc_labels']:
                    error(locator, 'ugc', 'illegal_or_empty')
            elif ugc != 'N/A':
                error(locator, 'ugc', 'label_outside_applicable_scope')
            if relevant == '是':
                if dimension not in config.get('dimension_labels', ['品牌', '产品', '活动']):
                    error(locator, 'dimension', 'illegal_or_empty')
            elif dimension != 'N/A':
                error(locator, 'dimension', 'label_outside_applicable_scope')
    return {'status': 'pass' if not errors else 'fail', 'stage': stage,
            'rows': len(rows), 'error_count': len(errors),
            'errors_by_reason': dict(Counter(e['reason'] for e in errors)),
            'errors': errors, 'semantic_accuracy_checked': False,
            'source_modified': False, 'llm_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='JSON rows or {columns, rows}')
    parser.add_argument('--config', help='Approved category config JSON; required for round two')
    parser.add_argument('--stage', choices=['round1', 'positive', 'negative', 'attributes', 'full'], required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.input)
    output = Path(args.output)
    if output.resolve() == source.resolve() or (args.config and output.resolve() == Path(args.config).resolve()):
        parser.error('Output must not overwrite input or config')
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
    report = check(load_rows(source), config, args.stage)
    report['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    if args.config:
        report['config_sha256'] = hashlib.sha256(Path(args.config).read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'errors'}, ensure_ascii=False))
    return 0 if report['status'] == 'pass' else 2


if __name__ == '__main__':
    raise SystemExit(main())
