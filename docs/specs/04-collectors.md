# Spec 04：资源类型采集规格（M2 / M5）

## 目标

逐个定义 v1 资源类型的采集方式。每个类型对应一个 collector 类，遵守 spec 03 的规则。

- M2 先实现：`AWS::EC2::VPC`、`AWS::EC2::Instance`、`AWS::EC2::Image`（用于验证框架）。
- M5 实现其余类型。

## 通用约定

- 文件：一个 AWS 服务一个文件，`inventory/collectors/<service>.py`。
- `data` 保存 describe 原始返回中对应资源的那个对象（不含外层包装），去掉 `drop_fields`。
- `tags` 统一为 `{key: value}`。
- 时间字段保持 boto3 返回的 datetime，存入数据库时由 `canonical_json` 转成 ISO 8601。
- ARN 中缺少账号或 region 的类型（标记为 ⚠️），刷新时由调用方通过 `find_account` 补全，见 spec 07。
- 每个类型在测试中使用：`tests/fixtures/collectors/<service>/<type>.seed.json`（通过 moto 建立数据）与 `<type>.expected.json`（期望的 `ResourceRecord` 列表，时间已冻结）。

## 新增资源类型的检查清单

1. 在对应服务文件中新增 collector 类并 `@register`。
2. 在本文件中补充该类型的规格表。
3. 新增 Alembic 迁移，创建分区（spec 02 §6）。
4. 确认所需 endpoint；在 Terraform 中为各 region 的 cell 配置 `endpoints`（spec 08）。
5. 新增 seed / expected fixture 与测试：list、get（存在 / 不存在）、ARN 解析、list 与 get 结构一致。

---

## EC2（`collectors/ec2.py`）

以下前四个类型均继承 `Ec2DescribeCollector`。

### `AWS::EC2::VPC`

| 项 | 值 |
|---|---|
| list / get | `describe_vpcs`（分页）/ `describe_vpcs(VpcIds=[id])` |
| resource_id | `VpcId` |
| ARN | `arn:aws:ec2:{region}:{account}:vpc/{id}` |
| relations | `{}` |
| not found | `InvalidVpcID.NotFound` |

### `AWS::EC2::Subnet`

| 项 | 值 |
|---|---|
| list / get | `describe_subnets` / `SubnetIds=[id]` |
| resource_id | `SubnetId` |
| ARN | 使用返回中的 `SubnetArn` |
| relations | `{"vpc_id": VpcId}` |
| not found | `InvalidSubnetID.NotFound` |

### `AWS::EC2::SecurityGroup`

| 项 | 值 |
|---|---|
| list / get | `describe_security_groups` / `GroupIds=[id]` |
| resource_id | `GroupId` |
| ARN | `arn:aws:ec2:{region}:{account}:security-group/{id}` |
| name | `GroupName`（覆盖默认的 tags["Name"]） |
| relations | `{"vpc_id": VpcId}` |
| not found | `InvalidGroup.NotFound`、`InvalidGroupId.NotFound` |

### `AWS::EC2::Instance`

| 项 | 值 |
|---|---|
| list / get | `describe_instances`，展开 `Reservations[].Instances[]` / `InstanceIds=[id]` |
| resource_id | `InstanceId` |
| ARN | `arn:aws:ec2:{region}:{account}:instance/{id}` |
| relations | `{"vpc_id", "subnet_id", "security_group_ids": [...], "image_id"}` |
| 过滤 | `State.Name == "terminated"` 视为不存在 |
| not found | `InvalidInstanceID.NotFound` |

### `AWS::EC2::Image` ⚠️ ARN 无账号

| 项 | 值 |
|---|---|
| list / get | `describe_images(Owners=["self"])` / `ImageIds=[id], Owners=["self"]` |
| resource_id | `ImageId` |
| ARN | `arn:aws:ec2:{region}::image/{id}` |
| relations | `{"snapshot_ids": [BlockDeviceMappings[].Ebs.SnapshotId]}` |
| 注意 | **必须**带 `Owners=["self"]`，否则会返回所有公共 AMI |
| not found | 返回空列表，或 `InvalidAMIID.NotFound` / `InvalidAMIID.Unavailable` |

