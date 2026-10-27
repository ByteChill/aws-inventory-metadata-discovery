# Spec 02：数据模型与存储（M2）

## 目标

定义 Aurora PostgreSQL 中的全部表、索引、分区与迁移，并实现同步 Repository（worker、planner、API 刷新共用）与 API 的异步只读查询。

## 范围

做：DDL、Alembic 迁移、`inventory/db.py`。
不做：`change_outbox`（历史，预留，见 spec 09）。

## 文件清单

```
migrations/env.py  migrations/helpers.py  migrations/versions/0001_initial.py
alembic.ini
inventory/db.py
tests/db/...   tests/fixtures/db/...
```

## 1. 连接

- 驱动：psycopg 3，同步与异步共用。URL 形如 `postgresql+psycopg://...`。
- 使用 SQLAlchemy **Core**（不使用 ORM）。表对象定义在 `db.py` 中，迁移脚本手写（不使用 autogenerate 结果直接提交）。
- 每个连接设置 `application_name=<service_name>`；`statement_timeout`：worker/planner 60s，API 10s。
- AWS 上经 RDS Proxy 连接，密码来自 `database_secret_arn`；本地使用 `database_url`。

## 2. 表结构

### 2.1 `resource`

```sql
CREATE TABLE resource (
  resource_type   text        NOT NULL,
  account_id      char(12)    NOT NULL,
  region          text        NOT NULL,              -- 'global' 表示全局资源
  resource_id     text        NOT NULL,
  arn             text        NOT NULL,
  name            text,
  tags            jsonb       NOT NULL DEFAULT '{}',  -- {key: value}
  relations       jsonb       NOT NULL DEFAULT '{}',
  data            jsonb       NOT NULL,
  content_hash    bytea       NOT NULL,
  observed_at     timestamptz NOT NULL,
  first_seen_at   timestamptz NOT NULL,
  last_changed_at timestamptz NOT NULL,
  deleted_at      timestamptz,
  last_job_id     text,
  PRIMARY KEY (resource_type, account_id, region, resource_id)
) PARTITION BY LIST (resource_type);

CREATE INDEX ix_resource_arn      ON resource (arn);
CREATE INDEX ix_resource_scope    ON resource (account_id, region) WHERE deleted_at IS NULL;
CREATE INDEX ix_resource_changed  ON resource (last_changed_at, account_id, region, resource_id);
CREATE INDEX ix_resource_name     ON resource (name text_pattern_ops);
CREATE INDEX ix_resource_tags     ON resource USING gin (tags jsonb_path_ops);
CREATE INDEX ix_resource_deleted  ON resource (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE TABLE r_default PARTITION OF resource DEFAULT;
```

分区命名：`r_` + resource type 去掉 `AWS::` 后转小写蛇形，`::` 换成 `_`。例：`AWS::EC2::Instance` → `r_ec2_instance`，`AWS::ECS::TaskDefinition` → `r_ecs_task_definition`。函数 `partition_name(resource_type)` 放在 `db.py`，迁移与测试共用。

v1 的分区：

| resource_type | 分区表 |
|---|---|
| `AWS::EC2::VPC` | `r_ec2_vpc` |
| `AWS::EC2::Subnet` | `r_ec2_subnet` |
| `AWS::EC2::SecurityGroup` | `r_ec2_security_group` |
| `AWS::EC2::Instance` | `r_ec2_instance` |
| `AWS::EC2::Image` | `r_ec2_image` |
| `AWS::ECS::Cluster` | `r_ecs_cluster` |
| `AWS::ECS::Service` | `r_ecs_service` |
| `AWS::ECS::TaskDefinition` | `r_ecs_task_definition` |
| `AWS::S3::Bucket` | `r_s3_bucket` |
| `AWS::IAM::Role` | `r_iam_role` |
| `AWS::IAM::User` | `r_iam_user` |
| `AWS::IAM::ManagedPolicy` | `r_iam_managed_policy` |
| `AWS::Route53::HostedZone` | `r_route53_hosted_zone` |

各分区可以单独加表达式索引（例如 `CREATE INDEX ON r_ec2_instance ((data->'State'->>'Name'))`），需要时在新迁移中添加。

### 2.2 `scope_state`

