# Spec 00：项目基础（M0）

## 目标

建立所有组件共用的基础：项目骨架、依赖与工具链、配置、日志与链路追踪、AWS client 工厂、测试基建、镜像与本地依赖服务。完成后其他 spec 可以并行开发。

## 范围

做：下文"文件清单"中的全部内容。
不做：任何业务逻辑（broker、collector、API 路由等）。

## 文件清单

```
pyproject.toml  uv.lock  .python-version  .gitignore  Makefile
compose.yaml    Dockerfile  .env.example     # .env.example 指向 compose 中的服务与 moto server
inventory/__init__.py
inventory/config.py
inventory/errors.py
inventory/observability.py
inventory/aws.py            # 本 spec 只实现 EndpointResolver、ClientFactory；BrokerLambdaProvider 属于 spec 01
tests/conftest.py
tests/helpers.py
tests/fixtures/foundation/*.json
tests/test_config.py  tests/test_observability.py  tests/test_aws_clients.py
```

## 1. 依赖与工具

- Python 3.12，`.python-version` 写 `3.12`。
- 用 uv 管理，**只有一个** `pyproject.toml`，包名 `inventory`，平铺布局（无 `src/`）。
- 运行时依赖：`pydantic>=2`、`pydantic-settings`、`boto3`、`aws-lambda-powertools`、`fastapi`、`uvicorn[standard]`、`sqlalchemy>=2`、`psycopg[binary]>=3`、`alembic`、`redis`、`httpx`、`defusedxml`、`cryptography`、`python-ulid`。可选 tracing：`opentelemetry-sdk`、`opentelemetry-instrumentation-fastapi`、`opentelemetry-instrumentation-botocore`、`opentelemetry-instrumentation-sqlalchemy`、`opentelemetry-exporter-otlp`、`opentelemetry-sdk-extension-aws`、`opentelemetry-propagator-aws-xray`。
- dev 依赖组：`pytest`、`pytest-cov`、`moto[all]`、`fakeredis[lua]`、`respx`、`time-machine`、`ruff`、`mypy`、`boto3-stubs[...]`。
- ruff：行宽 100，启用 `E F I B UP SIM RUF`；ruff format 作为格式化工具。
- mypy：对 `inventory/` 使用 `strict = true`，测试代码不强制。
- pytest：`addopts = "-m 'not integration' --strict-markers"`，注册 marker `integration`（需要本地 PostgreSQL / Redis）。

### Makefile 目标

| 目标 | 命令 |
|---|---|
| `setup` | `uv sync` |
| `fmt` | `uv run ruff format . && uv run ruff check --fix .` |
| `lint` | `uv run ruff check . && uv run ruff format --check .` |
| `typecheck` | `uv run mypy inventory` |
| `test` | `uv run pytest` |
| `test-int` | `uv run pytest -m integration` |
| `cov` | `uv run pytest -m "" --cov=inventory --cov-report=term-missing` |
| `up` / `down` | `podman compose up -d` / `podman compose down` |
| `moto` | `uv run moto_server -p 5000` |
| `migrate` | `uv run alembic upgrade head` |
| `image` | `podman build --platform linux/arm64 -t inventory:dev .` |
| `check` | `lint typecheck test` |

## 2. 本地依赖服务与镜像

`compose.yaml`（使用 `podman compose` 运行，不依赖 Docker）：

| 服务 | 镜像 | 端口 | 用途 |
|---|---|---|---|
| `postgres` | `public.ecr.aws/docker/library/postgres:16-alpine` | 5432 | 集成测试与本地运行，库/用户/密码均为 `inventory` |
| `redis` | `public.ecr.aws/docker/library/redis:7-alpine` | 6379 | 集成测试与本地运行 broker |

镜像取自 ECR Public（Docker 官方镜像的副本），不依赖 Docker Hub。本地运行 API/worker 需要模拟 AWS 时，moto server 直接在本机运行：`make moto`（即 `uv run moto_server -p 5000`）。

`Dockerfile`：

