import { useStyletron, type Theme } from 'baseui';
import { Accordion, Panel as AccordionPanel } from 'baseui/accordion';
import { KIND as NOTIFICATION_KIND, Notification } from 'baseui/notification';
import { Spinner } from 'baseui/spinner';
import { HIERARCHY, KIND as TAG_KIND, Tag } from 'baseui/tag';
import { HeadingXSmall, LabelSmall, ParagraphSmall } from 'baseui/typography';
import { useEffect, useState, type ReactNode } from 'react';
import { Link, type LinkProps } from 'react-router-dom';

import { ApiError } from '../api/client';
import type { ScanScope } from '../api/types';
import { formatTime, useI18n, type Lang } from '../i18n';
import { errorText, serviceOf, statusLabel, typeLabel } from '../i18n/labels';

// API data (names, tags, data) comes from scanned accounts: it is only ever rendered as
// React text children, never as HTML.

const STATUS_KIND: Record<string, (typeof TAG_KIND)[keyof typeof TAG_KIND]> = {
  succeeded: TAG_KIND.green,
  created: TAG_KIND.green,
  updated: TAG_KIND.green,
  active: TAG_KIND.green,
  partial: TAG_KIND.yellow,
  deleted: TAG_KIND.yellow,
  not_found: TAG_KIND.yellow,
  expired: TAG_KIND.yellow,
  retrying: TAG_KIND.blue,
  running: TAG_KIND.blue,
  pending: TAG_KIND.blue,
  queued: TAG_KIND.gray,
  unchanged: TAG_KIND.gray,
  never: TAG_KIND.gray,
  failed: TAG_KIND.red,
  timed_out: TAG_KIND.red,
  auth_denied: TAG_KIND.red,
  error: TAG_KIND.red,
  unsupported_type: TAG_KIND.red,
  revoked: TAG_KIND.red,
};

export function StatusTag({ status }: { status: string }) {
  const { lang } = useI18n();
  return (
    <Tag
      closeable={false}
      kind={STATUS_KIND[status] ?? TAG_KIND.gray}
      hierarchy={HIERARCHY.secondary}
      size="small"
      overrides={{ Root: { style: { marginTop: 0, marginBottom: 0, marginLeft: 0 } } }}
    >
      {statusLabel(status, lang)}
    </Tag>
  );
}

/** Keyboard focus for controls styled with `all: unset` (which also removes the browser's
 * focus ring): spread into the control's style. Mouse clicks do not show it. */
export function focusRing(theme: Theme) {
  return {
    ':focus-visible': {
      outline: `2px solid ${theme.colors.borderSelected}`,
      outlineOffset: '2px',
    },
  };
}

/** Table cells centred vertically, so tags line up with the text next to them. */
export const tableOverrides = {
  TableBodyCell: { style: { verticalAlign: 'middle', paddingLeft: '12px', paddingRight: '12px' } },
  // headers are labels for the column, quieter than the data under them
  TableHeadCell: {
    style: ({ $theme }: { $theme: Theme }) => ({
      verticalAlign: 'middle',
      paddingLeft: '12px',
      paddingRight: '12px',
      whiteSpace: 'nowrap',
      ...$theme.typography.LabelSmall,
      color: $theme.colors.contentSecondary,
    }),
  },
  // on phones the wrapper scrolls sideways instead of squeezing words letter by letter
  Root: { style: { width: '100%' } },
  Table: { style: { width: '100%', minWidth: '640px' } },
};

export function ErrorNotice({ error }: { error: unknown }) {
  const { lang, t } = useI18n();
  if (!error) return null;
  const known = error instanceof ApiError ? error : null;
  return (
    <Notification kind={NOTIFICATION_KIND.negative} overrides={{ Body: { style: { width: 'auto' } } }}>
      <div>{known ? errorText(known.code, lang) : t('Could not reach the server', '无法连接服务器')}</div>
      {known?.message && known.code === 'VALIDATION_ERROR' ? <div>{known.message}</div> : null}
      <TechDetails
        items={[
          [t('Code', '错误码'), known?.code ?? '—'],
          [t('Message', '信息'), known ? known.message : String(error)],
          ['HTTP', known ? String(known.status) : '—'],
          ['request_id', known?.requestId ? <Mono>{known.requestId}</Mono> : '—'],
        ]}
      />
    </Notification>
  );
}