```sql
CREATE TABLE scope_state (
  account_id      char(12)    NOT NULL,
  region          text        NOT NULL,
  resource_type   text        NOT NULL,
  status          text        NOT NULL,   -- succeeded | failed | auth_denied | skipped_no_endpoint
  resource_count  integer,                -- 最近一次成功扫描时的数量
  last_attempt_at timestamptz NOT NULL,
  last_success_at timestamptz,
  last_job_id     text,
  error_code      text,
  error_message   text,
  PRIMARY KEY (account_id, region, resource_type)
);
```

### 2.3 `account_region`

```sql
CREATE TABLE account_region (
  account_id char(12)    NOT NULL,
  region     text        NOT NULL,
  enabled    boolean     NOT NULL,
  checked_at timestamptz NOT NULL,
  PRIMARY KEY (account_id, region)
);
```

### 2.4 `scan_job`

```sql
CREATE TABLE scan_job (
  job_id      text        PRIMARY KEY,           -- ULID
  trigger     text        NOT NULL,              -- schedule | api
  scope       jsonb       NOT NULL,
  scope_hash  text        NOT NULL,              -- sha256(规范化 scope JSON)
  status      text        NOT NULL,              -- queued | running | succeeded | partial | failed | timed_out
  request_id  text,
  flags       jsonb       NOT NULL DEFAULT '{}', -- {"full_refresh": true}
  stats       jsonb       NOT NULL DEFAULT '{}',
  error       text,
  created_at  timestamptz NOT NULL,
  started_at  timestamptz,
  finished_at timestamptz,
  deadline_at timestamptz
);
CREATE UNIQUE INDEX ux_scan_job_active_scope ON scan_job (scope_hash)
  WHERE status IN ('queued', 'running');
CREATE INDEX ix_scan_job_created ON scan_job (created_at DESC);
```

`ux_scan_job_active_scope` 保证同一 scope 只有一个未完成 job（API 去重与"定时任务重叠"检测都依赖它）。定时任务的 scope 固定为 `{"all": true}`。

### 2.5 `scan_task`

```sql
CREATE TABLE scan_task (
  task_id        text        PRIMARY KEY,
  job_id         text        NOT NULL REFERENCES scan_job(job_id),
  account_id     char(12)    NOT NULL,
  region         text        NOT NULL,
  resource_types text[],                  -- NULL 表示该 region 所有可用类型
  resource_ids   text[],                  -- 仅批量刷新时使用，此时 resource_types 只有一个元素
  queue          text        NOT NULL,    -- scheduled | ondemand | inline
  status         text        NOT NULL,    -- queued | running | retrying | succeeded | partial | failed | auth_denied | timed_out
  attempts       integer     NOT NULL DEFAULT 0,
  result         jsonb       NOT NULL DEFAULT '{}',   -- {resource_type: TypeResult}
  error_code     text,
  error_message  text,
  worker_id      text,
  queued_at      timestamptz NOT NULL,
  started_at     timestamptz,
  finished_at    timestamptz,
  updated_at     timestamptz NOT NULL     -- worker 心跳会更新
);
CREATE INDEX ix_scan_task_job    ON scan_task (job_id, status);
CREATE INDEX ix_scan_task_active ON scan_task (status, updated_at)
  WHERE status IN ('queued', 'running', 'retrying');
```

终态：`succeeded | partial | failed | auth_denied | timed_out`。

## 3. 内容 hash

```python
def content_hash(record: ResourceRecord) -> bytes:
    payload = {"arn": ..., "name": ..., "tags": ..., "relations": ..., "data": ...}
    return sha256(canonical_json(payload)).digest()
```

`canonical_json`：`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)`，其中 datetime 转为 UTC ISO 8601，`bytes` 转 base64，其他未知类型直接报错（防止静默产生不稳定的 hash）。

## 4. Repository（同步，`inventory/db.py`）

```python
@dataclass(frozen=True)
class IndexEntry:
    content_hash: bytes
    observed_at: datetime
    deleted: bool

class ResourceRepository:
    def load_index(self, conn, resource_type, account_id, region) -> dict[str, IndexEntry]: ...
    def upsert(self, conn, records: Sequence[ResourceRecord], job_id: str | None) -> None: ...
    def soft_delete(self, conn, resource_type, account_id, region, resource_ids, before: datetime, job_id) -> int: ...
    def get(self, conn, key: ResourceKey) -> ResourceRow | None: ...
    def find_account(self, conn, resource_type, resource_id, region: str | None) -> list[tuple[str, str]]: ...
    def purge_deleted(self, conn, older_than: datetime, batch_size: int = 5000) -> int: ...

class ScopeStateRepository:      upsert(...), list_for_account(...)
class AccountRegionRepository:   get_enabled(account_id, max_age) -> list[str] | None, replace(account_id, regions, checked_at)
class JobRepository:             见下
```

