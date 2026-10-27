# CLAUDE.md

本文件是本仓库的技术细节与编码规范，供开发者与 AI agent 使用。`AGENT.md` 是指向本文件的链接，只修改本文件。
开发流程与命令见 [README.md](README.md)；设计见 [docs/design.md](docs/design.md)；API 契约见 [docs/api.md](docs/api.md)；分块规格见 [docs/specs/](docs/specs/)。

## 开始工作前

1. 找到对应的 spec（`docs/specs/NN-*.md`），只做该 spec "范围"内的事情。
2. spec 中的接口签名、表结构、错误码是约定，实现需要偏离时，先修改 spec（同一提交），不要让代码与文档不一致。
3. 方案 B（非 VPC Lambda）与变更历史**只预留接口**（spec 09），不要实现，也不要创建空壳类。

## 技术栈

| 用途 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| 包管理 | uv，单个 `pyproject.toml`，单个包 `inventory`（平铺布局，**不要** `src/` 或 `a/src/a` 结构） |
| API | FastAPI + pydantic v2 |
| 配置 | pydantic-settings（只在 `inventory/config.py` 中） |
| Lambda | AWS Lambda Powertools（Logger / Tracer / Metrics / Parameters） |
| 数据库 | Aurora PostgreSQL 16；SQLAlchemy **Core**（不用 ORM）+ psycopg 3；Alembic 手写迁移 |
| 缓存 | Redis（broker） |
| AWS SDK | boto3，只通过 `ClientFactory` 创建 client |
| 测试 | pytest、moto、fakeredis[lua]、respx、time-machine |
| 代码质量 | ruff（lint + format，行宽 100）、mypy（`inventory/` 为 strict） |
| 容器 | Podman（`podman build` / `podman compose`），镜像 arm64 |
| IaC | Terraform ≥ 1.10 |

## 常用命令

```bash
make setup      # uv sync
make up         # podman compose up -d（PostgreSQL、Redis）
make moto       # 本机运行 moto server（端口 5000），本地跑 API/worker 时使用
make migrate    # alembic upgrade head
make fmt        # 格式化并自动修复
make check      # lint + typecheck + 单元测试（提交前必须通过）
make test-int   # 集成测试（PostgreSQL / Redis）
```

单个测试：`uv run pytest tests/broker/test_service.py -k lock`。

Terraform：`terraform -chdir=terraform fmt -recursive`，`terraform -chdir=terraform validate`（先 `init -backend=false`）。新增资源类型如需新的 endpoint，在 `terraform/envs/*.tfvars` 的 `regions.*.endpoints` 中配置。

## 代码组织

| 放在哪里 | 内容 |
|---|---|
| `inventory/config.py` | 所有 Settings 类；其他模块不得读取 `os.environ` |
| `inventory/errors.py` | `InventoryError` 及子类 |
| `inventory/observability.py` | 日志、上下文、脱敏、AWS 调用日志、指标、计时 |
| `inventory/models.py` | 跨模块共享的数据类型与消息/契约模型 |
| `inventory/aws.py` | `EndpointResolver`、`ClientFactory`、`BrokerLambdaProvider` |
| `inventory/db.py` | 表定义、Repository（同步） |
| `inventory/engine.py` | `ScanEngine`、`reconcile`、`TaskExecutor` |
| `inventory/scheduling.py` | scope 展开、task 创建与投递 |
| `inventory/bootstrap.py` | 各入口共用的组装（`build_runtime`） |
| `inventory/transport.py` | `Dispatcher` / `CredentialProvider` / `ResultSink` / `ChangeSink` / `RefreshExecutor` 接口与方案 A 实现 |
| `inventory/collectors/<service>.py` | 每个 AWS 服务一个文件 |
| `inventory/broker/` | broker v2 内部模块 |
| `inventory/api/` | FastAPI 应用：`app.py`（工厂）、`middleware.py`、`errors.py`、`schemas.py`、`queries.py`（异步查询）、`refresh.py`、`state.py`、`resources.py`、`scans.py` |
| `inventory/handlers/` | 部署入口，只做组装与参数转换，**不写业务逻辑** |

单个模块超过约 800 行时再拆分，拆分前先在对应 spec 中更新文件清单。

## 编码规范

- 全部代码有类型标注，`mypy --strict` 通过。避免 `Any`，确需使用时限定在边界（AWS 原始响应、JSON）。
- 数据类型：跨进程边界（SQS 消息、Lambda event、API、broker 契约）用 **pydantic 模型**；进程内高频数据用 `@dataclass(frozen=True, slots=True)`。
- 时间：一律使用带时区的 UTC `datetime`（`datetime.now(UTC)`），不使用 naive datetime。
- ID：`job_id`、`task_id`、生成的 `request_id` 使用 ULID。
- 错误：业务错误抛 `InventoryError` 子类，调用方依据 `code` / `retryable` 处理，不解析异常消息。不要 `except Exception: pass`；捕获宽泛异常时必须记录日志并重新抛出或转换。
- AWS client：`inventory/` 中只通过 `ClientFactory.client(...)` 或 `CollectorContext.client(...)` 创建，禁止直接 `boto3.client(...)`（否则会丢失 endpoint 路由、重试配置和调用日志）。测试代码中可以直接创建 moto client。
- 线程：`ThreadPoolExecutor` 提交任务必须用 `observability.submit_with_context`，否则日志上下文丢失。
- 敏感信息：凭证、SAML、密码只用 `SecretStr` 承载，不传给 logger，不写入异常消息。
- 不写只是复述代码的注释；只在"为什么"不明显时写注释。

