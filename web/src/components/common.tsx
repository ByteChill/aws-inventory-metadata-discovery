import { useStyletron } from 'baseui';
import { KIND as NOTIFICATION_KIND, Notification } from 'baseui/notification';
import { Spinner } from 'baseui/spinner';
import { HIERARCHY, KIND as TAG_KIND, Tag } from 'baseui/tag';
import { HeadingSmall, LabelSmall, ParagraphSmall } from 'baseui/typography';
import type { ReactNode } from 'react';
import { Link, type LinkProps } from 'react-router-dom';

import { ApiError } from '../api/client';
import type { ScanScope } from '../api/types';

// API data (names, tags, data) comes from scanned accounts: it is only ever rendered as
// React text children, never as HTML.

const STATUS_KIND: Record<string, (typeof TAG_KIND)[keyof typeof TAG_KIND]> = {
  succeeded: TAG_KIND.green,
  created: TAG_KIND.green,
  updated: TAG_KIND.green,
  partial: TAG_KIND.yellow,
  deleted: TAG_KIND.yellow,
  not_found: TAG_KIND.yellow,
  never: TAG_KIND.yellow,
  retrying: TAG_KIND.blue,
  running: TAG_KIND.blue,
  pending: TAG_KIND.blue,
  queued: TAG_KIND.gray,
  unchanged: TAG_KIND.gray,
  failed: TAG_KIND.red,
  timed_out: TAG_KIND.red,
  auth_denied: TAG_KIND.red,
  error: TAG_KIND.red,
  unsupported_type: TAG_KIND.red,
  revoked: TAG_KIND.red,
  expired: TAG_KIND.yellow,
  active: TAG_KIND.green,
};

export function StatusTag({ status }: { status: string }) {
  return (
    <Tag closeable={false} kind={STATUS_KIND[status] ?? TAG_KIND.gray} hierarchy={HIERARCHY.secondary} size="small">
      {status.replace(/_/g, ' ')}
    </Tag>
  );
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const known = error instanceof ApiError ? error : null;
  return (
    <Notification kind={NOTIFICATION_KIND.negative} overrides={{ Body: { style: { width: 'auto' } } }}>
      <div>
        <strong>{known ? known.code : 'Error'}</strong>: {known ? known.message : String(error)}
      </div>
      {known?.details?.length ? (
        <ul>
          {known.details.map((d, i) => (
            <li key={i}>
              {(d.loc ?? []).join('.')}: {d.msg ?? ''}
            </li>
          ))}
        </ul>
      ) : null}
      {known ? (
        <LabelSmall color="contentSecondary">
          HTTP {known.status}
          {known.requestId ? ` · request_id ${known.requestId}` : ''}
        </LabelSmall>
      ) : null}
    </Notification>
  );
}

