# 设计文档：aws-inventory-metadata-discovery

> 状态：v1 设计已确认（2026-10-27）
> 实现细节见 [specs/](specs/)，HTTP 契约见 [api.md](api.md)。本文件只描述"是什么、为什么"，不描述逐行实现。

## 1. 背景与目标

我们有一个 center 账号，在 `ap-east-1` / `eu-west-1` / `eu-west-2` / `us-east-1` 各有一个私有 VPC，并配置了 AWS 服务的 regional interface endpoint。`ap-east-1` 的 VPC 中有一个 RDS，里面存放 account list 表。其他三个 region 的 VPC 通过 VPC peering 与 `ap-east-1` 相连（中心辐射，peering 不可传递）。

已有/将有的组件：

| 组件 | 说明 |
|---|---|
| `aws-inventory-account-directory` λ | VPC 内，从 RDS 的 account 表读取账号列表 |
| broker λ（本项目重写为 v2） | VPC 内，可访问公司内网 ADFS；用账号密码登录 ADFS 拿 SAML response，再 `AssumeRoleWithSAML` 换取目标账号的 ReadOnly 临时凭证。没有权限时返回错误 |

目标：

1. **定时扫描**：每 4 小时扫描所有账号、所有已启用的受支持 region、所有已注册的资源类型，把结果存储下来。
2. **API 服务**：提供查询（过滤、分页、数量统计），并能按需触发扫描，粒度为：账号 / 账号+region / 账号+region+type / 单个 ARN。
3. **存储始终是 AWS 真实状态的映射**：消费者修改资源后，调用 API 刷新该 ARN，由我们**重新从 AWS 拉取**（真实数据永远来自 AWS，不接受消费者写入资源内容）。
4. **可扩展**：资源采集按 resource type 隔离，新增类型只需新增一个 collector 类。

非目标（v1）：权限控制（无权限的账号在 broker 阶段就被拦截）；变更历史（只预留接口，见 §11）；方案 B 网络（只预留接口，见 §7.2）。

## 2. 术语

| 术语 | 含义 |
|---|---|
| resource type | CloudFormation 风格的类型名，如 `AWS::EC2::Instance` |
| resource key | `(resource_type, account_id, region, resource_id)`，资源的唯一标识，也是最小扫描粒度 |
| global | 全局资源（IAM、Route 53）使用的虚拟 region 值 `global` |
| scope | 一次扫描的范围：`{account_id, region?, resource_type?, arn?}` |
| scan job | 一次扫描请求（定时或 API 触发），对应表 `scan_job` |
| scan task | job 拆分后投递到队列的一条消息，粒度为 `(account, region[, types][, ids])`，对应表 `scan_task` |
| region cell | 一个 region 内的执行单元：SQS 队列 + ECS worker |
| collector | 某一个 resource type 的采集实现类 |

## 3. 约束与规模

- 账号 1000+；当前扫描 4 个 region（账号启用的 region ∩ 受支持 region），以后会增加，届时会给新 region 配好网络。
- 单个账号单个 region 的某类资源可达 1500+（如 ECS service）。
- STS 凭证最长 60 分钟；ADFS SAML assertion 有效期 30 分钟，且**一个 assertion 包含所有账号的 role**。
- 扫描周期 4 小时。

估算：每轮约 1000 × 4（regional）+ 1000（global）≈ 5000 条 task 消息。单条平均 2–3 分钟，每个 region 20 个 worker 并发，约 2 小时内完成，留有余量。

## 4. 总体架构（方案 A，v1 实现）

```
                ┌──────────────── ap-east-1 (hub) ──────────────────────────┐
                │ EventBridge Scheduler (rate 4h)                            │
                │      └─► Step Functions: scan-run                          │
                │            ├─ planner λ: create_job / list_targets         │
                │            ├─ Distributed Map: planner λ dispatch_batch    │
                │            └─ check_progress 循环 → finalize               │
                │ account-directory λ        broker λ ──► ElastiCache Redis  │
                │ Aurora PostgreSQL (+RDS Proxy)                             │
                │ FastAPI on ECS (internal ALB)                              │
                │ region cell: SQS(scheduled/ondemand) → ECS worker          │
                └────▲─────────────────▲─────────────────▲───────────────────┘
             peering │          peering│          peering│
  ┌──────────────────┴─┐ ┌─────────────┴──────┐ ┌────────┴────────────────────┐
  │ eu-west-1 cell     │ │ eu-west-2 cell     │ │ us-east-1 cell              │
  │ SQS → ECS workers  │ │ SQS → ECS workers  │ │ SQS → ECS workers           │
  │ ec2/ecs/s3 vpce    │ │ ...                │ │ + global (IAM/R53，如有 vpce)│
  └────────────────────┘ └────────────────────┘ └─────────────────────────────┘
```

