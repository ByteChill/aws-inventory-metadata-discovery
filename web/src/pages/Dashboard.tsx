import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import type { ScanJob } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { AppLink, BarList, ErrorNotice, Loading, Mono, PageTitle, Panel, Stat, StatusTag, formatTime, scopeText, shortType } from '../components/common';
import { useAsync } from '../components/useAsync';

function rows(groups: Array<Record<string, string | number>>, key: string) {
  return groups
    .map((g) => ({ label: String(g[key]), value: Number(g.count) }))
    .sort((a, b) => b.value - a.value);
}

export function DashboardPage() {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const { can } = useAuth();
  const stats = useAsync(
    async () => {
      const [byType, byRegion, byAccount, types] = await Promise.all([
        api.stats('resource_type'),
        api.stats('region'),
        api.stats('account_id'),
        api.resourceTypes(),
      ]);
      return { byType, byRegion, byAccount, types: types.items };
    },
    [],
  );
  const jobs = useAsync(() => (can('scans:read') ? api.scans({}, undefined, 50) : Promise.resolve(null)), [can]);

  const recent: ScanJob[] = jobs.data?.items ?? [];
  const finished = recent.filter((j) => j.finished_at);
  const succeeded = finished.filter((j) => j.status === 'succeeded').length;
  const running = recent.filter((j) => j.status === 'queued' || j.status === 'running').length;

  return (
    <>
      <PageTitle
        title="Dashboard"
        subtitle="What is in the inventory, and how the latest scans went."
        actions={
          can('scans:write') ? (
            <Button size={SIZE.compact} onClick={() => navigate('/scans?new=1')}>
              New scan
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
            <Stat label="Resources" value={stats.data.byType.total.toLocaleString()} hint="current, not deleted" />
            <Stat label="Accounts" value={stats.data.byAccount.groups.length.toLocaleString()} hint="with resources" />
            <Stat label="Resource types" value={stats.data.types.length} hint="registered collectors" />
            <Stat
              label="Recent scan success"
              value={finished.length ? `${Math.round((succeeded / finished.length) * 100)}%` : '—'}
              hint={`${finished.length} finished · ${running} in progress (last ${recent.length})`}
            />
          </div>
          <div
            className={css({
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))',
              gap: theme.sizing.scale600,
              [theme.mediaQuery.small]: {},
            })}
          >
            <Panel title="By resource type">
              <BarList
                rows={rows(stats.data.byType.groups, 'resource_type').map((r) => ({ ...r, label: shortType(r.label) }))}
                onSelect={(label) => navigate(`/resources?resource_type=${encodeURIComponent(`AWS::${label}`)}`)}
              />
            </Panel>
            <Panel title="By region">
              <BarList
                rows={rows(stats.data.byRegion.groups, 'region')}
                onSelect={(label) => navigate(`/resources?region=${encodeURIComponent(label)}`)}
              />
            </Panel>
            <Panel title="Top accounts">
              <BarList
                rows={rows(stats.data.byAccount.groups, 'account_id').slice(0, 10)}
                onSelect={(label) => navigate(`/accounts/${encodeURIComponent(label)}`)}
              />
            </Panel>
          </div>
        </>
      ) : null}
      {can('scans:read') ? (
        <Panel
          title="Recent scan jobs"
          actions={
            <Button size={SIZE.compact} kind={KIND.tertiary} onClick={() => navigate('/scans')}>
              All jobs
            </Button>
          }
        >
          <ErrorNotice error={jobs.error} />
          <TableBuilder data={recent.slice(0, 8)} isLoading={jobs.loading && !jobs.data} emptyMessage="No scan jobs yet">
            <TableBuilderColumn<ScanJob> header="Job">
              {(job) => (
                <AppLink to={`/scans/${encodeURIComponent(job.job_id)}`}>
                  <Mono>{job.job_id}</Mono>
                </AppLink>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Scope">{(job) => scopeText(job.scope)}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Status">{(job) => <StatusTag status={job.status} />}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="By">{(job) => job.requested_by ?? job.trigger}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Created">{(job) => formatTime(job.created_at)}</TableBuilderColumn>
          </TableBuilder>
        </Panel>
      ) : null}
    </>
  );
}
