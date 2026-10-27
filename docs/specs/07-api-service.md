# Spec 07：API 服务实现（M4）

## 目标

按 [api.md](../api.md) 实现 FastAPI 服务，包括请求 ID 与链路追踪、统一错误格式、查询、触发扫描与单 ARN 刷新。

## 范围

做：`inventory/api/` 全部文件、`InlineRefreshExecutor`（`transport.py`）。
不做：`/v1/history/*`（固定返回 501）。

## 文件清单

```
inventory/api/app.py          # create_app(settings)、lifespan、依赖注入、tracing 初始化
inventory/api/middleware.py   # RequestContextMiddleware
inventory/api/errors.py       # 异常处理器、ErrorResponse
inventory/api/schemas.py      # 与 api.md 一一对应的 pydantic 模型
inventory/api/resources.py    # /v1/resources*、/v1/resource-types、/v1/accounts/{id}/coverage
inventory/api/scans.py        # /v1/scans*、/v1/resources/refresh
inventory/transport.py        # RefreshExecutor 接口 + InlineRefreshExecutor
tests/api/...  tests/fixtures/api/...
```

## 1. 配置（`ApiSettings`，前缀 `API_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` | 8080 | |
| `db_pool_size` / `db_max_overflow` | 10 / 10 | 异步与同步 engine 各一套 |
| `statement_timeout_ms` | 10000 | |
| `search_timeout_ms` | 5000 | `data_jsonpath` 查询 |
| `wait_max_seconds` | 25 | |
| `wait_poll_interval_ms` | 300 | |
| `refresh_inline_max` | 20 | 超过即转异步 |
| `refresh_max_arns` | 100 | |
| `refresh_parallelism` | 8 | 单个请求内并发刷新的 ARN 数 |
| `refresh_thread_limit` | 32 | 整个进程内同时执行刷新的线程上限 |
| `refresh_timeout_seconds` | 15 | 单个 ARN |
| `tracing_enabled` | false | |
| `otlp_endpoint` | `http://localhost:4317` | ADOT sidecar |

## 2. 应用结构

- `create_app(settings) -> FastAPI`；模块级 `app = create_app(get_api_settings())` 供 uvicorn 使用，测试中直接调用 `create_app`。
- `lifespan` 中创建并放入 `app.state`：异步 engine（查询）、同步 engine（刷新与 Repository）、`ClientFactory`、`BrokerLambdaProvider`、`SqsDirectDispatcher`、`Scheduler`、`ScanEngine`、`InlineRefreshExecutor`、`registry`；关闭时释放连接池与线程池。
- 路由通过 `Depends` 从 `app.state` 获取依赖，测试中用 `app.dependency_overrides` 替换。
- 启动参数：`uvicorn inventory.api.app:app --host 0.0.0.0 --port 8080 --workers 1 --no-access-log`（访问日志由中间件输出）。横向扩展靠 ECS 任务数。

## 3. 请求上下文与链路追踪

### 3.1 `RequestContextMiddleware`（纯 ASGI）

不使用 `BaseHTTPMiddleware`（它会在单独的 task 中运行下游，导致 contextvars 与流式响应问题）。

1. 读取 `X-Request-ID`；符合 `^[A-Za-z0-9._-]{1,128}$` 则使用，否则生成 ULID（不合法的传入值记录在日志字段 `client_request_id` 中）。
2. `bind_context(request_id=...)`，请求结束时恢复。
3. 包装 `send`：在 `http.response.start` 中加入 `X-Request-ID`；tracing 开启时加入 `X-Trace-Id`。
4. 请求结束输出一条访问日志：`method path route status duration_ms client_ip user_agent`；`/healthz`、`/readyz` 不输出。
5. 未捕获异常由异常处理器转换为 500，访问日志仍会输出。

### 3.2 下游传递

| 去向 | 方式 |
|---|---|
| 数据库 | `scan_job.request_id` |
| SQS | 消息体 `request_id` + 消息属性 `request_id` |
| broker | `BrokerLambdaProvider` 自动把上下文中的 `request_id` 作为 `correlation_id` |
| AWS API | 不传递，通过日志中的 `aws_request_id` 关联 |

### 3.3 OpenTelemetry（`tracing_enabled=true` 时）

- `TracerProvider(id_generator=AwsXRayIdGenerator())`，propagator 使用 `AwsXRayPropagator`，`BatchSpanProcessor` + OTLP gRPC exporter（发送到 ADOT sidecar）。
- 自动埋点：`FastAPIInstrumentor`、`BotocoreInstrumentor`、`SQLAlchemyInstrumentor`（同步与异步 engine）。
- 在日志 `ContextFilter` 中加入 `xray_trace_id`（格式 `1-xxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx`）。
- tracing 相关 import 放在函数内部，关闭时不加载这些依赖。

## 4. 错误处理（`api/errors.py`）

