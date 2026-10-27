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
  versions.tf  providers.tf       # 一个 provider（primary region）；其他 region 的资源各自设置 region
  variables.tf  outputs.tf
  main.tf                         # ap-east-1 的中心资源
  regions.tf                      # module "cell"（for_each = var.regions）
  monitoring.tf                   # 告警、Logs Insights 查询
  statemachines/scan_run.asl.json
  modules/
    region_cell/                  # SQS（scheduled、ondemand、DLQ）、ECS 集群与 worker 服务、自动扩缩、日志组
    lambda_fn/                    # 容器镜像 Lambda（x86_64）：函数、日志组、可选 VPC 配置
  envs/dev.tfvars  dev.backend.hcl  prod.tfvars  prod.backend.hcl
```

## 输入变量（主要）

```hcl
variable "env" {}
variable "component_versions" {}   # { broker, planner, api, worker }：各组件镜像 tag
variable "primary_region"      { default = "ap-east-1" }
variable "regions" {               # 扫描 region 及其网络
  type = map(object({
    vpc_id             = string
    cidr_block         = string
    private_subnet_ids = list(string)
    endpoints          = optional(map(string), {})   # 只写跨 region 调用的专用 DNS（R7）
  }))
}
variable "lambda_architecture" { default = "x86_64" }
variable "adfs_idp_url" {}
variable "account_directory_function_name" {}
variable "worker_min_count"    { default = 1 }
variable "worker_max_count"    { default = 20 }
variable "deleted_retention_days" { default = 180 }
```

## 中心资源（`main.tf`，ap-east-1）

| 资源 | 说明 |
|---|---|
| ECR 仓库 `aws-inventory-discover/{broker,planner,api,worker}` | tag 不可变；worker 仓库复制到其他扫描 region（各 region 的 ECS 从本地仓库拉取） |
| Aurora PostgreSQL 16 集群 + RDS Proxy | writer + 1 reader；Secrets Manager 托管主密码；安全组放行 4 个 VPC 的 CIDR |
| ElastiCache Redis（复制组） | 1 主 1 从，开启传输与静态加密，AUTH token 存 Secrets Manager |
| KMS key | broker 加密凭证用 |
| Secrets Manager `inventory/<env>/adfs` | 只创建 secret，值由人工写入，Terraform 忽略其值的变化 |
| Lambda `inventory-<env>-adfs-broker` | VPC 内；broker 镜像；环境变量见 R4 |
| Lambda `inventory-<env>-planner` | VPC 内；planner 镜像（入口在镜像 CMD 中） |
| Step Functions `inventory-scan-run` | Standard 类型；`templatefile("statemachines/scan_run.asl.json", {...})` |
| EventBridge Scheduler | `rate(4 hours)` → 状态机；`rate(15 minutes)` → planner `maintenance` |
| ECS 服务 `inventory-<env>-api` | Fargate X86_64；api 镜像；内部 ALB；可选 ADOT sidecar；健康检查 `/readyz` |
| `region_cell`（ap-east-1 自身） | 与其他 region 相同 |

各组件的环境变量由 Terraform 按组件生成（R4）。

## `region_cell` 模块

| 资源 | 说明 |
|---|---|
| SQS `inventory-<env>-scheduled` / `-ondemand` | 标准队列；可见性超时 900s；保留 4 天；各自一个 DLQ，`maxReceiveCount = 5` |
| ECS 集群与服务 `inventory-<env>-worker` | Fargate X86_64，0.5 vCPU / 1 GB；`stopTimeout = 120`；worker 镜像 |
| 自动扩缩 | step scaling：两个队列可见消息数之和；min `worker_min_count`，max `worker_max_count` |
| CloudWatch 日志组 | 保留 30 天 |

## 各 region VPC 需要的 interface endpoint（输入，不创建）

| 用途 | 服务 |
|---|---|
| ECS 运行 | `ecr.api`、`ecr.dkr`、`logs`、S3 gateway endpoint（镜像层） |
| worker 访问 | `sqs`（本 region）；ap-east-1 的 `lambda`（经 peering）；RDS Proxy（经 peering） |
| 采集 | `ec2`、`ecs`、S3 gateway endpoint；跨 region 刷新 S3 需要 S3 interface endpoint |
| 全局（仅 us-east-1） | `iam`、`route53` |
| 中心（仅 ap-east-1） | `sts`、`kms`、`secretsmanager`、`lambda`、`states` |

peering 相关要求：被跨 region 访问的 endpoint（R7 中列出的服务）安全组必须放行对端 VPC 的 CIDR，并在 tfvars 的 `regions.*.endpoints` 中写入其专用 DNS。

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

---

## 发布与配置（2026-10-28 修订）

> 状态：已实现。本节优先于上文与之冲突的内容及「实现修订」中的历史记录。

### R1. 发布方式

| 环节 | 谁做 | 内容 |
|---|---|---|
| 构建并推送镜像 | 你们的 workflow（调用本仓库 `deploy/` 的构建命令） | 4 个镜像推送到 **ap-east-1 的 ECR**；跨 region 复制由 ECR 复制规则完成 |
| 部署 | Terraform | Lambda、ECS task definition / 服务、环境变量、扩缩容，全部由 Terraform 管理 |
| 发布某个组件 | 改 tfvars 中该组件的版本 → 提 PR → 合并后打 tag → 自动触发 workflow 执行 `terraform apply` | 只改一个组件的版本，就只发布这一个组件 |
| 数据库结构 | Terraform 的 `migrate` 任务（发布前执行），另保留生成离线 DDL 的能力 | 见 R5 |

### R2. 组件、版本与镜像

| 组件 | 运行在 | ECR 仓库（`<namespace>/<name>`） | 基础镜像 |
|---|---|---|---|
| broker | Lambda（容器镜像） | `aws-inventory-discover/broker` | `public.ecr.aws/lambda/python:3.12` |
| planner | Lambda（容器镜像） | `aws-inventory-discover/planner` | 同上 |
| api | ECS Fargate | `aws-inventory-discover/api` | `python:3.12-slim` |
| worker | ECS Fargate（每个扫描 region） | `aws-inventory-discover/worker` | `python:3.12-slim` |

- 架构：全部 **x86_64**（Lambda `x86_64`，Fargate `X86_64`），构建只需要一个平台。
- 每个组件有自己的版本 `x.y.z`，镜像 tag = 版本；ECR 仓库 tag 不可变（同一版本不能重复推送）。
- 组件版本的来源（建议）：`deploy/versions.json`，如 `{"broker": "1.0.0", "planner": "1.0.0", "api": "1.0.0", "worker": "1.0.0"}`；构建脚本读取它作为 tag。组件代码有变化时在同一个 PR 中升版本。
- 依赖按组件拆成 `pyproject.toml` 的 optional extras（`broker`、`planner`、`api`、`worker`），公共依赖留在主依赖；仍然只有一个 `pyproject.toml`、一个包。
- `deploy/`：

```
deploy/
  Containerfile        # 4 个 target：broker、planner（Lambda 基础镜像）、api、worker（slim）
  build.sh             # build.sh <component> -> 本地镜像 aws-inventory-discover/<component>:<version>（linux/amd64）
  ddl.sh               # ddl.sh <from_rev> <to_rev> -> 离线 SQL（upgrade 与 downgrade）
  versions.json
  README.md            # 构建、推送、发布、迁移的步骤
