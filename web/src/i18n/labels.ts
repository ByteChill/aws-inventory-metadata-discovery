import type { Lang } from './index';

// Names people use instead of CloudFormation types, status and error codes. Region codes
// (ap-east-1) stay as they are: that is how the teams refer to regions.

type Pair = [en: string, zh: string];
const pick = ([en, zh]: Pair, lang: Lang) => (lang === 'zh' ? zh : en);

const TYPES: Record<string, Pair> = {
  'AWS::EC2::Instance': ['EC2 instance', 'EC2 实例'],
  'AWS::EC2::VPC': ['VPC', 'VPC'],
  'AWS::EC2::Subnet': ['Subnet', '子网'],
  'AWS::EC2::SecurityGroup': ['Security group', '安全组'],
  'AWS::EC2::Image': ['AMI', 'AMI 镜像'],
  'AWS::ECS::Cluster': ['ECS cluster', 'ECS 集群'],
  'AWS::ECS::Service': ['ECS service', 'ECS 服务'],
  'AWS::ECS::TaskDefinition': ['ECS task definition', 'ECS 任务定义'],
  'AWS::S3::Bucket': ['S3 bucket', 'S3 存储桶'],
  'AWS::IAM::Role': ['IAM role', 'IAM 角色'],
  'AWS::IAM::User': ['IAM user', 'IAM 用户'],
  'AWS::IAM::ManagedPolicy': ['IAM policy', 'IAM 策略'],
  'AWS::Route53::HostedZone': ['Hosted zone', 'Route 53 托管区域'],
};

export function typeLabel(resourceType: string, lang: Lang): string {
  const known = TYPES[resourceType];
  return known ? pick(known, lang) : resourceType.replace(/^AWS::/, '').replace('::', ' ');
}

/** "EC2", "S3", ... for grouping and the coloured service badge. */
export function serviceOf(resourceType: string): string {
  return resourceType.split('::')[1] ?? resourceType;
}

const STATUSES: Record<string, Pair> = {
  succeeded: ['Succeeded', '成功'],
  partial: ['Partly failed', '部分失败'],
  failed: ['Failed', '失败'],
  queued: ['Queued', '排队中'],
  running: ['Running', '进行中'],
  retrying: ['Retrying', '重试中'],
  timed_out: ['Timed out', '超时'],
  auth_denied: ['No access', '无访问权限'],
  never: ['Never scanned', '尚未扫描'],
  created: ['Added', '新增'],
  updated: ['Updated', '已更新'],
  unchanged: ['No change', '无变化'],
  deleted: ['Deleted', '已删除'],
  not_found: ['Not found', '不存在'],
  pending: ['In progress', '处理中'],
  error: ['Error', '出错'],
  unsupported_type: ['Not supported', '不支持'],
  revoked: ['Revoked', '已撤销'],
  expired: ['Expired', '已过期'],
  active: ['Active', '有效'],
};

export function statusLabel(status: string, lang: Lang): string {
  const known = STATUSES[status];
  return known ? pick(known, lang) : status.replace(/_/g, ' ');
}

const ERRORS: Record<string, Pair> = {
  DISPATCH_FAILED: ["Could not hand the work to the region's scanner", '未能发送给该区域的扫描程序'],
  THROTTLED: ['AWS asked us to slow down; it will be retried', 'AWS 限流，稍后会自动重试'],
  UPSTREAM_THROTTLED: ['AWS asked us to slow down; try again shortly', 'AWS 限流，请稍后再试'],
  API_ACCESS_DENIED: ['No permission to read this in the account', '没有读取该账号此类资源的权限'],
  ACCOUNT_ACCESS_DENIED: ['No access to this account', '没有该账号的访问权限'],
  REGION_NOT_ENABLED: ['The account has not enabled this region', '该账号未启用这个区域'],
  UPSTREAM_FAILED: ['A resource type it depends on could not be scanned', '它依赖的资源类型扫描失败'],
  TIMED_OUT: ['Took too long and was stopped', '耗时过长，已停止'],
  NETWORK_ERROR: ['Could not reach AWS', '无法连接 AWS'],
  AWS_SERVER_ERROR: ['AWS returned an error; it will be retried', 'AWS 服务出错，稍后会重试'],
  UPSTREAM_ERROR: ['A service we depend on failed', '依赖的服务出错'],
  BROKER_FUNCTION_ERROR: ['Could not obtain credentials for the account', '无法获取该账号的凭证'],
  INTERNAL_ERROR: ['Something went wrong on our side', '系统内部错误'],
  VALIDATION_ERROR: ['Please check the input', '请检查输入'],
  BAD_REQUEST: ['The request was not understood', '请求无法处理'],
  UNAUTHENTICATED: ['Please sign in again', '请重新登录'],
  FORBIDDEN: ['You do not have permission for this', '你没有执行此操作的权限'],
  NOT_FOUND: ['Not found', '未找到'],
  ACCOUNT_UNRESOLVED: ['Unknown account: scan the account first', '找不到所属账号，请先扫描该账号'],
  ACCOUNT_AMBIGUOUS: ['The resource matches several accounts', '该资源匹配到多个账号'],
};

/** A sentence for people; the raw code stays available in technical details. */
export function errorText(code: string | null | undefined, lang: Lang): string {
  if (!code) return '';
  const known = ERRORS[code] ?? (code.startsWith('AWS_') ? (['AWS rejected the request', 'AWS 拒绝了请求'] as Pair) : undefined);
  return known ? pick(known, lang) : pick(['Something went wrong', '出错了'], lang);
}
