import hashlib
import json
import time
from datetime import datetime, timezone

from app.config import ROOT, RULE_VERSION
from app.errors import DomainError
from app.modules.m8_release.rules import evaluate

SAMPLES = {'replicas.csv'}


def load_sample(name):
    if name not in SAMPLES:
        raise DomainError('BAD_SAMPLE', '样例不存在', 404)
    return (ROOT / 'sample_data' / name).read_bytes()


def digest(raw, params=None):
    return hashlib.sha256(
        raw
        + json.dumps(params or {}, sort_keys=True, ensure_ascii=False).encode()
        + (RULE_VERSION + ':m8').encode()
    ).hexdigest()


def manifest(raw, params, started):
    return {
        'input_sha256': hashlib.sha256(raw).hexdigest(),
        'rule_version': RULE_VERSION,
        'parameters': params or {},
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'elapsed_ms': round((time.monotonic() - started) * 1000, 2),
        'module': 'm8_release',
    }


def calculate(raw, params=None):
    params = params or {}
    started = time.monotonic()
    result = evaluate(raw, params)
    run_id = digest(raw, params)[:20]
    return {'run_id': run_id, 'manifest': manifest(raw, params, started),
            'parameters': params, **result}


def execute(raw, params=None):
    from app import release_store
    result = calculate(raw, params)
    return release_store.save(result['run_id'], result)