跨 region 访问路径（全部经 peering，使用 interface endpoint 的 **endpoint-specific DNS**，如 `vpce-xxx.sqs.eu-west-1.vpce.amazonaws.com`）：

| 调用方 → 目标 | 用途 |
|---|---|
| planner / API → 各 region SQS endpoint | 投递 scan task |
| 各 region worker → ap-east-1 Lambda endpoint | 调用 broker |
| 各 region worker → RDS Proxy | 写入扫描结果 |
| API → 目标 region 的 ec2/ecs/... endpoint | 单 ARN 同步刷新 |

代码中所有 boto3 client 都经 `ClientFactory` 创建：同 region 只给服务名和 region（默认域名解析到本 VPC 的 endpoint）；跨 region 的 endpoint 专用 DNS 由环境变量 `INVENTORY_ENDPOINTS` 提供，每个组件只配置自己用到的（spec 08 R4、R7）。

## 5. 核心流程

### 5.1 定时扫描

1. EventBridge Scheduler 每 4 小时启动 Step Functions `scan-run`。
2. `create_job`：写入 `scan_job(trigger=schedule)`。若是 UTC 当天第一轮，设置 `full_refresh=true`（部分 collector 用它做每日全量兜底）。
3. `list_targets`：调用 account-directory 拿账号列表（含名称、EIM、账号类型，写入 `account` 表）；调用 broker `list_accessible_accounts` 取交集。无权限账号直接记 `scan_task(status=auth_denied)`。
4. Distributed Map 按批（每批 50 个账号）调用 `dispatch_batch`：对每个账号确定启用的 region（`account_region` 表缓存 24h，过期则用 `ec2:DescribeRegions` 刷新），与受支持 region 取交集，为每个 `(account, region)` 和 `(account, global)` 创建 `scan_task` 并投递到对应 region 的 `scheduled` 队列（global 投到 home region `us-east-1`）。
5. worker 消费消息，按 collector 依赖分组执行扫描、对账、写库，更新 `scan_task` 和 `scope_state`。
6. `check_progress` 每 5 分钟检查一次，全部 task 到达终态或超过 deadline（3.5h）后进入 `finalize`：超时 task 标记 `timed_out`，汇总 job 状态与统计。

### 5.2 API 触发扫描

1. `POST /v1/scans`，body 为 scope。相同 scope 已有未完成 job 时直接返回该 job（去重）。
2. API 将 scope 展开成 task（账号级需要 enabled regions，同样读 `account_region` 缓存），投递到对应 region 的 `ondemand` 队列。worker 优先消费 `ondemand`。
3. 支持 `wait_seconds`（≤25）：在时限内 job 完成则返回 200 和结果，否则返回 202 和 `job_id`。

### 5.3 单 ARN 刷新（消费者修改资源后调用）

1. `POST /v1/resources/refresh`，body 为 ARN 列表（≤100 个）。
2. API 创建 job，每个 ARN 一个 task（`resource_ids=[id]`），投递到资源所在 region 的 `ondemand` 队列；该 region 的 worker 调用 `get_resource` 并对账写库（存在则 upsert，不存在则软删除）。API 进程不调用 AWS 采集资源。
3. 在 `wait_seconds`（默认 20）内完成返回 200 和每个 ARN 的结果（`created / updated / unchanged / deleted / not_found / error`）；未完成返回 202。

注意：EC2 等 API 是最终一致的，刚修改完立即刷新可能读到旧值，API 文档中需说明。

