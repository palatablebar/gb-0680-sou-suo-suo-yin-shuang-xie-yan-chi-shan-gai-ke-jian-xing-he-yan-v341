import csv
import io
import json
import hashlib
from collections import defaultdict

from app.errors import DomainError
from app.reasons import reject
from app.values import number, instant

COLUMNS = ['receipt_id', 'doc_id', 'version', 'op', 'replica_id', 'body_digest',
           'received_at', 'source_time', 'note']
OPS = {'upsert', 'delete'}

VERDICT_LABELS = {
    'PUBLISH_NEW': '放行新内容',
    'TAKEDOWN_CONFIRMED': '下架确认',
    'HOLD_STALE_REPLICA': '暂缓发布：副本留旧正文',
    'HOLD_MISSING_ACK': '暂缓发布：副本缺回执',
    'HOLD_CONTENT_DIVERGENCE': '暂缓发布：同版本正文分歧',
    'HOLD_OP_DIVERGENCE': '暂缓发布：同版本操作分歧',
    'HOLD_REPLICA_CONFLICT': '暂缓发布：副本版本冲突',
    'HOLD_TAKEDOWN_INCOMPLETE': '保持拦截：下架未全副本确认',
    'HOLD_NO_VALID_RECEIPT': '暂缓发布：无有效回执',
}


def decode(raw):
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise DomainError('ENCODING', '无法识别文件编码')


def _configured_replicas(params):
    raw = params.get('replicas') if params else None
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(',')]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        raise DomainError('BAD_REPLICAS', '副本清单需要数组或逗号分隔文本')
    items = [x for x in items if x]
    if not items or len(set(items)) != len(items):
        raise DomainError('BAD_REPLICAS', '副本清单为空或存在重复')
    return sorted(items)


def parse(raw, params=None):
    """阶段 R1：采信回执。重试回执幂等折叠，不新增记录。"""
    params = params or {}
    text, encoding = decode(raw)
    if not text:
        raise DomainError('EMPTY_FILE', '文件为空')
    if not text.strip():
        raise DomainError('BLANK_ONLY', '全部为空行')
    stream = csv.reader(io.StringIO(text, newline=''))
    header = next(stream, None)
    if header != COLUMNS:
        raise DomainError('BAD_HEADER', '表头不匹配')
    rows, bad = [], []
    seen_receipt, seen_fingerprint = {}, {}
    blank = repeated = logical = retries = 0
    configured = _configured_replicas(params)
    known_replicas = set(configured or [])
    try:
        for cells in stream:
            logical += 1
            line = stream.line_num
            if not cells or not any(c.strip() for c in cells):
                blank += 1
                continue
            if cells == header:
                repeated += 1
                continue
            row = {'_line': line, '_sources': [line], '_raw': cells}
            if len(cells) != len(header):
                bad.append(reject(row, 'BAD_COLUMNS', '列数不匹配'))
                continue
            row.update(dict(zip(COLUMNS, [x.strip() for x in cells])))
            try:
                if not row['receipt_id'] or not row['doc_id'] or not row['replica_id']:
                    raise DomainError('EMPTY_VALUE', '回执号、文档号、副本号均不能为空')
                version = number(row['version'])
                if version < 1 or version != version.to_integral_value():
                    raise DomainError('BAD_VERSION', '版本必须为正整数')
                if row['op'] not in OPS:
                    raise DomainError('BAD_OPERATION', '操作仅支持 upsert/delete')
                if not row['body_digest']:
                    raise DomainError('EMPTY_VALUE', '正文摘要不能为空')
                source = instant(row['source_time'])
                received = instant(row['received_at'])
                if received < source:
                    raise DomainError('NEGATIVE_LATENCY', '回执时间早于源事件')
                if configured is not None and row['replica_id'] not in known_replicas:
                    raise DomainError('UNKNOWN_REPLICA', '回执来自未登记副本 %s' % row['replica_id'])
            except DomainError as exc:
                if exc.code == 'UNKNOWN_REPLICA':
                    raise
                bad.append(reject(row, exc.code, exc.message))
                continue
            clean = {**row, 'version': format(version.to_integral_value(), 'f'),
                     'source_at': source.isoformat(), 'received_at': received.isoformat()}
            fingerprint = (clean['replica_id'], clean['doc_id'], clean['version'],
                           clean['op'], clean['body_digest'])
            prior_receipt = seen_receipt.get(clean['receipt_id'])
            if prior_receipt is not None and prior_receipt != fingerprint:
                bad.append(reject(row, 'RECEIPT_CONFLICT', '同回执号内容不一致'))
                continue
            if fingerprint in seen_fingerprint:
                keeper = seen_fingerprint[fingerprint]
                keeper['_sources'].append(line)
                keeper['retry_count'] += 1
                retries += 1
                continue
            clean['_sources'] = [line]
            clean['retry_count'] = 0
            seen_fingerprint[fingerprint] = clean
            seen_receipt[clean['receipt_id']] = fingerprint
            rows.append(clean)
    except csv.Error:
        raise DomainError('BAD_CSV', '引号未闭合或文件损坏')
    if logical == 0:
        raise DomainError('HEADER_ONLY', '仅有表头')
    if not rows and not bad:
        raise DomainError('BLANK_ONLY', '全部为空行')
    metrics = {'input': logical, 'accepted': len(rows), 'quarantine': len(bad),
               'retry_receipts': retries, 'blank': blank, 'repeated_headers': repeated,
               'encoding': encoding,
               'balanced': logical == len(rows) + len(bad) + retries + blank + repeated}
    return {'rows': rows, 'quarantine': bad, 'metrics': metrics,
            'replica_set': sorted(known_replicas | {r['replica_id'] for r in rows})}


