# Spec 05：调度、Planner 与任务消息（M3）

## 目标

每 4 小时把"所有账号 × 已启用的受支持 region（含 global）"展开成 scan task 并投递到各 region 的队列；跟踪 job 进度直至结束。同时提供 API 与 planner 共用的 scope 展开和投递逻辑。

## 范围

做：Step Functions 状态机定义、planner Lambda 的各个 action、`scheduling.py`（scope 展开、task 创建与投递）、`Dispatcher` 的方案 A 实现、定期维护任务。
不做：worker 消费逻辑（spec 06）。

## 文件清单

```
inventory/scheduling.py
inventory/transport.py                 # Dispatcher 接口 + SqsDirectDispatcher
inventory/handlers/planner_lambda.py
inventory/models.py                    # ScanScope、ScanTaskMessage
terraform/statemachines/scan_run.asl.json
tests/planner/...  tests/fixtures/planner/...
```

## 1. 消息契约（`inventory/models.py`）

```python
class ScanScope(BaseModel):
    account_id: str | None = Field(None, pattern=r"^\d{12}$")
    region: str | None = None
    resource_type: str | None = None
    arn: str | None = None
    all: bool = False  # 仅定时任务使用：{"all": true}
    # 校验规则见 api.md §2.2


class ScanTaskMessage(BaseModel):
    version: Literal[1] = 1
    job_id: str
    task_id: str
    request_id: str | None  # API 触发时为请求的 request_id；定时任务为 None
    trigger: Literal["schedule", "api"]
    account_id: str
    region: str  # 包括 'global'
    resource_types: list[str] | None  # None 表示全部可用类型
    resource_ids: list[str] | None  # 仅批量刷新，此时 resource_types 只有一个元素
    full_refresh: bool = False
    attempt: int = 1
```

SQS 消息属性（String 类型）：`request_id`（没有时用 job_id）、`job_id`、`task_id`。示例：`tests/fixtures/planner/scan_task_message.json`。

## 2. `Dispatcher`（`inventory/transport.py`）

```python
class Dispatcher(Protocol):
    def dispatch(self, messages: Sequence[tuple[ScanTaskMessage, QueueKind]]) -> DispatchResult:
        ...
        # QueueKind = Literal["scheduled", "ondemand"]
        # DispatchResult: succeeded task_ids、failed [(task_id, error)]
```

`SqsDirectDispatcher`：

- 目标队列：按名字 `inventory-<env>-<kind>` 在目标 region 用 `GetQueueUrl` 解析并缓存（`QueueLocator`）；`region == 'global'` 时投递到 us-east-1。
- SQS client 经 `ClientFactory(service="sqs", region=目标 region)` 创建，跨 region 时使用 `INVENTORY_ENDPOINTS` 中的专用 DNS。解析队列失败时该批 task 记为投递失败。
- 使用 `send_message_batch`（每批 10 条），逐条检查 `Failed` 并汇总。
- `retry` 消息（spec 06）通过同一接口发送，附带 `DelaySeconds`。

## 3. 共享调度逻辑（`inventory/scheduling.py`）

```python
class Scheduler:
    def __init__(
        self,
        settings,
        jobs: JobRepository,
        account_regions: AccountRegionRepository,
        credentials: CredentialProvider,
        clients: ClientFactory,
        registry: Registry,
        resolver: EndpointResolver,
        dispatcher: Dispatcher,
    ) -> None: ...

    def enabled_regions(
        self, account_id: str, max_age: timedelta = timedelta(hours=24)
    ) -> list[str]: ...
    def expand(self, job: ScanJob, scope: ScanScope) -> list[NewTask]: ...
    def submit(self, job: ScanJob, tasks: list[NewTask], queue: QueueKind) -> DispatchResult: ...
```

### 3.1 `enabled_regions`

