# Spec 06：Worker（M3）

## 目标

每个 region 的 ECS Fargate 服务：从 SQS 拉取 scan task，调用 `ScanEngine` 执行扫描，更新 task / job 状态，处理重试与重复投递。

## 范围

做：`inventory/handlers/worker.py`（主循环）、`TaskExecutor`（与运行环境无关的单条消息处理，方案 B 的 Lambda 版 worker 以后复用）。
不做：方案 B 的 Lambda 入口（spec 09）。

## 文件清单

```
inventory/handlers/worker.py
inventory/engine.py          # TaskExecutor 放在这里，与 ScanEngine 相邻
tests/worker/...  tests/fixtures/worker/...
```

## 1. 配置（`WorkerSettings`，前缀 `WORKER_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `region` | 必填 | 本 worker 所在 region；队列按名字 `inventory-<env>-scheduled|ondemand` 用 `GetQueueUrl` 查找 |
| `message_concurrency` | 2 | 同时处理的消息数 |
| `type_concurrency` | 4 | 单条消息内并行执行的 collector 组数 |
| `visibility_timeout_seconds` | 900 | 与队列配置一致 |
| `heartbeat_interval_seconds` | 120 | |
| `max_attempts` | 3 | |
| `retry_base_delay_seconds` | 60 | 第 n 次重试延迟 `min(900, base * 2^(n-1))` |
| `ondemand_wait_seconds` | 1 | |
| `scheduled_wait_seconds` | 10 | |
| `shutdown_grace_seconds` | 100 | 小于 ECS `stopTimeout`（120） |
| `task_stale_minutes` | 30 | 与 planner 一致，用于 `claim_task` |

## 2. 主循环（`python -m inventory.handlers.worker`）

```
setup_logging("inventory-worker"); 构建 ScanEngine / TaskExecutor
while not stopping:
    if 有空闲处理槽:
        msgs = receive(ondemand, wait=1s)            # 先取按需队列
        if not msgs: msgs = receive(scheduled, wait=10s)
        每条消息提交给线程池（submit_with_context）处理
```

- `receive_message` 每次最多取空闲槽数量的消息，`MessageAttributeNames=["All"]`。
- SIGTERM / SIGINT：停止拉取新消息；等待在途消息最多 `shutdown_grace_seconds`；未完成的消息不删除，可见性超时后会被重新投递，由 `claim_task` 的心跳过期规则接手。
- `worker_id` = `<ECS task id 或主机名>:<pid>`。

## 3. `TaskExecutor.handle(message_body, attributes) -> HandleOutcome`

与运行环境无关，返回 `delete`（处理完，删除消息）或 `keep`（保留，让 SQS 重新投递）。

```
1. 解析 ScanTaskMessage；失败 → 记录 ERROR、指标 BadMessage，返回 keep（多次失败后进入 DLQ）
2. context_scope(request_id, job_id, task_id, account_id, region)
3. task = claim_task(...)；None → 重复投递，记录 INFO，返回 delete
4. 启动心跳线程：每 heartbeat_interval 调用 ChangeMessageVisibility 与 jobs.heartbeat
5. results = engine.scan(message)
6. 计算 task 状态并写库（见 §4）；需要重试时先发送重试消息，再标记 retrying
7. 停止心跳，返回 delete
```

第 5 步抛出未预期异常时：记录 ERROR（带堆栈），按"所有类型 `INTERNAL_ERROR`、可重试"进入 §4 的流程。

## 4. 状态与重试

单条消息结束后，按各类型的 `TypeResult` 计算：

| 情况 | 处理 |
|---|---|
| 全部 `succeeded` | task `succeeded` |
| 全部 `auth_denied` | task `auth_denied` |
| 有失败类型，其中可重试的存在且 `attempt < max_attempts` | 发送新消息：同一 `task_id`，`resource_types = 可重试的失败类型`（含其下游依赖），`attempt + 1`，带延迟；task 置为 `retrying`，`result` 合并本次结果 |
| 有失败类型，但都不可重试或已达上限 | 有成功的类型 → `partial`；没有 → `failed` |