| 异常 | 响应 |
|---|---|
| `InventoryError` | `http_status` + `code`，`message` 为异常消息 |
| `RequestValidationError` | 422 `VALIDATION_ERROR`，`details` 为 pydantic 错误列表（去掉 `input` 字段，避免回显敏感输入） |
| `HTTPException` | 对应状态码，code 由状态码映射（404 → `NOT_FOUND` 等） |
| 其他 `Exception` | 500 `INTERNAL_ERROR`，message 固定为 `"internal error"`；日志记录完整堆栈 |

所有错误响应的 `error.request_id` 取自上下文。4xx 记 INFO，5xx 记 ERROR。

## 5. 刷新（`/v1/resources/refresh`）

### 5.1 ARN 解析

对每个 ARN：

1. `registry.route_arn(arn)`；无匹配 → `unsupported_type`。
2. ARN 缺少账号或 region（AMI、S3、Route 53、旧格式 ECS service）→ `ResourceRepository.find_account`：
   - 恰好 1 个匹配 → 补全；
   - 0 个 → 结果 `error`，code `ACCOUNT_UNRESOLVED`（资源从未被扫描到，请先对账号发起扫描）；
   - 多个 → 结果 `error`，code `ACCOUNT_AMBIGUOUS`。
3. 得到 `ResourceKey`。

### 5.2 执行

```python
class RefreshExecutor(Protocol):
    async def refresh(
        self, job: ScanJob, keys: Sequence[ResourceKey], wait_seconds: float
    ) -> RefreshBatchResult: ...
```

`InlineRefreshExecutor`：

1. 每个 ARN 创建一个 `scan_task(queue=inline)`。
2. 判断能否在 API 进程中直接执行：`resolver.has_endpoint(collector.service, region)` 为真即可（cell 中配置了经 peering 可达的 endpoint）。
   - 可以：在线程池中执行 `ScanEngine.refresh`（`anyio.to_thread.run_sync` + 进程级 `CapacityLimiter(refresh_thread_limit)`，单请求并发 `refresh_parallelism`，单个 ARN 超时 `refresh_timeout_seconds`）。
   - 不可以（例如对端 region 没有 S3 interface endpoint）：按 `(account, region, type)` 分组，生成带 `resource_ids` 的 task 投递到 `ondemand` 队列，由该 region 的 worker 执行。
3. 等待所有 task 到终态，最多 `wait_seconds`：线程中的任务直接 await；队列中的任务按 `wait_poll_interval_ms` 轮询 `scan_task`。
4. 返回每个 ARN 的结果；有未完成的返回 202。

API 进程在执行过程中退出时，未完成的 task 由 planner 的 `maintenance` 置为 `timed_out`（spec 05 §5.6）。

超过 `refresh_inline_max` 个 ARN：直接走第 2 步的队列方式，立即返回 202。

## 6. 触发扫描（`/v1/scans`）

1. 校验 scope（api.md §2.2）。`scope.arn` 转到 §5 的刷新流程，响应格式为 ScanJob。
2. `create_job(trigger=api, request_id=上下文中的 request_id, deadline_at=now+60min)`；去重时直接返回已有 job，`deduplicated=true`。
3. 新 job：`Scheduler.expand` + `submit(queue=ondemand)`。`expand` 可能需要调用 `describe_regions`（账号级 scope 且缓存过期时），这一步在线程中执行。
4. `wait_seconds > 0` 时轮询 job 状态直到终态或超时。

## 7. 查询

- 列表与搜索：`ResourceFilters` 由 query 参数或 body 构造；cursor 为 base64url 编码的 `{"v":1,"s":sort,"o":order,"f":<过滤条件 hash>,"k":[排序键值...]}`，`f` 不匹配或解码失败返回 400 `BAD_REQUEST`。
- `tag` 参数：在第一个 `=` 处分割；没有 `=` 表示只要求存在该 key。
- `stats`：没有 tag / name / changed_since 过滤且 `include_deleted=false` 时读 `scope_state.resource_count`（`status=succeeded` 的行）；否则执行 `COUNT ... GROUP BY`。
- `by-arn`：返回 full 视图，并附带 `verified_at`（对应 scope 的 `last_success_at`）。
- `coverage`：合并 `account_region` 与 `scope_state`；对已启用 region 中没有 `scope_state` 记录的可用类型返回 `status=never`。

## 8. OpenAPI 快照

测试 `test_openapi_snapshot`：把 `app.openapi()` 与 `tests/fixtures/api/openapi.json` 比较。接口变化时用 `UPDATE_FIXTURES=1` 更新快照，并在同一提交中修改 `docs/api.md`。

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| request_id：透传合法值、替换非法值、生成 ULID；响应头存在；错误响应体中存在；并发请求互不串号 | httpx `AsyncClient(transport=ASGITransport(app))` |
| 访问日志字段完整；healthz 不输出日志 | caplog |
| 错误格式：422 / 404 / 500 / 501 | `api/errors.*.response.json` |
| 资源列表：每种过滤、排序、分页、`view`、`with_total` | PG + `db/resources.seed.json`；请求与期望响应放在 `api/resources.*.json` |
| 搜索：`data_jsonpath` 正常与超时 | PG |
| stats 两条路径结果一致 | PG |
| 扫描：各种 scope 校验、去重、`wait_seconds` 内完成返回 200、超时返回 202 | PG + moto SQS + 假 worker（测试中直接更新 task 状态） |
| 刷新：updated / unchanged / deleted / not_found / unsupported_type / ACCOUNT_UNRESOLVED / ACCOUNT_AMBIGUOUS；缺少 endpoint 时走队列；超过 20 个转异步 | PG + moto（EC2）+ 假 broker transport；`api/refresh.*.json` |
| OpenAPI 快照 | `api/openapi.json` |

