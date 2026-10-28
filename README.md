# aws-inventory-metadata-discovery

定时扫描 1000+ 个 AWS 账号、多个 region 的资源元数据，存入 PostgreSQL，并通过 HTTP API 提供查询、统计、按需扫描和单资源刷新。存储的数据始终以 AWS 的真实状态为准。

- 设计：[docs/design.md](docs/design.md)
- API 契约：[docs/api.md](docs/api.md)
- 分块实现规格：[docs/specs/](docs/specs/)
- 技术细节与编码规范（给开发者和 AI agent）：[CLAUDE.md](CLAUDE.md)（[AGENT.md](AGENT.md) 是它的链接）

## 架构概览

```
EventBridge Scheduler (4h) ─► Step Functions ─► planner λ ─► 各 region SQS ─► ECS worker ─► AWS API（目标账号）
                                                   │                              │
                       account-directory λ ◄───────┤                              ├─► broker λ ─► ADFS / STS（凭证，Redis 缓存）
                                                   │                              │
                                                   └──────► Aurora PostgreSQL ◄───┘
                                                                  ▲
                                      消费者 ─► FastAPI (ECS) ─────┘（查询、触发扫描、刷新）
```

- 控制面（调度、数据库、API、broker）在 primary region `ap-east-1`；`ap-east-1`、`eu-west-1`、`eu-west-2`、`us-east-1` 各有一个 worker cell，经 VPC peering 与中心互通。
- 四个组件（broker、planner、api、worker）各有自己的版本与镜像，可以单独发布（[spec 08](docs/specs/08-infrastructure.md)）。
- 每种资源类型是一个 collector 类，新增类型只需新增一个类、一个迁移和测试。

## 技术栈

Python 3.12 · uv · FastAPI · pydantic v2 · pydantic-settings · SQLAlchemy Core + psycopg 3 · Alembic · boto3 · AWS Lambda Powertools · Redis · pytest + moto · Terraform · Podman

## 仓库结构

```
pyproject.toml / uv.lock     唯一的项目配置与依赖锁
Makefile                     常用命令
compose.yaml                 本地依赖：PostgreSQL、Redis、moto server（podman compose）
deploy/                      镜像构建（Containerfile、build.sh、versions.json）与离线 DDL（ddl.sh）
web/                         Web UI（React + baseui + Vite），构建后由 API 在 /ui/ 提供
inventory/                   唯一的 Python 包
  config.py                  所有配置（pydantic-settings）
  errors.py                  错误类型
  observability.py           日志、上下文、脱敏、指标
  models.py                  共享数据模型与消息契约
  aws.py                     EndpointResolver、ClientFactory、BrokerLambdaProvider
  db.py                      表定义、Repository
  engine.py                  ScanEngine、Reconciler、TaskExecutor
  scheduling.py              scope 展开、task 创建与投递
  bootstrap.py               各入口共用的组装（settings、client、数据库、broker、引擎）
  transport.py               Dispatcher / ResultSink / RefreshExecutor 等接口及方案 A 实现
  collectors/                每个 AWS 服务一个文件
  broker/                    broker v2
  api/                       FastAPI 应用
  handlers/                  部署入口：broker_lambda、planner_lambda、worker
migrations/                  Alembic 迁移
tests/                       测试；测试数据全部在 tests/fixtures/
terraform/                   基础设施
docs/                        设计、API 契约、spec
```

## 环境准备

| 工具 | 版本 | 安装 |
|---|---|---|
| Python | 3.12 | uv 会自动安装 |
| uv | 最新 | `brew install uv` |
| Podman + podman compose | Podman ≥ 4.7 | `brew install podman`、`uv tool install podman-compose`，然后 `podman machine init && podman machine start` |
| Terraform | ≥ 1.10 | `brew install terraform` |

## 本地开发

> 以下命令在 M0（[spec 00](docs/specs/00-foundation.md)）完成后可用。

