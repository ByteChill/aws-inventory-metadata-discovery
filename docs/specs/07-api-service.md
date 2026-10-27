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
inventory/api/resources.py    # /v1/resources*（含路径形式 /all、/{account}/{region}/{type|arn}）、/v1/resource-types、/v1/accounts/{id}/coverage
inventory/api/scans.py        # /v1/scans*、/v1/resources/refresh
inventory/api/auth.py, keys.py # 认证、RBAC、/v1/me 与 API key（spec 10）
inventory/api/ui/             # 构建好的 web UI（镜像中由 web/ 构建，不进 git；spec 10）
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
| `refresh_max_arns` | 100 | |
| `tracing_enabled` | false | |
| `otlp_endpoint` | `http://localhost:4317` | ADOT sidecar |
| `ui_enabled` | true | 是否挂载 `/ui`；`ui_dir`、认证相关设置见 spec 10 |

## 2. 应用结构

- `create_app(settings) -> FastAPI`；模块级 `app = create_app(get_api_settings())` 供 uvicorn 使用，测试中直接调用 `create_app`。
- `lifespan` 中创建并放入 `app.state`：异步 engine（查询）、同步 engine（刷新与 Repository）、`ClientFactory`、`BrokerLambdaProvider`、`SqsDirectDispatcher`、`Scheduler`、`RefreshService`、`registry`；关闭时释放连接池。
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

刷新一律交给资源所在 region 的 worker（spec 08 R6），API 进程不调用 AWS 采集资源：

1. 创建（或去重到）刷新 job；每个 ARN 一个 task：`resource_types=[type]`、`resource_ids=[id]`，投递到该 region 的 `ondemand` 队列（global 资源投递到 us-east-1）。
2. 在 `wait_seconds` 内按 `wait_poll_interval_ms` 轮询 `scan_task`，直到全部到终态。
3. 每个 ARN 的状态由资源行的 `first_seen_at` / `last_changed_at` / `deleted_at` 与 task 开始时间推断（`created | updated | unchanged | deleted | not_found`）；未完成的为 `pending`，有未完成的返回 202。

## 6. 触发扫描（`/v1/scans`）

1. 校验 scope（api.md §2.2）。`scope.arn` 转到 §5 的刷新流程，响应格式为 ScanJob。
2. `Scheduler.validate(scope)`，再 `Scheduler.expand(scope, "ondemand")`（在线程中执行；`account_region` 缓存过期时会调用 `describe_regions`）。`BadRequestError` 返回 422，此时不创建 job；broker / AWS 错误按错误类型返回（502 / 503），同样不创建 job。
3. `create_job(trigger=api, request_id=上下文中的 request_id, deadline_at=now+60min)`；去重时直接返回已有 job，`deduplicated=true`，丢弃第 2 步的展开结果。
4. 新 job：`submit(job, tasks)`。`submit` 本身出错（如写库失败）时 job 置为 `failed`，不阻塞该 scope。
5. `wait_seconds > 0` 时轮询 job 状态直到终态或超时。

## 7. 查询

- 列表与搜索：`ResourceFilters` 由 query 参数或 body 构造；cursor 为 base64url 编码的 `{"v":1,"s":sort,"o":order,"f":<过滤条件 hash>,"k":[排序键值...]}`，`f` 不匹配或解码失败返回 400 `BAD_REQUEST`。
- `tag` 参数：在第一个 `=` 处分割；没有 `=` 表示只要求存在该 key。
- `stats`：没有 tag / name / changed_since 过滤且 `include_deleted=false` 时读 `scope_state.resource_count`（`status=succeeded` 的行）；否则执行 `COUNT ... GROUP BY`。
- `by-arn`：返回 full 视图，并附带 `verified_at`（对应 scope 的 `last_success_at`）。
- 路径形式（api.md §3.1）：与 `GET /v1/resources` 共用查询参数依赖 `list_options`，路径段只换成对应的单值过滤，cursor 指纹因此天然与路径绑定。`{account_id}` 与 `{arn}` 用 Starlette 自定义 convertor（`[0-9]{12}`、`arn:.+`）匹配，路由定义在固定路径（`stats`、`by-arn` 等）之后，互不遮挡；ARN 路由在类型路由之前。ARN 形式复用 `by-arn` 的查找，并要求结果的 `(account_id, region)` 与路径一致。
- `coverage`：合并 `account_region` 与 `scope_state`；对已启用 region 中没有 `scope_state` 记录的可用类型返回 `status=never`。

