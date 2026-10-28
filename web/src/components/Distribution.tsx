import { useStyletron } from 'baseui';
import { Button, SIZE } from 'baseui/button';
import { ButtonGroup, MODE } from 'baseui/button-group';
import { StatefulTooltip } from 'baseui/tooltip';
import { useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import { ACCOUNT_TYPES } from '../api/types';
import { useI18n } from '../i18n';
import { accountLabel, useAccounts } from './accounts';
import { Empty, ErrorNotice, Loading, TypeName } from './common';
import { useAsync } from './useAsync';

type Dimension = 'eim' | 'account_id' | 'account_type' | 'resource_type' | 'region';
const DIMENSIONS: Dimension[] = ['eim', 'account_id', 'account_type', 'resource_type', 'region'];
const TOP = 10;
const NONE = 'none'; // accounts not in the account directory have no type

// categorical slots 1-3 of the validated reference palette (dataviz skill), in fixed order;
// colour follows the account type, so it never changes with the dimension shown
const TYPE_COLORS: Record<string, { light: string; dark: string }> = {
  dev: { light: '#2a78d6', dark: '#3987e5' },
  preprod: { light: '#eb6834', dark: '#d95926' },
  prod: { light: '#1baf7a', dark: '#199e70' },
  [NONE]: { light: '#b4b2ab', dark: '#5f5e5a' },
};
const SEGMENTS = [...ACCOUNT_TYPES, NONE];

interface Row {
  key: string | null; // null: no EIM (account not in the directory), or the folded "Other"
  other?: boolean;
  folded?: number; // how many keys "Other" stands for
  total: number;
  parts: Record<string, number>;
}

function toRows(groups: Array<Record<string, string | number | null>>, dimension: Dimension): Row[] {
  const byKey = new Map<string, Row>();
  for (const g of groups) {
    const raw = g[dimension];
    const id = raw == null ? '' : String(raw);
    const row = byKey.get(id) ?? { key: raw == null ? null : String(raw), total: 0, parts: {} };
    const type = g.account_type == null ? NONE : String(g.account_type);
    row.parts[type] = (row.parts[type] ?? 0) + Number(g.count);
    row.total += Number(g.count);
    byKey.set(id, row);
  }
  const rows = [...byKey.values()].sort((a, b) => b.total - a.total);
  if (rows.length <= TOP + 1) return rows;
  const rest = rows.slice(TOP);
  const other: Row = { key: null, other: true, folded: rest.length, total: 0, parts: {} };
  for (const r of rest) {
    other.total += r.total;
    for (const [type, n] of Object.entries(r.parts)) other.parts[type] = (other.parts[type] ?? 0) + n;
  }
  return [...rows.slice(0, TOP), other];
}

/** Resources by one dimension at a time, each bar split by account type. */
export function Distribution() {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const { t } = useI18n();
  const directory = useAccounts();
  const [dimension, setDimension] = useState<Dimension>('eim');
  const stats = useAsync(
    () => api.stats(dimension === 'account_type' ? 'account_type' : `${dimension},account_type`),
    [dimension],
  );
  const dark = theme.name?.includes('dark') ?? false;
  const color = (type: string) => TYPE_COLORS[type]?.[dark ? 'dark' : 'light'] ?? TYPE_COLORS[NONE].light;

  const names: Record<Dimension, string> = {
    eim: 'EIM',
    account_id: t('Account', '账号'),
    account_type: t('Account type', '账号类型'),
    resource_type: t('Resource type', '资源类型'),
    region: t('Region', '区域'),
  };
  const typeName = (type: string) => (type === NONE ? t('Not in directory', '不在账号目录中') : type);
  const rows = stats.data ? toRows(stats.data.groups, dimension) : [];
  const max = Math.max(1, ...rows.map((r) => r.total));
  const present = SEGMENTS.filter((s) => rows.some((r) => r.parts[s]));

  const label = (row: Row): ReactNode => {
    if (row.other) return t(`Other (${row.folded})`, `其他（${row.folded} 项）`);
    if (row.key === null) return dimension === 'eim' ? t('No EIM', '无 EIM') : '—';
    if (dimension === 'account_id') return accountLabel(row.key, directory.byId.get(row.key)?.name);
    if (dimension === 'resource_type') return <TypeName type={row.key} />;
    return row.key;
  };
  const open = (row: Row, type?: string) => {
    if (row.other || row.key === null) return;
    const query = new URLSearchParams({ [dimension]: row.key });
    if (type && type !== NONE && dimension !== 'account_type') query.set('account_type', type);
    navigate(`/resources?${query.toString()}`);
  };

  return (
    <div>
      <div
        className={css({
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: theme.sizing.scale400,
          marginBottom: theme.sizing.scale600,
        })}
      >
        <ButtonGroup
          mode={MODE.radio}
          size={SIZE.compact}
          selected={DIMENSIONS.indexOf(dimension)}
          onClick={(_, index) => setDimension(DIMENSIONS[index])}
        >
          {DIMENSIONS.map((d) => (
            <Button key={d}>{names[d]}</Button>
          ))}
        </ButtonGroup>
        <div className={css({ display: 'flex', gap: theme.sizing.scale500, flexWrap: 'wrap', ...theme.typography.ParagraphXSmall, color: theme.colors.contentSecondary })}>
          {present.map((s) => (
            <span key={s} className={css({ display: 'inline-flex', alignItems: 'center', gap: theme.sizing.scale200 })}>
              <span className={css({ width: '10px', height: '10px', borderRadius: '2px', backgroundColor: color(s) })} />
              {typeName(s)}
              <span className={css({ fontVariantNumeric: 'tabular-nums', color: theme.colors.contentPrimary })}>
                {rows.reduce((n, r) => n + (r.parts[s] ?? 0), 0).toLocaleString()}
              </span>
            </span>
          ))}
        </div>
      </div>
      <ErrorNotice error={stats.error} />
      {stats.loading && !stats.data ? <Loading /> : null}
      {stats.data && !rows.length ? <Empty>{t('No data yet', '暂无数据')}</Empty> : null}
      <div className={css({ display: 'grid', rowGap: theme.sizing.scale200 })}>
        {rows.map((row) => {
          const clickable = !row.other && row.key !== null;
          return (
            <div
              key={row.other ? '__other' : (row.key ?? '__none')}
              className={css({
                display: 'grid',
                gridTemplateColumns: 'minmax(0, 15rem) 1fr 4rem',
                alignItems: 'center',
                columnGap: theme.sizing.scale500,
                minHeight: '28px',
                ...theme.typography.ParagraphSmall,
                color: theme.colors.contentPrimary,
              })}
            >
              <button
                type="button"
                disabled={!clickable}
                onClick={() => open(row)}
                className={css({
                  all: 'unset',
                  cursor: clickable ? 'pointer' : 'default',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  color: row.other || row.key === null ? theme.colors.contentSecondary : theme.colors.contentPrimary,
                  ':hover': clickable ? { color: theme.colors.contentAccent } : {},
                })}
              >
                {label(row)}
              </button>
              <div className={css({ display: 'flex', gap: '2px', height: '14px' })}>
                {SEGMENTS.filter((s) => row.parts[s]).map((s, i, shown) => (
                  <StatefulTooltip
                    key={s}
                    showArrow
                    content={() => (
                      <span>
                        {typeName(s)} · {row.parts[s].toLocaleString()} ({Math.round((row.parts[s] / row.total) * 100)}%)
                      </span>
                    )}
                  >
                    <button
                      type="button"
                      aria-label={`${typeName(s)}: ${row.parts[s]}`}
                      onClick={() => open(row, s)}
                      className={css({
                        all: 'unset',
                        cursor: clickable ? 'pointer' : 'default',
                        width: `${(row.parts[s] / max) * 100}%`,
                        minWidth: '3px',
                        height: '100%',
                        backgroundColor: color(s),
                        // square at the baseline, rounded at the data end
                        borderRadius: i === shown.length - 1 ? '0 4px 4px 0' : 0,
                        ':hover': { opacity: 0.8 },
                      })}
                    />
                  </StatefulTooltip>
                ))}
              </div>
              <span className={css({ textAlign: 'right', fontVariantNumeric: 'tabular-nums' })}>{row.total.toLocaleString()}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
