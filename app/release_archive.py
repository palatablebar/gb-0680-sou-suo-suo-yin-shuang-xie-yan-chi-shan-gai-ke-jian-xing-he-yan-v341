import csv
import io
import json


def build_release_archive(run):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['run_id', 'input_sha256', 'rule_version', 'kind',
                     'doc_id', 'source_lines', 'verdict_code', 'verdict',
                     'target_version', 'intended_op', 'publishable',
                     'still_searchable', 'evidence'])
    meta = run['manifest']
    prefix = [run['run_id'], meta['input_sha256'], meta['rule_version']]
    for row in run['rows']:
        evidence = {
            'replica_states': row['replica_states'],
            'missing_replicas': row['missing_replicas'],
            'stale_replicas': row['stale_replicas'],
            'divergent_replicas': row['divergent_replicas'],
            'conflict_replicas': row['conflict_replicas'],
            'retry_count': row['retry_count'],
            'decision_reason': row['decision_reason'],
        }
        writer.writerow(prefix + [
            'verdict', row['doc_id'],
            json.dumps(row['_sources'], ensure_ascii=False),
            row['verdict_code'], row['verdict'],
            row.get('target_version') or '', row.get('intended_op') or '',
            row['publishable'], row['still_searchable'],
            json.dumps(evidence, ensure_ascii=False)])
    for row in run.get('quarantine', []):
        writer.writerow(prefix + [
            'quarantine', row.get('doc_id', ''),
            json.dumps(row.get('_sources', []), ensure_ascii=False),
            row.get('reason', ''), row.get('detail', ''),
            '', '', '', '', json.dumps(row, ensure_ascii=False, default=str)])
    for item in run['release_scope']:
        writer.writerow(prefix + [
            'release_scope', item['doc_id'], '[]',
            'RELEASE_SCOPE', '发布范围（唯一）',
            item['target_version'], item['intended_op'], True, True,
            json.dumps(item, ensure_ascii=False)])
    writer.writerow(prefix + [
        'SCOPE_SHA256', '', '[]', 'manifest',
        run['metrics']['release_scope_sha256'], '', '', '', '',
        json.dumps({'release_scope_count': run['metrics']['release_scope_count'],
                    'retry_receipts': run['metrics']['retry_receipts']},
                   ensure_ascii=False)])
    return '﻿' + stream.getvalue()
