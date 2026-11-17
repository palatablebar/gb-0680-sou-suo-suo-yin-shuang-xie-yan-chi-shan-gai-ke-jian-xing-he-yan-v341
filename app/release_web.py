from flask import render_template

from app.config import TITLE, SHORT
from app import release_pipeline, release_store

VERDICT_LABELS = {
    'doc_id': '文档号', 'verdict': '发布结论', 'verdict_code': '结论码',
    'target_version': '目标版本', 'intended_op': '源操作',
    'publishable': '可对外发布', 'still_searchable': '当前仍可检索',
    'retry_count': '重试回执',
}
METRIC_LABELS = {
    'documents': '裁决文档', 'replicas_per_doc': '每文档副本数',
    'publish_new': '放行新内容', 'takedown_confirmed': '下架确认',
    'holds': '暂缓/拦截', 'stale_holds': '旧正文暂缓',
    'missing_holds': '缺回执暂缓', 'divergence_holds': '正文分歧暂缓',
    'conflict_holds': '冲突/无回执暂缓', 'takedown_holds': '下架未完成拦截',
    'retry_receipts': '折叠重试回执', 'quarantine_rows': '隔离回执',
    'release_scope_count': '发布范围文档数', 'release_scope_sha256': '发布范围摘要',
}


def register_release_pages(app):
    def page():
        run = (release_store.latest()
               or release_pipeline.execute(release_pipeline.load_sample('replicas.csv')))
        return render_template('release.html', title=TITLE, short=SHORT, run=run,
                               verdict_labels=VERDICT_LABELS, metric_labels=METRIC_LABELS)
    app.add_url_rule('/release', view_func=page, endpoint='release_page')