```bash
make setup        # uv sync，安装全部依赖
make up           # podman compose up -d：PostgreSQL、Redis（镜像来自 ECR Public，不需要 Docker Hub）
make moto         # 本机运行 moto server（端口 5000），本地跑 API/worker 时使用
make migrate      # alembic upgrade head
make queues       # 在 moto 中创建各 region 的扫描队列（按名字查找，与 AWS 一致）
make check        # ruff + mypy + 单元测试
make test-int     # 集成测试（需要 make up）
make cov          # 全部测试 + 覆盖率
make web          # 构建 Web UI 到 web/dist（需要 Node 22）
make web-dev      # Vite 开发服务器（:5173）
make images       # 构建 4 个组件镜像（linux/amd64），见下文「发布」
make ddl          # 生成全部迁移的离线 SQL 到 dist/ddl/
make down         # 停止本地依赖
```

本地配置：复制 `.env.example` 为 `.env`（pydantic-settings 会自动读取 `INVENTORY_*` 等配置），默认指向 compose 中的服务，`INVENTORY_AWS_ENDPOINT_URL` 指向 moto server。其中的 `AWS_*` 变量 boto3 不会从 `.env` 读取，需要先导出：`set -a; source .env; set +a`。

本地运行各组件（M3/M4 完成后）：

```bash
uv run uvicorn --factory inventory.api.app:create_app --reload --port 8080    # API
uv run python -m inventory.handlers.worker                   # worker（使用 .env 中的 WORKER_REGION）
```

Web UI（[spec 10](docs/specs/10-web-ui-and-auth.md)，baseui）：需要 Node 22。`make web` 构建到 `web/dist`，启动 API 时设置 `API_UI_DIR=web/dist`，浏览器打开 <http://localhost:8080/ui/>；改界面时用 `make web-dev`（Vite，端口 5173，`/v1` 代理到 8080）。本地没有 broker Lambda，扫描与刷新会以 broker 错误结束，UI 会显示错误码与 `request_id`。

认证默认关闭（`API_AUTH_MODE=disabled`，所有调用方都是 admin）。接入 SSO 后设置 `API_AUTH_MODE=enforced` 与 `API_JWT_JWKS_URL`、`API_JWT_ISSUER`、`API_JWT_AUDIENCE`；用户用 SSO 的 JWT，其他服务用 admin 在 UI 中创建的 API key（`X-API-Key`）。见 [api.md §1.5](docs/api.md)。

macOS 的"隔空播放接收器"默认占用 5000 端口：可在系统设置中关闭，或运行 `uv run moto_server -p 5001` 并把 `.env` 中的 `localhost:5000` 改为 `localhost:5001`。

## 开发流程

### 分块实现

工作按 spec 拆分，每个 spec 包含目标、范围、文件清单、接口、测试要求和验收标准。按依赖顺序实现：

| 里程碑 | Spec | 依赖 | 状态 |
|---|---|---|---|
| M0 基础 | [00-foundation](docs/specs/00-foundation.md) | — | 已完成 |
| M1 broker | [01-broker](docs/specs/01-broker.md) | M0 | 已完成 |
| M2 存储与框架 | [02-data-model](docs/specs/02-data-model.md)、[03-collector-framework](docs/specs/03-collector-framework.md)、[04-collectors](docs/specs/04-collectors.md)（EC2 三个类型） | M0 | 已完成 |
| M3 调度与执行 | [05-planner](docs/specs/05-planner.md)、[06-worker](docs/specs/06-worker.md) | M1、M2 | 已完成 |
| M4 API | [07-api-service](docs/specs/07-api-service.md)、[api.md](docs/api.md) | M2、M3 | 已完成 |
| M5 其余资源类型 | [04-collectors](docs/specs/04-collectors.md)（ECS、S3、IAM、Route 53） | M2 | 已完成 |
| M6 基础设施 | [08-infrastructure](docs/specs/08-infrastructure.md) | M0 | 已完成 |
| 预留 | [09-reserved-extensions](docs/specs/09-reserved-extensions.md)（方案 B、变更历史） | — | 不在 v1 |

