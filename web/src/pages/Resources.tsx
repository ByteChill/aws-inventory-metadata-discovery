import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { Checkbox } from 'baseui/checkbox';
import { Drawer } from 'baseui/drawer';
import { FormControl } from 'baseui/form-control';
import { Input } from 'baseui/input';
import { Select, type Value } from 'baseui/select';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { HIERARCHY, Tag } from 'baseui/tag';
import { toaster } from 'baseui/toast';
import { LabelMedium } from 'baseui/typography';
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { ApiError, api, type ResourceFilters } from '../api/client';
import type { Resource } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { AppLink, ErrorNotice, KeyValues, Loading, Mono, PageTitle, Panel, StatusTag, formatTime, shortType } from '../components/common';
import { useAsync } from '../components/useAsync';

const FILTER_KEYS = ['account_id', 'region', 'resource_type', 'name_prefix', 'tag'] as const;

function option(value: string | undefined, label?: string): Value {
  return value ? [{ id: value, label: label ?? value }] : [];
}

export function ResourcesPage() {
  const [css, theme] = useStyletron();
  const [params, setParams] = useSearchParams();
  const filters: ResourceFilters = {
    ...Object.fromEntries(FILTER_KEYS.map((k) => [k, params.get(k) ?? undefined])),
    include_deleted: params.get('include_deleted') === 'true',
  };
  const key = params.toString();
  const types = useAsync(() => api.resourceTypes(), []);
  const regions = [...new Set((types.data?.items ?? []).flatMap((t) => t.enabled_regions))].sort();

  const [draft, setDraft] = useState<Record<string, string>>({});
  useEffect(() => setDraft(Object.fromEntries(FILTER_KEYS.map((k) => [k, params.get(k) ?? '']))), [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const [items, setItems] = useState<Resource[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [more, setMore] = useState<{ loading: boolean; error: unknown }>({ loading: false, error: null });
  // the query a "Load more" belongs to: a page for an older query is dropped (its cursor is
  // bound to the old filters and would be rejected)
  const currentKey = useRef(key);
  currentKey.current = key;
  const first = useAsync(() => api.resources(filters), [key]);
  useEffect(() => {
    setItems([]);
    setCursor(null);
    setMore({ loading: false, error: null });
  }, [key]);
  useEffect(() => {
    if (first.data) {
      setItems(first.data.items);
      setCursor(first.data.next_cursor);
    }
  }, [first.data]);

  const loadMore = async () => {
    if (!cursor) return;
    const issuedFor = key;
    setMore({ loading: true, error: null });
    try {
      const page = await api.resources(filters, cursor);
      if (currentKey.current !== issuedFor) return;
      setItems((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      setMore({ loading: false, error: null });
    } catch (error) {
      if (currentKey.current === issuedFor) setMore({ loading: false, error });
    }
  };

  const apply = (next: Record<string, string | boolean>) => {
    const search = new URLSearchParams();
    for (const [k, v] of Object.entries(next)) if (v) search.set(k, String(v));
    setParams(search);
  };
  const [selected, setSelected] = useState<Resource | null>(null);

  return (
    <>
      <PageTitle title="Resources" subtitle="Everything the scanners found, as of their last scan." />
      <Panel>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            apply({ ...draft, include_deleted: filters.include_deleted ?? false });
          }}
          className={css({
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
            columnGap: theme.sizing.scale500,
            alignItems: 'end',
          })}
        >
          <div>
          <FormControl label="Account">
            <Input
              size={SIZE.compact}
              value={draft.account_id ?? ''}
              placeholder="123456789012"
              onChange={(e) => setDraft({ ...draft, account_id: e.currentTarget.value.trim() })}
            />
          </FormControl>
          </div>
          <div>
          <FormControl label="Region">
            <Select
              size={SIZE.compact}
              options={['global', ...regions].map((r) => ({ id: r, label: r }))}
              value={option(draft.region)}
              placeholder="Any region"
              onChange={({ value }) => setDraft({ ...draft, region: String(value[0]?.id ?? '') })}
            />
          </FormControl>
          </div>
          <div>
          <FormControl label="Resource type">
            <Select
              size={SIZE.compact}
              options={(types.data?.items ?? []).map((t) => ({ id: t.resource_type, label: shortType(t.resource_type) }))}
              value={option(draft.resource_type, draft.resource_type ? shortType(draft.resource_type) : undefined)}
              placeholder="Any type"
              onChange={({ value }) => setDraft({ ...draft, resource_type: String(value[0]?.id ?? '') })}
            />
          </FormControl>
          </div>
          <div>
          <FormControl label="Name starts with">
            <Input size={SIZE.compact} value={draft.name_prefix ?? ''} onChange={(e) => setDraft({ ...draft, name_prefix: e.currentTarget.value })} />
          </FormControl>
          </div>
          <div>
          <FormControl label="Tag" caption="Key=Value or Key">
            <Input size={SIZE.compact} value={draft.tag ?? ''} onChange={(e) => setDraft({ ...draft, tag: e.currentTarget.value })} />
          </FormControl>
          </div>
          <div className={css({ display: 'flex', gap: theme.sizing.scale300, alignItems: 'center', marginBottom: theme.sizing.scale600, flexWrap: 'wrap' })}>
            <Button size={SIZE.compact} type="submit">
              Filter
            </Button>
            <Button size={SIZE.compact} kind={KIND.tertiary} type="button" onClick={() => apply({})}>
              Reset
            </Button>
          </div>
        </form>
        <Checkbox
          checked={filters.include_deleted}
          onChange={(e) => apply({ ...Object.fromEntries(FILTER_KEYS.map((k) => [k, params.get(k) ?? ''])), include_deleted: e.currentTarget.checked })}
        >
          Include deleted resources
        </Checkbox>
      </Panel>

      <Panel title={first.data?.total != null ? `${first.data.total.toLocaleString()} resources` : 'Resources'}>
        <ErrorNotice error={first.error} />
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder
            data={items}
            isLoading={first.loading}
            emptyMessage="No resources match these filters"
            overrides={{ TableBodyRow: { style: { cursor: 'pointer' } } }}
          >
            <TableBuilderColumn<Resource> header="Name">
              {(r) => (
                <button type="button" onClick={() => setSelected(r)} className={css({ all: 'unset', cursor: 'pointer', color: theme.colors.contentAccent })}>
                  {r.name || r.resource_id}
                </button>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<Resource> header="Type">{(r) => shortType(r.resource_type)}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header="Account">{(r) => <Mono>{r.account_id}</Mono>}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header="Region">{(r) => r.region}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header="Last changed">{(r) => formatTime(r.last_changed_at)}</TableBuilderColumn>
            <TableBuilderColumn<Resource> header="State">
              {(r) => (r.deleted_at ? <StatusTag status="deleted" /> : <StatusTag status="active" />)}
            </TableBuilderColumn>
          </TableBuilder>
        </div>
        <ErrorNotice error={more.error} />
        {cursor && !first.loading ? (
          <div className={css({ marginTop: theme.sizing.scale600 })}>
            <Button size={SIZE.compact} kind={KIND.secondary} isLoading={more.loading} onClick={() => void loadMore()}>
              Load more
            </Button>
          </div>
        ) : null}
      </Panel>
      <ResourceDrawer resource={selected} onClose={() => setSelected(null)} onRefreshed={first.reload} />
    </>
  );
}

function ResourceDrawer({ resource, onClose, onRefreshed }: { resource: Resource | null; onClose: () => void; onRefreshed: () => void }) {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const { can } = useAuth();
  const detail = useAsync(() => (resource ? api.resourceByArn(resource.arn) : Promise.resolve(null)), [resource?.arn]);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => setError(null), [resource?.arn]);
  const shown = detail.data ?? resource;

  const refresh = async () => {
    if (!resource) return;
    setRefreshing(true);
    setError(null);
    try {
      const response = await api.refresh([resource.arn]);
      const result = response.results?.[0];
      if (result?.error) setError(new ApiError(0, result.error.code, result.error.message, response.job.request_id, null));
      else toaster.positive(`Refresh ${result?.status ?? 'queued'}`, {});
      detail.reload();
      onRefreshed();
    } catch (failure) {
      setError(failure);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <Drawer isOpen={Boolean(resource)} onClose={onClose} size="min(720px, 100vw)" anchor="right">
      {shown ? (
        <div>
          <LabelMedium>{shortType(shown.resource_type)}</LabelMedium>
          <h2 className={css({ ...theme.typography.HeadingSmall, marginTop: theme.sizing.scale200, wordBreak: 'break-all' })}>
            {shown.name || shown.resource_id}
          </h2>
          <div className={css({ display: 'flex', gap: theme.sizing.scale300, flexWrap: 'wrap', marginBottom: theme.sizing.scale600 })}>
            {can('scans:write') ? (
              <Button size={SIZE.compact} isLoading={refreshing} onClick={() => void refresh()}>
                Refresh from AWS
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
                Scan this type
              </Button>
            ) : null}
            <AppLink to={`/accounts/${encodeURIComponent(shown.account_id)}`}>Account coverage</AppLink>
          </div>
          <ErrorNotice error={error ?? detail.error} />
          <KeyValues
            items={[
              ['ARN', <Mono>{shown.arn}</Mono>],
              ['Resource ID', <Mono>{shown.resource_id}</Mono>],
              ['Account', shown.account_id],
              ['Region', shown.region],
              ['First seen', formatTime(shown.first_seen_at)],
              ['Last changed', formatTime(shown.last_changed_at)],
              ['Observed', formatTime(shown.observed_at)],
              ['Verified', formatTime(shown.verified_at)],
              ['Deleted', shown.deleted_at ? formatTime(shown.deleted_at) : '—'],
            ]}
          />
          <h3 className={css({ ...theme.typography.LabelLarge, marginTop: theme.sizing.scale700 })}>Tags</h3>
          <div>
            {Object.keys(shown.tags).length
              ? Object.entries(shown.tags).map(([k, v]) => (
                  <Tag key={k} closeable={false} hierarchy={HIERARCHY.secondary} kind="gray" size="small">
                    {k}={v}
                  </Tag>
                ))
              : '—'}
          </div>
          <h3 className={css({ ...theme.typography.LabelLarge, marginTop: theme.sizing.scale700 })}>Data</h3>
          {detail.loading && !detail.data ? (
            <Loading />
          ) : (
            <pre
              className={css({
                ...theme.typography.MonoParagraphXSmall,
                backgroundColor: theme.colors.backgroundSecondary,
                padding: theme.sizing.scale500,
                borderRadius: theme.borders.radius300,
                overflow: 'auto',
                maxHeight: '480px',
              })}
            >
              {JSON.stringify(detail.data?.data ?? null, null, 2)}
            </pre>
          )}
        </div>
      ) : null}
    </Drawer>
  );
}
