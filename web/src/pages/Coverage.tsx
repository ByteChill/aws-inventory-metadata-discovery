import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { toaster } from 'baseui/toast';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import type { CoverageScope, ScanScope } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { AppLink, ErrorNotice, Mono, PageTitle, Panel, StatusTag, formatTime, shortType } from '../components/common';
import { useAsync } from '../components/useAsync';

export function CoveragePage() {
  const [css, theme] = useStyletron();
  const { accountId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useAuth();
  const coverage = useAsync(() => api.coverage(accountId), [accountId]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const scan = async (scope: ScanScope, key: string) => {
    setBusy(key);
    setError(null);
    try {
      const job = await api.startScan(scope);
      if (job.deduplicated) toaster.info('A job with the same scope is already running; showing it.', {});
      navigate(`/scans/${encodeURIComponent(job.job_id)}`);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const data = coverage.data;
  const counts = (data?.scopes ?? []).reduce<Record<string, number>>((acc, s) => ({ ...acc, [s.status]: (acc[s.status] ?? 0) + 1 }), {});

  return (
    <>
      <PageTitle
        title={
          <span>
            Account <Mono>{accountId}</Mono>
          </span>
        }
        subtitle="Which regions and types were scanned, when, and with what result."
        actions={
          can('scans:write') ? (
            <Button size={SIZE.compact} isLoading={busy === 'account'} onClick={() => void scan({ account_id: accountId }, 'account')}>
              Scan whole account
            </Button>
          ) : null
        }
      />
      <ErrorNotice error={error} />
      <ErrorNotice error={coverage.error} />
      {data ? (
        <Panel>
          <div className={css({ display: 'flex', gap: theme.sizing.scale600, flexWrap: 'wrap' })}>
            <span>Enabled regions: {data.enabled_regions.length ? data.enabled_regions.join(', ') : 'unknown (not checked yet)'}</span>
            <span>Checked: {formatTime(data.regions_checked_at)}</span>
            {Object.entries(counts).map(([status, n]) => (
              <span key={status}>
                <StatusTag status={status} /> {n}
              </span>
            ))}
          </div>
        </Panel>
      ) : null}
      <Panel title="Scopes">
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder data={data?.scopes ?? []} isLoading={coverage.loading} emptyMessage="Nothing scanned yet for this account">
            <TableBuilderColumn<CoverageScope> header="Region">{(s) => s.region}</TableBuilderColumn>
            <TableBuilderColumn<CoverageScope> header="Type">
              {(s) => (
                <AppLink
                  to={`/resources?account_id=${encodeURIComponent(accountId)}&region=${encodeURIComponent(s.region)}&resource_type=${encodeURIComponent(s.resource_type)}`}
                >
                  {shortType(s.resource_type)}
                </AppLink>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<CoverageScope> header="Status">{(s) => <StatusTag status={s.status} />}</TableBuilderColumn>
            <TableBuilderColumn<CoverageScope> header="Resources" numeric>{(s) => s.resource_count ?? '—'}</TableBuilderColumn>
            <TableBuilderColumn<CoverageScope> header="Last success">{(s) => formatTime(s.last_success_at)}</TableBuilderColumn>
            <TableBuilderColumn<CoverageScope> header="Error">{(s) => (s.error_code ? `${s.error_code}: ${s.error_message ?? ''}` : '—')}</TableBuilderColumn>
            <TableBuilderColumn<CoverageScope> header="">
              {(s) =>
                can('scans:write') ? (
                  <Button
                    size={SIZE.mini}
                    kind={KIND.secondary}
                    isLoading={busy === `${s.region}/${s.resource_type}`}
                    onClick={() => void scan({ account_id: accountId, region: s.region, resource_type: s.resource_type }, `${s.region}/${s.resource_type}`)}
                  >
                    Scan
                  </Button>
                ) : null
              }
            </TableBuilderColumn>
          </TableBuilder>
        </div>
      </Panel>
    </>
  );
}
