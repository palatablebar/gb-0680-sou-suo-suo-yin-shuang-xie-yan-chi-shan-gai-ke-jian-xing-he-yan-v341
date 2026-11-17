import csv
import io

import pytest

from app.config import ROOT
from app import release_store
from app.modules.m8_release.rules import evaluate, parse, adjudicate
from app.release_pipeline import calculate, execute, load_sample
from app.errors import DomainError

SAMPLE = ROOT / 'sample_data' / 'replicas.csv'


@pytest.fixture
def raw():
    return SAMPLE.read_bytes()


@pytest.fixture
def result(raw):
    return evaluate(raw)


def by_doc(result, doc):
    return next(r for r in result['rows'] if r['doc_id'] == doc)


def test_sample_all_expected_verdicts(result):
    expected = {
        'D1001': 'PUBLISH_NEW',          # 三副本同版本同正文收敛
        'D1002': 'HOLD_STALE_REPLICA',   # rep-b 留旧正文
        'D1003': 'TAKEDOWN_CONFIRMED',   # 全副本墓碑
        'D1004': 'HOLD_TAKEDOWN_INCOMPLETE',  # rep-b 旧正文未下架
        'D1005': 'HOLD_MISSING_ACK',     # rep-c 缺回执
        'D1006': 'HOLD_CONTENT_DIVERGENCE',  # 同版本跨副本正文摘要分歧
        'D1007': 'PUBLISH_NEW',          # 重试折叠后收敛
    }
    actual = {r['doc_id']: r['verdict_code'] for r in result['rows']}
    assert actual == expected


def test_publish_only_when_all_replicas_converge(result):
    d1001 = by_doc(result, 'D1001')
    assert d1001['publishable'] is True
    assert len(d1001['replica_states']) == 3
    assert all(s['version'] == '7' and s['body_digest'] == 'h7a' for s in d1001['replica_states'])


def test_stale_replica_blocks_publish_and_flags_searchable(result):
    d1002 = by_doc(result, 'D1002')
    assert d1002['publishable'] is False
    assert d1002['stale_replicas'] == ['rep-b']
    assert d1002['still_searchable'] is True
    assert '两个版本' in d1002['decision_reason']


def test_takedown_requires_every_replica_no_majority(result):
    d1003 = by_doc(result, 'D1003')
    assert d1003['verdict_code'] == 'TAKEDOWN_CONFIRMED'
    assert d1003['publishable'] is True
    assert d1003['still_searchable'] is False
    d1004 = by_doc(result, 'D1004')
    # 三副本中两个已确认墓碑，多数派也不得误报下架完成
    assert d1004['verdict_code'] == 'HOLD_TAKEDOWN_INCOMPLETE'
    assert d1004['publishable'] is False
    assert d1004['still_searchable'] is True
    assert d1004['stale_replicas'] == ['rep-b']


def test_missing_ack_is_hold_not_silent_release(result):
    d1005 = by_doc(result, 'D1005')
    assert d1005['publishable'] is False
    assert d1005['missing_replicas'] == ['rep-c']
    assert d1005['still_searchable'] is True


def test_retry_receipts_folded_no_new_conclusion(result):
    d1007 = by_doc(result, 'D1007')
    assert d1007['verdict_code'] == 'PUBLISH_NEW'
    assert d1007['retry_count'] == 2
    # 折叠后每个副本只保留一条有效状态
    assert len(d1007['replica_states']) == 3
    assert result['metrics']['retry_receipts'] == 2
    assert result['metrics']['documents'] == 7


def test_retry_does_not_change_release_scope(raw):
    first = calculate(raw)
    lines = raw.decode().splitlines()
    trimmed = '\n'.join(lines[:-2]).encode()  # 去掉两条重试回执
    second = calculate(trimmed)
    assert first['metrics']['release_scope_sha256'] == second['metrics']['release_scope_sha256']
    assert first['release_scope'] == second['release_scope']
    # 去掉重试只改变 run_id 之外的裁决：结论集合一致
    assert {r['doc_id']: r['verdict_code'] for r in first['rows']} == \
           {r['doc_id']: r['verdict_code'] for r in second['rows']}


def test_receipt_id_conflict_quarantined():
    header = ','.join(['receipt_id', 'doc_id', 'version', 'op', 'replica_id',
                       'body_digest', 'received_at', 'source_time', 'note'])
    csv_text = '\n'.join([
        header,
        'x1,D1,1,upsert,ra,h1,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,a',
        'x1,D1,1,upsert,rb,h2,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,b',
    ])
    result = evaluate(csv_text.encode())
    assert any(q['reason'] == 'RECEIPT_CONFLICT' for q in result['quarantine'])


def test_unknown_replica_rejected_when_configured():
    with pytest.raises(DomainError):
        parse(SAMPLE.read_bytes(), {'replicas': 'rep-a,rep-b'})


def test_configured_replica_without_any_receipt_holds():
    result = evaluate(SAMPLE.read_bytes(), {'replicas': 'rep-a,rep-b,rep-c,rep-d'})
    # 每个文档都缺少 rep-d
    assert all('rep-d' in r['missing_replicas'] for r in result['rows'])
    assert all(not r['publishable'] for r in result['rows'])


def test_ingest_line_balance(raw):
    parsed = parse(raw)
    m = parsed['metrics']
    assert m['input'] == m['accepted'] + m['quarantine'] + m['retry_receipts'] + m['blank'] + m['repeated_headers']
    assert m['balanced'] is True


