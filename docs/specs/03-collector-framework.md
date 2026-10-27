# Spec 03：Collector 框架、ScanEngine 与 Reconciler（M2）

## 目标

提供"新增资源类型只需写一个类"的扩展框架，以及执行一次扫描（或单 ARN 刷新）的引擎：采集 → 对账 → 写入。worker 与 API 共用。

## 范围

做：`collectors/base.py`、`engine.py`、`transport.py` 中的 `ResultSink` / `CredentialProvider` 接口及方案 A 实现、`models.py` 中的内部数据类型。
不做：具体 collector（spec 04）、队列处理（spec 06）。

## 文件清单

```
inventory/models.py              # ResourceKey、ResourceRecord、TypeResult 等
inventory/collectors/__init__.py # 导入所有 collector 模块以触发注册
inventory/collectors/base.py
inventory/engine.py
inventory/transport.py
tests/engine/...  tests/fixtures/engine/...
```

## 1. 数据类型（`inventory/models.py`）

规则：跨进程边界的数据（消息、API、broker 契约）用 pydantic；进程内高频使用的数据用 `@dataclass(frozen=True, slots=True)`。

```python
@dataclass(frozen=True, slots=True)
class ResourceKey:
    resource_type: str
    account_id: str
    region: str  # 'global' 表示全局资源
    resource_id: str


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    key: ResourceKey
    arn: str
    name: str | None
    tags: Mapping[str, str]
    relations: Mapping[str, Any]
    data: Mapping[str, Any]
    observed_at: datetime
    content_hash: bytes  # 构造时计算，见 spec 02 §3


@dataclass(frozen=True, slots=True)
class KeepExisting:
    resource_id: str  # collector 确认资源仍存在且无需重新 describe


@dataclass(frozen=True, slots=True)
class ChangeEvent:  # v1 只生成、不持久化（NullChangeSink），见 spec 09
    key: ResourceKey
    arn: str
    change_type: Literal["created", "updated", "deleted"]
    changed_at: datetime
    job_id: str
    old_hash: bytes | None
    new_hash: bytes | None
    data: Mapping[str, Any] | None  # 变更后的完整数据；deleted 时为 None


class TypeStatus(StrEnum):
    succeeded | failed | auth_denied | skipped_no_endpoint


@dataclass
class TypeResult:
    status: TypeStatus
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
```

## 2. Collector 基类（`inventory/collectors/base.py`）

```python
class Collector(ABC):
    resource_type: ClassVar[str]  # "AWS::EC2::Instance"
    service: ClassVar[str]  # boto3 服务名
    is_global: ClassVar[bool] = False
    required_endpoints: ClassVar[tuple[str, ...]]  # 默认 (service,)
    depends_on: ClassVar[tuple[str, ...]] = ()
    drop_fields: ClassVar[tuple[str, ...]] = ()  # 从 data 中去掉的噪声字段（顶层 key）
    arn_pattern: ClassVar[re.Pattern[str]]  # 必须包含命名组 id；可包含 region、account

    def __init__(self, ctx: CollectorContext) -> None: ...

    # 子类必须实现
    @abstractmethod
    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[dict[str, Any] | KeepExisting]: ...
    @abstractmethod
    def get_resource(self, resource_id: str) -> dict[str, Any] | None: ...  # None 表示资源不存在
    @abstractmethod
    def resource_id(self, raw: dict[str, Any]) -> str: ...
    @abstractmethod
    def build_arn(self, raw: dict[str, Any]) -> str: ...

    # 可选覆盖
    def extract_tags(
        self, raw
    ) -> dict[str, str]: ...  # 默认处理 [{"Key","Value"}] 与 [{"key","value"}]
    def extract_name(self, raw) -> str | None: ...  # 默认 tags["Name"]
    def extract_relations(self, raw) -> dict[str, Any]: ...  # 默认 {}
    @classmethod
    def parse_arn(cls, arn: str) -> ParsedArn | None: ...  # 默认用 arn_pattern

    # 模板方法，子类不覆盖
    def to_record(self, raw: dict[str, Any], observed_at: datetime) -> ResourceRecord: ...
```