- 基础镜像 `public.ecr.aws/lambda/python:3.12`，架构 arm64（Lambda 与 Fargate 都用 Graviton）。
- 用 `uv export --frozen --no-dev` 导出依赖安装到 `${LAMBDA_TASK_ROOT}`，再复制 `inventory/` 与 `migrations/`。
- 默认 `CMD ["inventory.handlers.broker_lambda.handler"]`。其他 Lambda 在 Terraform 中覆盖 `image_config.command`；ECS 任务覆盖 entrypoint：API 为 `uvicorn --factory inventory.api.app:create_app --host 0.0.0.0 --port 8080`，worker 为 `python -m inventory.handlers.worker`。

## 3. 配置（`inventory/config.py`）

规则：**只有** `config.py` 读取环境变量；其他模块通过参数或依赖注入拿到 settings 对象。

```python
class RegionCell(BaseModel):
    region: str
    endpoints: dict[
        str, str
    ] = {}  # boto3 服务名 -> endpoint URL；未列出的服务使用默认 regional DNS
    scheduled_queue_url: str | None = None
    ondemand_queue_url: str | None = None


class CommonSettings(BaseSettings):  # env_prefix="INVENTORY_", env_nested_delimiter="__"
    environment: Literal["local", "dev", "prod"] = "local"
    service_name: str = "inventory"
    log_level: str = "INFO"
    log_sample_rate: float = 0.0  # DEBUG 日志采样比例
    home_region: str = "ap-east-1"
    supported_regions: list[str]  # ["ap-east-1","eu-west-1","eu-west-2","us-east-1"]
    global_home_region: str = "us-east-1"
    transport: Literal["peering", "lambda"] = "peering"  # lambda = 方案 B，v1 未实现
    history_sink: Literal["null", "outbox"] = "null"  # outbox = 变更历史，v1 未实现
    cells: list[RegionCell]  # JSON 字符串形式的环境变量
    broker_function_name: str = "inventory-broker"
    database_url: SecretStr | None = None  # 本地用；AWS 上用 database_secret_arn
    database_secret_arn: str | None = None
    aws_endpoint_url: str | None = None  # 本地指向 moto server，设置后覆盖所有 endpoint

    def cell(self, region: str) -> RegionCell: ...
```

- 组件专属配置各自一个类：`BrokerSettings`（`BROKER_`）、`PlannerSettings`（`PLANNER_`）、`WorkerSettings`（`WORKER_`）、`ApiSettings`（`API_`），字段定义在各自 spec 中，类本身都放在 `config.py`。
- 提供 `get_common_settings()` 等带 `functools.cache` 的工厂函数；测试中通过 `monkeypatch.setenv` + `cache_clear()` 覆盖。
- 启动时校验：`transport == "lambda"` 抛 `NotImplementedFeatureError("方案 B 未实现")`；`history_sink == "outbox"` 抛 `NotImplementedFeatureError("变更历史未实现")`；`supported_regions` 中每个 region 都必须有对应 `cells` 项。
- 密钥（数据库密码、ADFS 密码、Redis AUTH）不放进 settings 值，只放 Secrets Manager ARN，运行时用 Powertools Parameters 读取并缓存（`max_age=300`）。

## 4. 错误类型（`inventory/errors.py`）

```python
class InventoryError(Exception):
    default_code: ClassVar[str] = "INTERNAL_ERROR"
    default_retryable: ClassVar[bool] = False
    http_status: ClassVar[int] = 500
    # 实例属性 code / retryable / message：构造时可覆盖，例如 UpstreamError(msg, retryable=True)

class BadRequestError(InventoryError):          # BAD_REQUEST, 400
class NotFoundError(InventoryError):            # NOT_FOUND, 404
class AccessDeniedError(InventoryError):        # ACCOUNT_ACCESS_DENIED, 403
class ThrottledError(InventoryError):           # UPSTREAM_THROTTLED, 503, retryable
class UpstreamError(InventoryError):            # UPSTREAM_ERROR, 502；构造时可指定 retryable
class NotImplementedFeatureError(InventoryError):  # NOT_IMPLEMENTED, 501
```