## 日志与链路追踪

- 业务代码：`log = logging.getLogger(__name__)`，结构化字段用 `extra={...}`。禁止 `print`。
- 入口启动时调用一次 `setup_logging(service_name, settings)`；Lambda 入口额外使用 Powertools `Logger.inject_lambda_context`。
- 标准上下文字段：`request_id job_id task_id account_id region resource_type arn`，用 `bind_context` / `context_scope` 设置，不要在每条日志里手写。
- 级别：
  - DEBUG：单次 AWS 调用、详细数据。
  - INFO：一次请求、一条消息、一个资源类型的开始/结束汇总。
  - WARNING：可自动恢复的问题（限流重试、等锁超时、定时任务重叠）。
  - ERROR：需要人处理的问题（附堆栈）。
- `request_id` 必须贯穿 API → `scan_job.request_id` → SQS 消息体与属性 → worker 日志 → broker `correlation_id`。新增任何跨组件调用时都要传递它。
- API 并发场景**不要**用 Powertools `Logger.append_keys`（实例级状态，会在请求间串号），用 `bind_context`。
- 每个 API 错误响应包含 `request_id`；响应头总是包含 `X-Request-ID`。
- 指标用 `observability.metric(...)`（EMF），namespace `Inventory`。

## 测试规范

- **测试数据放文件，不写在代码里**：Lambda event、AWS 响应、API 请求/响应、moto 种子数据、期望输出全部放在 `tests/fixtures/<area>/<case>.<kind>.json`（`kind`：`event | request | response | seed | expected`）。原始格式不是 JSON 的（SAML XML、ADFS HTML）用对应扩展名。用 `tests/helpers.py` 的 `load_fixture()` 读取，用 `assert_matches_fixture()` 比较。
- AWS 一律用 moto 模拟（`mock_aws`）；moto 不支持或需要容器的部分（例如 Lambda Invoke）用 Protocol 的测试替身，替身的输入输出同样来自 fixture。
- Redis 用 fakeredis；ADFS HTTP 用 respx；时间用 time-machine 固定为 `2026-10-27T08:00:00Z`。
- 需要 PostgreSQL / Redis 的测试标记 `@pytest.mark.integration`，默认 `make check` 不运行。
- 测试目录与模块对应：`tests/broker/`、`tests/engine/`、`tests/collectors/`、`tests/api/` 等。
- 更新期望文件：`UPDATE_FIXTURES=1 uv run pytest ...`，只在行为有意变化时使用，并检查 diff。
- 每个 spec 的覆盖率要求写在 spec 的验收标准中。

## 数据库规范

- `resource` 表按 `resource_type` LIST 分区，分区名由 `db.partition_name()` 生成。**新增资源类型必须新增迁移创建分区**；测试会检查 `r_default` 为空。
- 迁移手写，`upgrade` 与 `downgrade` 都要实现并可往返。
- 写 `resource` 只能通过 `ResourceRepository.upsert` / `soft_delete`，它们包含 `observed_at` 并发保护，不要绕过。
- 资源从不硬删除，只软删除；由 `maintenance` 在 30 天后清理。
- API 只读查询使用异步 engine；worker、planner、刷新使用同步 engine。

## Collector 规范

详见 [spec 03](docs/specs/03-collector-framework.md) 与 [spec 04](docs/specs/04-collectors.md)。要点：

- `get_resource` 返回结构必须与 `list_resources` 中的元素完全一致（共用拼装函数）。
- 列表中途出错直接抛出，不能返回部分结果。
- 已终止 / `INACTIVE` 的资源视为不存在。
- 不写数据库，不记录变更。
- 新增类型按 spec 04 的检查清单完成全部步骤。

## API 规范

- 所有路由在 `/v1` 下；请求与响应模型在 `api/schemas.py`，与 `docs/api.md` 一一对应。
- 接口有任何变化：同时更新 `docs/api.md` 与 OpenAPI 快照 `tests/fixtures/api/openapi.json`。
- 分页只用 keyset cursor，不提供 offset。
- 错误统一为 `{"error": {"code", "message", "request_id", "details"}}`，code 取自 `errors.py`。

## Git

- 分支：`feat/<spec 编号>-<描述>`、`fix/<描述>`、`docs/<描述>`。
- 提交信息：Conventional Commits（`feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`）。
- 不提交 `.env`、密钥、真实账号数据；fixture 中的账号 ID 使用 `123456789012`、`210987654321` 等示例值。

## 不要做

- 不要用 `src/` 布局或新增 `pyproject.toml`。
- 不要在测试代码里内联 event、AWS 响应或期望结果。
- 不要直接 `boto3.client(...)`、直接读 `os.environ`、使用 `print`。
- 不要用 ORM 或 autogenerate 的迁移直接提交。
- 不要实现方案 B 或变更历史（spec 09），除非 spec 已更新为"实现"。
- 不要使用 Docker 专属的命令或配置；本地容器工具是 Podman。
