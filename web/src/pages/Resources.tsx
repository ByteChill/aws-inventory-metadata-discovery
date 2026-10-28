import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { Checkbox } from 'baseui/checkbox';
import { Drawer } from 'baseui/drawer';
import { Input } from 'baseui/input';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { toaster } from 'baseui/toast';
import { StatefulTooltip } from 'baseui/tooltip';
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { ApiError, api, type ResourceFilters } from '../api/client';
import { ACCOUNT_TYPES, type Resource } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import {
  AppLink,
  Copyable,
  Empty,
  ErrorNotice,
  KeyValues,
  Loading,
  Mono,
  PageTitle,
  Panel,
  Secondary,
  StatusTag,
  TechDetails,
  TypeName,
  When,
  tableOverrides,
  useNarrow,
} from '../components/common';
import { CheckFilter } from '../components/CheckFilter';
import { Pager, useKeysetPages } from '../components/Pager';
import { accountLabel, useAccounts } from '../components/accounts';
import { useAsync } from '../components/useAsync';
import { formatTime, useI18n, type Lang } from '../i18n';
import { KEY_ATTRIBUTES, readPath, type Attribute } from '../i18n/attributes';
import { errorText, statusLabel, typeLabel } from '../i18n/labels';

// multi-valued filters repeat in the URL: ?region=a&region=b
const LIST_KEYS = ['resource_type', 'region', 'account_id', 'eim', 'account_type'] as const;
const TEXT_KEYS = ['name_prefix', 'tag'] as const;
type ListKey = (typeof LIST_KEYS)[number];
type TextKey = (typeof TEXT_KEYS)[number];