def adjudicate(parsed):
    """阶段 R2：逐文档发布裁决。全副本收敛才放行，不按多数派。"""
    rows, bad = parsed['rows'], parsed['quarantine']
    replicas = parsed['replica_set']
    # 同副本同版本内容冲突 → 隔离并标记该副本该文档不可裁决
    by_version = defaultdict(list)
    for row in rows:
        by_version[(row['doc_id'], row['replica_id'], row['version'])].append(row)
    conflicted = set()
    extra_bad = []
    for (doc, replica, version), events in sorted(by_version.items()):
        fingerprints = {(e['op'], e['body_digest']) for e in events}
        if len(fingerprints) > 1:
            conflicted.add((doc, replica))
            extra_bad.extend(reject(e, 'SAME_VERSION_CONFLICT', '同副本同版本正文或操作冲突') for e in events)
    good = [r for r in rows if (r['doc_id'], r['replica_id']) not in conflicted]
    quarantine = bad + extra_bad

    by_doc_replica = defaultdict(dict)
    doc_lines = defaultdict(list)
    doc_retries = defaultdict(int)
    for row in good:
        doc_lines[row['doc_id']].extend(row['_sources'])
        doc_retries[row['doc_id']] += row['retry_count']
        bucket = by_doc_replica[row['doc_id']]
        current = bucket.get(row['replica_id'])
        if current is None or number(row['version']) > number(current['version']):
            bucket[row['replica_id']] = row
    for q in quarantine:
        doc = q.get('doc_id')
        if doc:
            doc_lines[doc].extend(q.get('_sources', []))

    verdicts = []
    for doc in sorted(doc_lines):
        winners = by_doc_replica.get(doc, {})
        states, missing, conflicts = [], [], []
        for replica in replicas:
            if (doc, replica) in conflicted:
                conflicts.append(replica)
                states.append({'replica_id': replica, 'state': 'REPLICA_CONFLICT',
                               'version': None, 'op': None, 'body_digest': None,
                               'receipt_id': None, 'received_at': None})
            elif replica not in winners:
                missing.append(replica)
                states.append({'replica_id': replica, 'state': 'MISSING_ACK',
                               'version': None, 'op': None, 'body_digest': None,
                               'receipt_id': None, 'received_at': None})
            else:
                w = winners[replica]
                states.append({'replica_id': replica, 'state': 'ACK',
                               'version': w['version'], 'op': w['op'],
                               'body_digest': w['body_digest'], 'receipt_id': w['receipt_id'],
                               'received_at': w['received_at']})
        record = _verdict(doc, replicas, states, missing, conflicts,
                          sorted(doc_lines[doc]), doc_retries[doc])
        verdicts.append(record)

    scope = [{k: v[k] for k in ('doc_id', 'target_version', 'intended_op', 'body_digest')}
             for v in verdicts if v['publishable']]
    scope_blob = json.dumps(scope, ensure_ascii=False, sort_keys=True).encode()
    counts = defaultdict(int)
    for v in verdicts:
        counts[v['verdict_code']] += 1
    metrics = {'documents': len(verdicts),
               'replicas_per_doc': len(replicas),
               'publish_new': counts['PUBLISH_NEW'],
               'takedown_confirmed': counts['TAKEDOWN_CONFIRMED'],
               'holds': sum(1 for v in verdicts if not v['publishable']),
               'stale_holds': counts['HOLD_STALE_REPLICA'],
               'missing_holds': counts['HOLD_MISSING_ACK'],
               'divergence_holds': counts['HOLD_CONTENT_DIVERGENCE'] + counts['HOLD_OP_DIVERGENCE'],
               'conflict_holds': counts['HOLD_REPLICA_CONFLICT'] + counts['HOLD_NO_VALID_RECEIPT'],
               'takedown_holds': counts['HOLD_TAKEDOWN_INCOMPLETE'],
               'retry_receipts': parsed['metrics']['retry_receipts'],
               'quarantine_rows': len(quarantine),
               'release_scope_count': len(scope),
               'release_scope_sha256': hashlib.sha256(scope_blob).hexdigest()}
    return {'rows': verdicts, 'quarantine': quarantine, 'metrics': metrics,
            'release_scope': scope, 'replica_set': replicas}