export function Panel({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  const [css, theme] = useStyletron();
  return (
    <section
      className={css({
        backgroundColor: theme.colors.backgroundPrimary,
        border: `1px solid ${theme.colors.borderOpaque}`,
        borderRadius: theme.borders.radius400,
        padding: theme.sizing.scale600,
        marginBottom: theme.sizing.scale600,
        minWidth: 0,
      })}
    >
      {title || actions ? (
        <div
          className={css({
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: theme.sizing.scale400,
            flexWrap: 'wrap',
            marginBottom: theme.sizing.scale500,
          })}
        >
          {title ? <HeadingSmall margin={0}>{title}</HeadingSmall> : <span />}
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function PageTitle({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  const [css, theme] = useStyletron();
  return (
    <div
      className={css({
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        gap: theme.sizing.scale600,
        flexWrap: 'wrap',
        marginBottom: theme.sizing.scale700,
      })}
    >
      <div>
        <h1 className={css({ ...theme.typography.HeadingMedium, color: theme.colors.contentPrimary, margin: 0 })}>{title}</h1>
        {subtitle ? (
          <ParagraphSmall margin={0} color="contentSecondary">
            {subtitle}
          </ParagraphSmall>
        ) : null}
      </div>
      {actions}
    </div>
  );
}

export function Loading() {
  const [css, theme] = useStyletron();
  return (
    <div className={css({ display: 'flex', justifyContent: 'center', padding: theme.sizing.scale900 })}>
      <Spinner $size="medium" />
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  const [css, theme] = useStyletron();
  return (
    <div className={css({ padding: theme.sizing.scale700, textAlign: 'center', color: theme.colors.contentSecondary })}>
      {children}
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  const [css, theme] = useStyletron();
  return <span className={css({ ...theme.typography.MonoParagraphXSmall, wordBreak: 'break-all' })}>{children}</span>;
}

export function KeyValues({ items }: { items: Array<[string, ReactNode]> }) {
  const [css, theme] = useStyletron();
  return (
    <dl
      className={css({
        display: 'grid',
        gridTemplateColumns: 'max-content 1fr',
        columnGap: theme.sizing.scale600,
        rowGap: theme.sizing.scale300,
        margin: 0,
      })}
    >
      {items.map(([label, value]) => (
        <div key={label} className={css({ display: 'contents' })}>
          <dt className={css({ ...theme.typography.LabelSmall, color: theme.colors.contentSecondary })}>{label}</dt>
          <dd className={css({ ...theme.typography.ParagraphSmall, margin: 0, wordBreak: 'break-word' })}>
            {value ?? '—'}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Horizontal bars, largest first: a dependency-free chart for counts. */
export function BarList({ rows, onSelect }: { rows: Array<{ label: string; value: number }>; onSelect?: (label: string) => void }) {
  const [css, theme] = useStyletron();
  const max = Math.max(1, ...rows.map((r) => r.value));
  if (!rows.length) return <Empty>No data yet</Empty>;
  return (
    <div className={css({ display: 'grid', rowGap: theme.sizing.scale300 })}>
      {rows.map((row) => (
        <button
          key={row.label}
          type="button"
          onClick={onSelect ? () => onSelect(row.label) : undefined}
          className={css({
            display: 'grid',
            gridTemplateColumns: 'minmax(0, 14rem) 1fr auto',
            alignItems: 'center',
            columnGap: theme.sizing.scale400,
            background: 'none',
            border: 'none',
            padding: 0,
            textAlign: 'left',
            color: theme.colors.contentPrimary,
            cursor: onSelect ? 'pointer' : 'default',
            ...theme.typography.ParagraphSmall,
          })}
        >
          <span className={css({ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' })} title={row.label}>
            {row.label}
          </span>
          <span className={css({ height: '10px', backgroundColor: theme.colors.backgroundTertiary, borderRadius: '5px' })}>
            <span
              className={css({
                display: 'block',
                height: '100%',
                width: `${(row.value / max) * 100}%`,
                minWidth: '2px',
                backgroundColor: theme.colors.accent,
                borderRadius: '5px',
              })}
            />
          </span>
          <span className={css({ fontVariantNumeric: 'tabular-nums' })}>{row.value.toLocaleString()}</span>
        </button>
      ))}
    </div>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  const [css, theme] = useStyletron();
  return (
    <div
      className={css({
        backgroundColor: theme.colors.backgroundPrimary,
        border: `1px solid ${theme.colors.borderOpaque}`,
        borderRadius: theme.borders.radius400,
        padding: theme.sizing.scale600,
      })}
    >
      <LabelSmall color="contentSecondary">{label}</LabelSmall>
      <div className={css({ ...theme.typography.DisplaySmall, color: theme.colors.contentPrimary, fontVariantNumeric: 'tabular-nums' })}>
        {value}
      </div>
      {hint ? <LabelSmall color="contentTertiary">{hint}</LabelSmall> : null}
    </div>
  );
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function shortType(resourceType: string): string {
  return resourceType.replace(/^AWS::/, '');
}

export function scopeText(scope: ScanScope | null | undefined): string {
  if (!scope) return '—';
  if (scope.all) return 'all accounts';
  if (scope.arn) return scope.arn;
  if (scope.arns) return scope.arns.length === 1 ? scope.arns[0] : `${scope.arns.length} ARNs`;
  return [scope.account_id, scope.region, scope.resource_type].filter(Boolean).join(' · ');
}

/** A router link in the theme's link colour (readable in light and dark themes). */
export function AppLink(props: LinkProps) {
  const [css, theme] = useStyletron();
  return (
    <Link
      {...props}
      className={css({
        color: theme.colors.contentAccent,
        textDecoration: 'none',
        ':hover': { textDecoration: 'underline', color: theme.colors.linkHover },
      })}
    />
  );
}