```

推送由你们的 workflow 完成（`podman push` 到 ap-east-1 的仓库）。

### R3. Terraform 的变化

| 项目 | 变化 |
|---|---|
| 镜像版本 | `image_tag` 改为 `component_versions = { broker, planner, api, worker }`（tfvars 中维护），每个组件引用自己仓库与版本 |
| ECR | 创建 4 个仓库 `aws-inventory-discover/<name>`（tag 不可变、推送时扫描），复制规则把 `aws-inventory-discover/` 前缀的仓库复制到其他扫描 region（Lambda 与 API 只在 ap-east-1 使用，worker 在各 region 从本地仓库拉取） |
| Lambda | 仍为容器镜像；`architectures = ["x86_64"]` |
| ECS | `runtime_platform.cpu_architecture = "X86_64"`；`desired_count` / 扩缩容沿用 `api_desired_count`、`worker_min_count`、`worker_max_count` |
| `migrate` 任务 | 保留，使用 **worker 镜像**（见 R5） |
| region cell | 改为 `module "cell" { for_each = var.regions }`，模块内区域性资源设置 `region = var.region`（AWS provider 6.x 支持）；删除 4 个 provider 别名，新增 region 只改 tfvars |
| 环境变量 | 按组件生成（R4），不再给所有组件同一份 `common_env` |
| 变量 | `home_region` → `primary_region`；删除 `global_home_region`、`broker_sts_region`；`regions.*.endpoints` 只保留跨 region 用到的 endpoint 专用 DNS |
| 资源名 | 保留 env：`inventory-<env>-*`；broker 函数改名为 `inventory-<env>-adfs-broker` |

### R4. 配置模型

| 参数 | 含义 | 谁需要 |
|---|---|---|
| `INVENTORY_PRIMARY_REGION` | 控制平面所在 region（ap-east-1）：broker、planner、API、数据库、Redis | 全部 |
| `WORKER_REGION` | worker 所在 region | worker |
| `INVENTORY_SCAN_REGIONS` | 扫描哪些 region（原 `supported_regions`），必须包含 `us-east-1` | planner、API |
| `INVENTORY_ENDPOINTS` | **只含跨 region 调用**的 endpoint 专用 DNS：`{"<region>": {"<service>": "https://vpce-…"}}`，每个组件只给自己用到的条目（R7） | 有跨 region 调用的组件 |

推导规则（不再配置）：

- AWS 调用：`INVENTORY_ENDPOINTS` 中有 `(region, service)` 就用它作为 `endpoint_url`，否则 boto3 只给服务名和 region，用默认 DNS（同 region）。
- 队列：名字按约定 `inventory-<env>-scheduled|ondemand`，URL 用 `sqs.get_queue_url` 解析并缓存。
- broker 函数名：`inventory-<env>-adfs-broker`。
- global 资源（IAM、Route 53）的扫描 region、broker 调用 STS 的 region、`DescribeRegions` 的调用 region：代码常量 `us-east-1`。

各组件的环境变量（由 Terraform 生成，dev 示例）：

| 变量 | broker | planner | api | worker | 示例 / 来源 |
|---|:-:|:-:|:-:|:-:|---|
| `INVENTORY_ENVIRONMENT` | ✓ | ✓ | ✓ | ✓ | `dev` |
| `INVENTORY_PRIMARY_REGION` | ✓ | ✓ | ✓ | ✓ | `ap-east-1` |
| `WORKER_REGION` | | | | ✓ | `eu-west-1` |
| `INVENTORY_SCAN_REGIONS` | | ✓ | ✓ | | `["ap-east-1","eu-west-1","eu-west-2","us-east-1"]` |
| `INVENTORY_ENDPOINTS` | ✓ | ✓ | ✓ | ✓（primary 的 worker 不需要） | 见 R7 |
| `INVENTORY_DATABASE_SECRET_ARN`、`INVENTORY_DATABASE_HOST` | | ✓ | ✓ | ✓ | RDS 托管 secret、RDS Proxy |
| `INVENTORY_METRICS_MODE` | | | ✓ | ✓ | `api` |
| `BROKER_ADFS_IDP_URL`、`BROKER_ADFS_CREDENTIALS_SECRET_ARN`、`BROKER_REDIS_URL`、`BROKER_REDIS_AUTH_SECRET_ARN`、`BROKER_KMS_KEY_ID` | ✓ | | | | |
| `PLANNER_ACCOUNT_DIRECTORY_FUNCTION_NAME` | | ✓ | | | `aws-inventory-account-directory` |
| `PLANNER_DELETED_RETENTION_DAYS` | | ✓ | | | 默认 180（tfvars `deleted_retention_days`） |
| `API_TRACING_ENABLED` | | | ✓ | | 默认 false |

删除：`INVENTORY_CELLS`（`RegionCell`）、`INVENTORY_HOME_REGION`、`INVENTORY_SUPPORTED_REGIONS`、`INVENTORY_GLOBAL_HOME_REGION`、`INVENTORY_BROKER_FUNCTION_NAME`、`BROKER_STS_REGION`。Lambda 环境变量约 0.5–1 KB。

新增扫描 region：tfvars 的 `regions` 加一项（VPC、子网、跨 region endpoint）→ PR → apply。Terraform 创建该 region 的队列与 worker，并更新 planner / API 的 `INVENTORY_SCAN_REGIONS` 与 `INVENTORY_ENDPOINTS`。

### R5. 数据库结构

- Alembic 仍是来源：手写迁移、`upgrade` / `downgrade` 可往返的测试不变。
- 在线迁移：Terraform 保留 `migrate` 任务定义（`python -m alembic upgrade head`），**使用 worker 镜像**：新增资源类型时 worker 最先发布（R8），迁移随 worker 的新版本一起提供。发布含迁移的版本前，workflow 先用该镜像运行一次 `migrate` 任务。
- 离线 DDL：`deploy/ddl.sh <from> <to>` 用 `alembic upgrade <from>:<to> --sql`（及 downgrade）生成带版本号的 SQL，供需要人工审核或由 DBA 执行时使用。

### R6. 代码层面的简化

1. **不再判断 endpoint 是否存在**（基础设施由 infra 保证）：删除 `EndpointResolver.has_endpoint` / `reachable_from`、`Registry.for_region` 的跳过逻辑、`skipped_no_endpoint` 状态、`Scheduler.validate` 中的 endpoint 检查；`/v1/resource-types` 的 `enabled_regions` 改为：regional 类型为全部扫描 region，global 类型为 `["global"]`；账号级扫描总是生成 global task。
2. **单个 ARN 刷新一律走队列**：投递到资源所在 region 的 `ondemand` 队列（global 资源投到 us-east-1），由 worker 执行，`wait_seconds` 内轮询结果。删除 `queue=inline`、`InlineRefreshExecutor`、`refresh_inline_max`、`refresh_parallelism`、`refresh_thread_limit`、`refresh_timeout_seconds`。
3. **账号已启用的 region**：planner 与 API 直接调用 us-east-1 的 `ec2:DescribeRegions`，写入 `account_region`（超过 `account_region_max_age_hours`，默认 24 小时，才重新获取）；去掉"先试 primary、再逐个 region 回退"的逻辑。

### R7. 调用链与跨 region endpoint

访问方式：**同 region** = 默认域名解析到本 VPC 的 interface endpoint，不需要配置；**跨 region** = private DNS 不会经 peering 共享，必须用对端 interface endpoint 的专用 DNS，通过 `INVENTORY_ENDPOINTS` 配置；**内网** = 经 VPC / peering 直连。

| 组件（所在 region） | 调用 | 目标 region | 方式 |
|---|---|---|---|
| broker（primary） | Secrets Manager（ADFS 账号、Redis AUTH）、KMS | primary | 同 region |
| | Redis、ADFS | primary / 公司内网 | 内网 |
| | STS `AssumeRoleWithSAML` | us-east-1 | 跨 region |
| planner（primary） | Lambda：account-directory、broker | primary | 同 region |
| | EC2 `DescribeRegions`（目标账号凭证） | us-east-1 | 跨 region |
| | Secrets Manager（数据库密码）、RDS Proxy | primary | 同 region / 内网 |
| | SQS `GetQueueUrl`、`SendMessageBatch`（scheduled） | 每个扫描 region | primary 为同 region，其余跨 region |
| api（primary） | Secrets Manager、RDS Proxy、Lambda（broker）、CloudWatch | primary | 同 region / 内网 |
| | EC2 `DescribeRegions` | us-east-1 | 跨 region |
| | SQS（ondemand：扫描与单个 ARN 刷新） | 每个扫描 region | primary 为同 region，其余跨 region |
| worker（region R） | SQS（本 region 两个队列）、CloudWatch | R | 同 region |
| | 采集：EC2、ECS、S3；us-east-1 的 worker 另有 IAM、Route 53 | R | 同 region |
| | Lambda（broker）、Secrets Manager（数据库密码） | primary | 跨 region（R = primary 时同 region） |
| | RDS Proxy | primary | 内网（Proxy 域名本身解析为私有 IP） |

ECS 平台本身（ECR、CloudWatch Logs、S3 gateway endpoint）都在服务所在 region。

各组件的 `INVENTORY_ENDPOINTS`（dev，URL 省略）：

| 组件 | 内容 |
|---|---|
| broker | `{"us-east-1": {"sts": …}}` |
| planner、api | `{"eu-west-1": {"sqs": …}, "eu-west-2": {"sqs": …}, "us-east-1": {"sqs": …, "ec2": …}}` |
| worker（primary 以外） | `{"ap-east-1": {"lambda": …, "secretsmanager": …}}` |
| worker（primary） | 不需要 |

这些 endpoint 的安全组需放行调用方 VPC 的 CIDR（infra）。

### R8. 独立发布的兼容规则

每个组件单独升版本、单独发布，所以线上会**同时运行不同版本的组件**（例如 worker 1.3.0、API 1.2.0、planner 1.1.0）。以下规则保证新旧版本能一起工作：

1. **数据库结构先加后删**：迁移只做旧代码也能工作的变更（加表、加可空列、加索引、加分区）。要删除或重命名时分两次发布：先加新结构并让所有组件改用新结构，全部发布后再删旧结构。
2. **跨组件消息带版本**：SQS 消息（planner / API → worker）和 broker 的请求 / 响应都带 `version`。改格式时，接收方先发布一个同时接受新旧格式的版本，发送方再改发新格式。
3. **新增资源类型的发布顺序**：迁移（建分区）→ worker → planner / API。反过来，API 会把新类型投递给还不认识它的旧 worker。
4. **新增环境变量**：必须有默认值，或者先在 Terraform 中配置再发布使用它的代码。

### 决定记录（2026-10-28）

- 组件版本来源：`deploy/versions.json`。
- `migrate` 任务使用 worker 镜像。
