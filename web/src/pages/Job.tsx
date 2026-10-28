import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { ProgressBar } from 'baseui/progress-bar';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { toaster } from 'baseui/toast';
import { useEffect, useState, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import { TERMINAL_JOB, type ScanJob, type ScanScope, type ScanTask } from '../api/types';
import {
  AppLink,
  Copyable,
  ErrorNotice,
  KeyValues,
  Loading,
  Mono,
  PageTitle,
  ScopeTag,
  Panel,
  Secondary,
  Stat,
  StatusTag,
  startedBy,
  TechDetails,
  TypeName,
  When,
  scopeText,
  tableOverrides,
} from '../components/common';
import { useI18n } from '../i18n';
import { errorText } from '../i18n/labels';

const POLL_MS = 2000;
// scheduled runs have thousands of tasks: only small jobs list them
const TASK_LIST_MAX = 200;

export function JobPage() {
  const [css, theme] = useStyletron();
  const { jobId = '' } = useParams();
  const navigate = useNavigate();
  const { t, lang } = useI18n();
  const [job, setJob] = useState<ScanJob | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const poll = async () => {
      try {
        // the size is known before the (unpaged) task list is requested
        let next = await api.scan(jobId, false);
        const total = next.stats.tasks_total ?? 0;
        if (total > 0 && total <= TASK_LIST_MAX) next = await api.scan(jobId, true);
        if (stopped) return;
        setJob(next);
        setError(null);
        failures = 0;
        if (!TERMINAL_JOB.has(next.status)) timer = setTimeout(poll, POLL_MS);
      } catch (failure) {
        if (stopped) return;
        setError(failure);
        failures += 1;
        const notFound = (failure as { status?: number }).status === 404;
        if (!notFound) timer = setTimeout(poll, Math.min(POLL_MS * 2 ** failures, 30000));
      }
    };
    setJob(null);
    setError(null);
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [jobId]);

  if (!job) return error ? <ErrorNotice error={error} /> : <Loading />;
  const total = job.stats.tasks_total ?? 0;
  const done = job.stats.tasks_done ?? 0;
  const finished = TERMINAL_JOB.has(job.status);

  const rerun = async () => {
    setBusy(true);
    setActionError(null);
    try {
      const scope = job.scope;
      const next =
        scope.arn || scope.arns
          ? (await api.refresh(scope.arns ?? [scope.arn as string], 0)).job
          : await api.startScan({ account_id: scope.account_id, region: scope.region, resource_type: scope.resource_type });
      if (next.deduplicated) toaster.info(t('The same scan is already running; showing it.', '相同范围的扫描正在进行，已为你打开。'), {});
      navigate(`/scans/${encodeURIComponent(next.job_id)}`);
    } catch (failure) {
      setActionError(failure);
    } finally {
      setBusy(false);
    }
  };

  const failedTasks = (job.tasks ?? []).filter((x) => x.error_code);

  return (
    <>
      <PageTitle
        title={scopeText(job.scope, lang)}
        subtitle={
          <>
            {t('Scan ID', '扫描 ID')} <Mono>{job.job_id}</Mono> · {t('Started by', '发起人')}: {startedBy(job, lang)} · <When value={job.created_at} />
          </>
        }
        actions={
          <div className={css({ display: 'flex', gap: theme.sizing.scale300, flexWrap: 'wrap' })}>
            {!job.scope.all ? (
              <Button size={SIZE.compact} kind={KIND.secondary} isLoading={busy} onClick={() => void rerun()}>
                {t('Run again', '再次扫描')}
              </Button>
            ) : null}
            {job.scope.account_id ? (
              <Button size={SIZE.compact} kind={KIND.tertiary} onClick={() => navigate(`/accounts/${encodeURIComponent(String(job.scope.account_id))}`)}>
                {t('View account', '查看账号')}
              </Button>
            ) : null}
          </div>
        }
      />
      <ErrorNotice error={actionError} />
      <ErrorNotice error={error} />
      <Panel title={t('Scope', '范围')}>
        <ScopeDetails scope={job.scope} />
      </Panel>
      <Panel
        title={
          <span className={css({ display: 'inline-flex', alignItems: 'center', gap: theme.sizing.scale300 })}>
            {t('Progress', '进度')} <StatusTag status={job.status} />
          </span>
        }
      >
        <ProgressBar
          value={total ? (done / total) * 100 : finished ? 100 : 0}
          showLabel
          getProgressLabel={() => (total ? t(`${done} of ${total} parts done`, `已完成 ${done}/${total} 个部分`) : t('Waiting to start', '等待开始'))}
        />
        {job.error ? <ErrorText code={job.error.split(':')[0]} /> : null}
        <div
          className={css({
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
            gap: theme.sizing.scale500,
            marginTop: theme.sizing.scale600,
          })}
        >
          <Stat label={t('Added', '新增')} value={job.stats.created ?? 0} />
          <Stat label={t('Updated', '更新')} value={job.stats.updated ?? 0} />
          <Stat label={t('Removed', '删除')} value={job.stats.deleted ?? 0} />
          <Stat label={t('Unchanged', '无变化')} value={job.stats.unchanged ?? 0} />
        </div>
      </Panel>
      {job.tasks ? (
        <Panel title={t('Regions and types', '区域与类型')}>
          <div className={css({ overflowX: 'auto' })}>
            <TableBuilder data={job.tasks} emptyMessage={t('Nothing to scan', '没有需要扫描的内容')} overrides={tableOverrides}>
              <TableBuilderColumn<ScanTask> header={t('Region', '区域')}>
                {(x) => (
                  <>
                    {x.region}
                    <Secondary>
                      <AppLink to={`/accounts/${encodeURIComponent(x.account_id)}`}>{x.account_id}</AppLink>
                    </Secondary>
                  </>
                )}
              </TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header={t('Status', '状态')}>{(x) => <StatusTag status={x.status} />}</TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header={t('By type', '按类型')}>
                {(x) =>
                  Object.keys(x.result).length ? (
                    <div className={css({ display: 'grid', rowGap: theme.sizing.scale200 })}>
                      {Object.entries(x.result).map(([type, r]) => (
                        <div key={type} className={css({ display: 'flex', alignItems: 'center', gap: theme.sizing.scale300, flexWrap: 'wrap' })}>
                          <TypeName type={type} />
                          <StatusTag status={r.status} />
                          {r.status === 'succeeded' ? (
                            <Secondary>{t(`+${r.created ?? 0} · ~${r.updated ?? 0} · −${r.deleted ?? 0}`, `新增 ${r.created ?? 0} · 更新 ${r.updated ?? 0} · 删除 ${r.deleted ?? 0}`)}</Secondary>
                          ) : r.error_code ? (
                            <Secondary>{errorText(r.error_code, lang)}</Secondary>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : x.resource_types ? (
                    x.resource_types.map((type) => <TypeName key={type} type={type} />)
                  ) : (
                    <Secondary>{t('All types', '全部类型')}</Secondary>
                  )
                }
              </TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header={t('Problem', '问题')}>{(x) => (x.error_code ? errorText(x.error_code, lang) : '—')}</TableBuilderColumn>
            </TableBuilder>
          </div>
        </Panel>
      ) : total > TASK_LIST_MAX ? (
        <Panel>{t(`${total.toLocaleString()} parts: too many to list here.`, `共 ${total.toLocaleString()} 个部分，数量太多不在此列出。`)}</Panel>
      ) : null}
      <Panel>
        <TechDetails
          items={[
            ['request_id', job.request_id ? <Mono>{job.request_id}</Mono> : '—'],
            [t('Origin', '来源'), job.trigger],
            [t('Started', '开始'), <When value={job.started_at} />],
            [t('Finished', '完成'), <When value={job.finished_at} />],
            [t('Error', '错误'), job.error ?? '—'],
          ]}
        >
          {failedTasks.length ? (
            <KeyValues items={failedTasks.map((x) => [`${x.region} (${x.attempts}×)`, <Mono>{`${x.error_code}: ${x.error_message ?? ''}`}</Mono>])} />
          ) : null}
        </TechDetails>
      </Panel>
    </>
  );
}

function ScopeDetails({ scope }: { scope: ScanScope }) {
  const [css, theme] = useStyletron();
  const { t } = useI18n();
  const arns = scope.arns ?? (scope.arn ? [scope.arn] : []);
  const items: Array<[string, ReactNode]> = [[t('Level', '层级'), <ScopeTag scope={scope} />]];
  if (scope.all) items.push([t('Accounts', '账号'), t('All accounts with access (scheduled)', '全部有权限的账号（定时扫描）')]);
  if (scope.account_id) items.push([t('Account', '账号'), <AppLink to={`/accounts/${encodeURIComponent(scope.account_id)}`}>{scope.account_id}</AppLink>]);
  if (!arns.length && !scope.all) {
    items.push([t('Region', '区域'), scope.region ?? t('All enabled regions', '全部已启用区域')]);
    items.push([t('Type', '类型'), scope.resource_type ? <TypeName type={scope.resource_type} /> : t('All types', '全部类型')]);
  }
  if (arns.length)
    items.push([
      t('Resources', '资源'),
      <div className={css({ display: 'grid', rowGap: theme.sizing.scale100 })}>
        {arns.map((arn) => (
          <span key={arn} className={css({ display: 'inline-flex', gap: theme.sizing.scale300, alignItems: 'flex-start', flexWrap: 'wrap' })}>
            <Copyable value={arn} />
            <AppLink to={`/resources?include_deleted=true&arn=${encodeURIComponent(arn)}`}>{t('View', '查看')}</AppLink>
          </span>
        ))}
      </div>,
    ]);
  return <KeyValues items={items} />;
}

function ErrorText({ code }: { code: string }) {
  const [css, theme] = useStyletron();
  const { lang } = useI18n();
  return <div className={css({ ...theme.typography.ParagraphSmall, color: theme.colors.contentNegative, marginTop: theme.sizing.scale300 })}>{errorText(code, lang)}</div>;
}