### 4.1 upsert（带并发保护）

```sql
INSERT INTO resource AS r (...) VALUES (...)
ON CONFLICT (resource_type, account_id, region, resource_id) DO UPDATE SET
  arn = EXCLUDED.arn, name = EXCLUDED.name, tags = EXCLUDED.tags,
  relations = EXCLUDED.relations, data = EXCLUDED.data,
  last_changed_at = CASE
      WHEN r.content_hash IS DISTINCT FROM EXCLUDED.content_hash OR r.deleted_at IS NOT NULL
      THEN EXCLUDED.observed_at ELSE r.last_changed_at END,
  content_hash = EXCLUDED.content_hash,
  observed_at = EXCLUDED.observed_at,
  deleted_at = NULL,
  last_job_id = EXCLUDED.last_job_id
WHERE r.observed_at < EXCLUDED.observed_at;
```

- 插入时 `first_seen_at = last_changed_at = observed_at`。
- 每批最多 500 行。
- `WHERE r.observed_at < EXCLUDED.observed_at` 保证较旧的数据不会覆盖较新的数据。

### 4.2 软删除（带并发保护）

```sql
UPDATE resource SET deleted_at = :now, last_changed_at = :now, last_job_id = :job_id
WHERE resource_type = :t AND account_id = :a AND region = :r
  AND resource_id = ANY(:ids) AND deleted_at IS NULL
  AND observed_at < :before;          -- before = 本次扫描开始时间
```

扫描开始后被单 ARN 刷新过的资源（`observed_at >= before`）不会被删除。

### 4.3 `find_account`

用于 ARN 中不含账号的资源（AMI、S3 bucket、Route 53 hosted zone）：按 `(resource_type, resource_id[, region])` 查找所有匹配的 `(account_id, region)`，调用方处理"0 个 / 多个"的情况。

### 4.4 `JobRepository`

| 方法 | 说明 |
|---|---|
| `create_job(conn, trigger, scope, request_id, flags, deadline_at) -> (ScanJob, created: bool)` | 依赖 `ux_scan_job_active_scope`：冲突时返回已有的活跃 job，`created=False` |
| `create_tasks(conn, job_id, tasks: list[NewTask])` | 批量插入，`status=queued` |
| `claim_task(conn, task_id, worker_id, stale_after) -> ScanTask \| None` | 见下 |
| `heartbeat(conn, task_id)` | 更新 `updated_at` |
| `finish_task(conn, task_id, status, result, error_code, error_message)` | 写终态；随后调用 `refresh_job_status` |
| `mark_retrying(conn, task_id, result)` | |
| `refresh_job_status(conn, job_id)` | `SELECT ... FOR UPDATE` 锁住 job；若所有 task 都已终态则计算 job 终态与 stats；否则若 job 为 `queued` 则改为 `running` |
| `timeout_stale(conn, stale_before) -> list[job_id]` | 把 `updated_at < stale_before` 的非终态 task 置为 `timed_out`，返回受影响的 job |
| `get_job / list_jobs / list_tasks` | |

`claim_task`：

```sql
UPDATE scan_task SET status = 'running', attempts = attempts + 1, worker_id = :w,
       started_at = COALESCE(started_at, :now), updated_at = :now
WHERE task_id = :id
  AND (status IN ('queued', 'retrying')
       OR (status = 'running' AND updated_at < :stale_before))
RETURNING *;
```

返回 `None` 表示重复投递，或者 task 已被其他 worker 处理。

job 终态计算规则：

| 条件 | job.status |
|---|---|
| 所有 task 为 `succeeded` 或 `auth_denied` | `succeeded` |
| 所有 task 为 `failed`（不计 `auth_denied`） | `failed` |
| 其他情况（含 `partial`、`timed_out`） | `partial`；deadline 到期由 finalize 判定的为 `timed_out` |

`stats` 汇总：`tasks_total`、`tasks_done`、各状态计数，以及各类资源的 `created / updated / deleted / unchanged` 总数。