强制规则：

1. **`get_resource` 返回的结构必须与 `list_resources` 中对应元素完全一致**，否则同一资源在定时扫描与刷新之间会被反复判为"已变化"。需要多次调用拼装数据的类型（例如 IAM Role），list 与 get 共用同一个拼装函数。
2. collector 只做读取与整理，不写数据库、不记录变更。
3. 列表中途出现任何异常都直接抛出，不能吞掉异常后返回部分结果（否则会误删资源）。
4. 已终止或不可用的资源（例如 `terminated` 的 EC2 实例、`INACTIVE` 的 ECS service）在 list 中跳过，在 get 中返回 `None`，统一视为"不存在"。
5. 子类必须在模块级用 `@register` 注册。

### 2.1 声明式 EC2 基类

```python
class Ec2DescribeCollector(Collector):
    service = "ec2"
    operation: ClassVar[str]  # "describe_vpcs"
    result_key: ClassVar[str]  # "Vpcs"
    id_field: ClassVar[str]  # "VpcId"
    ids_param: ClassVar[str]  # "VpcIds"
    arn_resource: ClassVar[str]  # "vpc"，用于拼 arn:aws:ec2:{region}:{account}:{arn_resource}/{id}
    not_found_codes: ClassVar[tuple[str, ...]]
    extra_params: ClassVar[dict[str, Any]] = {}
```

已实现 `list_resources`（分页器）、`get_resource`（按 `ids_param` 查询，`not_found_codes` → None）、`resource_id`、`build_arn`。

### 2.2 `CollectorContext`

```python
@dataclass
class CollectorContext:
    account_id: str
    region: str  # 记录用的 region；全局资源为 'global'
    api_region: str  # 实际调用 API 的 region；全局资源为 global_home_region
    session: boto3.Session
    clients: ClientFactory
    full_refresh: bool
    known_ids: frozenset[str]  # 库中该 scope 未删除的 resource_id
    executor_workers: int  # 用于 ctx.map

    def client(self, service: str) -> BaseClient: ...
    def memo(self, key: str, loader: Callable[[], T]) -> T: ...  # 同一 task 内共享、线程安全
    def map(
        self, fn: Callable[[A], R], items: Iterable[A]
    ) -> Iterator[R]: ...  # 有限并发、带上下文
```

`memo` 用于同一 task 内多个 collector 共享一次昂贵的调用（例如 IAM 的 `GetAccountAuthorizationDetails`）。

## 3. 注册表

```python
class Registry:
    def register(self, cls: type[Collector]) -> type[Collector]: ...
    def get(self, resource_type: str) -> type[Collector]: ...
    def all(self) -> list[type[Collector]]: ...
    def for_region(
        self, region: str, resolver: EndpointResolver, api_region: str
    ) -> tuple[list[type[Collector]], list[type[Collector]]]:
        ...
        # 返回 (可用, 因缺少 endpoint 而跳过)；region == 'global' 时只返回 is_global 的类型

    def route_arn(self, arn: str) -> tuple[type[Collector], ParsedArn] | None: ...
    def groups(self, types: Sequence[str]) -> list[list[type[Collector]]]:
        ...
        # 按 depends_on 计算连通分量；组内按拓扑序；缺失的依赖自动补入；存在循环依赖时报错


registry = Registry()  # 全局默认实例
register = registry.register  # 装饰器
```

`Registry` 可以实例化，测试使用独立实例注册假 collector，不污染全局。

`ParsedArn`：`account_id: str | None`、`region: str | None`、`resource_id: str`。账号或 region 缺失的情况（AMI、S3、Route 53）由调用方通过 `ResourceRepository.find_account` 补全。

## 4. 接口（`inventory/transport.py`）

