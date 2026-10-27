# API 契约（v1）

> 实现规格见 [specs/07-api-service.md](specs/07-api-service.md)。任何接口行为变更必须同步修改本文件。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| Base path | `/v1` |
| 格式 | 请求与响应均为 `application/json`，字段名 `snake_case` |
| 时间 | ISO 8601 UTC，带 `Z`，如 `2026-10-27T08:00:00Z` |
| ID | `job_id`、`task_id`、生成的 `request_id` 均为 ULID 字符串 |
| ARN 参数 | 放在 query 或 body 中，不放在 path 中（ARN 含 `/` 和 `:`） |

### 1.1 请求头 / 响应头

| Header | 方向 | 说明 |
|---|---|---|
| `X-Request-ID` | 请求（可选）/ 响应（总有） | 客户端可传入自己的 ID（≤128 字符，`[A-Za-z0-9._-]`），否则服务端生成 ULID。该 ID 会出现在本次调用产生的所有日志中，包括 worker 与 broker |
| `X-Trace-Id` | 响应 | 开启 tracing（`API_TRACING_ENABLED=true`）时返回 X-Ray trace id |

### 1.2 错误格式

所有非 2xx 响应：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "account_id is required",
    "request_id": "01JBX6Y7Z8A9B0C1D2E3F4G5H6",
    "details": [{"loc": ["body", "scope"], "msg": "Value error, account_id is required"}]
  }
}
```

| HTTP | code | 场景 |
|---|---|---|
| 400 | `BAD_REQUEST` | 无法解析的请求，如非法 cursor |
| 404 | `NOT_FOUND` | 资源或 job 不存在 |
| 422 | `VALIDATION_ERROR` | 参数校验失败 |
| 403 | `ACCOUNT_ACCESS_DENIED` | broker 拒绝该账号（刷新结果中的 `error.code`） |
| 501 | `NOT_IMPLEMENTED` | 预留接口，如 `/v1/history/*` |
| 502 | `UPSTREAM_ERROR` | AWS 或 broker 返回不可重试错误 |
| 503 | `UPSTREAM_THROTTLED` | AWS 或 broker 限流，客户端可稍后重试 |
| 500 | `INTERNAL_ERROR` | 未预期错误，message 不暴露内部细节 |

### 1.3 分页

基于游标（keyset），不支持 offset。

- 请求：`limit`（1–500，默认 100）、`cursor`（上一页返回的 `next_cursor`，不透明字符串）。
- 响应：

```json
{"items": [...], "next_cursor": "eyJ2IjoxLCJrIjpb...", "total": null}
```

- `next_cursor` 为 `null` 表示没有下一页。
- `total` 仅在请求 `with_total=true` 时计算（会多一次 COUNT 查询）。
- cursor 与排序参数绑定；更换排序或过滤条件后旧 cursor 返回 400。

### 1.4 长轮询 `wait_seconds`

触发类接口支持 `wait_seconds`（0–25）。在时限内 job 到达终态返回 `200`，否则返回 `202` 与 job 当前状态，客户端用 `GET /v1/scans/{job_id}` 继续查询。

## 2. 数据结构

### 2.1 Resource

```json
{
  "resource_type": "AWS::EC2::Instance",
  "account_id": "123456789012",
  "region": "eu-west-1",
  "resource_id": "i-0abc123def4567890",
  "arn": "arn:aws:ec2:eu-west-1:123456789012:instance/i-0abc123def4567890",
  "name": "web-01",
  "tags": {"Name": "web-01", "Env": "prod"},
  "relations": {"vpc_id": "vpc-0a1b2c3d", "subnet_id": "subnet-0a1b2c3d"},
  "data": {"InstanceId": "i-0abc123def4567890", "State": {"Name": "running"}},
  "first_seen_at": "2026-10-01T00:05:12Z",
  "last_changed_at": "2026-10-27T04:03:44Z",
  "observed_at": "2026-10-27T04:03:44Z",
  "deleted_at": null,
  "verified_at": "2026-10-27T08:02:10Z"
}
```

| 字段 | 说明 |
|---|---|
| `data` | AWS describe 接口的原始返回（去掉 collector 声明的噪声字段）。列表接口默认不返回，需 `view=full` |
| `relations` | 与其他资源的关系，结构由各 collector 定义，见 [specs/04-collectors.md](specs/04-collectors.md) |
| `observed_at` | 这份数据从 AWS 读取的时间 |
| `last_changed_at` | 内容最后一次变化的时间 |
| `verified_at` | 所属 scope 最后一次成功完整扫描的时间（来自 `scope_state`），表示数据至少在该时刻是准确的。可能早于 `observed_at`（单 ARN 刷新只更新 `observed_at`） |
| `deleted_at` | 非空表示资源在 AWS 已不存在（软删除，默认保留 180 天，可配置） |

### 2.2 ScanScope

```json
{"account_id": "123456789012", "region": "eu-west-1", "resource_type": "AWS::ECS::Service"}
```

| 组合 | 含义 |
|---|---|
| `account_id` | 该账号所有已启用受支持 region（含 global）的所有类型 |
| `account_id` + `region` | 该 region 的所有类型；`region` 可为 `global` |
| `account_id` + `resource_type` | 该类型在所有可扫描 region 中扫描：regional 类型取「账号已启用的 region ∩ 扫描 region（部署了 worker 的 region）」，每个 region 一个 task；global 类型即 `global` |
| `account_id` + `region` + `resource_type` | 该 region 的某一类型 |
| `arn`（单独使用） | 单个资源；等价于 `POST /v1/resources/refresh` 只传一个 ARN |

校验（不通过返回 422 `VALIDATION_ERROR`，不创建 job）：

- `arn` 不能与其他字段同时出现；其余组合必须有 `account_id`。
- `region` 必须是 `global` 或扫描 region 之一；非 `global` 的 `region` 还必须是该账号已启用的 region（`account_region`，缓存过期时调用 `describe_regions` 刷新）。
- `resource_type` 必须已注册；指定 `region` 时须适用于该 region（global 类型只能配 `global`）。
- 只有 `resource_type` 没有 `region` 时，上述交集为空即返回 422（该类型对此账号不可扫描）。

### 2.3 ScanJob

```json
{
  "job_id": "01JBX7AAAAAAAAAAAAAAAAAAAA",
  "trigger": "api",
  "scope": {"account_id": "123456789012", "region": "eu-west-1"},
  "status": "running",
  "request_id": "01JBX6Y7Z8A9B0C1D2E3F4G5H6",
  "deduplicated": false,
  "created_at": "2026-10-27T08:00:00Z",
  "started_at": "2026-10-27T08:00:01Z",
  "finished_at": null,
  "stats": {"tasks_total": 1, "tasks_done": 0, "created": 0, "updated": 0, "deleted": 0, "unchanged": 0},
  "tasks": null
}
```

- `status`：`queued | running | succeeded | partial | failed | timed_out`。`partial` 表示部分 task 或部分类型失败。
- `tasks` 仅在 `include_tasks=true` 时返回，元素为 ScanTask。

### 2.4 ScanTask

```json
{
  "task_id": "01JBX7BBBBBBBBBBBBBBBBBBBB",
  "account_id": "123456789012",
  "region": "eu-west-1",
  "resource_types": null,
  "status": "partial",
  "attempts": 2,
  "result": {
    "AWS::EC2::Instance": {"status": "succeeded", "created": 1, "updated": 3, "deleted": 0, "unchanged": 120, "duration_ms": 2310},
    "AWS::ECS::Service": {"status": "failed", "error_code": "THROTTLED", "error_message": "Rate exceeded"}
  },
  "error_code": null,
  "error_message": null,
  "started_at": "2026-10-27T08:00:02Z",
  "finished_at": "2026-10-27T08:02:40Z"
}
```

`status`：`queued | running | retrying | succeeded | partial | failed | auth_denied | timed_out`。

`error_code` / `error_message`：task 整体失败的原因（例如 `DISPATCH_FAILED`、`AUTH_DENIED`、超时），与某个类型无关；单个类型的错误在 `result` 中。

## 3. 接口

### 3.1 `GET /v1/resources` 资源列表

| 参数 | 类型 | 说明 |
|---|---|---|
| `account_id` | string，可重复 | |
| `region` | string，可重复 | 包括 `global` |
| `resource_type` | string，可重复 | |
| `tag` | string，可重复 | `Key=Value` 精确匹配（在第一个 `=` 处分割）；只写 `Key` 表示存在该 tag。key 含 `=` 时请用 `POST /v1/resources/search` |
| `name_prefix` | string | |
| `changed_since` | datetime | `last_changed_at >= changed_since` |
| `include_deleted` | bool，默认 false | |
| `view` | `summary`（默认）\| `full` | `summary` 不含 `data` |
| `sort` | `last_changed_at`（默认）\| `resource_id` | |
| `order` | `desc`（默认）\| `asc` | |
| `limit` / `cursor` / `with_total` | | 见 §1.3 |

响应 200：`Page[Resource]`。

#### 路径形式

按 scope 层级获取资源，路径中的段等价于对应的单值过滤条件；其余参数（`tag`、`name_prefix`、`changed_since`、`include_deleted`、`view`、`sort`、`order`、`limit`、`cursor`、`with_total`）与上表相同。

| 路径 | 等价于 | 响应 |
|---|---|---|
| `GET /v1/resources/all` | 不带 scope 过滤的 `GET /v1/resources` | `Page[Resource]` |
| `GET /v1/resources/{account_id}` | `account_id=…` | `Page[Resource]` |
| `GET /v1/resources/{account_id}/{region}` | `account_id=…&region=…` | `Page[Resource]` |
| `GET /v1/resources/{account_id}/{region}/{resource_type}` | `account_id=…&region=…&resource_type=…` | `Page[Resource]` |
| `GET /v1/resources/{account_id}/{region}/{arn}` | `GET /v1/resources/by-arn?arn=…`（只接受 `include_deleted`） | `Resource`（full 视图） |

- `account_id` 必须是 12 位数字，否则 404；`region` 为 `global` 或扫描 region 之一，否则 422；`resource_type` 必须已注册且与 region 匹配（global 类型只能配 `global`），否则 422。
- 第三段以 `arn:` 开头时按 ARN 处理，ARN 中的 `/` 原样放在路径中（如 `/v1/resources/123456789012/eu-west-1/arn:aws:ec2:eu-west-1:123456789012:vpc/vpc-0a`）。资源不属于路径中的账号与 region（global 资源为 `global`）时返回 404。
- `cursor` 与发出它的路径绑定，换到其他路径使用返回 400。

### 3.2 `POST /v1/resources/search` 资源搜索

GET 接口的完整版，过滤条件放在 body 中：

```json
{
  "account_ids": ["123456789012"],
  "regions": ["eu-west-1"],
  "resource_types": ["AWS::EC2::Instance"],
  "tags": {"Env": "prod"},
  "tag_keys": ["Owner"],
  "name_prefix": "web-",
  "changed_since": "2026-10-20T00:00:00Z",
  "data_jsonpath": "$.State.Name == \"running\"",
  "include_deleted": false,
  "view": "summary",
  "sort": "last_changed_at",
  "order": "desc",
  "limit": 100,
  "cursor": null,
  "with_total": false
}
```

`data_jsonpath` 使用 PostgreSQL `jsonb_path_match` 语法，查询有超时保护（默认 5 秒，超时返回 422 `QUERY_TIMEOUT`）。建议同时指定 `resource_types`。

响应 200：`Page[Resource]`。

### 3.3 `GET /v1/resources/by-arn` 单个资源

| 参数 | 说明 |
|---|---|
| `arn` | 必填 |
| `include_deleted` | 默认 false |

响应 200：`Resource`（总是 full 视图）。404：不存在或不支持该 ARN。

### 3.4 `GET /v1/resources/stats` 数量统计

| 参数 | 说明 |
|---|---|
| `group_by` | 逗号分隔，取值 `account_id`、`region`、`resource_type` 的任意组合；默认 `resource_type` |
| `account_id` / `region` / `resource_type` | 可重复，过滤 |
| `include_deleted` | 默认 false |

```json
{
  "groups": [
    {"account_id": "123456789012", "resource_type": "AWS::EC2::Instance", "count": 124},
    {"account_id": "123456789012", "resource_type": "AWS::ECS::Service", "count": 1532}
  ],
  "total": 1656
}
```

不带 tag/name 等过滤时直接读 `scope_state` 预聚合结果；否则走 `COUNT`。

### 3.5 `POST /v1/resources/refresh` 刷新资源

```json
{"arns": ["arn:aws:ec2:eu-west-1:123456789012:instance/i-0abc123def4567890"], "wait_seconds": 20}
```

- `arns`：1–100 个（重复的会被去重）。每个 ARN 都由资源所在 region 的 worker 执行（global 资源为 us-east-1 的 worker）。
- `wait_seconds`：默认 20；为 0 时立即返回 202，各 ARN 状态为 `pending`。

响应 200（全部完成）/ 202（未在时限内完成或转异步）：

```json
{
  "job": {"job_id": "01JBX7...", "status": "succeeded", "...": "..."},
  "results": [
    {
      "arn": "arn:aws:ec2:eu-west-1:123456789012:instance/i-0abc123def4567890",
      "status": "updated",
      "resource": {"...": "Resource，full 视图"},
      "error": null
    }
  ]
}
```

`results[].status`：

| 值 | 含义 |
|---|---|
| `created` | 库中原本没有，已新增 |
| `updated` | 内容有变化，已更新 |
| `unchanged` | 内容无变化（仍会更新 `observed_at`） |
| `deleted` | AWS 中已不存在，已软删除 |
| `not_found` | AWS 与库中都不存在 |
| `unsupported_type` | 没有能处理该 ARN 的 collector |
| `pending` | 在 `wait_seconds` 内尚未完成（响应为 202），稍后用 `GET /v1/scans/{job_id}` 查询 |
| `error` | 失败，`error` 字段为 `{code, message}`。code 除 §1.2 中的值外，还可能是 `ACCOUNT_UNRESOLVED`（ARN 中没有账号，且库中找不到该资源，请先对所属账号发起扫描）或 `ACCOUNT_AMBIGUOUS`（库中匹配到多个账号） |

ARN 中不含账号或 region 的资源（AMI、S3 bucket、Route 53 hosted zone、旧格式 ECS service ARN），服务端会根据已扫描的数据补全。


注意：EC2 等服务的 API 是最终一致的。修改资源后立即刷新可能读到旧数据，建议修改完成后间隔几秒再调用。

### 3.6 `POST /v1/scans` 触发扫描

```json
{"scope": {"account_id": "123456789012", "region": "eu-west-1"}, "wait_seconds": 0}
```

- 相同 scope 已有 `queued/running` 的 job 时返回该 job，`deduplicated=true`。
- 响应：202（未完成）/ 200（`wait_seconds` 内完成），body 为 ScanJob。
- `scope.arn` 时行为同 3.5 的单个 ARN，但响应为 ScanJob。

### 3.7 `GET /v1/scans/{job_id}` 查询扫描任务

| 参数 | 说明 |
|---|---|
| `include_tasks` | 默认 false |

响应 200：ScanJob。404：不存在。

### 3.8 `GET /v1/scans` 扫描任务列表

参数：`status`（可重复）、`trigger`（`schedule | api`）、`account_id`、`created_since`、`limit`、`cursor`。按 `created_at desc` 排序。响应：`Page[ScanJob]`（不含 tasks）。

### 3.9 `GET /v1/accounts/{account_id}/coverage` 账号扫描覆盖情况

用于排查"某类资源为什么没有/不新"：

```json
{
  "account_id": "123456789012",
  "enabled_regions": ["ap-east-1", "eu-west-1"],
  "regions_checked_at": "2026-10-27T00:01:00Z",
  "scopes": [
    {
      "region": "eu-west-1",
      "resource_type": "AWS::EC2::Instance",
      "status": "succeeded",
      "resource_count": 124,
      "last_attempt_at": "2026-10-27T08:02:10Z",
      "last_success_at": "2026-10-27T08:02:10Z",
      "error_code": null,
      "error_message": null
    }
  ]
}
```

`scopes[].status`：`succeeded | failed | auth_denied | never`。

### 3.10 `GET /v1/resource-types` 已注册的资源类型

```json
{
  "items": [
    {"resource_type": "AWS::EC2::Instance", "service": "ec2", "is_global": false, "depends_on": [], "enabled_regions": ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"]},
    {"resource_type": "AWS::IAM::Role", "service": "iam", "is_global": true, "depends_on": [], "enabled_regions": ["global"]}
  ]
}
```

`enabled_regions`：regional 类型为全部扫描 region，global 类型为 `["global"]`。

### 3.11 健康检查

- `GET /healthz`：进程存活，总是 200 `{"status": "ok"}`。
- `GET /readyz`：检查数据库连接，失败返回 503。

两者不记录访问日志。

### 3.12 内置 UI

- `GET /ui/`：浏览资源、触发扫描并跟踪进度的静态页面（实现见 [spec 07 §9](specs/07-api-service.md#9-内置-uiui)），只调用本文档中的 `/v1` 接口。
- `GET /`：重定向到 `/ui/`。
- 不属于 API 契约，不出现在 OpenAPI 中；`API_UI_ENABLED=false` 时不提供。

### 3.13 预留

`/v1/history/*` 当前返回 501 `NOT_IMPLEMENTED`。设计见 [specs/09-reserved-extensions.md](specs/09-reserved-extensions.md)。