## 验收标准

- 以上测试全部通过；`inventory/api/` 覆盖率 ≥ 85%。
- 本地（compose PG + moto server）启动 API，按 README 的示例请求跑通：查询资源、触发账号扫描、刷新一个 EC2 实例。
- 用同一个 `X-Request-ID` 能在本地日志中检索到 API、worker、broker 客户端三处的日志。

## 实现修订（M4，与上文冲突时以本节为准）

- 模块：`api/app.py`（`create_app` 工厂）、`api/middleware.py`、`api/errors.py`、`api/schemas.py`、`api/queries.py`（异步只读查询）、`api/refresh.py`（`RefreshService`：ARN 解析、inline 执行、排队与等待）、`api/state.py`、`api/resources.py`、`api/scans.py`。`RefreshService` 放在 api 包而不是 `transport.py`，避免与 `scheduling.py` 循环依赖。
- 启动命令：`uvicorn --factory inventory.api.app:create_app --host 0.0.0.0 --port 8080 --no-access-log`。
- 未处理的异常由 `RequestContextMiddleware` 直接返回标准 500 错误体（带 `request_id` 与 `X-Request-ID`），因为 Starlette 自带的错误中间件在最外层，拿不到请求上下文。
- 刷新：每个 ARN 一个 task（`resource_types=[type]`、`resource_ids=[id]`）。能经 endpoint 直达的在 API 线程池中执行（`queue=inline`），否则投递到对应 region 的 `ondemand` 队列；超过 `refresh_inline_max` 个 ARN 时全部排队、立即返回 202。等待结束时尚未完成的 ARN 状态为 `pending`。worker 执行的刷新，其每个 ARN 的状态根据资源行的 `first_seen_at` / `last_changed_at` / `deleted_at` 与 task 开始时间推断。
- 刷新的写入若被更新的已存储观察值挡住（`observed_at` 守卫），结果为 `unchanged`。
- 运行中的 job 的 `stats` 在读取时实时汇总（`queries.live_job_stats`）；结束时写入 `scan_job.stats`。

### 第二轮修订（M4 审查后）

- `RefreshService` 用两个独立的线程限制器：数据库读写（轮询、解析）与 AWS 调用分开，慢的 AWS 调用不会让轮询饿死；单个请求内再用 `refresh_parallelism` 信号量限制并发。inline 执行出错时把 task 标记为 `failed`（`INTERNAL_ERROR`），后台任务保留引用直到完成。刷新 job 的 deadline 为 `refresh_job_deadline_minutes`（默认 5）；`maintenance` 会把停在 `queued` 的 inline task 超时（API 进程中途退出的情况）。
- 相同 ARN 的并发刷新：去重请求在对方写入 task 之前不会误判为"已完成"；`deduplicated` 正确返回。
- `POST /v1/scans`：先校验 scope、建立（或去重到）job，再展开与投递；展开或投递失败时 job 置为 `failed` 并写入 `error`，不再阻塞该 scope。scope 校验错误返回 pydantic 的原始消息与 `details`。
- 刷新结果的错误码映射到契约：`ACCOUNT_ACCESS_DENIED`、`UPSTREAM_THROTTLED`、`UPSTREAM_ERROR`（消息带内部错误码）、`INTERNAL_ERROR`（消息固定）、`ACCOUNT_UNRESOLVED` / `ACCOUNT_AMBIGUOUS`。
- 游标在服务端完整校验（版本、排序、过滤指纹、键数量与类型），任何不合法都返回 400。
- 只有在请求带 `data_jsonpath` 时，jsonpath 相关的 SQL 错误才转为 422；其他数据库错误仍是 500。语句超时为 422 `QUERY_TIMEOUT`。
- tag key 过滤使用 `tags @? '$."key"'`（可走 `jsonb_path_ops` GIN 索引）。迁移 `0004`：排序索引与 keyset 键完全一致（`(last_changed_at, resource_type, account_id, region, resource_id)` 与 `(resource_id, resource_type, account_id, region)`）。
- stats 快速路径只统计 `scope_state.status = 'succeeded'` 的行。`GET /v1/scans` 列表返回已存储的 stats（不逐个实时汇总）；`GET /v1/scans/{id}` 才实时汇总运行中的 job。
- 输入上限：搜索的各列表 / tags / tag_keys / name_prefix 有长度限制，ARN 最长 2048；`status`、`trigger` 参数为枚举。
- 已知限制：日志中暂无 `xray_trace_id` 字段（OTel span 在请求中间件内部才开始），未启用 SQLAlchemy 埋点；旧格式 ECS service ARN 的补全（`LIKE '%/name'`）不走索引，但只在这类 ARN 出现时执行。