---

## ECS（`collectors/ecs.py`）

### `AWS::ECS::Cluster`

| 项 | 值 |
|---|---|
| list | `list_clusters`（分页）→ `describe_clusters(clusters=[...≤100], include=["TAGS","SETTINGS","CONFIGURATIONS"])` |
| get | `describe_clusters(clusters=[arn])` |
| resource_id | `clusterName` |
| ARN | `clusterArn` |
| tags | `tags`，元素为 `{"key","value"}`（小写） |
| 过滤 | `status == "INACTIVE"` 视为不存在 |
| not found | `failures[].reason == "MISSING"` |

### `AWS::ECS::Service`

| 项 | 值 |
|---|---|
| depends_on | `AWS::ECS::Cluster` |
| list | 对上游每个 cluster：`list_services(cluster, maxResults=100)` → `describe_services(cluster, services=[...≤10], include=["TAGS"])`；不同 cluster 之间用 `ctx.map` 并发 |
| get | 由 ARN 解析出 cluster 与 service，`describe_services` |
| resource_id | `{clusterName}/{serviceName}` |
| ARN | `serviceArn`。兼容旧格式 `arn:aws:ecs:{r}:{a}:service/{name}`（不含 cluster）：解析时 cluster 未知，由调用方用 `find_account` 按 `serviceName` 后缀查库补全 |
| drop_fields | `events`（滚动事件日志，变化频繁且无业务价值） |
| relations | `{"cluster_arn", "task_definition_arns": [...]}`，见下 |
| 过滤 | `status == "INACTIVE"` 视为不存在 |
| not found | `failures[].reason == "MISSING"` |

`task_definition_arns`（在用的 revision，去重后排序）：
- 部署控制器为 `ECS`（默认）：`deployments[].taskDefinition`（包括 PRIMARY 与 ACTIVE，滚动发布期间新旧版本都在运行）。
- 部署控制器为 `CODE_DEPLOY` / `EXTERNAL`：`taskSets[].taskDefinition`。
- 两者都为空时退回 `taskDefinition`。

### `AWS::ECS::TaskDefinition`（只采集在用的 revision）

| 项 | 值 |
|---|---|
| depends_on | `AWS::ECS::Service` |
| list | 收集上游所有 service 的 `relations.task_definition_arns` 并去重；对每个 ARN：若 `not ctx.full_refresh` 且 `family:revision` 在 `ctx.known_ids` 中 → `KeepExisting`；否则 `describe_task_definition(taskDefinition=arn, include=["TAGS"])` |
| get | `describe_task_definition(taskDefinition=arn, include=["TAGS"])` |
| data | `taskDefinition` 对象，外加 `tags` 字段（来自响应顶层） |
| resource_id | `{family}:{revision}` |
| ARN | `taskDefinitionArn` |
| relations | `{"family"}` |
| not found | `ClientException` 且消息表明不存在 |

说明：
- revision 一旦发布内容就不会再变，所以已知的 revision 不再 describe。tags 可能被单独修改，由每天一次的 `full_refresh` 兜底。
- 某个 revision 不再被任何 service 引用时会被软删除。对这个类型来说，"删除"表示"不再使用"。
- "被哪些 service 使用"不存在 task definition 上（否则 `KeepExisting` 时这个关系无法更新），通过查询 service 的 `relations.task_definition_arns` 反查。
- 单 ARN 刷新只 describe 该 revision，不检查它是否仍在使用。

---

## S3（`collectors/s3.py`）

### `AWS::S3::Bucket` ⚠️ ARN 无账号与 region