1. 读 `account_region`；`checked_at` 在 `max_age` 以内、且记录的 region 集合与当前 `scan_regions` 相同时直接使用（新增或去掉扫描 region 后缓存立即失效）。
2. 否则用该账号的凭证在 **us-east-1** 调用 `ec2.describe_regions(AllRegions=False)`（所有账号默认启用 us-east-1；primary region 是 opt-in）。
3. 结果与 `scan_regions` 取交集，写回 `account_region`（同时写入未启用的扫描 region，`enabled=false`）。

### 3.2 `expand`

| scope | 生成的 task |
|---|---|
| `all` | 由 planner 按账号批次调用 `expand(account_id=...)`，不直接展开 |
| `account_id` | 每个启用的扫描 region 一个，再加一个 `global` |
| `account_id + region` | 一个；非 `global` 的 region 必须在 `enabled_regions(account)` 中 |
| `account_id + resource_type` | regional 类型：`enabled_regions(account)` 的每个 region 一个，`resource_types=[type]`，为空时抛 `BadRequestError`；global 类型：一个 `global` |
| `account_id + region + resource_type` | 一个，`resource_types=[type]`；region 规则同上 |
| `arn` | 不经队列，由 API 直接处理（spec 07） |

task 的 `queue`：定时任务为 `scheduled`，API 为 `ondemand`。

`validate(scope)` 只做不依赖账号的检查（region 是扫描 region 或 `global`、类型已注册、global/regional 匹配），API 在调用 `expand` 前先调用它；依赖账号已启用 region 的检查在 `expand` 中进行。两者都抛 `BadRequestError`。

### 3.3 `submit`

先在一个事务中批量插入 `scan_task`（`queued`），提交后再投递；投递失败的 task 用 `finish_task(status=failed, error_code=DISPATCH_FAILED)` 标记。顺序是"先写库再投递"，保证 worker 收到的消息在库里一定有记录。

## 4. Step Functions：`scan-run`

由 EventBridge Scheduler `rate(4 hours)` 启动（Terraform 中定义）。ASL 放在 `terraform/statemachines/scan_run.asl.json`，用 `templatefile` 注入函数 ARN。

```
CreateJob ──skipped?──► Done
   │
ListTargets
   │
DispatchAccounts (Distributed Map, ItemBatcher MaxItemsPerBatch=50, MaxConcurrency=10)
   │   └─ planner λ action=dispatch_batch
WaitLoop: Wait 300s → CheckProgress ──done?──► Finalize ──► Done
                ▲───────── no ───────┘
```

每个状态都配置 `Retry`（`Lambda.ServiceException`、`Lambda.TooManyRequestsException`，指数退避 3 次）和 `Catch`（转到 `Finalize`，带上错误信息）。

## 5. Planner Lambda actions

一个函数，按 `event.action` 分派。每个 action 的输入/输出用 pydantic 模型定义，示例放在 `tests/fixtures/planner/<action>.event.json` / `<action>.response.json`。

### 5.1 `create_job`

输入：`{"action": "create_job", "scheduled_at": "<Scheduler 提供的时间>"}`
1. `create_job(trigger=schedule, scope={"all": true}, deadline_at=now+3.5h)`；如果因唯一索引冲突返回已有的活跃 job → 上一轮还没结束，输出 `{"skipped": true, "active_job_id": ...}`，记录 WARNING 与指标 `ScheduleOverlap`。
2. UTC 当天第一轮（当天还没有 `trigger=schedule` 的 job）设置 `flags.full_refresh = true`。

输出：`{"skipped": false, "job_id": "...", "full_refresh": true}`

### 5.2 `list_targets`

1. 调用 account-directory λ 获取账号列表（`AccountDirectoryClient`，见 §6）。
2. 调用 broker `list_accessible_accounts`，取交集。
3. 无权限的账号：为每个账号插入一个 `region='*'` 的 `scan_task(status=auth_denied, queue=scheduled)`，便于统计与排查。
4. 输出 `{"job_id": ..., "accounts": ["123456789012", ...], "denied_count": 3}`。1000 个账号约 15KB，在 Step Functions 256KB 的限制内。