/** Identifiers and raw values for troubleshooting, collapsed by default. */
export function TechDetails({ items, children }: { items?: Array<[string, ReactNode]>; children?: ReactNode }) {
  const { t } = useI18n();
  return (
    <Accordion
      overrides={{
        Header: { style: { paddingLeft: 0, paddingRight: 0, paddingTop: '8px', paddingBottom: '8px', backgroundColor: 'transparent' } },
        Content: { style: { paddingLeft: 0, paddingRight: 0, backgroundColor: 'transparent' } },
        PanelContainer: { style: { borderBottomWidth: 0 } },
      }}
    >
      <AccordionPanel title={t('Technical details', '技术详情')}>
        {items ? <KeyValues items={items} /> : null}
        {children}
      </AccordionPanel>
    </Accordion>
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
        // blocks sit further apart than the things inside them
        marginBottom: theme.sizing.scale800,
        minWidth: 0,
        [theme.mediaQuery.medium]: { padding: theme.sizing.scale700 },
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
          {title ? <HeadingXSmall margin={0} $style={{ textWrap: 'balance' }}>{title}</HeadingXSmall> : <span />}
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
        marginBottom: theme.sizing.scale800,
      })}
    >
      <div className={css({ minWidth: 0 })}>
        <h1 className={css({ ...theme.typography.HeadingMedium, color: theme.colors.contentPrimary, margin: 0, wordBreak: 'break-word', textWrap: 'balance' })}>
          {title}
        </h1>
        {subtitle ? (
          <ParagraphSmall marginTop="scale200" marginBottom={0} color="contentSecondary">
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

/** A value people paste elsewhere (an ARN), with a copy button. `truncate` keeps it on one
 * line (tables); the full value is in the tooltip and on the clipboard. */
export function Copyable({ value, truncate = false }: { value: string; truncate?: boolean }) {
  const [css, theme] = useStyletron();
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const copy = async (event: { stopPropagation: () => void }) => {
    event.stopPropagation();
    await navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <span className={css({ display: 'inline-flex', alignItems: 'flex-start', gap: theme.sizing.scale200, maxWidth: '100%' })}>
      {truncate ? (
        <span title={value} className={css({ ...theme.typography.MonoParagraphXSmall, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 })}>
          {value}
        </span>
      ) : (
        <Mono>{value}</Mono>
      )}
      <button
        type="button"
        onClick={(event) => void copy(event)}
        title={copied ? t('Copied', '已复制') : t('Copy', '复制')}
        aria-label={copied ? t('Copied', '已复制') : t('Copy', '复制')}
        className={css({
          all: 'unset',
          cursor: 'pointer',
          flexShrink: 0,
          display: 'inline-flex',
          // the hit area is larger than the 14px mark
          padding: '3px',
          margin: '-3px 0',
          borderRadius: theme.borders.radius200,
          color: copied ? theme.colors.contentPositive : theme.colors.contentTertiary,
          ':hover': { color: copied ? theme.colors.contentPositive : theme.colors.contentAccent },
          ...focusRing(theme),
        })}
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          {copied ? (
            <path d="M3 8.5l3 3 7-7" />
          ) : (
            <>
              <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
              <path d="M10.5 5.5V3.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" />
            </>
          )}
        </svg>
      </button>
    </span>
  );
}

/** Secondary text under a primary value (an ID under a name, a code under a label). */
export function Secondary({ children }: { children: ReactNode }) {
  const [css, theme] = useStyletron();
  return (
    <div className={css({ ...theme.typography.ParagraphXSmall, color: theme.colors.contentTertiary, wordBreak: 'break-all' })}>
      {children}
    </div>
  );
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
          <dd className={css({ ...theme.typography.ParagraphSmall, color: theme.colors.contentPrimary, margin: 0, wordBreak: 'break-word' })}>
            {value ?? '—'}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export interface Fact {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
}

/** A row of headline figures in one band, divided by hairlines rather than boxed one by
 * one. `framed` draws the band's own border; inside a Panel leave it off. */
export function Facts({ items, framed = true }: { items: Fact[]; framed?: boolean }) {
  const [css, theme] = useStyletron();
  return (
    <div
      className={css({
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
        backgroundColor: theme.colors.backgroundPrimary,
        border: framed ? `1px solid ${theme.colors.borderOpaque}` : 'none',
        borderRadius: theme.borders.radius400,
        overflow: 'hidden',
        marginBottom: framed ? theme.sizing.scale800 : 0,
      })}
    >
      {items.map((item) => (
        <div
          key={item.label}
          className={css({
            // hairlines on each figure's right and bottom; the band clips the outer ones, and
            // an empty cell in a wrapped last row stays plain
            boxShadow: `1px 0 0 ${theme.colors.borderOpaque}, 0 1px 0 ${theme.colors.borderOpaque}`,
            paddingTop: theme.sizing.scale500,
            paddingBottom: theme.sizing.scale500,
            paddingLeft: theme.sizing.scale600,
            paddingRight: theme.sizing.scale600,
            minWidth: 0,
          })}
        >
          <LabelSmall color="contentSecondary">{item.label}</LabelSmall>
          <div className={css({ ...theme.typography.HeadingSmall, color: theme.colors.contentPrimary, fontVariantNumeric: 'tabular-nums', marginTop: theme.sizing.scale100 })}>
            {item.value}
          </div>
          {item.hint ? <LabelSmall color="contentTertiary">{item.hint}</LabelSmall> : null}
        </div>
      ))}
    </div>
  );
}

/** "2026-10-27 16:00" in local time, with the raw UTC value on hover. */
export function When({ value }: { value: string | null | undefined }) {
  if (!value) return <span>—</span>;
  return (
    <span title={value} style={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
      {formatTime(value)}
    </span>
  );
}

const SERVICE_COLORS: Record<string, string> = {
  EC2: '#E07B00',
  ECS: '#C7511F',
  S3: '#3F8624',
  // not the red that means "failed" elsewhere
  IAM: '#B0084D',
  Route53: '#8C4FFF',
};

/** A resource type as people name it, with a coloured service badge. */
export function TypeName({ type }: { type: string }) {
  const [css, theme] = useStyletron();
  const { lang } = useI18n();
  const service = serviceOf(type);
  return (
    <span className={css({ display: 'inline-flex', alignItems: 'center', gap: theme.sizing.scale300 })}>
      <span
        aria-hidden
        className={css({
          display: 'inline-block',
          minWidth: '34px',
          padding: '1px 4px',
          borderRadius: '4px',
          textAlign: 'center',
          fontSize: '11px',
          fontWeight: 700,
          lineHeight: '16px',
          color: '#fff',
          backgroundColor: SERVICE_COLORS[service] ?? theme.colors.contentTertiary,
        })}
      >
        {service === 'Route53' ? 'R53' : service}
      </span>
      <span>{typeLabel(type, lang)}</span>
    </span>
  );
}

/** A router link in the theme's accent colour (readable in light and dark themes). */
export function AppLink(props: LinkProps) {
  const [css, theme] = useStyletron();
  return (
    <Link
      {...props}
      className={css({
        color: theme.colors.contentAccent,
        textDecoration: 'none',
        ':hover': { textDecoration: 'underline' },
      })}
    />
  );
}

/** A scope as a sentence: "Account 1234…9012 · eu-west-1 · VPC". */
export type ScopeLevel = 'all' | 'account' | 'region' | 'type' | 'resource';

/** The narrowest level a scan covers; the job page shows the exact scope. */
export function scopeLevel(scope: ScanScope | null | undefined): ScopeLevel {
  if (!scope || scope.all) return 'all';
  if (scope.arn || scope.arns) return 'resource';
  if (scope.resource_type) return 'type';
  if (scope.region) return 'region';
  return 'account';
}

const LEVELS: Record<ScopeLevel, { kind: (typeof TAG_KIND)[keyof typeof TAG_KIND]; en: string; zh: string }> = {
  all: { kind: TAG_KIND.gray, en: 'All accounts', zh: '全部账号' },
  account: { kind: TAG_KIND.blue, en: 'Account', zh: '账号' },
  region: { kind: TAG_KIND.green, en: 'Region', zh: '区域' },
  type: { kind: TAG_KIND.purple, en: 'Type', zh: '类型' },
  resource: { kind: TAG_KIND.orange, en: 'Resource', zh: '资源' },
};

export function ScopeTag({ scope }: { scope: ScanScope | null | undefined }) {
  const { lang } = useI18n();
  const level = LEVELS[scopeLevel(scope)];
  return (
    <Tag
      closeable={false}
      kind={level.kind}
      hierarchy={HIERARCHY.secondary}
      size="small"
      overrides={{ Root: { style: { marginTop: 0, marginBottom: 0, marginLeft: 0 } } }}
    >
      {lang === 'zh' ? level.zh : level.en}
    </Tag>
  );
}

export function scopeText(scope: ScanScope | null | undefined, lang: Lang): string {
  const t = (en: string, zh: string) => (lang === 'zh' ? zh : en);
  if (!scope) return '—';
  if (scope.all) return t('All accounts (scheduled)', '全部账号（定时扫描）');
  if (scope.arn) return t('One resource', '单个资源');
  if (scope.arns) return t(`${scope.arns.length} resources`, `${scope.arns.length} 个资源`);
  const parts = [t(`Account ${scope.account_id}`, `账号 ${scope.account_id}`)];
  if (scope.region) parts.push(scope.region);
  if (scope.resource_type) parts.push(typeLabel(scope.resource_type, lang));
  if (!scope.region && !scope.resource_type) parts.push(t('all regions and types', '全部区域与类型'));
  return parts.join(' · ');
}

/** Who started a scan: a person (8-digit ID), a service key, the schedule, or unknown for
 * jobs recorded before requested_by existed. */
export function startedBy(job: { trigger: string; requested_by: string | null }, lang: Lang): string {
  const t = (en: string, zh: string) => (lang === 'zh' ? zh : en);
  if (job.trigger === 'schedule') return t('Schedule', '定时任务');
  if (!job.requested_by) return '—';
  if (job.requested_by === 'anonymous') return t('Local user', '本地用户');
  if (job.requested_by.startsWith('apikey:')) return t('Service (API key)', '服务（API 密钥）');
  return job.requested_by;
}

/** "+3 added · 1 updated · 2 deleted" from job or task counters. */
export function changeSummary(stats: Record<string, number | undefined>, lang: Lang): string {
  const t = (en: string, zh: string) => (lang === 'zh' ? zh : en);
  const parts = [
    stats.created ? t(`${stats.created} added`, `新增 ${stats.created}`) : '',
    stats.updated ? t(`${stats.updated} updated`, `更新 ${stats.updated}`) : '',
    stats.deleted ? t(`${stats.deleted} removed`, `删除 ${stats.deleted}`) : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : t('No changes', '无变化');
}

/** True below baseui's "small" breakpoint (phones); follows window resizes. */
export function useNarrow(): boolean {
  const query = '(max-width: 599px)';
  const [narrow, setNarrow] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const media = window.matchMedia?.(query);
    if (!media) return undefined;
    const update = () => setNarrow(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  return narrow;
}