| 项 | 值 |
|---|---|
| 区域性 | 按 bucket 所在 region 归属（**不是**全局类型） |
| list | `list_buckets(BucketRegion=ctx.region)`（分页），对每个 bucket 用 `ctx.map` 并发获取详情 |
| 详情 | `get_bucket_tagging`（`NoSuchTagSet` → 无 tags）、`get_bucket_versioning`、`get_bucket_encryption`（`ServerSideEncryptionConfigurationNotFoundError` → `null`）、`get_public_access_block`（`NoSuchPublicAccessBlockConfiguration` → `null`） |
| data | `{"Bucket": <list_buckets 中的对象>, "Versioning": ..., "Encryption": ..., "PublicAccessBlock": ...}` |
| get | `head_bucket` 不存在 → None；否则组装与 list 相同的结构（共用拼装函数） |
| resource_id | bucket 名 |
| ARN | `arn:aws:s3:::{name}` |
| not found | `NoSuchBucket`、`head_bucket` 404 |

待验证：moto 是否支持 `ListBuckets` 的 `BucketRegion` 参数。不支持时测试中用 `get_bucket_location` 过滤作为替代实现的分支，并在 collector 中保留该分支（由配置开关切换）。

跨 region 刷新：S3 默认通过 gateway endpoint 访问，gateway endpoint 不能跨 region。API（ap-east-1）刷新其他 region 的 bucket 时，需要该 region 配置了 S3 interface endpoint；否则走异步队列（spec 07 §5）。

---

## IAM（`collectors/iam.py`，全局类型）

三个类型共享一次 `get_account_authorization_details`（分页，`Filter=["Role","User","LocalManagedPolicy"]`），通过 `ctx.memo("iam_authz", ...)` 在同一 task 内只调用一次。`required_endpoints = ("iam",)`。

### `AWS::IAM::Role`

| 项 | 值 |
|---|---|
| list | `RoleDetailList[]` |
| get | `get_role` + `list_role_policies`/`get_role_policy` + `list_attached_role_policies` + `list_instance_profiles_for_role`，拼装成与 `RoleDetail` **相同的结构** |
| resource_id | `RoleName` |
| ARN | `Arn` |
| tags | `Tags` |
| relations | `{"attached_policy_arns": [...], "instance_profile_arns": [...]}` |
| not found | `NoSuchEntity` |

### `AWS::IAM::User`

与 Role 相同的模式：list 用 `UserDetailList[]`；get 用 `get_user` + 内联策略 + 附加策略 + `list_groups_for_user` 拼装。relations `{"attached_policy_arns", "group_names"}`。

### `AWS::IAM::ManagedPolicy`（仅客户托管策略）

| 项 | 值 |
|---|---|
| list | `Policies[]`；`PolicyVersionList` 只保留 `IsDefaultVersion == true` 的版本 |
| tags | 每个策略调用 `list_policy_tags`（`ctx.map` 并发） |
| get | `get_policy` + `get_policy_version(默认版本)` + `list_policy_tags`，拼成相同结构 |
| resource_id | `PolicyName` |
| ARN | `Arn` |
| not found | `NoSuchEntity` |

---

## Route 53（`collectors/route53.py`，全局类型）

### `AWS::Route53::HostedZone` ⚠️ ARN 无账号

| 项 | 值 |
|---|---|
| list | `list_hosted_zones`（分页）；tags 用 `list_tags_for_resources(ResourceType="hostedzone", ResourceIds=[≤10])` 批量获取；私有 zone 额外调用 `get_hosted_zone` 取 `VPCs` |
| data | `{"HostedZone": ..., "VPCs": [...]}`（公有 zone 的 `VPCs` 为空列表） |
| get | `get_hosted_zone` + `list_tags_for_resource`，拼成相同结构 |
| resource_id | zone id，去掉 `/hostedzone/` 前缀 |
| ARN | `arn:aws:route53:::hostedzone/{id}` |
| relations | `{"vpc_ids": [...]}` |
| not found | `NoSuchHostedZone` |
| 范围 | v1 不采集 record set |