所有业务异常都继承 `InventoryError`，API 与 worker 依据 `code` / `retryable` 处理，不解析异常消息。

## 5. 日志与上下文（`inventory/observability.py`）

### 5.1 用法约定

- 业务代码使用标准库：`log = logging.getLogger(__name__)`，结构化字段通过 `extra={...}` 传入。禁止 `print`。
- 每个入口（Lambda handler、worker、API）启动时调用一次 `setup_logging(service_name, level, sample_rate)`：
  - root handler 输出到 stdout，使用 Powertools 的 `LambdaPowertoolsFormatter` 输出 JSON；
  - 挂载 `ContextFilter`（把上下文字段注入每条日志）与 `RedactionFilter`（脱敏）；
  - `botocore`、`urllib3`、`httpx` 等第三方库日志级别设为 WARNING。
- Lambda 入口另外使用 Powertools `Logger` 的 `inject_lambda_context`，但业务日志仍走标准库 logger。

### 5.2 上下文

```python
def bind_context(**fields: str | None) -> contextvars.Token: ...
@contextmanager
def context_scope(**fields): ...  # 进入时绑定，退出时恢复
def get_context() -> dict[str, str]: ...
def submit_with_context(executor, fn, *args, **kwargs) -> Future: ...
```

- 上下文存于 `ContextVar[dict]`，FastAPI 并发请求之间互不影响。
- **contextvars 不会自动传入线程池**，所有 `ThreadPoolExecutor` 提交必须使用 `submit_with_context`（内部 `contextvars.copy_context().run`）。
- 标准字段名：`request_id job_id task_id account_id region resource_type arn`。

### 5.3 脱敏

`RedactionFilter` 对日志 message（dict 时）、args、extra 字段递归处理，key 不区分大小写匹配以下任一则值替换为 `"***"`：
`secretaccesskey secret_access_key sessiontoken session_token samlresponse saml_response samlassertion saml_assertion password credentials authorization`。

### 5.4 AWS 调用日志

`ClientFactory` 创建的每个 client 都注册 botocore 事件钩子（`before-call` / `after-call` / `after-call-error`），每次调用记录：

| 字段 | 说明 |
|---|---|
| `aws_service` / `aws_operation` / `aws_region` | |
| `http_status` / `aws_request_id` | 来自 `ResponseMetadata` |
| `retry_attempts` | `ResponseMetadata.RetryAttempts` |
| `duration_ms` | |
| `error_code` | 失败时 |

成功记 DEBUG；`retry_attempts > 0` 或错误码属于限流集合时记 WARNING，并调用 `metric("AwsThrottle", dims={"AwsService": ..., "Operation": ...})`（`Service` 维度已固定为组件名）。

### 5.5 指标

```python
def metric(name: str, value: float = 1, unit: str = "Count", dims: dict[str, str] | None = None) -> None
```

用 Powertools `single_metric` 输出 EMF，namespace `Inventory`，固定维度 `Service=<service_name>`。Lambda 与 ECS（awslogs）都会被 CloudWatch 自动解析成指标。

### 5.6 计时

```python
@contextmanager
def timed(
    event: str, level: int = logging.INFO, **fields
): ...  # 退出时输出 event + duration_ms，异常时附带 error_code
```

## 6. AWS client（`inventory/aws.py`，本 spec 部分）

```python
class EndpointResolver:
    def __init__(self, settings: CommonSettings): ...
    def endpoint_url(
        self, service: str, region: str
    ) -> (
        str | None
    ): ...  # aws_endpoint_url 优先，其次 cell.endpoints[service]，否则 None（boto3 默认）
    def has_endpoint(
        self, service: str, region: str
    ) -> bool: ...  # 供 collector 的 required_endpoints 检查


class ClientFactory:
    def __init__(self, resolver: EndpointResolver, max_pool_connections: int = 32): ...
    def client(self, service: str, region: str, session: boto3.Session | None = None,
               config: Config | None = None) -> AwsClient: ...   # config 与默认配置合并
```