### 5.3 `dispatch_batch`

输入（Distributed Map 的批次格式）：`{"BatchInput": {"job_id", "full_refresh"}, "Items": ["123456789012", ...]}`
对每个账号：`enabled_regions` → `expand` → `submit(queue=scheduled)`。单个账号失败（例如凭证错误）不影响同批其他账号：失败账号写一个 `status=failed` 的 task 记录原因。
输出：`{"accounts": 50, "tasks": 248, "failed_accounts": 0}`

### 5.4 `check_progress`

读取 job 状态（job 终态由 worker 在最后一个 task 结束时设置，见 spec 02 §4.4）。
输出：`{"done": bool, "status": ..., "counts": {...}}`。job 已到终态或 `now > deadline_at` 时 `done=true`。

### 5.5 `finalize`

1. 若 job 仍未到终态（已超过 deadline）：把非终态 task 置为 `timed_out`，job 置为 `timed_out`。
2. 计算并写入 `stats`；上报指标 `JobDuration`、`JobTasksFailed`、`JobTasksTimedOut`。
3. 如果由 `Catch` 进入，记录错误到 `scan_job.error`。

### 5.6 `maintenance`

由另一个 EventBridge Scheduler `rate(15 minutes)` 直接调用 planner λ（不经 Step Functions）：

1. `timeout_stale(stale_before = now - 30min)`：心跳过期的 task 置为 `timed_out`，并对受影响的 job 调用 `refresh_job_status`（主要用于 API job，API 进程中途退出时的兜底）。
2. 对已过 `deadline_at` 且仍未终态的 API job，执行与 `finalize` 相同的超时处理。
3. `purge_deleted(older_than = now - deleted_retention_days)`，分批执行直到本次调用剩余时间不足 30 秒。

## 6. `AccountDirectoryClient`

```python
class AccountDirectoryClient:
    def list_accounts(self) -> list[str]: ...
```

⚠️ account-directory λ 的实际请求/响应格式**待确认**。先按以下假设实现，接入时只改这个类与对应 fixture：

- 请求：`{}`（fixture：`planner/account_directory.request.json`）
- 响应：`{"accounts": [{"account_id": "123456789012", "status": "ACTIVE"}]}`，只取 `status == "ACTIVE"` 的账号（fixture：`planner/account_directory.response.json`）

## 7. 配置（`PlannerSettings`，前缀 `PLANNER_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `account_directory_function_name` | 必填 | |
| `job_deadline_minutes` | 210 | |
| `account_region_max_age_hours` | 24 | |
| `task_stale_minutes` | 30 | |
| `deleted_retention_days` | 180 | 软删除资源的保留天数（≥ 1），Terraform 变量 `deleted_retention_days` |

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| 每个 action 的输入输出 | `planner/<action>.event.json` / `.response.json` |
| `create_job`：重叠时 skipped；当天第一轮 full_refresh | 集成（PG） |
| `enabled_regions`：缓存命中、过期刷新、扫描 region 变化时失效、固定在 us-east-1 调用 | moto + PG |
| `expand`：表 §3.2 的每种 scope；账号级总是生成 global task | `foundation/settings.local.json` |
| `SqsDirectDispatcher`：按 region 路由、global 路由到 us-east-1、部分失败 | moto SQS（4 个 region 各建队列） |
| `submit`：投递失败的 task 被标记为 `DISPATCH_FAILED` | moto + PG |
| `maintenance`：stale task 超时、purge 分批 | PG |
| ASL 文件是合法 JSON，且所有 `Resource` 占位符都在 Terraform 的 `templatefile` 变量中 | — |

## 验收标准

- 以上测试全部通过；`scheduling.py` 覆盖率 ≥ 85%。
- 本地用假的 account-directory 与 broker（fixture 驱动），对 5 个账号 × 4 个 region 跑一遍 `create_job → list_targets → dispatch_batch`，moto SQS 中出现 25 条消息（20 条 regional + 5 条 global）。