`required_endpoints = ("route53",)`。

## 验收标准

- 每个类型都有 list、get（存在 / 不存在）、ARN 解析、"list 与 get 结构一致"四类测试，全部通过。
- M2 完成时 EC2 三个类型可用；M5 完成时本文件所有类型可用。

## 实现修订（2026-10-27）

- EC2 测试数据：一个 `collectors/ec2/seed.json` 描述所有 EC2 种子资源（含 `missing_ids`）；`<type>.expected.json` 保存稳定投影（name、tags、relation 键），因为 moto 生成的 id 是随机的；具体 relation 值、ARN 往返与 list/get 一致性由单独的测试覆盖。
- `AWS::EC2::Image` 的 `drop_fields = ("LastLaunchedTime",)`（每次启动都会变化）。

### M5 实现修订

- 每个服务一个 `collectors/<service>/seed.json`，类型级期望为 `<type>.expected.json`（稳定投影）；测试通用三项（list 投影、get 与 list 哈希一致及 ARN 往返、不存在返回 None）对所有类型参数化执行，另有各类型的专项测试。
- ECS：`ServiceCollector.parse_arn` 同时支持新旧两种 ARN，旧格式返回 `ParsedArn(id_is_suffix=True)`，由 API 按 `resource_id` 后缀在库中补全 cluster。TaskDefinition 的 `get_resource` 遇到 `ClientException` 视为不存在。
- S3：`data` 为 `{"Bucket", "Tags", "Versioning", "Encryption", "PublicAccessBlock"}`（`Tags` 放在 data 中，供通用 tags 提取）；`get_resource` 用 `head_bucket` 判断存在，再用 `ListBuckets(BucketRegion, Prefix)` 取得与列表相同的 `Bucket` 对象。moto 支持 `BucketRegion` 与 `Prefix`。
- IAM：list 与 get 都通过 `role_view` / `user_view` / `policy_view` 拼成同一结构；去掉易变或只在一侧出现的字段（`RoleLastUsed`、`MaxSessionDuration`、`Description`、`PolicyId`、`PermissionsBoundaryUsageCount`）。`PolicyName` 取自 ARN 最后一段。AWS 托管策略的 ARN（账号为 `aws`）不匹配，刷新时为 `unsupported_type`。
- Route 53：`data` 为 `{"HostedZone", "VPCs", "Tags"}`，公有 zone 的 `VPCs` 为空列表；name 去掉末尾的 `.`。

### 第二轮修订（M5 审查后）

- 去掉会导致每次扫描都"变化"的运行时计数：ECS Service（`events`、`runningCount`、`pendingCount`，以及 deployments / taskSets 中的计数、`updatedAt`、rollout 状态等，见 `normalize_service`）、ECS Cluster（任务 / 服务 / 容器实例计数与 `statistics`）、Route 53（`ResourceRecordSetCount`）、IAM 托管策略（`AttachmentCount`）。
- ECS 的 `failures[]` 中只有 `MISSING` 表示不存在，其他原因让整个列表失败（不会把资源当成已删除）。在用的 task definition 在列表路径中 describe 失败也会让列表失败；`get_resource` 只把消息包含 "Unable to describe" 的 `ClientException` 视为不存在。service 刷新时 `ClusterNotFoundException` 视为不存在。`INACTIVE` 的 deployment 不计入在用 revision。
- S3：存在性只看 `ListBuckets(BucketRegion, Prefix)`（只返回本账号、本 region 的 bucket），不再用 `HeadBucket`；`ListBuckets` 使用 `PageSize=1000`；列表与读详情之间被删除的 bucket 跳过。bucket ARN 也接受 us-east-1 的旧式命名（大写、`_`）。
- Route 53 按顺序调用（账号级 5 次/秒限流），私有 zone 的 `get_hosted_zone` 每个 zone 只调用一次。
- 已知限制：ARN 只匹配 `aws` 分区（不含 `aws-cn` / `aws-us-gov`）。
