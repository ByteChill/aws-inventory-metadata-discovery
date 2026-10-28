// The few attributes people look for first, per resource type. Paths point into the raw
// AWS data the collectors store (e.g. EC2 DescribeInstances), see spec 04.

type Kind = 'text' | 'date' | 'bool' | 'count' | 'list' | 'last-segment';

export interface Attribute {
  label: [en: string, zh: string];
  path: string;
  kind?: Kind;
}

const A = (en: string, zh: string, path: string, kind: Kind = 'text'): Attribute => ({ label: [en, zh], path, kind });

export const KEY_ATTRIBUTES: Record<string, Attribute[]> = {
  'AWS::EC2::Instance': [
    A('Instance type', '实例类型', 'InstanceType'),
    A('State', '运行状态', 'State.Name'),
    A('Private IP', '私有 IP', 'PrivateIpAddress'),
    A('Public IP', '公有 IP', 'PublicIpAddress'),
    A('VPC', 'VPC', 'VpcId'),
    A('Subnet', '子网', 'SubnetId'),
    A('Platform', '平台', 'PlatformDetails'),
    A('Launched', '启动时间', 'LaunchTime', 'date'),
  ],
  'AWS::EC2::VPC': [
    A('CIDR', 'CIDR', 'CidrBlock'),
    A('State', '状态', 'State'),
    A('Default VPC', '默认 VPC', 'IsDefault', 'bool'),
  ],
  'AWS::EC2::Subnet': [
    A('CIDR', 'CIDR', 'CidrBlock'),
    A('Availability zone', '可用区', 'AvailabilityZone'),
    A('VPC', 'VPC', 'VpcId'),
    A('Free IP addresses', '可用 IP 数', 'AvailableIpAddressCount'),
    A('Public IP on launch', '自动分配公有 IP', 'MapPublicIpOnLaunch', 'bool'),
  ],
  'AWS::EC2::SecurityGroup': [
    A('Group name', '组名', 'GroupName'),
    A('Description', '描述', 'Description'),
    A('VPC', 'VPC', 'VpcId'),
    A('Inbound rules', '入站规则', 'IpPermissions', 'count'),
    A('Outbound rules', '出站规则', 'IpPermissionsEgress', 'count'),
  ],
  'AWS::EC2::Image': [
    A('Image name', '镜像名', 'Name'),
    A('State', '状态', 'State'),
    A('Architecture', '架构', 'Architecture'),
    A('Platform', '平台', 'PlatformDetails'),
    A('Public', '公开', 'Public', 'bool'),
    A('Created', '创建时间', 'CreationDate', 'date'),
  ],
  'AWS::ECS::Cluster': [
    A('Status', '状态', 'status'),
    A('Services', '服务数', 'activeServicesCount'),
    A('Running tasks', '运行中任务', 'runningTasksCount'),
    A('Container instances', '容器实例', 'registeredContainerInstancesCount'),
  ],
  'AWS::ECS::Service': [
    A('Status', '状态', 'status'),
    A('Launch type', '启动类型', 'launchType'),
    A('Desired tasks', '期望任务数', 'desiredCount'),
    A('Running tasks', '运行中任务', 'runningCount'),
    A('Task definition', '任务定义', 'taskDefinition', 'last-segment'),
    A('Created', '创建时间', 'createdAt', 'date'),
  ],
  'AWS::ECS::TaskDefinition': [
    A('Family', '系列', 'family'),
    A('Revision', '版本', 'revision'),
    A('CPU', 'CPU', 'cpu'),
    A('Memory', '内存', 'memory'),
    A('Network mode', '网络模式', 'networkMode'),
    A('Runs on', '运行环境', 'requiresCompatibilities', 'list'),
  ],
  'AWS::S3::Bucket': [
    A('Created', '创建时间', 'Bucket.CreationDate', 'date'),
    A('Versioning', '版本控制', 'Versioning.Status'),
    A('Encryption', '加密', 'Encryption.ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm'),
    A('Block public access', '阻止公共访问', 'PublicAccessBlock.PublicAccessBlockConfiguration.BlockPublicPolicy', 'bool'),
  ],
  'AWS::IAM::Role': [
    A('Role name', '角色名', 'RoleName'),
    A('Path', '路径', 'Path'),
    A('Created', '创建时间', 'CreateDate', 'date'),
    A('Attached policies', '附加的策略', 'AttachedManagedPolicies', 'count'),
    A('Inline policies', '内联策略', 'RolePolicyList', 'count'),
  ],
  'AWS::IAM::User': [
    A('User name', '用户名', 'UserName'),
    A('Created', '创建时间', 'CreateDate', 'date'),
    A('Groups', '所属组', 'GroupList', 'list'),
    A('Attached policies', '附加的策略', 'AttachedManagedPolicies', 'count'),
  ],
  'AWS::IAM::ManagedPolicy': [
    A('Policy name', '策略名', 'PolicyName'),
    A('Attachable', '可附加', 'IsAttachable', 'bool'),
    A('Default version', '默认版本', 'DefaultVersionId'),
    A('Updated', '更新时间', 'UpdateDate', 'date'),
  ],
  'AWS::Route53::HostedZone': [
    A('Domain', '域名', 'HostedZone.Name'),
    A('Private zone', '私有区域', 'HostedZone.Config.PrivateZone', 'bool'),
    A('Records', '记录数', 'HostedZone.ResourceRecordSetCount'),
    A('Associated VPCs', '关联的 VPC', 'VPCs', 'count'),
  ],
};

/** Reads "A.B[0].C" from raw data; undefined when any step is missing. */
export function readPath(data: unknown, path: string): unknown {
  let current: unknown = data;
  for (const part of path.split('.')) {
    const match = /^([^[]+)(?:\[(\d+)\])?$/.exec(part);
    if (!match || current === null || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[match[1]];
    if (match[2] !== undefined) current = Array.isArray(current) ? current[Number(match[2])] : undefined;
  }
  return current;
}