- `result` 按类型合并：重试成功的类型会覆盖之前的失败结果。
- task 到达终态时调用 `finish_task`，其中会执行 `refresh_job_status`；如果这是该 job 的最后一个 task，job 在此时进入终态。

## 5. 日志与指标

- 每条消息开始与结束各一条 INFO 日志；结束日志包含 `status attempt duration_ms` 与各类型的计数汇总。
- 指标：`TaskDuration`、`TaskSucceeded`、`TaskFailed`、`TaskRetried`、`DuplicateDelivery`、`BadMessage`（维度 `Region`）。

## 6. 部署要点（细节见 spec 08）

- 每个 region 一个 ECS 服务，x86_64，0.5 vCPU / 1 GB 起步；镜像 `aws-inventory-discover/worker`（spec 08 R2）。
- 自动扩缩：按两个队列的 `ApproximateNumberOfMessagesVisible` 之和做 step scaling；最小任务数可配置（默认 1，保证按需扫描能被及时处理），最大 20。
- 队列：`visibility_timeout = 900`，DLQ `maxReceiveCount = 5`。

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| 正常消息：task `succeeded`，消息被删除 | moto SQS + PG + 假 `ScanEngine`；`worker/message.ok.json` |
| 重复投递：第二次 claim 返回 None，直接删除 | 同上 |
| 部分可重试失败：发送重试消息（类型、attempt、延迟正确），task 为 `retrying` | `worker/results.partial_retryable.json` |
| 达到最大次数：task 为 `partial` 或 `failed` | `worker/results.exhausted.json` |
| 全部 `auth_denied` | `worker/results.auth_denied.json` |
| 消息格式错误：返回 keep，并有 `BadMessage` 日志 | `worker/message.invalid.json` |
| 优先级：ondemand 有消息时不取 scheduled | moto SQS |
| SIGTERM：停止拉取，等待在途消息完成 | 假 engine 内阻塞 + 线程 |
| 心跳：长时间任务期间可见性超时被延长 | moto SQS |
| 最后一个 task 结束时 job 进入终态 | PG |

## 验收标准

- 以上测试全部通过；`handlers/worker.py` 与 `TaskExecutor` 覆盖率 ≥ 85%。
- 本地（moto server + compose PG）跑通：spec 05 验收中投递的 25 条消息被一个 worker 进程全部处理，job 进入终态。

## 实现修订（M3 审查后，与上文冲突时以本节为准）

- **认领分三种结果**（`JobRepository.claim(task_id, worker_id, attempt=, stale_before=)`）：
  - `claimed`：`queued/retrying` 且 `attempts == attempt - 1`，或 `running` 且心跳过期且 `attempts == attempt`（接管同一次尝试，attempts 不变）→ 执行；
  - `busy`：同一次尝试正由其他 worker 执行且心跳新鲜 → **保留消息**（`KEEP`），SQS 超时后再投递；
  - `done`：已结束、已被更新的尝试取代、或不存在 → 删除消息。
  这样被重新投递的消息不会在任务仍在运行时被当作重复删除，旧尝试的消息也不会再次执行。
- `finish_task` / `mark_retrying` 带 `worker_id`，只有当前持有者能写入。
- 心跳中 SQS 可见性与数据库心跳互相独立，任一失败不影响另一个；心跳线程带日志上下文。
- `task_stale_minutes` 默认 10；`WorkerSettings.region` 必填；ECS 任务 ID 来自 `WorkerSettings.ecs_metadata_uri`（环境变量 `ECS_CONTAINER_METADATA_URI_V4`）。
- 优雅退出：等待在途消息最多 `shutdown_grace_seconds`，超时则直接退出进程（`os._exit`），未完成的消息由心跳过期后的接管机制处理。
- 指标 `BadMessage`、`DuplicateDelivery` 带 `Region` 维度。