## 8. OpenAPI 快照

测试 `test_openapi_snapshot`：把 `app.openapi()` 与 `tests/fixtures/api/openapi.json` 比较。接口变化时用 `UPDATE_FIXTURES=1` 更新快照，并在同一提交中修改 `docs/api.md`。

## 9. 内置 UI（`/ui`）

已由 [spec 10](10-web-ui-and-auth.md) 的 baseui UI 取代（原生 JS 页面已删除）；API 的认证与权限也见 spec 10。

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| request_id：透传合法值、替换非法值、生成 ULID；响应头存在；错误响应体中存在；并发请求互不串号 | httpx `AsyncClient(transport=ASGITransport(app))` |
| 访问日志字段完整；healthz 不输出日志 | caplog |
| 错误格式：422 / 404 / 500 / 501 | `api/errors.*.response.json` |
| 资源列表：每种过滤、排序、分页、`view`、`with_total` | PG + `db/resources.seed.json`；请求与期望响应放在 `api/resources.*.json` |
| 资源路径形式：各层级结果、ARN 归属不符 404、非法 region / 类型 422、cursor 与路径绑定 | PG；`api/resources.paths.cases.json` |
| 搜索：`data_jsonpath` 正常与超时 | PG |
| stats 两条路径结果一致 | PG |
| 扫描：各种 scope 校验（类型不带 region 时展开到已启用 ∩ 受支持 region；不可扫描的 scope 422 且不建 job，`api/scan.unscannable.*.json`）、去重、`wait_seconds` 内完成返回 200、超时返回 202；task 的 `error_code` / `error_message` 返回（`api/scan.task_failed.*.json`） | PG + moto SQS + 假 worker（测试中直接更新 task 状态） |
| 刷新：updated / unchanged / deleted / not_found / unsupported_type / ACCOUNT_UNRESOLVED / ACCOUNT_AMBIGUOUS；缺少 endpoint 时走队列；超过 20 个转异步 | PG + moto（EC2）+ 假 broker transport；`api/refresh.*.json` |
| OpenAPI 快照 | `api/openapi.json` |
| UI：见 spec 10 的测试要求 | `api/ui.*.json`、`tests/fixtures/api/ui/` |

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

### 第三轮修订（内置 UI）

- 新增 §9 内置 UI 与 `ApiSettings.ui_enabled`。
- `ScanTask` 增加 `error_code` / `error_message`（task 整体失败的原因，例如 `DISPATCH_FAILED`），此前只存在于表中，API 与 UI 看不到 task 为什么失败；OpenAPI 快照同步更新。
- `.env.example` 中的 JSON 值改为单引号包裹：README 要求的 `set -a; source .env` 会去掉未加引号的双引号，导致 `INVENTORY_SUPPORTED_REGIONS` / `INVENTORY_CELLS` 解析失败。

### 第四轮修订（类型级 scope 不再要求 region）

- `POST /v1/scans` 先校验并展开 scope，再创建 job：账号相关的不支持（region 未对该账号启用、类型在任何可扫描 region 都不可用）直接返回 422，不再产生一个 `failed` 的 job。
- UI 的"账号 + 类型"粒度中 region 改为可选。
- 新增资源的路径形式：`/v1/resources/all`、`/{account_id}`、`/{account_id}/{region}`、`/{account_id}/{region}/{resource_type}`、`/{account_id}/{region}/{arn}`。

### 第五轮修订（发布与配置，spec 08 R4–R6）

- 单个 ARN 刷新一律走队列：删除 `queue=inline`、`InlineRefreshExecutor`、`ScanEngine.refresh` 与 `refresh_inline_max`、`refresh_parallelism`、`refresh_thread_limit`、`refresh_timeout_seconds`。`maintenance` 不再处理停在 `queued` 的 inline task。
- `/v1/resource-types` 的 `enabled_regions`：regional 类型为全部扫描 region，global 类型为 `["global"]`（不再按 endpoint 配置计算）；账号覆盖情况按 `registry.applicable(region)` 列出每个类型。
- 资源路径形式与扫描校验中的 region 列表改为 `scan_regions`。
