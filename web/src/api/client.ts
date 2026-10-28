import type {
  Account,
  ApiKey,
  ApiKeyCreated,
  Coverage,
  Me,
  Page,
  RefreshResponse,
  Resource,
  ResourceType,
  RoleName,
  ScanJob,
  ScanScope,
  StatsResponse,
} from './types';

// Every call goes to the documented /v1 API (docs/api.md); a test checks the paths below.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
    readonly details: Array<{ loc?: unknown[]; msg?: string }> | null,
  ) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | string[] | undefined | null>;

let tokenSource: () => string | null = () => null;
let onUnauthenticated: () => void = () => undefined;

/** The auth layer plugs in how to get the bearer token (SSO later) and what to do on 401. */
export function configureAuth(source: () => string | null, unauthenticated: () => void): void {
  tokenSource = source;
  onUnauthenticated = unauthenticated;
}

function withQuery(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') continue;
    for (const v of Array.isArray(value) ? value : [value]) params.append(key, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(method: string, path: string, opts: { query?: Query; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = tokenSource();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(withQuery(path, opts.query), {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = (payload as { error?: Record<string, unknown> } | null)?.error ?? {};
    const failure = new ApiError(
      response.status,
      String(error.code ?? `HTTP_${response.status}`),
      String(error.message ?? response.statusText),
      (error.request_id as string | undefined) ?? response.headers.get('X-Request-ID'),
      (error.details as ApiError['details']) ?? null,
    );
    if (response.status === 401) onUnauthenticated();
    throw failure;
  }
  return payload as T;
}

export interface ResourceFilters {
  account_id?: string[];
  region?: string[];
  resource_type?: string[];
  eim?: string[];
  account_type?: string[];
  name_prefix?: string;
  tag?: string;
  include_deleted?: boolean;
}

export const api = {
  me: () => request<Me>('GET', '/v1/me'),

  resources: (filters: ResourceFilters, cursor?: string, limit = 50) =>
    request<Page<Resource>>('GET', '/v1/resources', {
      query: { ...filters, limit, cursor, with_total: !cursor },
    }),
  resourceByArn: (arn: string) =>
    request<Resource>('GET', '/v1/resources/by-arn', { query: { arn, include_deleted: true } }),
  stats: (groupBy: string, query: Query = {}) =>
    request<StatsResponse>('GET', '/v1/resources/stats', { query: { group_by: groupBy, ...query } }),
  accounts: () => request<{ items: Account[] }>('GET', '/v1/accounts'),
  resourceTypes: () => request<{ items: ResourceType[] }>('GET', '/v1/resource-types'),
  coverage: (accountId: string) =>
    request<Coverage>('GET', `/v1/accounts/${encodeURIComponent(accountId)}/coverage`),
  refresh: (arns: string[], waitSeconds = 20) =>
    request<RefreshResponse>('POST', '/v1/resources/refresh', { body: { arns, wait_seconds: waitSeconds } }),

  scans: (query: Query, cursor?: string, limit = 50) =>
    request<Page<ScanJob>>('GET', '/v1/scans', { query: { ...query, limit, cursor } }),
  scan: (jobId: string, includeTasks: boolean) =>
    request<ScanJob>('GET', `/v1/scans/${encodeURIComponent(jobId)}`, {
      query: { include_tasks: includeTasks },
    }),
  startScan: (scope: ScanScope) => request<ScanJob>('POST', '/v1/scans', { body: { scope, wait_seconds: 0 } }),

  apiKeys: () => request<{ items: ApiKey[] }>('GET', '/v1/api-keys'),
  createApiKey: (name: string, roles: RoleName[], expiresAt: string | null) =>
    request<ApiKeyCreated>('POST', '/v1/api-keys', { body: { name, roles, expires_at: expiresAt } }),
  revokeApiKey: (keyId: string) => request<void>('DELETE', `/v1/api-keys/${encodeURIComponent(keyId)}`),
};
