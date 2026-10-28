import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { PLACEMENT, StatefulPopover } from 'baseui/popover';
import { toaster } from 'baseui/toast';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import type { CoverageScope, ScanScope } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { ErrorNotice, KeyValues, Loading, PageTitle, Panel, Secondary, Stat, StatusTag, TypeName, When } from '../components/common';
import { useAsync } from '../components/useAsync';
import { useI18n } from '../i18n';
import { errorText, serviceOf, typeLabel } from '../i18n/labels';

// status colours only for the people who run scans; everyone else sees counts
const STATUS_COLORS: Record<string, string> = {
  succeeded: '#E6F2ED',
  failed: '#FFEFED',
  auth_denied: '#FFF3EF',
  never: '#F6F6F6',
};
const STATUS_BORDERS: Record<string, string> = {
  succeeded: '#3AA76D',
  failed: '#E11900',
  auth_denied: '#ED6E33',
  never: '#CBCBCB',
};

export function CoveragePage() {
  const [css, theme] = useStyletron();
  const { accountId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { t, lang } = useI18n();
  const managesScans = can('scans:write');
  const coverage = useAsync(() => api.coverage(accountId), [accountId]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const scan = async (scope: ScanScope, key: string) => {
    setBusy(key);
    setError(null);
    try {
      const job = await api.startScan(scope);
      if (job.deduplicated) toaster.info(t('The same scan is already running; showing it.', '相同范围的扫描正在进行，已为你打开。'), {});
      navigate(`/scans/${encodeURIComponent(job.job_id)}`);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const data = coverage.data;
  const scopes = data?.scopes ?? [];
  const byKey = new Map(scopes.map((s) => [`${s.resource_type}|${s.region}`, s]));
  const regions = [...new Set(scopes.map((s) => s.region))].sort((a, b) => (a === 'global' ? 1 : b === 'global' ? -1 : a.localeCompare(b)));
  const types = [...new Set(scopes.map((s) => s.resource_type))].sort(
    (a, b) => serviceOf(a).localeCompare(serviceOf(b)) || typeLabel(a, lang).localeCompare(typeLabel(b, lang)),
  );
  const total = scopes.reduce((n, s) => n + (s.resource_count ?? 0), 0);
  const lastSuccess = scopes.map((s) => s.last_success_at).filter(Boolean).sort().pop() ?? null;
  const problems = scopes.filter((s) => s.status === 'failed' || s.status === 'auth_denied').length;
  const never = scopes.filter((s) => s.status === 'never').length;

  const cell = (scope: CoverageScope | undefined) => {
    if (!scope) return <span className={css({ color: theme.colors.contentTertiary })}>·</span>;
    const showResources = () =>
      navigate(
        `/resources?account_id=${encodeURIComponent(accountId)}&region=${encodeURIComponent(scope.region)}&resource_type=${encodeURIComponent(scope.resource_type)}`,
      );
    const box = (
      <button
        type="button"
        onClick={managesScans ? undefined : showResources}
        title={managesScans ? undefined : typeLabel(scope.resource_type, lang)}
        className={css({
          minWidth: '56px',
          padding: '6px 8px',
          borderRadius: theme.borders.radius200,
          border: `1px solid ${managesScans ? STATUS_BORDERS[scope.status] ?? theme.colors.borderOpaque : theme.colors.borderOpaque}`,
          backgroundColor: managesScans ? STATUS_COLORS[scope.status] ?? theme.colors.backgroundSecondary : theme.colors.backgroundSecondary,
          color: managesScans ? '#000' : theme.colors.contentPrimary,
          cursor: 'pointer',
          fontVariantNumeric: 'tabular-nums',
          ...theme.typography.LabelSmall,
        })}
      >
        {scope.resource_count ?? (scope.status === 'never' ? '—' : 0)}
      </button>
    );
    if (!managesScans) return box;
    const key = `${scope.region}/${scope.resource_type}`;
    return (
      <StatefulPopover
        placement={PLACEMENT.bottom}
        showArrow
        content={() => (
          <div className={css({ padding: theme.sizing.scale600, maxWidth: '320px' })}>
            <TypeName type={scope.resource_type} />
            <Secondary>{scope.region}</Secondary>
            <div className={css({ marginTop: theme.sizing.scale400 })}>
              <KeyValues
                items={[
                  [t('Scan status', '扫描状态'), <StatusTag status={scope.status} />],
                  [t('Resources', '资源数'), String(scope.resource_count ?? '—')],
                  [t('Last successful scan', '最近成功扫描'), <When value={scope.last_success_at} />],
                  ...(scope.error_code ? [[t('Problem', '问题'), errorText(scope.error_code, lang)] as [string, string]] : []),
                ]}
              />
            </div>
            <div className={css({ display: 'flex', gap: theme.sizing.scale300, marginTop: theme.sizing.scale500 })}>
              <Button size={SIZE.compact} isLoading={busy === key} onClick={() => void scan({ account_id: accountId, region: scope.region, resource_type: scope.resource_type }, key)}>
                {t('Scan now', '立即扫描')}
              </Button>
              <Button size={SIZE.compact} kind={KIND.tertiary} onClick={showResources}>
                {t('View resources', '查看资源')}
              </Button>
            </div>
          </div>
        )}
      >
        {box}
      </StatefulPopover>
    );
  };

  return (
    <>
      <PageTitle
        title={coverage.data?.account?.name ?? t(`Account ${accountId}`, `账号 ${accountId}`)}
        subtitle={
          coverage.data?.account
            ? [
                coverage.data.account.name ? accountId : null,
                coverage.data.account.eim ? `EIM ${coverage.data.account.eim}` : null,
                coverage.data.account.account_type,
                coverage.data.account.status !== 'ACTIVE' ? coverage.data.account.status : null,
              ]
                .filter(Boolean)
                .join(' · ')
            : t('Resources in this account by type and region.', '该账号按类型和区域划分的资源。')
        }
        actions={
          managesScans ? (
            <Button size={SIZE.compact} isLoading={busy === 'account'} onClick={() => void scan({ account_id: accountId }, 'account')}>
              {t('Scan whole account', '扫描整个账号')}
            </Button>
          ) : null
        }
      />
      <ErrorNotice error={error} />
      <ErrorNotice error={coverage.error} />
      {coverage.loading && !data ? <Loading /> : null}
      {data ? (
        <>
          <div className={css({ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: theme.sizing.scale600, marginBottom: theme.sizing.scale600 })}>
            <Stat label={t('Resources', '资源总数')} value={total.toLocaleString()} />
            <Stat label={t('Regions in use', '启用的区域')} value={data.enabled_regions.length || '—'} hint={data.enabled_regions.join(', ') || undefined} />
            <Stat label={t('Data refreshed', '数据更新')} value={lastSuccess ? <When value={lastSuccess} /> : '—'} />
            {managesScans ? (
              <Stat
                label={t('Needs attention', '需要关注')}
                value={problems}
                hint={t(`${never} never scanned`, `${never} 个尚未扫描`)}
              />
            ) : null}
          </div>
          <Panel
            title={t('By type and region', '按类型与区域')}
            actions={
              managesScans ? (
                <div className={css({ display: 'flex', gap: theme.sizing.scale500, flexWrap: 'wrap', alignItems: 'center' })}>
                  {(['succeeded', 'failed', 'auth_denied', 'never'] as const).map((status) => (
                    <span key={status} className={css({ display: 'inline-flex', alignItems: 'center', gap: theme.sizing.scale200, ...theme.typography.ParagraphXSmall })}>
                      <span className={css({ width: '12px', height: '12px', borderRadius: '3px', backgroundColor: STATUS_COLORS[status], border: `1px solid ${STATUS_BORDERS[status]}` })} />
                      <StatusTag status={status} />
                    </span>
                  ))}
                </div>
              ) : (
                <Secondary>{t('Click a number to see the resources', '点击数字查看资源')}</Secondary>
              )
            }
          >
            {types.length ? (
              <div className={css({ overflowX: 'auto' })}>
                <table className={css({ borderCollapse: 'separate', borderSpacing: '6px', minWidth: '100%' })}>
                  <thead>
                    <tr>
                      <th />
                      {regions.map((region) => (
                        <th key={region} className={css({ ...theme.typography.LabelSmall, color: theme.colors.contentSecondary, textAlign: 'center', whiteSpace: 'nowrap', padding: '4px' })}>
                          {region}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {types.map((type) => (
                      <tr key={type}>
                        <th className={css({ textAlign: 'left', whiteSpace: 'nowrap', paddingRight: theme.sizing.scale500, ...theme.typography.ParagraphSmall, color: theme.colors.contentPrimary, fontWeight: 'normal' })}>
                          <TypeName type={type} />
                        </th>
                        {regions.map((region) => (
                          <td key={region} className={css({ textAlign: 'center' })}>
                            {cell(byKey.get(`${type}|${region}`))}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Secondary>{t('Nothing has been scanned in this account yet.', '该账号还没有被扫描过。')}</Secondary>
            )}
          </Panel>
        </>
      ) : null}
    </>
  );
}