def _verdict(doc, replicas, states, missing, conflicts, source_lines, retry_count):
    acked = [s for s in states if s['state'] == 'ACK']
    base = {'doc_id': doc, 'replica_count': len(replicas), 'replica_states': states,
            'missing_replicas': missing, 'conflict_replicas': conflicts,
            'stale_replicas': [], 'divergent_replicas': [],
            '_sources': source_lines, 'retry_count': retry_count,
            'intended_op': None, 'target_version': None, 'body_digest': None,
            'publishable': False, 'externally_visible': None, 'still_searchable': None,
            'release_scope': [], 'decision_reason': ''}
    if not acked:
        base.update(verdict_code='HOLD_NO_VALID_RECEIPT',
                    verdict=VERDICT_LABELS['HOLD_NO_VALID_RECEIPT'],
                    externally_visible=None, still_searchable=None,
                    decision_reason='全部回执被隔离，没有可采信的副本状态')
        return base
    target_version = max(number(s['version']) for s in acked)
    base['target_version'] = format(target_version, 'f')
    leading = [s for s in acked if number(s['version']) == target_version]
    ops = {s['op'] for s in leading}
    digests = {s['body_digest'] for s in leading}
    intended_op = next(iter(ops)) if len(ops) == 1 else None
    lagging = sorted(s['replica_id'] for s in acked if number(s['version']) < target_version)

    if conflicts:
        code = 'HOLD_REPLICA_CONFLICT'
        reason = '副本 %s 存在同版本内容冲突，无法裁决' % '、'.join(conflicts)
    elif intended_op is None:
        code = 'HOLD_OP_DIVERGENCE'
        base['divergent_replicas'] = sorted({s['replica_id'] for s in leading})
        reason = '最高版本 v%s 的操作在副本间不一致（upsert/delete 分歧）' % base['target_version']
    elif len(digests) > 1:
        code = 'HOLD_CONTENT_DIVERGENCE'
        base['divergent_replicas'] = sorted({s['replica_id'] for s in leading})
        reason = '最高版本 v%s 正文摘要在副本间不一致' % base['target_version']
    elif intended_op == 'delete':
        confirmed = (not missing and not lagging
                     and all(s['state'] == 'ACK' and s['op'] == 'delete' for s in states))
        if confirmed:
            code = 'TAKEDOWN_CONFIRMED'
            reason = '全部 %d 个副本确认 v%s 墓碑，下架生效且外部不可检索' % (len(replicas), base['target_version'])
        else:
            code = 'HOLD_TAKEDOWN_INCOMPLETE'
            base['stale_replicas'] = lagging
            unresolved = missing + lagging
            reason = '副本 %s 未确认墓碑，文档仍可检索，禁止误报下架完成' % '、'.join(unresolved)
    else:
        if missing:
            code = 'HOLD_MISSING_ACK'
            reason = '副本 %s 尚无回执，无法确认全副本收敛' % '、'.join(missing)
        elif lagging:
            code = 'HOLD_STALE_REPLICA'
            base['stale_replicas'] = lagging
            reason = '副本 %s 仍保留旧版本正文，放行会出现同一文档两个版本' % '、'.join(lagging)
        elif all(s['state'] == 'ACK' and s['op'] == 'upsert' for s in states):
            code = 'PUBLISH_NEW'
            reason = '全部 %d 个副本收敛到 v%s 同一正文，放行发布' % (len(replicas), base['target_version'])
        else:
            code = 'HOLD_CONTENT_DIVERGENCE'
            reason = '副本未在同一正文上收敛'

    base['intended_op'] = intended_op
    base['body_digest'] = next(iter(digests)) if len(digests) == 1 else None
    base['verdict_code'] = code
    base['verdict'] = VERDICT_LABELS[code]
    base['publishable'] = code in {'PUBLISH_NEW', 'TAKEDOWN_CONFIRMED'}
    if code == 'PUBLISH_NEW':
        base['externally_visible'] = True
        base['still_searchable'] = True
        base['release_scope'] = [{'doc_id': doc, 'target_version': base['target_version'],
                                  'intended_op': 'upsert', 'body_digest': base['body_digest']}]
    elif code == 'TAKEDOWN_CONFIRMED':
        base['externally_visible'] = False
        base['still_searchable'] = False
    else:
        # 暂缓期间外部状态保持原样：旧版仍在或下架未完成，均显式标注仍可检索
        base['externally_visible'] = True
        base['still_searchable'] = True
    base['decision_reason'] = reason
    return base


def evaluate(raw, params=None):
    parsed = parse(raw, params)
    result = adjudicate(parsed)
    result['intake'] = parsed['metrics']
    return result
