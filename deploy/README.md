# deploy/

Build commands for the release workflow. Deployment itself is done by Terraform
(`terraform/`); the workflow only builds images, pushes them to ECR in the primary region
(ap-east-1) and runs `terraform apply` after a version change is merged and tagged.

| 文件 | 用途 |
|---|---|
| `versions.json` | 每个组件的版本 `x.y.z`，即镜像 tag |
| `Containerfile` | 4 个 target：`broker`、`planner`（Lambda 基础镜像）、`api`、`worker`（python slim） |
| `build.sh` | `deploy/build.sh <component>`：构建 linux/amd64 镜像，输出 `aws-inventory-discover/<component>:<version>` |
| `ddl.sh` | `deploy/ddl.sh <from> <to>`：生成离线 SQL（upgrade 与 downgrade）到 `dist/ddl/` |

## 组件

| 组件 | 运行在 | ECR 仓库 | 入口 |
|---|---|---|---|
| broker | Lambda（镜像，x86_64） | `aws-inventory-discover/broker` | `inventory.handlers.broker_lambda.handler` |
| planner | Lambda（镜像，x86_64） | `aws-inventory-discover/planner` | `inventory.handlers.planner_lambda.handler` |
| api | ECS Fargate（X86_64） | `aws-inventory-discover/api` | uvicorn，端口 8080，健康检查 `/readyz` |
| worker | ECS Fargate（X86_64），每个扫描 region | `aws-inventory-discover/worker` | `python -m inventory.handlers.worker`；同一镜像也运行 `migrate` 任务 |

环境变量由 Terraform 按组件生成（`terraform/main.tf` 中的 `*_env`），见 spec 08 R4。

## 发布

1. 组件代码有变化时，在同一个 PR 中升高 `versions.json` 中它的版本（ECR 的 tag 不可变，同一版本不能重复推送）。
2. 构建并推送：

```bash
image=$(deploy/build.sh api)
podman tag "$image" <account>.dkr.ecr.ap-east-1.amazonaws.com/$image
podman push <account>.dkr.ecr.ap-east-1.amazonaws.com/$image
```

worker 镜像由 ECR 复制规则复制到其他扫描 region。

3. 修改 `terraform/envs/<env>.tfvars` 的 `component_versions` → PR → 合并后打 tag → `terraform apply`。

## 数据库迁移

版本中包含新的迁移时，在 apply 之前用新的 worker 镜像运行一次 `migrate` 任务：

```bash
terraform -chdir=terraform apply -var-file=envs/<env>.tfvars -target=aws_ecs_task_definition.migrate
aws ecs run-task --cluster inventory-<env>-api --launch-type FARGATE \
  --task-definition inventory-<env>-migrate \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-...],securityGroups=[sg-...]}'
```

子网与安全组见 Terraform 输出 `api_subnets_and_security_group`；等任务退出码为 0 再继续。

需要人工审核或由 DBA 执行时，用 `deploy/ddl.sh <当前版本> head` 生成离线 SQL。

迁移必须向后兼容（先加后删），见 CLAUDE.md「独立发布的兼容规则」。
