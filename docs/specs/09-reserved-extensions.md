# Spec 09：预留扩展（v1 不实现）

本文件描述 v1 **只预留接口、不实现**的两项能力。v1 的代码必须保证：以后实现这些能力时，只需新增实现类和配置，不需要修改 collector、`ScanEngine`、`Reconciler` 或数据模型。

## A. 方案 B：region 之间没有 peering

### A.1 正式方案

非 VPC Lambda 集中部署在 ap-east-1（设计依据见 [design.md §7.2](../design.md)）。

```
ap-east-1
  VPC 内：broker λ(+Redis)、account-directory λ、Aurora PG、FastAPI、Ingester λ
  VPC 外：planner λ、scan-worker λ、refresh λ
  planner λ ──► SQS FIFO（MessageGroupId = account#region）──► scan-worker λ ──► 各 region 公网 AWS API
  scan-worker λ ──► S3 结果桶（NDJSON.gz + _manifest.json）──► S3 事件 ──► SQS ──► Ingester λ ──► PG
  FastAPI ── 同步 Invoke ──► refresh λ ──► 目标 region API；记录随返回值带回，API 写库
```

### A.2 v1 预留的接口与 v1 的处理

| 接口（`transport.py`） | v1（方案 A）实现 | 方案 B 实现（以后） |
|---|---|---|
| `Dispatcher` | `SqsDirectDispatcher` | `SqsFifoDispatcher`：一个 FIFO 队列，`MessageGroupId=account#region`，`MessageDeduplicationId=task_id:attempt` |
| `CredentialProvider` | `BrokerLambdaProvider` | 同一实现（非 VPC Lambda 也能 Invoke broker） |
| `ResultSink` | `PostgresResultSink` | `S3SnapshotResultSink`：`load_index` 返回空（差异由 Ingester 计算）；`apply` 把全量结果写成 S3 对象 |
| `RefreshExecutor` | `InlineRefreshExecutor` | `LambdaInvokeRefreshExecutor`：同步 Invoke refresh λ，拿到 `ResourceRecord` 后在 API 进程内调用 `PostgresResultSink.apply_refresh` |

- 配置 `INVENTORY_TRANSPORT=lambda` 在 v1 启动时直接抛出 `NotImplementedFeatureError`。
- v1 **不创建**上述任何方案 B 类的空壳。

### A.3 方案 B 需要的额外改动（实现时再做）

1. task 粒度改为 `(account, region, collector 组)`：`Scheduler.expand` 根据 `registry.groups` 拆分（适应 Lambda 15 分钟上限）。
2. Lambda 版 worker 入口 `handlers/worker_lambda.py`：Powertools `BatchProcessor` + 复用 `TaskExecutor`。
3. Ingester：读取 manifest，确认所有数据文件到齐、条数一致后，用同一个 `reconcile()` 计算差异并写库；`complete=false` 时不执行删除。
4. S3 结果格式：

```
s3://<bucket>/results/job_id=<job>/account=<acct>/region=<region>/type=<type>/
    part-0000.ndjson.gz          # 每行一个 ResourceRecord（JSON）
    _manifest.json               # {"complete": true, "count": 1532, "scan_started_at": "...", "parts": 1, "task_id": "...", "attempt": 1}
```

5. 前提检查（实现前必须确认）：目标账号策略与 SCP 没有 `aws:SourceIp` / `aws:SourceVpc` / `aws:SourceVpce` 限制；公司允许非 VPC Lambda；ap-east-1 的 Lambda 并发配额足够。

## B. 变更历史

### B.1 v1 已具备的部分

- `Reconciler` 在每次对账时生成 `ChangeEvent`（spec 03 §1），`PostgresResultSink` 在写库的同一事务中调用 `ChangeSink.emit(conn, events)`。
- v1 默认 `NullChangeSink`（配置 `INVENTORY_HISTORY_SINK=null`）。
- `scan_task.result` 中记录每类资源的 created / updated / deleted / unchanged 数量。

### B.2 以后的实现

1. `OutboxChangeSink`（`INVENTORY_HISTORY_SINK=outbox`）：在同一事务中写入 outbox 表，保证与当前状态一致。

```sql
CREATE TABLE change_outbox (
  seq           bigserial   PRIMARY KEY,
  resource_type text        NOT NULL,
  account_id    char(12)    NOT NULL,
  region        text        NOT NULL,
  resource_id   text        NOT NULL,
  arn           text        NOT NULL,
  change_type   text        NOT NULL,      -- created | updated | deleted
  changed_at    timestamptz NOT NULL,
  job_id        text        NOT NULL,
  old_hash      bytea,
  new_hash      bytea,
  data          jsonb                       -- 变更后的完整数据；deleted 为 NULL
);
```

2. Exporter（ap-east-1，每 5 分钟）：按 `seq` 取一批 → Firehose（转换为 Parquet、合并小文件）→ 确认写入后删除这批行。
3. S3 布局：

```
s3://<history-bucket>/changes/dt=YYYY-MM-DD/resource_type=<type>/...parquet
s3://<history-bucket>/snapshots/dt=YYYY-MM-DD/resource_type=<type>/...parquet   # 每日全量
```

4. Glue 表使用 partition projection（`dt`、`resource_type`），不需要 crawler。
5. T 时刻的状态 = T 之前最近一次快照 + 当天截至 T 的变更，每个 resource key 取最后一条。
6. API（当前返回 501）：

| 接口 | 说明 |
|---|---|
| `GET /v1/history/changes?arn=&since=&until=` | 某个资源的变更记录；经 Athena 查询，支持 `wait_seconds` |
| `POST /v1/history/queries` | 提交时间点查询（`{"at": ..., "account_id": ..., "region": ..., "resource_type": ...}`），返回 `query_id` |
| `GET /v1/history/queries/{query_id}` | 查询状态与分页结果 |

限制：只有切换到 outbox 之后发生的变更才会进入历史。
