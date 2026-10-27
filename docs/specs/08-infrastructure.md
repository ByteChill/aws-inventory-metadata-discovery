# Spec 08：基础设施（Terraform，M6）

## 目标

用 Terraform 部署方案 A 的全部资源。VPC、子网、peering 和已有的 interface endpoint 由网络团队维护，这里只作为输入引用，不创建。

## 版本与状态

- Terraform ≥ 1.10，AWS provider `~> 6.0`。
- 状态后端：S3（`use_lockfile = true`，不需要 DynamoDB 锁表），每个环境一个 key。
- 环境：`envs/dev.tfvars`、`envs/prod.tfvars`。

```bash
terraform -chdir=terraform init -backend-config=envs/dev.backend.hcl
terraform -chdir=terraform plan -var-file=envs/dev.tfvars
terraform -chdir=terraform apply -var-file=envs/dev.tfvars
```

## 目录

```
terraform/
  versions.tf  providers.tf       # provider 别名：ap_east_1、eu_west_1、eu_west_2、us_east_1
  variables.tf  outputs.tf
  main.tf                         # ap-east-1 的中心资源
  regions.tf                      # 4 个 module "region_cell"
  monitoring.tf                   # 告警、Logs Insights 查询
  statemachines/scan_run.asl.json
  modules/
    region_cell/                  # SQS（scheduled、ondemand、DLQ）、ECS 集群与 worker 服务、自动扩缩、日志组
    lambda_fn/                    # 容器镜像 Lambda：函数、日志组、可选 VPC 配置
  envs/dev.tfvars  dev.backend.hcl  prod.tfvars  prod.backend.hcl
```

## 输入变量（主要）

```hcl
variable "env" {}
variable "image_tag" {}
variable "regions" {
  type = map(object({
    vpc_id             = string
    private_subnet_ids = list(string)
    endpoints          = map(string)   # 服务名 -> endpoint URL（endpoint-specific DNS），写入 RegionCell.endpoints
  }))
}
variable "home_region"         { default = "ap-east-1" }
variable "global_home_region"  { default = "us-east-1" }
variable "adfs_idp_url" {}
variable "account_directory_function_name" {}
variable "worker_min_count"    { default = 1 }
variable "worker_max_count"    { default = 20 }
```

## 中心资源（`main.tf`，ap-east-1）

| 资源 | 说明 |
|---|---|
| ECR 仓库 `inventory` | 开启跨 region 复制到其他 3 个 region（spoke 的 ECS 从本地仓库拉取镜像） |
| Aurora PostgreSQL 16 集群 + RDS Proxy | writer + 1 reader；Secrets Manager 托管主密码；安全组放行 4 个 VPC 的 CIDR |
| ElastiCache Redis（复制组） | 1 主 1 从，开启传输与静态加密，AUTH token 存 Secrets Manager |
| KMS key | broker 加密凭证用 |
| Secrets Manager `inventory/<env>/adfs` | 只创建 secret，值由人工写入，Terraform 忽略其值的变化 |
| Lambda `inventory-broker` | VPC 内；环境变量来自 `BrokerSettings` |
| Lambda `inventory-planner` | VPC 内；`image_config.command = ["inventory.handlers.planner_lambda.handler"]` |
| Step Functions `inventory-scan-run` | Standard 类型；`templatefile("statemachines/scan_run.asl.json", {...})` |
| EventBridge Scheduler | `rate(4 hours)` → 状态机；`rate(15 minutes)` → planner `maintenance` |
| ECS 服务 `inventory-api` | Fargate arm64；内部 ALB；可选 ADOT sidecar；健康检查 `/readyz` |
| `region_cell`（ap-east-1 自身） | 与其他 region 相同 |

所有组件都通过环境变量 `INVENTORY_CELLS` 拿到同一份 `cells` JSON（由 Terraform 根据 `var.regions` 与各 cell 的队列 URL 生成）。

## `region_cell` 模块

| 资源 | 说明 |
|---|---|
| SQS `inventory-<env>-scheduled` / `-ondemand` | 标准队列；可见性超时 900s；保留 4 天；各自一个 DLQ，`maxReceiveCount = 5` |
| ECS 集群与服务 `inventory-worker` | Fargate arm64，0.5 vCPU / 1 GB；`stopTimeout = 120`；command `python -m inventory.handlers.worker` |
| 自动扩缩 | step scaling：两个队列可见消息数之和；min `worker_min_count`，max `worker_max_count` |
| CloudWatch 日志组 | 保留 30 天 |

## 各 region VPC 需要的 interface endpoint（输入，不创建）

| 用途 | 服务 |
|---|---|
| ECS 运行 | `ecr.api`、`ecr.dkr`、`logs`、S3 gateway endpoint（镜像层） |
| worker 访问 | `sqs`（本 region）；ap-east-1 的 `lambda`（经 peering）；RDS Proxy（经 peering） |
| 采集 | `ec2`、`ecs`、S3 gateway endpoint；跨 region 刷新 S3 需要 S3 interface endpoint |
| 全局（仅 us-east-1） | `iam`、`route53`（如果存在；不存在则对应 collector 自动跳过） |
| 中心（仅 ap-east-1） | `sts`、`kms`、`secretsmanager`、`lambda`、`states` |

peering 相关要求：被跨 region 访问的 endpoint（各 region 的 `sqs`、`ec2`、`ecs` 等，ap-east-1 的 `lambda`）安全组必须放行对端 VPC 的 CIDR。

## 监控（`monitoring.tf`）

告警（SNS topic 由变量传入）：

