# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## M8 多副本发布裁决（独立流水线）

- `GET|POST /api/release/preview`：GET用 `replicas.csv`，POST可传 `csv` 与 `parameters.replicas`（数组或逗号分隔的在役副本清单），只计算不落库。
- `POST /api/release/runs`：原子保存发布批次；同输入同参数复用同一 `run_id`。
- `POST /api/release/upload`：multipart 回执 CSV，4MB 上限。
- `GET /api/release/runs`、`GET /api/release/runs/<id>`：批次历史与详情。
- `POST /api/release/runs/<id>/review`：乐观锁复核，需 `version` 与 `decision`，陈旧版本 409。
- `GET /api/release/runs/<id>/archive`：唯一证据 CSV，含 verdict、quarantine、release_scope 与 SCOPE_SHA256 四类行。
- 页面：`/release`。

输入列为 `receipt_id,doc_id,version,op,replica_id,body_digest,received_at,source_time,note`。时间必须带时区；op 仅 `upsert/delete`；未登记副本（配置了 replicas 时）直接 422，不静默采信。