M1 与 M2 可以并行；M5、M6 可以与 M3、M4 并行。

### 实现一个 spec 的步骤

1. 从 `main` 新建分支：`feat/<spec 编号>-<简短描述>`，例如 `feat/01-broker`。
2. 通读 spec 与 [CLAUDE.md](CLAUDE.md) 中的规范。
3. 先在 `tests/fixtures/` 中准备测试数据（JSON 等文件），再写测试，再实现代码。
4. 本地通过 `make check`；涉及数据库或 Redis 时同时通过 `make test-int`。
5. 实现与 spec / API 契约有出入时，**在同一个提交中**修改对应文档；完成后更新上表的"状态"列。
6. 提交 PR，描述中链接 spec，并逐条勾选该 spec 的验收标准。

### 新增一种资源类型

见 [spec 04 的检查清单](docs/specs/04-collectors.md#新增资源类型的检查清单)：collector 类、spec 规格表、分区迁移、endpoint 配置、fixture 与测试。

## 发布

四个组件各自有版本（`deploy/versions.json`），镜像为 ECR `aws-inventory-discover/<component>:<version>`，架构 x86_64。部署全部由 Terraform 完成。详见 [spec 08](docs/specs/08-infrastructure.md) 与 [deploy/README.md](deploy/README.md)。

### 发布一个组件

1. 组件代码有变化时，在同一个 PR 中升高 `deploy/versions.json` 中它的版本。
2. workflow 构建并推送镜像到 primary region（ap-east-1）的 ECR：

```bash
image=$(deploy/build.sh worker)          # 输出 aws-inventory-discover/worker:<version>
podman tag "$image" <account>.dkr.ecr.ap-east-1.amazonaws.com/$image
podman push <account>.dkr.ecr.ap-east-1.amazonaws.com/$image
```

3. 修改 `terraform/envs/<env>.tfvars` 中的 `component_versions` → 提 PR → 合并后打 tag → workflow 执行 `terraform apply`。只改了哪个组件的版本，就只发布哪个组件。
4. 版本包含新的迁移时，apply 之前先用新的 worker 镜像运行 `migrate` 任务（见 deploy/README.md）。

多个组件同时发布、或新增资源类型时，遵守 CLAUDE.md 的「独立发布的兼容规则」：迁移 → worker → planner / API。

### 首次部署

1. 先只创建 ECR 仓库与跨 region 复制（复制只对之后推送的镜像生效）：

```bash
terraform -chdir=terraform init -backend-config=envs/<env>.backend.hcl
terraform -chdir=terraform apply -var-file=envs/<env>.tfvars -target=aws_ecr_repository.component -target=aws_ecr_replication_configuration.inventory
```

2. 构建并推送 4 个组件的镜像（同上）。
3. 部署其余资源，定时任务先保持关闭：`terraform -chdir=terraform apply -var-file=envs/<env>.tfvars -var schedules_enabled=false`。
4. 写入 ADFS 账号的 secret（输出 `adfs_secret_arn`），内容为 `{"username": "...", "password": "..."}`。
5. 运行 `migrate` 任务，然后用 `schedules_enabled=true`（默认值）再 apply 一次，开启定时扫描与维护任务。

校验 Terraform（不需要 AWS 凭证）：

```bash
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
```

ECR 跨 region 复制是账号级的单例配置：同一账号部署多个环境时，只在其中一个 stack 中保留 `manage_ecr_replication = true`。

## 排查问题

每个 API 响应都带 `X-Request-ID`。在 CloudWatch Logs Insights 中使用保存的查询"按 request_id 追踪"，可以看到这次调用在 API、worker、broker 中的全部日志。定时任务用 `job_id` 追踪。