| 告警 | 条件 |
|---|---|
| DLQ 有消息 | 任一 DLQ `ApproximateNumberOfMessagesVisible > 0` |
| 定时 job 失败或超时 | 指标 `JobTasksFailed` / `JobTasksTimedOut` 超过阈值 |
| 定时任务重叠 | `ScheduleOverlap > 0` |
| ADFS 登录失败 | `AdfsLoginFailed > 0` |
| API 5xx | ALB `HTTPCode_Target_5XX_Count` |

Logs Insights 保存的查询（`aws_cloudwatch_query_definition`）：

| 名称 | 查询 |
|---|---|
| 按 request_id 追踪 | 所有组件日志组，`filter request_id = "<id>"`，按时间排序 |
| 按 job 汇总 | worker 日志组，`filter job_id = "<id>"`，按 `status` 统计 |
| AWS 限流排行 | `filter retry_attempts > 0 or error_code like /Throttl/`，按 `aws_service, aws_operation, account_id` 统计 |

## 测试与验收

- `terraform fmt -check`、`terraform validate` 通过；可选 `tflint`。
- `envs/dev.tfvars` 在 dev 账号 `plan` 无错误。
- 部署后在 dev 环境手动触发一次状态机，job 进入终态，API `/readyz` 返回 200。

## 实现修订（M6）

- 文件：`versions.tf`（aws `~> 6.0`、random `~> 3.6`，S3 后端为部分配置）、`providers.tf`、`variables.tf`、`main.tf`、`regions.tf`、`monitoring.tf`、`outputs.tf`、`modules/region_cell`、`modules/lambda_fn`、`envs/{dev,prod}.tfvars` 与 `envs/{dev,prod}.backend.hcl`。
- `var.regions` 每项为 `{vpc_id, cidr_block, private_subnet_ids, endpoints}`；provider 别名不能动态选择，所以每个受支持的 region 一个 `module "cell_*"` 块，按 `var.regions` 是否包含该 region 启用。新增 region 需要同时新增 provider 别名与 module 块。
- `INVENTORY_CELLS` 中的队列 URL 由账号 ID 与命名规则在 `locals` 中直接生成（不用模块输出），避免 cell 之间的依赖循环。
- 数据库：Aurora PostgreSQL 16，master 密码由 RDS 托管（`manage_master_user_password`），应用读取该 secret（只有 username/password），主机由 `INVENTORY_DATABASE_HOST`（RDS Proxy 端点）提供，见 `CommonSettings.database_host/port/name`。
- 告警必须通知同 region 的 SNS topic：`var.alarm_topic_arns` 按 region 提供；DLQ 告警在 `region_cell` 模块内创建。Logs Insights 保存的查询只覆盖 home region 的日志组（CloudWatch 查询不能跨 region）。
- API 的 ADOT sidecar 由 `var.api_tracing_enabled` 控制（同时设置 `API_TRACING_ENABLED`）。
- 已知限制：Lambda 环境变量总计 4 KB，按 dev 示例（4 个 region）约 3 KB；增加 region 或 endpoint 前需评估，必要时把 cells 配置移到 SSM Parameter Store。
- 校验：`terraform fmt -check -recursive`、`terraform init -backend=false`、`terraform validate` 通过（Terraform 1.13）。未在真实账号执行 `plan`。

### 第二轮修订（收尾审查后）

- `region_cell` 的 IAM 角色名包含 region（IAM 是全局的，否则第二个 cell 会因重名失败）。
- 自动扩缩的指标为两个队列 `Visible + NotVisible` 之和：只看可见消息会在一轮扫描的尾部把仍在处理消息的 worker 缩掉。
- 数据库连接经 RDS Proxy：不使用 `options` 启动参数（Proxy 不支持），`statement_timeout` 在建立连接时用 `SET` 设置（这会让该连接在 Proxy 上被"钉住"，多路复用失效，但功能正常）；关闭 psycopg 的自动预处理语句；每个新连接都从 Secrets Manager（缓存 60 秒）读取当前凭证，RDS 自动轮换密码后无需重启。
- 所有 region 的 worker 都要读数据库 secret：home region 的 `secretsmanager` 必须配置 endpoint-specific DNS，并放行各 region CIDR。
- broker 的 `AssumeRoleWithSAML` 走 `var.broker_sts_region`（默认 us-east-1，所有账号默认启用），该 region 的 cell 需配置 endpoint-specific 的 `sts` endpoint；home region（ap-east-1）是 opt-in region，未启用它的账号在那里调用 STS 会失败。broker 的 Secrets Manager 与 KMS 始终在 home region。
- ECS 组件设置 `INVENTORY_METRICS_MODE=api`：awslogs 驱动不会提取 EMF，指标改为在进程内聚合、每 60 秒 `PutMetricData` 一次（各 region cell 需配置 `monitoring` endpoint，任务角色有 `cloudwatch:PutMetricData`）。Lambda 继续用 EMF。
- 首次部署与每次发布的顺序见 README「部署」：ECR 先建、镜像 tag 不可变且每次唯一、`schedules_enabled=false` 直到迁移完成；迁移用独立的 `migrate` 任务定义（`python -m alembic upgrade head`）在滚动更新前执行。
- ECR 仓库名带环境前缀（`inventory-<env>-inventory`）；复制配置是账号级单例，由 `manage_ecr_replication` 控制。
- ADOT 镜像由 `var.adot_image` 指定（私有 VPC 无法访问 public.ecr.aws，需先同步到本账号 ECR）；API 任务角色补充了 ADOT 默认配置需要的 logs 权限。
- Aurora 集群与实例忽略 `engine_version` 的变化（AWS 自动小版本升级）。
- 镜像中 `pip --target` 安装的命令行工具在 `${LAMBDA_TASK_ROOT}/bin`，Dockerfile 已加入 `PATH`；ECS 入口使用 `python -m uvicorn` / `python -m alembic`。
