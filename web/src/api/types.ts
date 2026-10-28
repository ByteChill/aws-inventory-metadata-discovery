// Mirrors inventory/api/schemas.py (docs/api.md).

export type Permission = 'resources:read' | 'scans:read' | 'scans:write' | 'api_keys:manage';
export type RoleName = 'viewer' | 'operator' | 'admin';
export const ROLES: RoleName[] = ['viewer', 'operator', 'admin'];

export interface Me {
  subject: string;
  kind: 'user' | 'service' | 'anonymous';
  roles: RoleName[];
  permissions: Permission[];
  auth_mode: 'disabled' | 'enforced';
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total: number | null;
}

export interface Resource {
  resource_type: string;
  account_id: string;
  region: string;
  resource_id: string;
  arn: string;
  name: string | null;
  tags: Record<string, string>;
  relations: Record<string, unknown>;
  data: Record<string, unknown> | null;
  first_seen_at: string;
  last_changed_at: string;
  observed_at: string;
  deleted_at: string | null;
  verified_at: string | null;
  account_name: string | null;
  eim: string | null;
  account_type: string | null;
}

export const ACCOUNT_TYPES = ['dev', 'preprod', 'prod'] as const;

/** An account as account-directory lists it; an EIM owns accounts (and so their resources). */
export interface Account {
  account_id: string;
  name: string | null;
  eim: string | null;
  account_type: string | null;
  status: string;
  listed_at: string;
}

export interface StatsResponse {
  groups: Array<Record<string, string | number>>;
  total: number;
}

export interface ResourceType {
  resource_type: string;
  service: string;
  is_global: boolean;
  depends_on: string[];
  enabled_regions: string[];
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'timed_out';
export const JOB_STATUSES: JobStatus[] = [
  'queued',
  'running',
  'succeeded',
  'partial',
  'failed',
  'timed_out',
];
export const TERMINAL_JOB: ReadonlySet<string> = new Set(['succeeded', 'partial', 'failed', 'timed_out']);

export interface ScanScope {
  account_id?: string;
  region?: string;
  resource_type?: string;
  arn?: string;
  arns?: string[];
  all?: boolean;
}

export interface ScanTask {
  task_id: string;
  account_id: string;
  region: string;
  resource_types: string[] | null;
  status: string;
  attempts: number;
  result: Record<string, TypeResult>;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface TypeResult {
  status: string;
  created?: number;
  updated?: number;
  deleted?: number;
  unchanged?: number;
  error_code?: string | null;
  error_message?: string | null;
  duration_ms?: number;
}

export interface ScanJob {
  job_id: string;
  trigger: 'schedule' | 'api';
  scope: ScanScope;
  status: JobStatus;
  request_id: string | null;
  requested_by: string | null;
  deduplicated: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  stats: Record<string, number>;
  error: string | null;
  tasks: ScanTask[] | null;
}

export interface RefreshResult {
  arn: string;
  status: string;
  resource: Resource | null;
  error: { code: string; message: string } | null;
}

export interface RefreshResponse {
  job: ScanJob;
  results: RefreshResult[] | null;
}

export interface CoverageScope {
  region: string;
  resource_type: string;
  status: string;
  resource_count: number | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface Coverage {
  account_id: string;
  account: Account | null;
  enabled_regions: string[];
  regions_checked_at: string | null;
  scopes: CoverageScope[];
}

export interface ApiKey {
  key_id: string;
  name: string;
  roles: RoleName[];
  created_by: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface ApiKeyCreated {
  key: ApiKey;
  secret: string;
}