export function ResourcesPage() {
  const [css, theme] = useStyletron();
  const { t, lang } = useI18n();
  const narrow = useNarrow();
  const [params, setParams] = useSearchParams();
  const filters: ResourceFilters = {
    ...Object.fromEntries(LIST_KEYS.map((k) => [k, params.getAll(k)])),
    ...Object.fromEntries(TEXT_KEYS.map((k) => [k, params.get(k) ?? undefined])),
    include_deleted: params.get('include_deleted') === 'true',
  };
  const key = [...LIST_KEYS, ...TEXT_KEYS].map((k) => params.getAll(k).join(',')).join('|') + `|${filters.include_deleted}`;
  const types = useAsync(() => api.resourceTypes(), []);
  const regions = [...new Set((types.data?.items ?? []).flatMap((x) => x.enabled_regions))].sort();
  const directory = useAccounts();
  // accounts with resources but (not yet) in the directory still get an option
  const withResources = useAsync(() => api.stats('account_id'), []);
  const accountIds = [
    ...new Set([...directory.items.map((a) => a.account_id), ...(withResources.data?.groups ?? []).map((g) => String(g.account_id))]),
  ].sort();

  const setFilter = (name: ListKey | TextKey | 'include_deleted', value: string | string[]) => {
    const next = new URLSearchParams(params);
    next.delete('arn');
    next.delete(name);
    for (const v of Array.isArray(value) ? value : [value]) if (v) next.append(name, v);
    setParams(next, { replace: true });
  };

  // free-text filters apply after typing stops (selects apply at once)
  const [text, setText] = useState({ name_prefix: '', tag: '' });
  useEffect(
    () => setText({ name_prefix: params.get('name_prefix') ?? '', tag: params.get('tag') ?? '' }),
    [key], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const typeText = (name: TextKey, value: string) => {
    setText((current) => ({ ...current, [name]: value }));
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setFilter(name, value.trim()), 400);
  };

  const pages = useKeysetPages(key, (cursor, limit) => api.resources(filters, cursor, limit));
  const items: Resource[] = pages.page.data?.items ?? [];
  const first = pages.page;

  const selectedArn = params.get('arn');
  const openResource = (arn: string | null) => {
    const next = new URLSearchParams(params);
    if (arn) next.set('arn', arn);
    else next.delete('arn');
    setParams(next, { replace: true });
  };

  // a value in the list that narrows the list to it
  const filterLink = (name: 'region' | 'account_id' | 'eim' | 'account_type', value: string, label: string = value) => (
    <button
      type="button"
      title={t('Show only this', '只看这个')}
      onClick={(event) => {
        event.stopPropagation();
        setFilter(name, [value]);
      }}
      className={css({ all: 'unset', cursor: 'pointer', whiteSpace: 'nowrap', borderBottom: `1px dashed ${theme.colors.borderOpaque}`, ':hover': { color: theme.colors.contentAccent } })}
    >
      {label}
    </button>
  );

  // the Name tag is already the resource name; the rest are chips that filter by themselves
  const TAGS_SHOWN = 3;
  // cards are buttons themselves, so their chips are plain text (no nested buttons)
  const tagChips = (r: Resource, interactive = true) => {
    const tags = Object.entries(r.tags)
      .filter(([k]) => k !== 'Name')
      .sort(([a], [b]) => a.localeCompare(b));
    if (!tags.length) return <Secondary>—</Secondary>;
    const chip = css({
      all: 'unset',
      cursor: 'pointer',
      maxWidth: '16rem',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap',
      padding: `0 ${theme.sizing.scale200}`,
      borderRadius: theme.borders.radius200,
      backgroundColor: theme.colors.backgroundTertiary,
      ...theme.typography.ParagraphXSmall,
      color: theme.colors.contentPrimary,
      ':hover': { color: theme.colors.contentAccent },
    });
    const hidden = tags.slice(TAGS_SHOWN);
    return (
      <div className={css({ display: 'flex', flexWrap: 'wrap', gap: theme.sizing.scale100 })}>
        {tags.slice(0, TAGS_SHOWN).map(([k, v]) =>
          interactive ? (
          <button
            key={k}
            type="button"
            title={`${k}=${v}`}
            className={chip}
            onClick={(event) => {
              event.stopPropagation();
              setFilter('tag', `${k}=${v}`);
            }}
          >
            {k}={v}
          </button>
          ) : (
            <span key={k} className={chip}>
              {k}={v}
            </span>
          ),
        )}
        {hidden.length ? (
          <StatefulTooltip content={() => <div>{hidden.map(([k, v]) => <div key={k}>{`${k}=${v}`}</div>)}</div>}>
            <span className={css({ ...theme.typography.ParagraphXSmall, color: theme.colors.contentSecondary, cursor: 'default' })}>
              +{hidden.length}
            </span>
          </StatefulTooltip>
        ) : null}
      </div>
    );
  };

  const fieldGrid = css({
    display: 'grid',
    gridTemplateColumns: narrow ? '1fr' : 'repeat(auto-fill, minmax(160px, 1fr))',
    gap: theme.sizing.scale400,
    alignItems: 'center',
  });

  return (
    <>
      <PageTitle title={t('Resources', '资源')} subtitle={t('Everything found in the accounts, as of the latest scan.', '各账号中发现的全部资源，以最近一次扫描为准。')} />
      <Panel>
        <div className={fieldGrid}>
          <CheckFilter
            placeholder={t('All EIMs', '全部 EIM')}
            options={directory.eims.map((e) => ({ id: e, label: e }))}
            selected={filters.eim ?? []}
            onChange={(value) => setFilter('eim', value)}
          />
          <CheckFilter
            placeholder={t('All accounts', '全部账号')}
            options={accountIds.map((a) => ({ id: a, label: accountLabel(a, directory.byId.get(a)?.name) }))}
            selected={filters.account_id ?? []}
            onChange={(value) => setFilter('account_id', value)}
          />
          <CheckFilter
            placeholder={t('All account types', '全部账号类型')}
            options={ACCOUNT_TYPES.map((x) => ({ id: x, label: x }))}
            selected={filters.account_type ?? []}
            onChange={(value) => setFilter('account_type', value)}
          />
          <CheckFilter
            placeholder={t('All resource types', '全部资源类型')}
            options={(types.data?.items ?? [])
              .map((x) => ({ id: x.resource_type, label: typeLabel(x.resource_type, lang) }))
              .sort((a, b) => a.label.localeCompare(b.label))}
            selected={filters.resource_type ?? []}
            onChange={(value) => setFilter('resource_type', value)}
          />
          <CheckFilter
            placeholder={t('All regions', '全部区域')}
            options={['global', ...regions.filter((r) => r !== 'global')].map((r) => ({ id: r, label: r }))}
            selected={filters.region ?? []}
            onChange={(value) => setFilter('region', value)}
          />
          <div className={css({ gridColumn: narrow ? 'auto' : 'span 2' })}>
          <Input
            size={SIZE.compact}
            value={text.name_prefix}
            placeholder={t('Search by name (starts with)', '按名称搜索（开头匹配）')}
            onChange={(e) => typeText('name_prefix', e.currentTarget.value)}
            clearable
          />
          </div>
          <div className={css({ gridColumn: narrow ? 'auto' : 'span 2' })}>
            <Input
              size={SIZE.compact}
              value={text.tag}
              placeholder={t('Tag: Key=Value or Key', '标签：Key=Value 或 Key')}
              onChange={(e) => typeText('tag', e.currentTarget.value)}
              clearable
            />
          </div>
          <Checkbox checked={filters.include_deleted} onChange={(e) => setFilter('include_deleted', e.currentTarget.checked ? 'true' : '')}>
            {t('Include deleted', '包含已删除')}
          </Checkbox>
          <Button size={SIZE.compact} kind={KIND.tertiary} onClick={() => setParams(new URLSearchParams(), { replace: true })}>
            {t('Clear all', '清除全部')}
          </Button>
        </div>
      </Panel>

      <Panel title={pages.total != null ? t(`${pages.total.toLocaleString()} resources`, `共 ${pages.total.toLocaleString()} 个资源`) : t('Resources', '资源')}>
        <ErrorNotice error={first.error} />
        {narrow ? (
          <div className={css({ display: 'grid', rowGap: theme.sizing.scale300 })}>
            {first.loading ? <Loading /> : null}
            {!first.loading && !items.length ? <Empty>{t('No resources match these filters', '没有符合条件的资源')}</Empty> : null}
            {items.map((r) => (
              <button
                key={r.arn}
                type="button"
                onClick={() => openResource(r.arn)}
                className={css({
                  all: 'unset',
                  cursor: 'pointer',
                  display: 'grid',
                  rowGap: theme.sizing.scale100,
                  padding: theme.sizing.scale500,
                  borderRadius: theme.borders.radius300,
                  border: `1px solid ${theme.colors.borderOpaque}`,
                  ...theme.typography.ParagraphSmall,
                  color: theme.colors.contentPrimary,
                })}
              >
                <span className={css({ color: theme.colors.contentAccent, wordBreak: 'break-all' })}>{r.name || r.resource_id}</span>
                <TypeName type={r.resource_type} />
                <Mono>{r.arn}</Mono>
                <Secondary>
                  {r.eim ? `EIM ${r.eim} · ` : ''}
                  {accountLabel(r.account_id, r.account_name)}
                  {r.account_type ? ` · ${r.account_type}` : ''}
                </Secondary>
                <Secondary>
                  {r.region} · <When value={r.last_changed_at} />
                </Secondary>
                {Object.keys(r.tags).some((k) => k !== 'Name') ? tagChips(r, false) : null}
                {r.deleted_at ? (
                  <span>
                    <StatusTag status="deleted" />
                  </span>
                ) : null}
              </button>
            ))}
          </div>
        ) : (
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder data={items} isLoading={first.loading} emptyMessage={t('No resources match these filters', '没有符合条件的资源')} overrides={tableOverrides}>
            <TableBuilderColumn<Resource> header="EIM">{(r) => (r.eim ? filterLink('eim', r.eim) : '—')}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Account', '账号')}>
              {(r) => (
                <>
                  {filterLink('account_id', r.account_id, r.account_name ?? r.account_id)}
                  {r.account_name ? (
                    <Secondary>
                      <span className={css({ whiteSpace: 'nowrap' })}>{r.account_id}</span>
                    </Secondary>
                  ) : null}
                </>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Account type', '账号类型')}>
              {(r) => (r.account_type ? filterLink('account_type', r.account_type) : '—')}
            </TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Resource type', '资源类型')}>
              {(r) => (
                <span className={css({ whiteSpace: 'nowrap' })}>
                  <TypeName type={r.resource_type} />
                </span>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Region', '区域')}>{(r) => filterLink('region', r.region)}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Name and ARN', '名称与 ARN')}>
              {(r) => (
                <>
                  <button
                    type="button"
                    onClick={() => openResource(r.arn)}
                    className={css({ all: 'unset', cursor: 'pointer', color: theme.colors.contentAccent, wordBreak: 'break-all' })}
                  >
                    {r.name || r.resource_id}
                  </button>
                  <div className={css({ marginTop: theme.sizing.scale100 })}>
                    <Copyable value={r.arn} />
                  </div>
                </>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Tags', '标签')}>{(r) => tagChips(r)}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header={t('Last changed', '最近变化')}>
              {(r) => (
                <>
                  <When value={r.last_changed_at} />
                  {r.deleted_at ? (
                    <div>
                      <StatusTag status="deleted" />
                    </div>
                  ) : null}
                </>
              )}
            </TableBuilderColumn>
          </TableBuilder>
        </div>
        )}
        <Pager pages={pages} />
      </Panel>
      <ResourceDrawer arn={selectedArn} onClose={() => openResource(null)} onRefreshed={first.reload} />
    </>
  );
}

function formatAttribute(attribute: Attribute, data: unknown, lang: Lang): string | null {
  const value = readPath(data, attribute.path);
  if (value === undefined || value === null || value === '') return null;
  const yes = lang === 'zh' ? '是' : 'Yes';
  const no = lang === 'zh' ? '否' : 'No';
  switch (attribute.kind) {
    case 'bool':
      return value ? yes : no;
    case 'date':
      return formatTime(String(value));
    case 'count':
      return Array.isArray(value) ? String(value.length) : null;
    case 'list':
      return Array.isArray(value) ? value.join(', ') || '—' : String(value);
    case 'last-segment':
      return String(value).split('/').pop() ?? String(value);
    default:
      return typeof value === 'object' ? null : String(value);
  }
}

function ResourceDrawer({ arn, onClose, onRefreshed }: { arn: string | null; onClose: () => void; onRefreshed: () => void }) {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { t, lang } = useI18n();
  const detail = useAsync(() => (arn ? api.resourceByArn(arn) : Promise.resolve(null)), [arn]);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => setError(null), [arn]);
  const shown = detail.data;

  const refresh = async () => {
    if (!arn) return;
    setRefreshing(true);
    setError(null);
    try {
      const response = await api.refresh([arn]);
      const result = response.results?.[0];
      if (result?.error) setError(new ApiError(0, result.error.code, result.error.message, response.job.request_id, null));
      else if (result?.status === 'pending') toaster.info(t('Refresh requested: it will update shortly', '已提交刷新，稍后会更新'), {});
      else toaster.positive(`${t('Refreshed', '已刷新')}: ${statusLabel(result?.status ?? '', lang)}`, {});
      detail.reload();
      onRefreshed();
    } catch (failure) {
      setError(failure);
    } finally {
      setRefreshing(false);
    }
  };

  const attributes = shown
    ? (KEY_ATTRIBUTES[shown.resource_type] ?? [])
        .map((a) => [a.label[lang === 'zh' ? 1 : 0], formatAttribute(a, shown.data, lang)] as [string, string | null])
        .filter((pair): pair is [string, string] => pair[1] !== null)
    : [];
  const heading = css({ ...theme.typography.LabelLarge, color: theme.colors.contentPrimary, marginTop: theme.sizing.scale700, marginBottom: theme.sizing.scale300 });

  return (
    <Drawer isOpen={Boolean(arn)} onClose={onClose} size="min(680px, 100vw)" anchor="right">
      <ErrorNotice error={detail.error} />
      {detail.loading && !shown ? <Loading /> : null}
      {shown ? (
        <div>
          <TypeName type={shown.resource_type} />
          <h2 className={css({ ...theme.typography.HeadingSmall, color: theme.colors.contentPrimary, marginTop: theme.sizing.scale300, marginBottom: theme.sizing.scale200, wordBreak: 'break-all' })}>
            {shown.name || shown.resource_id}
          </h2>
          <Copyable value={shown.arn} />
          <Secondary>
            {t('Account', '账号')} {accountLabel(shown.account_id, shown.account_name)} · {shown.region}
            {shown.eim ? ` · EIM ${shown.eim}` : ''}
            {shown.account_type ? ` · ${shown.account_type}` : ''}
          </Secondary>
          {shown.deleted_at ? (
            <div className={css({ marginTop: theme.sizing.scale300 })}>
              <StatusTag status="deleted" /> <When value={shown.deleted_at} />
            </div>
          ) : null}
          <div className={css({ display: 'flex', gap: theme.sizing.scale300, flexWrap: 'wrap', alignItems: 'center', marginTop: theme.sizing.scale500 })}>
            {can('scans:write') ? (
              <Button size={SIZE.compact} isLoading={refreshing} onClick={() => void refresh()}>
                {t('Refresh from AWS', '从 AWS 重新获取')}
              </Button>
            ) : null}
            {can('scans:write') ? (
              <Button
                size={SIZE.compact}
                kind={KIND.secondary}
                onClick={() =>
                  navigate(
                    `/scans?new=1&account_id=${encodeURIComponent(shown.account_id)}&region=${encodeURIComponent(shown.region)}&resource_type=${encodeURIComponent(shown.resource_type)}`,
                  )
                }
              >
                {t('Scan this type', '扫描此类资源')}
              </Button>
            ) : null}
            <AppLink to={`/accounts/${encodeURIComponent(shown.account_id)}`}>{t('View account', '查看账号')}</AppLink>
          </div>
          <ErrorNotice error={error} />

          <h3 className={heading}>{t('Details', '概要')}</h3>
          {attributes.length ? <KeyValues items={attributes} /> : <Secondary>{t('No summary for this type', '此类型暂无概要')}</Secondary>}

          <h3 className={heading}>{t('Tags', '标签')}</h3>
          {Object.keys(shown.tags).length ? (
            <KeyValues items={Object.entries(shown.tags).sort(([a], [b]) => a.localeCompare(b))} />
          ) : (
            <Secondary>{t('No tags', '没有标签')}</Secondary>
          )}

          <h3 className={heading}>{t('History', '记录')}</h3>
          <KeyValues
            items={[
              [t('First seen', '首次发现'), <When value={shown.first_seen_at} />],
              [t('Last changed', '最近变化'), <When value={shown.last_changed_at} />],
              [t('Last confirmed', '最近确认'), <When value={shown.verified_at ?? shown.observed_at} />],
            ]}
          />

          <TechDetails
            items={[
              [t('Resource ID', '资源 ID'), <Mono>{shown.resource_id}</Mono>],
              [t('Type', '类型'), <Mono>{shown.resource_type}</Mono>],
              [t('Observed at', '观测时间'), <When value={shown.observed_at} />],
            ]}
          >
            {Object.keys(shown.relations).length ? (
              <pre className={css({ ...theme.typography.MonoParagraphXSmall, whiteSpace: 'pre-wrap' })}>{JSON.stringify(shown.relations, null, 2)}</pre>
            ) : null}
            <pre
              className={css({
                ...theme.typography.MonoParagraphXSmall,
                backgroundColor: theme.colors.backgroundSecondary,
                padding: theme.sizing.scale500,
                borderRadius: theme.borders.radius300,
                overflow: 'auto',
                maxHeight: '420px',
              })}
            >
              {JSON.stringify(shown.data ?? null, null, 2)}
            </pre>
          </TechDetails>
        </div>
      ) : !detail.loading && !detail.error && arn ? (
        <Empty>{errorText('NOT_FOUND', lang)}</Empty>
      ) : null}
    </Drawer>
  );
}