## 5. API 只读查询（异步，`inventory/db.py`）

```python
class ResourceQueries:
    async def list(self, filters: ResourceFilters, page: PageRequest) -> Page[ResourceRow]: ...
    async def get_by_arn(
        self, arn: str, resource_type: str | None, include_deleted: bool
    ) -> ResourceRow | None: ...
    async def stats(self, group_by, filters) -> StatsResult: ...
    async def coverage(self, account_id: str) -> Coverage: ...
```

- keyset 分页：排序键为 `(sort_field, resource_type, account_id, region, resource_id)`，保证全序。
- `get_by_arn`：优先用 collector 注册表把 ARN 解析成 resource key 后按主键查询（只命中一个分区）；无法解析时退回 `ix_resource_arn`。
- 搜索的 `data_jsonpath` 使用 `jsonb_path_match(data, :path)`，查询前 `SET LOCAL statement_timeout = <search_timeout_ms>`。

## 6. 迁移

- `migrations/helpers.py` 提供 `create_resource_partition(op, resource_type)` 与 `drop_resource_partition(op, resource_type)`。
- 新增资源类型时**必须**新增一个迁移来创建分区。
- 测试 `test_every_collector_has_partition`：对迁移后的数据库，检查注册表中的每个 resource type 都有对应分区，并且 `r_default` 中没有数据。

## 测试要求（集成测试，需要 `make up`）

| 用例 | fixture |
|---|---|
| `alembic upgrade head` / `downgrade base` 可往返 | — |
| upsert：新增、内容变化、内容不变、旧数据不覆盖新数据、已删除资源重新出现 | `db/upsert_cases.json` |
| 软删除：`observed_at >= before` 的行不被删除 | `db/soft_delete_cases.json` |
| 每个 resource type 的数据写入正确分区 | — |
| `claim_task`：重复 claim 返回 None；心跳过期后可以重新 claim | — |
| `refresh_job_status`：各种 task 状态组合下的 job 终态 | `db/job_status_cases.json` |
| keyset 分页：翻页无重复、无遗漏；cursor 与过滤条件绑定 | `db/resources.seed.json` |
| `content_hash`：key 顺序不同但内容相同时 hash 一致；遇到未知类型报错 | `db/hash_cases.json` |

## 验收标准

- 集成测试全部通过；`db.py` 覆盖率 ≥ 85%。
- 在 10 万行种子数据下，按 `(account_id, region, resource_type)` 过滤的列表查询 P95 < 50ms（本地 PostgreSQL，测试脚本放在 `tests/perf/`，不进入默认测试集）。

## 实现修订（2026-10-27，与上文冲突时以本节为准）

- `IndexEntry` 增加 `arn` 字段（`load_index` 一并读取），用于给删除事件填 ARN。
- `soft_delete` 同时把 `observed_at` 设为删除时间：开始得更早的扫描不能再把它"复活"。
- `finish_task(..., worker_id=None)` / `mark_retrying(..., worker_id=None) -> bool`：只更新非终态的 task；传入 `worker_id` 时只有当前持有者能更新（防止心跳过期被接管后，旧 worker 覆盖新结果）。
- `refresh_job_status`：先无锁检查是否还有未结束的 task（有则直接返回），全部结束后才加锁，用 SQL 聚合（`aggregate_stats`）计算 stats，不加载逐条 task。job 的 `flags.dispatching = true` 时不会结束（定时任务分批投递，见 spec 05）；`mark_dispatched(job_id)` 清除该标志并刷新状态。
- `timeout_stale_tasks` 只处理 `running` / `retrying`；`queued` 的 task 没有心跳，由 job deadline（`timeout_job`）兜底。
- 迁移 `0002`：`ix_resource_type_id (resource_type, resource_id)`，供 `find_account` 使用；`id_suffix` 匹配对 `%` / `_` 做转义。
- 迁移辅助函数签名为 `create_resource_partition(resource_type)` / `drop_resource_partition(resource_type)`（内部使用 `alembic.op`）。
- API 的异步只读查询放在 `inventory/api/queries.py`，不在 `db.py`。
- 测试 fixture：`db/records.json`（具名记录）、`db/job_status_cases.json`、`db/hash_cases.json`；迁移往返测试为 `test_migrations_round_trip`。