## 6. 关键设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 自建扫描，不用 AWS Config Aggregator / Resource Explorer | 二者都要求账号在同一 Organization 且在成员账号部署配置；我们只有 ADFS ReadOnly 凭证 |
| D2 | 控制面集中在 ap-east-1，执行面按 region 分 cell | worker 用本地 endpoint 调 AWS API，延迟低；cell 之间互不影响 |
| D3 | task 粒度为 `(account, region)`，cell 内按类型有限并发；失败类型单独重试 | AWS 限流按 account+region 计算，这个粒度最不容易被限流；凭证一个 task 只取一次 |
| D4 | 定时与按需使用同一个 `ScanScope` / `ScanTaskMessage` | worker 只有一套逻辑 |
| D5 | worker 本地计算差异，只写变化；列表不完整绝不删除；用 `observed_at` 做乐观并发控制 | 写入量小；避免限流/分页失败导致误删；定时扫描与按需刷新并发时数据新者胜 |
| D6 | "最后确认时间"记在 `scope_state`，不逐行更新 | 避免每 4 小时重写几百万行造成 MVCC 膨胀 |
| D7 | `resource` 表按 `resource_type` LIST 分区，各分区列结构相同，通用 `relations` 列 | 满足"按 type 分表"，同时跨类型查询无需 UNION |
| D8 | 唯一键为 resource key，不用 ARN | ARN 格式不统一（AMI 的 ARN 不含账号） |
| D9 | broker 两级 Redis 缓存（SAML assertion + 每账号 STS 凭证），加锁防并发重复获取 | assertion 含所有 role，ADFS 约 25 分钟登录一次 |
| D10 | 单 ARN 刷新投递给资源所在 region 的 worker，API 用 `wait_seconds` 长轮询结果 | worker 在本 region 调用（同 region 无需配置 endpoint）；API 只需各 region 的 SQS 地址 |
| D11 | 全局资源 `region=global`，由 us-east-1 cell 扫描；所需 endpoint 由 infra 保证，代码不判断是否存在 | 不为可能缺失的基础设施写兜底逻辑 |
| D12 | ECS TaskDefinition 只采集 service **正在使用**的 revision | 业务关心实际运行的定义 |
| D13 | 变更历史只预留 `ChangeSink` 接口（v1 为空实现） | v1 范围控制；以后接 outbox → S3 → Athena |
| D14 | 方案 B（region 间无 peering）只预留接口，正式方案为"非 VPC Lambda 集中部署在 ap-east-1" | 见 §7.2 |
| D15 | `request_id` 贯穿 API → 队列 → worker → broker 的所有日志 | 一个 ID 就能查到一次调用的完整链路 |
| D16 | 单个 Python 包、单个 `pyproject.toml`、单个镜像，多入口 | 维护简单 |

## 7. 网络方案

### 7.1 方案 A（v1 实现）

即 §4。前提：endpoint 安全组放行对端 VPC CIDR；RDS Proxy、ap-east-1 的 Lambda endpoint 放行三个 spoke region 的 CIDR。

新增 region（方案 A）：建 VPC + 被扫描服务的 interface endpoint → 与 ap-east-1 建 peering，配置路由与安全组 → 在 tfvars 的 `regions` 中加入该 region（VPC、子网、其 `sqs` 的专用 DNS）→ apply：Terraform 创建该 region 的队列与 worker，并更新 planner / API 的 `INVENTORY_SCAN_REGIONS` 与 `INVENTORY_ENDPOINTS`。

### 7.2 方案 B（只预留接口，不实现）

前提：region 之间没有 peering。正式方案为**非 VPC Lambda 集中部署在 ap-east-1**：

- 非 VPC Lambda 可直接调用任意 region 的公网 AWS API（含 IAM/Route 53），可 Invoke VPC 内的 broker λ 与 account-directory λ，但**不能访问 RDS**。
- planner 改为非 VPC Lambda；task 投递到 ap-east-1 的 SQS FIFO 队列，`MessageGroupId = account#region`。
- scan worker 为非 VPC Lambda，task 粒度改为 `(account, region, collector 组)` 以适应 15 分钟上限；结果写入 ap-east-1 S3（NDJSON.gz + manifest），由 VPC 内 Ingester 计算差异写 PG（`complete=true` 才删除）。
- 单 ARN 刷新：API 同步 Invoke 非 VPC 的 refresh λ，记录随返回值带回，由 API 写库（约 1–2 秒）。
- 前提检查：目标账号策略/SCP 无 `aws:SourceIp` / `aws:SourceVpc(e)` 限制；公司允许非 VPC Lambda；ap-east-1 Lambda 并发配额足够。

为此预留的接口（见 [specs/09-reserved-extensions.md](specs/09-reserved-extensions.md)）：

| 接口 | 方案 A 实现（v1） | 方案 B 实现（预留） |
|---|---|---|
| `Dispatcher` | `SqsDirectDispatcher` | `SqsFifoDispatcher` |
| `CredentialProvider` | `BrokerLambdaProvider` | 同左 |
| `ResultSink` | `PostgresResultSink` | `S3SnapshotResultSink` + Ingester |
| `RefreshExecutor` | `InlineRefreshExecutor` | `LambdaInvokeRefreshExecutor` |

配置项 `transport` 取值 `peering | lambda`；设为 `lambda` 时启动即报错 "方案 B 未实现"。

## 8. 数据存储概要

Aurora PostgreSQL（新库，与 account list 分离）。详见 [specs/02-data-model.md](specs/02-data-model.md)。

| 表 | 作用 |
|---|---|
| `resource` | 当前资源状态，按 `resource_type` LIST 分区；统一列 + `data jsonb` 原始值 + `tags jsonb` + `relations jsonb` |
| `scope_state` | 每个 `(account, region, type)` 的最后扫描时间、状态、数量、错误 |
| `account_region` | 账号已启用 region 的缓存 |
| `scan_job` / `scan_task` | 扫描任务与执行记录 |

软删除的资源默认保留 180 天后清理（`PLANNER_DELETED_RETENTION_DAYS`，Terraform 变量 `deleted_retention_days`）。