def test_order_independent(raw):
    text = raw.decode('utf-8-sig')
    lines = text.splitlines()
    header, body = lines[0], lines[1:]
    reversed_csv = ('﻿' + header + '\n' + '\n'.join(reversed(body))).encode()
    a = adjudicate(parse(raw))
    b = adjudicate(parse(reversed_csv))
    assert [(r['doc_id'], r['verdict_code']) for r in a['rows']] == \
           [(r['doc_id'], r['verdict_code']) for r in b['rows']]


def test_same_replica_version_conflict_quarantines_doc():
    header = ','.join(['receipt_id', 'doc_id', 'version', 'op', 'replica_id',
                       'body_digest', 'received_at', 'source_time', 'note'])
    csv_text = '\n'.join([
        header,
        'a1,D7,3,upsert,ra,hA,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,a',
        'a2,D7,3,upsert,ra,hB,2026-11-17T06:02:00Z,2026-11-17T06:00:00Z,同副本冲突',
        'b1,D7,3,upsert,rb,hA,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,b',
        'c1,D7,3,upsert,rc,hA,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,c',
    ])
    result = evaluate(csv_text.encode())
    row = result['rows'][0]
    assert row['verdict_code'] == 'HOLD_REPLICA_CONFLICT'
    assert row['conflict_replicas'] == ['ra']
    assert row['publishable'] is False
    assert any(q['reason'] == 'SAME_VERSION_CONFLICT' for q in result['quarantine'])


def test_content_divergence_at_same_top_version():
    header = ','.join(['receipt_id', 'doc_id', 'version', 'op', 'replica_id',
                       'body_digest', 'received_at', 'source_time', 'note'])
    csv_text = '\n'.join([
        header,
        'a1,D9,3,upsert,ra,hA,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,a',
        'b1,D9,3,upsert,rb,hB,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,b',
    ])
    result = evaluate(csv_text.encode())
    row = result['rows'][0]
    assert row['verdict_code'] == 'HOLD_CONTENT_DIVERGENCE'
    assert row['publishable'] is False and row['still_searchable'] is True


def test_late_old_write_after_delete_keeps_takedown_incomplete_not_resurrected():
    header = ','.join(['receipt_id', 'doc_id', 'version', 'op', 'replica_id',
                       'body_digest', 'received_at', 'source_time', 'note'])
    csv_text = '\n'.join([
        header,
        'a1,D8,5,delete,ra,h5,2026-11-17T06:01:00Z,2026-11-17T06:00:00Z,a',
        'b1,D8,4,upsert,rb,h4,2026-11-17T06:05:00Z,2026-11-17T06:00:00Z,迟到旧写',
    ])
    result = evaluate(csv_text.encode())
    row = result['rows'][0]
    assert row['verdict_code'] == 'HOLD_TAKEDOWN_INCOMPLETE'
    assert row['target_version'] == '5'
    assert row['stale_replicas'] == ['rb']
    assert row['still_searchable'] is True


def test_idempotent_persistence(raw, tmp_path, monkeypatch):
    monkeypatch.setattr(release_store, 'DATA_DIR', tmp_path)
    a = execute(raw)
    b = execute(raw)
    assert a['run_id'] == b['run_id']
    assert len(release_store.history()) == 1


def test_stale_review_409(raw, tmp_path, monkeypatch):
    monkeypatch.setattr(release_store, 'DATA_DIR', tmp_path)
    run = execute(raw)
    assert release_store.review(run['run_id'], 1, '已复核')['version'] == 2
    with pytest.raises(DomainError) as exc:
        release_store.review(run['run_id'], 1, '退回复核')
    assert exc.value.status == 409


def test_archive_is_single_evidence_chain(raw, tmp_path, monkeypatch):
    monkeypatch.setattr(release_store, 'DATA_DIR', tmp_path)
    from app.release_archive import build_release_archive
    # 追加一条坏列回执，使归档同时包含 verdict/quarantine/release_scope/SCOPE_SHA256
    polluted = raw + b'bad,row\n'
    run = execute(polluted)
    text = build_release_archive(run)
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    kinds = {r[3] for r in rows[1:]}
    assert {'verdict', 'quarantine', 'release_scope', 'SCOPE_SHA256'} <= kinds
    scope_rows = [r for r in rows[1:] if r[3] == 'release_scope']
    assert len(scope_rows) == run['metrics']['release_scope_count']
    # 每个文档恰好一条 verdict 结论
    verdict_rows = [r for r in rows[1:] if r[3] == 'verdict']
    docs = [r[4] for r in verdict_rows]
    assert len(docs) == len(set(docs)) == run['metrics']['documents']


def test_routes_end_to_end(client):
    r = client.post('/api/release/runs', json={'sample': 'replicas.csv'})
    assert r.status_code == 200
    run = r.get_json()
    assert run['metrics']['documents'] == 7
    assert client.get('/release').status_code == 200
    assert client.get('/api/release/preview').status_code == 200
    rid = run['run_id']
    assert client.post(f'/api/release/runs/{rid}/review',
                       json={'version': 1, 'decision': '已复核'}).status_code == 200
    assert client.get(f'/api/release/runs/{rid}/archive').status_code == 200
    assert client.get('/api/release/runs').get_json()['runs'][0]['run_id'] == rid


def test_metrics_release_scope_matches_rows(result):
    scope_docs = {item['doc_id'] for item in result['release_scope']}
    publishable = {r['doc_id'] for r in result['rows'] if r['publishable']}
    assert scope_docs == publishable
    assert result['metrics']['release_scope_count'] == len(scope_docs)


def test_bad_header_rejected():
    with pytest.raises(DomainError):
        evaluate(b'receipt_id,doc_id\nx,D1\n')
