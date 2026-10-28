import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import type { Resource, ScanJob } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import {
  AppLink,
  ErrorNotice,
  Loading,
  Mono,
  PageTitle,
  Panel,
  Secondary,
  Stat,
  StatusTag,
  TypeName,
  When,
  changeSummary,
  startedBy,
  ScopeTag,
  tableOverrides,
} from '../components/common';
import { useAsync } from '../components/useAsync';
import { Distribution } from '../components/Distribution';
import { accountLabel } from '../components/accounts';
import { formatTime, useI18n } from '../i18n';

const FINISHED_OK = new Set(['succeeded', 'partial']);

export function DashboardPage() {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { t, lang } = useI18n();
  const managesScans = can('scans:write');

  const stats = useAsync(async () => {
    const [byType, byAccount, byEim] = await Promise.all([
      api.stats('resource_type'),
      api.stats('account_id'),
      api.stats('eim'),
    ]);
    return { byType, byAccount, byEim };
  }, []);
  const recent = useAsync(
    () => api.resources({ include_deleted: true }, undefined, 8).then((page) => page.items),
    [],
  );
  // freshness: when the last scheduled scan of everything finished
  const scheduled = useAsync(
    () =>
      can('scans:read')
        ? api.scans({ trigger: 'schedule' }, undefined, 20).then((p) => p.items.find((j) => j.finished_at && FINISHED_OK.has(j.status)) ?? null)
        : Promise.resolve(null),
    [can],
  );
  const jobs = useAsync(() => (managesScans ? api.scans({}, undefined, 50) : Promise.resolve(null)), [managesScans]);

  const freshness = scheduled.data?.finished_at;
  const recentJobs: ScanJob[] = jobs.data?.items ?? [];
  const failing = recentJobs.filter((j) => j.status === 'failed' || j.status === 'timed_out' || j.status === 'partial');
  const inProgress = recentJobs.filter((j) => j.status === 'queued' || j.status === 'running');

  return (
    <>
      <PageTitle
        title={t('Overview', '概览')}
        subtitle={
          freshness
            ? t(`Inventory last refreshed ${formatTime(freshness)}`, `清单数据更新于 ${formatTime(freshness)}`)
            : t('What resources the accounts have, by type, region and account.', '各账号有哪些资源：按类型、区域和账号查看。')
        }
        actions={
          managesScans ? (
            <Button size={SIZE.compact} onClick={() => navigate('/scans?new=1')}>
              {t('New scan', '发起扫描')}
            </Button>
          ) : null
        }
      />
      <ErrorNotice error={stats.error} />
      {stats.loading && !stats.data ? (
        <Loading />
      ) : stats.data ? (
        <>
          <div
            className={css({
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
              gap: theme.sizing.scale600,
              marginBottom: theme.sizing.scale600,
            })}
          >
            <Stat label="EIM" value={stats.data.byEim.groups.filter((g) => g.eim !== null).length.toLocaleString()} />
            <Stat label={t('Accounts', '账号')} value={stats.data.byAccount.groups.length.toLocaleString()} />
            <Stat label={t('Resource types', '资源类型')} value={stats.data.byType.groups.length} />
            <Stat label={t('Resources', '资源总数')} value={stats.data.byType.total.toLocaleString()} />
            <Stat
              label={t('Data refreshed', '数据更新')}
              value={freshness ? <When value={freshness} /> : '—'}
              hint={freshness ? undefined : t('No full scan has finished yet', '尚未完成过全量扫描')}
            />
          </div>
          <Panel title={t('Resources by', '资源分布')}>
            <Distribution />
          </Panel>
        </>
      ) : null}

      <Panel
        title={t('Recently changed', '最近变化的资源')}
        actions={
          <Button size={SIZE.compact} kind={KIND.tertiary} onClick={() => navigate('/resources')}>
            {t('All resources', '全部资源')}
          </Button>
        }
      >
        <ErrorNotice error={recent.error} />
        <TableBuilder data={recent.data ?? []} isLoading={recent.loading && !recent.data} emptyMessage={t('Nothing yet', '暂无')} overrides={tableOverrides}>
          <TableBuilderColumn<Resource> header={t('EIM / account', 'EIM / 账号')}>
            {(r) => (
              <>
                {r.eim ?? '—'}
                <Secondary>{accountLabel(r.account_id, r.account_name)}</Secondary>
              </>
            )}
          </TableBuilderColumn>
          <TableBuilderColumn<Resource> header={t('Resource type', '资源类型')}>{(r) => <TypeName type={r.resource_type} />}</TableBuilderColumn>
          <TableBuilderColumn<Resource> header={t('Region', '区域')}>{(r) => r.region}</TableBuilderColumn>
          <TableBuilderColumn<Resource> header={t('Resource', '资源')}>
            {(r) => (
              <>
                <AppLink to={`/resources?include_deleted=true&arn=${encodeURIComponent(r.arn)}`}>
                  {r.name || r.resource_id}
                </AppLink>
                {r.name ? <Secondary>{r.resource_id}</Secondary> : null}
              </>
            )}
          </TableBuilderColumn>
          <TableBuilderColumn<Resource> header={t('Change', '变化')}>
            {(r) => (r.deleted_at ? <StatusTag status="deleted" /> : r.first_seen_at === r.last_changed_at ? <StatusTag status="created" /> : <StatusTag status="updated" />)}
          </TableBuilderColumn>
          <TableBuilderColumn<Resource> header={t('When', '时间')}>{(r) => <When value={r.deleted_at ?? r.last_changed_at} />}</TableBuilderColumn>
        </TableBuilder>
      </Panel>

      {managesScans ? (
        <Panel
          title={t('Scans', '扫描')}
          actions={
            <Button size={SIZE.compact} kind={KIND.tertiary} onClick={() => navigate('/scans')}>
              {t('All scans', '全部扫描')}
            </Button>
          }
        >
          <ErrorNotice error={jobs.error} />
          <div className={css({ display: 'flex', gap: theme.sizing.scale800, flexWrap: 'wrap', marginBottom: theme.sizing.scale500 })}>
            <AppLink to="/scans?status=failed">
              {t(`${failing.length} need attention`, `${failing.length} 个需要关注`)}
            </AppLink>
            <span>{t(`${inProgress.length} in progress`, `${inProgress.length} 个进行中`)}</span>
            <Secondary>{t(`of the last ${recentJobs.length}`, `（最近 ${recentJobs.length} 次）`)}</Secondary>
          </div>
          <TableBuilder data={recentJobs.slice(0, 6)} isLoading={jobs.loading && !jobs.data} emptyMessage={t('No scans yet', '暂无扫描')} overrides={tableOverrides}>
            <TableBuilderColumn<ScanJob> header={t('Scan ID', '扫描 ID')}>
              {(job) => (
                <AppLink to={`/scans/${encodeURIComponent(job.job_id)}`}>
                  <Mono>{job.job_id}</Mono>
                </AppLink>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Scope', '范围')}>{(job) => <ScopeTag scope={job.scope} />}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Status', '状态')}>{(job) => <StatusTag status={job.status} />}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Result', '结果')}>
              {(job) => (job.finished_at ? changeSummary(job.stats, lang) : '—')}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Started by', '发起人')}>
              {(job) => startedBy(job, lang)}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('When', '时间')}>{(job) => <When value={job.created_at} />}</TableBuilderColumn>
          </TableBuilder>
        </Panel>
      ) : null}
    </>
  );
}