```python
class CredentialProvider(Protocol):
    def session(self, account_id: str) -> boto3.Session: ...
    def accessible_accounts(self) -> set[str]: ...


class ResultSink(Protocol):
    def load_index(
        self, resource_type: str, account_id: str, region: str
    ) -> dict[str, IndexEntry]: ...
    def apply(
        self, scope: TypeScope, changes: ChangeSet, result: TypeResult, job_id: str
    ) -> None: ...
    def apply_refresh(
        self, key: ResourceKey, record: ResourceRecord | None, job_id: str
    ) -> RefreshStatus: ...
    def record_skip(
        self, scope: TypeScope, status: TypeStatus, job_id: str, error: ErrorInfo | None
    ) -> None: ...


class ChangeSink(Protocol):
    def emit(self, conn: Connection, events: Sequence[ChangeEvent]) -> None: ...
```

方案 A 实现：`BrokerLambdaProvider`（spec 01）、`PostgresResultSink`、`NullChangeSink`。

`PostgresResultSink.apply` 在**一个事务**中完成：upsert 新增与变化的记录 → 软删除 → `ChangeSink.emit` → 更新 `scope_state`。

## 5. Reconciler（纯函数，`inventory/engine.py`）

```python
@dataclass
class ChangeSet:
    upserts: list[ResourceRecord]  # created + updated
    deleted_ids: list[str]
    created: int
    updated: int
    unchanged: int
    events: list[ChangeEvent]
    scan_started_at: datetime


def reconcile(
    index: Mapping[str, IndexEntry],
    current: Sequence[ResourceRecord],
    kept: Collection[str],
    scan_started_at: datetime,
) -> ChangeSet: ...
```

| 情况 | 结果 |
|---|---|
| 在 current 中，不在 index 中（或 index 中已删除） | created |
| 两边都有，hash 不同 | updated |
| 两边都有，hash 相同 | unchanged（不写库） |
| 在 kept 中 | unchanged（不写库，也不删除） |
| 在 index 中（未删除），不在 current 与 kept 中 | deleted |

`current` 中出现重复的 resource_id 时报错（说明 collector 有 bug）。

## 6. ScanEngine（`inventory/engine.py`）

```python
class ScanEngine:
    def __init__(
        self,
        registry,
        clients: ClientFactory,
        resolver: EndpointResolver,
        credentials: CredentialProvider,
        sink: ResultSink,
        settings,
    ) -> None: ...

    def scan(self, task: ScanTaskMessage) -> dict[str, TypeResult]: ...
    def refresh(self, key: ResourceKey, job_id: str) -> RefreshOutcome: ...
```

### 6.1 `scan`

1. 用 `context_scope(job_id, task_id, account_id, region)` 绑定日志上下文。
2. `credentials.session(account_id)`；`AccessDeniedError` → 所有类型记为 `auth_denied`（`sink.record_skip`），直接返回。
3. 确定类型：`task.resource_types` 或 `registry.for_region(...)`；缺少 endpoint 的类型记为 `skipped_no_endpoint`。
4. `registry.groups(types)` 分组；各组用线程池并行执行（`type_concurrency`，提交时使用 `submit_with_context`），组内按拓扑序依次执行。
5. 对每个类型：
   ```
   started = utcnow()
   index = sink.load_index(...)
   ctx = CollectorContext(..., known_ids=未删除的 index key)
   current, kept = 遍历 collector.list_resources(upstream)
   changes = reconcile(index, current, kept, started)
   sink.apply(...)
   upstream[type] = current
   ```
   - `task.resource_ids` 非空时（批量刷新）：不调用 list，对每个 id 调用 `get_resource`，逐个 `apply_refresh`，**不做删除推断**。
   - 某个类型失败时，组内依赖它的后续类型标记为 `failed`（`error_code=UPSTREAM_FAILED`，retryable 跟随上游），不执行。
6. 每个类型输出一条汇总日志（`resource_type status created updated deleted unchanged duration_ms`），并上报指标 `TypeDuration`、`ResourcesCreated/Updated/Deleted`。

### 6.2 `refresh`