## 9. 凭证（broker v2）

详见 [specs/01-broker.md](specs/01-broker.md)。要点：

- Redis 两级缓存：`saml`（≤25 分钟）与 `cred`（到期前；返回时要求剩余 ≥ `min_ttl`，默认 20 分钟，大于 botocore 提前刷新的 15 分钟）。
- `SET NX PX` + Lua 释放锁，防止并发重复登录 ADFS / 重复 AssumeRole。
- 写入 Redis 前用 KMS 数据密钥做 AES-GCM 加密。
- 账号不在 assertion 的 role 列表中 → `ACCESS_DENIED`，不调用 STS。
- 调用方 `BrokerLambdaProvider` + botocore `RefreshableCredentials`，进程内再缓存一层。

## 10. 可观测性

详见 [specs/00-foundation.md](specs/00-foundation.md)。

- 所有组件输出 JSON 日志，公共字段：`request_id job_id task_id account_id region resource_type arn duration_ms`。
- API 接收或生成 `X-Request-ID`（ULID），写入 `scan_job.request_id`、SQS 消息属性、broker 请求的 `correlation_id`；响应头回写 `X-Request-ID`，错误响应体也带 `request_id`。
- 每次 AWS API 调用记录服务、操作、耗时、状态码、AWS RequestId、重试次数；限流记 WARNING 并上报指标。
- 敏感字段（`SecretAccessKey`、`SessionToken`、`SAMLResponse`、`password` 等）一律脱敏。
- Lambda 使用 Powertools Logger/Tracer/Metrics；API 可选 OpenTelemetry → X-Ray（`API_TRACING_ENABLED`）。

## 11. 变更历史（预留）

v1：`Reconciler` 计算出 `ChangeEvent`，在写库的同一事务中交给 `ChangeSink`，默认 `NullChangeSink`。`scan_task.result` 记录每类资源新增/修改/删除数量。

以后：`OutboxChangeSink` 写 `change_outbox` 表 → exporter 经 Firehose 写 S3（Parquet，`changes/` 与每日 `snapshots/`）→ Glue + Athena → `/v1/history/*`。v1 期间的变更无法事后补回。详见 [specs/09-reserved-extensions.md](specs/09-reserved-extensions.md)。

## 12. v1 范围

| 实现 | 只预留 |
|---|---|
| 方案 A 全链路：Scheduler、Step Functions、planner、4 个 region cell | 方案 B 各接口实现 |
| broker v2 + Redis | 历史（outbox、S3、Athena、`/v1/history`） |
| collector：EC2 Instance / AMI / VPC / Subnet / SecurityGroup；ECS Cluster / Service / TaskDefinition（在用）；S3 Bucket；IAM Role / User / Policy、Route 53 HostedZone（有 endpoint 才启用） | 其他资源类型 |
| PG 分区表、`scope_state`、`account_region`、`scan_job`、`scan_task` | |
| API：资源查询、统计、触发扫描、单 ARN 刷新、覆盖率查询 | |
| Terraform | |

## 13. 风险与待验证

| 项 | 验证方式 |
|---|---|
| 经 peering 用 endpoint-specific DNS 访问对端 region 的 SQS / Lambda / ec2 等 endpoint | M3 前做 PoC |
| us-east-1 是否有 IAM / Route 53 interface endpoint | 控制台确认，没有则对应 collector 自动跳过 |
| ADFS 登录流程是否为标准表单 | 先按标准实现，接入真实环境时调整 `AdfsClient` |
| ECS DescribeServices / IAM API 限流在大账号上的表现 | M5 后用真实账号压测 |

## 14. 文档索引

| 文档 | 内容 |
|---|---|
| [api.md](api.md) | HTTP API 契约 |
| [specs/00-foundation.md](specs/00-foundation.md) | M0：项目骨架、配置、日志与链路追踪、测试基建 |
| [specs/01-broker.md](specs/01-broker.md) | M1：broker v2 |
| [specs/02-data-model.md](specs/02-data-model.md) | M2：数据库表结构、迁移、Repository |
| [specs/03-collector-framework.md](specs/03-collector-framework.md) | M2：collector 基类、注册表、ScanEngine、Reconciler |
| [specs/04-collectors.md](specs/04-collectors.md) | M2/M5：各资源类型的采集规格 |
| [specs/05-planner.md](specs/05-planner.md) | M3：调度、planner、消息契约 |
| [specs/06-worker.md](specs/06-worker.md) | M3：worker |
| [specs/07-api-service.md](specs/07-api-service.md) | M4：API 服务实现 |
| [specs/08-infrastructure.md](specs/08-infrastructure.md) | M6：Terraform |
| [specs/09-reserved-extensions.md](specs/09-reserved-extensions.md) | 预留：方案 B、变更历史 |
