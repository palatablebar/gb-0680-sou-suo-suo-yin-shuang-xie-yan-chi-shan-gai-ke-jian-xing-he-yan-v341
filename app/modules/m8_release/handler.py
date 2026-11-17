from flask import Blueprint, jsonify, request

from app.errors import DomainError
from app import release_pipeline

bp = Blueprint('release', __name__)


def _payload():
    payload = request.get_json(silent=True) if request.method == 'POST' else {}
    if payload is None or not isinstance(payload, dict):
        raise DomainError('BAD_BODY', '请求需要对象')
    params = payload.get('parameters', {})
    if not isinstance(params, dict):
        raise DomainError('BAD_PARAMETERS', '参数需要对象')
    if 'rows' in payload:
        raise DomainError('UNSUPPORTED', '发布裁决仅接受完整回执 CSV，保证证据链完整')
    raw = payload.get('csv')
    if raw is not None:
        if not isinstance(raw, str):
            raise DomainError('BAD_CSV', 'CSV需要文本')
        return raw.encode(), params
    return release_pipeline.load_sample(payload.get('sample', 'replicas.csv')), params


@bp.route('/api/release/preview', methods=['GET', 'POST'])
def preview():
    raw, params = _payload()
    return jsonify(release_pipeline.calculate(raw, params))