`EndpointResolver` 另提供 `api_region(region)`（`global` → `global_home_region`）与 `reachable_from(service, region, caller_region)`（跨 region 必须配置了 endpoint-specific DNS）。

- botocore `Config`：`retries={"mode": "adaptive", "max_attempts": 10}`、`connect_timeout=5`、`read_timeout=30`、`max_pool_connections=32`、`user_agent_extra="inventory/<version>"`。
- `has_endpoint` 规则：**只有在 `cells[region].endpoints` 中列出的服务才算可用**，避免调用没有 endpoint 的服务一直等到超时。值为空字符串表示"使用默认 regional DNS"（即本 VPC 的 private DNS，只在本 region 内有效）；跨 region 访问必须配置 endpoint-specific DNS。设置了 `aws_endpoint_url`（本地 moto）时所有服务都视为可用。
- 禁止在 `inventory/` 的其他模块中直接调用 `boto3.client(...)`（测试代码中创建 moto client 不受此限制）。

## 7. 测试基建

- `tests/conftest.py`：
  - 自动设置 `AWS_ACCESS_KEY_ID=testing`、`AWS_SECRET_ACCESS_KEY=testing`、`AWS_DEFAULT_REGION=ap-east-1`，清空真实 profile。
  - fixture `aws`：`with mock_aws(): yield`。
  - fixture `settings_env`：从 `tests/fixtures/foundation/settings.local.json` 读取环境变量并 `monkeypatch.setenv`，并清理 settings 缓存。
  - fixture `frozen_time`：基于 time-machine，固定为 `2026-10-27T08:00:00Z`。
- `tests/helpers.py`：
  - `load_fixture(rel_path: str) -> Any`：读取 `tests/fixtures/<rel_path>`；`.json` 返回解析后的对象，`.xml` / `.html` / `.txt` 返回字符串。
  - `assert_matches_fixture(actual, rel_path)`：与 JSON 期望文件比较；设置环境变量 `UPDATE_FIXTURES=1` 时改为写入期望文件（只用于有意更新）。
- **测试数据规则**：Lambda event、AWS 响应、API 请求与响应、种子数据、期望输出一律放在 `tests/fixtures/` 下的文件中，代码里不内联。目录按领域划分：`tests/fixtures/<area>/<case>.<kind>.json`，`kind` 取 `event | request | response | seed | expected`。

## 测试要求

| 用例 | fixture |
|---|---|
| settings 能从环境变量解析 `cells` JSON；缺少 cell 时报错；`transport=lambda` 时报错 | `foundation/settings.local.json`、`foundation/settings.missing_cell.json` |
| `bind_context` 在 asyncio 并发任务间隔离；`submit_with_context` 能把上下文带入线程 | — |
| `RedactionFilter` 对嵌套 dict / list / extra 中的敏感 key 脱敏 | `foundation/redaction.input.json`、`foundation/redaction.expected.json` |
| moto 下调用 `ec2.describe_vpcs` 产生一条包含 `aws_service`、`aws_operation`、`duration_ms` 的日志 | — |
| `EndpointResolver` 优先级：`aws_endpoint_url` > cell 配置 > 默认 | `foundation/settings.local.json` |

## 验收标准

- `make check` 通过；`make up` 后 `make test-int` 可运行（此时可以没有集成用例）。
- `make image` 能在 podman 上构建成功。
- README 的"本地开发"一节与实际命令一致。

## 实现修订（收尾审查后）

- 数据库连接：`db.make_engine(..., credentials=)` / `install_session_settings`——不使用 `options` 启动参数（RDS Proxy 不支持），连接建立时 `SET statement_timeout`，关闭 psycopg 自动预处理；AWS 上每个新连接从 Secrets Manager 取当前凭证（`bootstrap.database_access`，缓存 60 秒）。
- 指标：`CommonSettings.metrics_mode`（`emf` | `api`）。`api` 模式由 `observability.enable_metric_api(cloudwatch)` 开启，进程内聚合、每 60 秒 `PutMetricData`；ECS 组件使用该模式。
- 各入口先 `setup_logging` 再构建运行时，启动阶段的错误也输出为 JSON。
