# 搜索索引双写延迟与删改可见性核验

供搜索数据同步值班工程师核对原始记录、处理差异并封存证据。

## 启动

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

打开 http://localhost:18249 。首次进入自动运行仓内脏样例。工具条可重跑、切换样例或上传CSV。SQLite保存在命名卷records中，重建服务保留批次。

## 模块

- 1. 双端采信：`/streams`，`/api/streams`。
- 2. 字段契约：`/schema`，`/api/schema`。
- 3. 版本裁决：`/versions`，`/api/versions`。
- 4. 可见性重建：`/visibility`，`/api/visibility`。
- 5. 延迟闸门：`/latency`，`/api/latency`。
- 6. 双写对账：`/parity`，`/api/parity`。
- 7. 回补归档：`/backfills`，`/api/backfills`。
- 8. 多副本发布裁决：`/release`，`/api/release/preview`、`/api/release/runs`（样例 `sample_data/replicas.csv`）。

## 核心规则

按源版本重建目标可见状态，删除墓碑拦截迟到旧写入；延迟与缺失分开定责。

多副本发布裁决（M8）按回执流逐文档给出**唯一结论**，不按多数派放行：

- 新内容：全部在役副本收敛到同一最高版本且正文摘要一致才 `PUBLISH_NEW`；任一副本留旧正文（`HOLD_STALE_REPLICA`）、缺回执（`HOLD_MISSING_ACK`）、同版本正文/操作分歧（`HOLD_CONTENT_DIVERGENCE`/`HOLD_OP_DIVERGENCE`）或副本内版本冲突（`HOLD_REPLICA_CONFLICT`）一律暂缓，避免同一文档两个版本。
- 下架：全部副本确认最高版本墓碑才 `TAKEDOWN_CONFIRMED`；任一副本未确认即 `HOLD_TAKEDOWN_INCOMPLETE`，显式标注文档仍可检索，禁止误报下架完成。
- 回执重试：同副本同事件同内容的重复回执幂等折叠，只增加 `retry_count`，不新增结论、不改变发布范围（范围由 `release_scope_sha256` 固化）。
- 每文档输出一条结论并附逐副本状态、源行号与裁决依据；`GET /api/release/runs/<id>/archive` 导出唯一证据 CSV（结论/隔离/发布范围/范围摘要同一证据链）。复核走乐观锁，陈旧版本返回 409。

## 演示

1. 打开首页，核对当次计算、差异和隔离原因。
2. 切换模块查看源行及裁决依据。
3. 重跑相同样例，批次号与行数保持一致。
4. 复核后导出一份证据CSV。版本已变化的复核提交返回409。

## 输入与输出

输入列见 `docs/DATA-DICTIONARY.md`。项目包含dirty.csv、clean.csv、legacy.csv，统一走实际导入路径。上传限制4MB，拒绝不带时区的时间。业务键按字符串保留，Decimal参与计算，空值不会当零。

`POST /api/runs` 接收sample或csv；`GET /api/runs`返回历史批次；`POST /api/runs/<id>/review`需要version与decision；`GET /api/runs/<id>/archive`返回唯一证据导出。

## 范围

样例均为虚构。单机复核工具不连接生产系统；不提供登录、远端数据库或自动支付。内存计算按4MB输入上限控制，SQLite事务保证结果完整写入。

## 目录

app为服务与规则，sample_data为输入，tests为测试，docs为定义与深度登记。