`get_resource(key.resource_id)` → `to_record` 或 `None` → `sink.apply_refresh`。返回 `RefreshOutcome(status, record, error)`，状态取值同 api.md §3.5。

### 6.3 错误分类

```python
def classify(exc: BaseException) -> ErrorInfo:   # ErrorInfo(code, message, retryable)
```

| 异常 | code | retryable |
|---|---|---|
| `ClientError`，code 属于限流集合¹ | `THROTTLED` | 是 |
| `ClientError`，code 属于权限集合² | `API_ACCESS_DENIED` | 否 |
| `ClientError`，`OptInRequired` / `AuthFailure` | `REGION_NOT_ENABLED` | 否 |
| `ClientError`，HTTP 5xx | `AWS_SERVER_ERROR` | 是 |
| `EndpointConnectionError` / `ConnectTimeoutError` / `ReadTimeoutError` | `NETWORK_ERROR` | 是 |
| `InventoryError` | 使用其 `code` / `retryable` | |
| 其他 | `INTERNAL_ERROR` | 是 |

¹ `Throttling ThrottlingException RequestLimitExceeded TooManyRequestsException RequestThrottled SlowDown ProvisionedThroughputExceededException`
² `AccessDenied AccessDeniedException UnauthorizedOperation UnauthorizedAccess`

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| `reconcile` 覆盖 §5 表格中所有情况，包括 kept 与重复 id | `engine/reconcile_cases.json`（每个 case 包含 index、current、kept、expected） |
| 注册表：`groups` 分组与拓扑序、缺失依赖自动补入、循环依赖报错、`route_arn` | 测试内独立 `Registry` 实例 + 假 collector |
| `for_region`：global 过滤、缺少 endpoint 的类型被跳过 | `foundation/settings.local.json` |
| `scan`：上游失败导致下游标记 `UPSTREAM_FAILED`；列表中途抛异常时不产生删除；`AccessDeniedError` 时全部为 `auth_denied` | 假 collector + 内存版 `ResultSink`（测试替身） |
| `classify` 覆盖每一行 | `engine/client_errors.json` |
| 上下文：线程池中执行的 collector 日志带有 `task_id` 与 `resource_type` | caplog |

## 验收标准

- 以上测试全部通过；`engine.py` 与 `collectors/base.py` 覆盖率 ≥ 90%。
- 新增一个 EC2 类资源（`Ec2DescribeCollector` 子类）不超过 15 行代码。

## 实现修订（2026-10-27，与上文冲突时以本节为准）

- `reconcile(scope, index, current, kept, scan_started_at, job_id)`：显式传入 `TypeScope`；`kept` 中的 id 必须是库中未删除的资源，否则报错；删除事件带 ARN，listing 为空时也会生成。
- `ResultSink.apply(scope, changes, result, job_id, resource_count)`；`Registry.for_region(region, resolver)`；另有 `Registry.applicable(region)` 与 `has(resource_type)`。
- **`observed_at` 取扫描开始时间**（单 ARN 刷新取调用 get 之前的时间），表示"数据不早于此刻读取"，保证扫描期间发生的刷新总能赢得 upsert 的新旧判断。
- 先展开依赖（`registry.groups`），再按 endpoint 拆分：自动补入但缺 endpoint 的依赖记为 `skipped_no_endpoint`，其下游记 `UPSTREAM_FAILED`。
- 有下游依赖的类型不得使用 `KeepExisting`（下游只能拿到本次列出的记录），引擎会将其判为失败。
- `to_record` 会按 key 排序 `Tags` / `tags` 列表，避免 AWS 返回顺序变化造成哈希变化。
- `CollectorContext.memo` 每个 key 一把锁（不同 key 可以并发加载）。
- 获取凭证失败（非 AccessDenied）也会写 `scope_state(status=failed)`。
- `ScanEngine.refresh` 对未注册的类型返回 `unsupported_type`。
- `classify` 额外的错误码：其他 4xx `ClientError` → `AWS_<错误码>`（不可重试）；其他 `BotoCoreError` → `AWS_SDK_ERROR`（可重试）。