## 实现修订（2026-10-27，与上文冲突时以本节为准）

- 定时 job 创建时带 `flags.dispatching = true`，在 Distributed Map 结束后由新增的 planner action `mark_dispatched` 清除。否则 worker 先完成前几批 task 时，job 会在后续批次投递之前就被判为结束。状态机：`CreateJob → ListTargets → DispatchAccounts → MarkDispatched → WaitLoop → Finalize`。
- 无权限的账号与展开失败的账号都会记一条 `region='*'` 的 task（`auth_denied` / `failed`）。
- 组装代码在 `inventory/bootstrap.py`（`build_runtime`），planner、worker、API 共用。

### 第二轮修订（M3 审查后）

- `Scheduler.expand(scope, queue) -> list[NewTask]`、`submit(job, tasks) -> SubmitResult(tasks, dispatch)`、`enabled_regions(account_id)`；`dispatch` 抛异常时所有未发送的 task 标记 `DISPATCH_FAILED`。`account_region` 的 `checked_at` 随机提前 0–120 分钟，避免 1000+ 账号同时过期。`account_region_max_age_hours` 移到 `CommonSettings`（API 也用）。
- `dispatch_batch` 幂等且从不抛异常：已有 task 的账号跳过（Map 重试），并有唯一索引 `ux_scan_task_scheduled (job_id, account_id, region) WHERE queue='scheduled'` 兜底；整批的 task 一次写入、一次投递。返回 `{accounts, skipped_accounts, tasks, failed_accounts, failed_tasks}`。展开失败按错误码区分 `auth_denied` / `failed`。
- 状态机：`CreateJob` 失败进入 `Fail`；Map 失败 → `MarkDispatchedWithError`（把错误写入 `scan_job.error`，job 最终为 `partial`）→ 继续等待；`CheckProgress` 失败回到 `WaitForWorkers`（仍受 deadline 约束）。
- `finalize` 带 error 且未到 deadline 时只记录错误、不超时 task（避免一次瞬时故障丢掉整轮工作），之后由 `maintenance` 收尾；`check_progress` 与 `maintenance` 都会调用 `refresh_job_status` 作为兜底。`maintenance` 也处理过期的定时 job（状态机异常退出时）。
- `task_stale_minutes` 默认 10（心跳 120 秒，5 次未更新即视为失联）；`PlannerSettings.account_directory_function_name` 为必填。

### 第三轮修订（类型级 scope 不再要求 region）

- `account_id + resource_type` 合法：regional 类型展开到「账号已启用 region ∩ `supported_regions` ∩ 该类型 endpoint 可用的 region」，交集为空时报错；global 类型展开为 `global`。
- 指定的非 `global` region 必须对该账号已启用；指定类型时该类型在该 region 的 endpoint 必须可用（之前只在 worker 中记为 `skipped_no_endpoint`）。
- 新增 `Scheduler.validate(scope)`，替代 `validate_region` / `validate_type`。

### 第四轮修订（软删除保留期）

- `deleted_retention_days` 默认值由 30 改为 180，并要求 ≥ 1；Terraform 新增变量 `deleted_retention_days`（默认 180），写入 planner λ 的 `PLANNER_DELETED_RETENTION_DAYS`。

### 第五轮修订（发布与配置，spec 08 R4–R6）

- `home_region` → `primary_region`，`supported_regions` → `scan_regions`；删除 `cells`：队列按名字查找。
- `DescribeRegions` 固定在 us-east-1 调用，去掉逐个 region 回退；`account_region` 缓存在扫描 region 集合变化时失效。
- 删除 endpoint 可用性判断：账号级扫描总是生成 global task；类型级 scope 不再检查 endpoint。`Scheduler` 不再依赖 `EndpointResolver`。
