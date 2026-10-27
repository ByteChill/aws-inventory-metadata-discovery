import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { ProgressBar } from 'baseui/progress-bar';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { toaster } from 'baseui/toast';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import { TERMINAL_JOB, type ScanJob, type ScanTask } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { AppLink, ErrorNotice, KeyValues, Loading, Mono, PageTitle, Panel, StatusTag, formatTime, scopeText, shortType } from '../components/common';

const POLL_MS = 2000;
// scheduled runs have thousands of tasks: only small jobs list them
const TASK_LIST_MAX = 200;

export function JobPage() {
  const [css, theme] = useStyletron();
  const { jobId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useAuth();
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

  const rerun = async () => {
    setBusy(true);
    setActionError(null);
    try {
      const scope = job.scope;
      const next =
        scope.arn || scope.arns
          ? (await api.refresh(scope.arns ?? [scope.arn as string], 0)).job
          : await api.startScan({ account_id: scope.account_id, region: scope.region, resource_type: scope.resource_type });
      if (next.deduplicated) toaster.info('A job with the same scope is already running; showing it.', {});
      navigate(`/scans/${encodeURIComponent(next.job_id)}`);
    } catch (failure) {
      setActionError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageTitle
        title={
          <span>
            Scan job <Mono>{job.job_id}</Mono>
          </span>
        }
        subtitle={scopeText(job.scope)}
        actions={
          <div className={css({ display: 'flex', gap: theme.sizing.scale300, flexWrap: 'wrap' })}>
            {can('scans:write') && !job.scope.all ? (
              <Button size={SIZE.compact} kind={KIND.secondary} isLoading={busy} onClick={() => void rerun()}>
                Scan the same scope again
              </Button>
            ) : null}
            {job.scope.account_id ? (
              <Button size={SIZE.compact} kind={KIND.tertiary} onClick={() => navigate(`/accounts/${encodeURIComponent(String(job.scope.account_id))}`)}>
                Account coverage
              </Button>
            ) : null}
          </div>
        }
      />
      <ErrorNotice error={actionError} />
      <ErrorNotice error={error} />
      <Panel title={<span>Progress <StatusTag status={job.status} /></span>}>
        <ProgressBar value={total ? (done / total) * 100 : TERMINAL_JOB.has(job.status) ? 100 : 0} showLabel getProgressLabel={() => `${done} of ${total} tasks done`} />
        <div className={css({ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: theme.sizing.scale600, marginTop: theme.sizing.scale600 })}>
          <KeyValues
            items={[
              ['Trigger', job.trigger],
              ['Requested by', job.requested_by ?? '—'],
              ['request_id', job.request_id ? <Mono>{job.request_id}</Mono> : '—'],
              ['Error', job.error ?? '—'],
            ]}
          />
          <KeyValues
            items={[
              ['Created', formatTime(job.created_at)],
              ['Started', formatTime(job.started_at)],
              ['Finished', formatTime(job.finished_at)],
            ]}
          />
          <KeyValues
            items={(['created', 'updated', 'deleted', 'unchanged'] as const).map((k) => [k, String(job.stats[k] ?? 0)])}
          />
        </div>
      </Panel>
      {job.tasks ? (
        <Panel title={`Tasks (${job.tasks.length})`}>
          <div className={css({ overflowX: 'auto' })}>
            <TableBuilder data={job.tasks} emptyMessage="No tasks">
              <TableBuilderColumn<ScanTask> header="Region">{(t) => t.region}</TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header="Account">{(t) => <AppLink to={`/accounts/${encodeURIComponent(t.account_id)}`}><Mono>{t.account_id}</Mono></AppLink>}</TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header="Types">{(t) => (t.resource_types ? t.resource_types.map(shortType).join(', ') : 'all')}</TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header="Status">{(t) => <StatusTag status={t.status} />}</TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header="Attempts" numeric>{(t) => t.attempts}</TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header="Results">
                {(t) => (
                  <div className={css({ display: 'grid', rowGap: theme.sizing.scale100 })}>
                    {Object.entries(t.result).map(([type, r]) => (
                      <span key={type} className={css({ ...theme.typography.ParagraphXSmall })}>
                        {shortType(type)}: {r.status}
                        {r.status === 'succeeded' ? ` (+${r.created ?? 0} ~${r.updated ?? 0} -${r.deleted ?? 0})` : r.error_code ? ` — ${r.error_code}` : ''}
                      </span>
                    ))}
                  </div>
                )}
              </TableBuilderColumn>
              <TableBuilderColumn<ScanTask> header="Error">{(t) => (t.error_code ? `${t.error_code}: ${t.error_message ?? ''}` : '—')}</TableBuilderColumn>
            </TableBuilder>
          </div>
        </Panel>
      ) : total > TASK_LIST_MAX ? (
        <Panel>{`${total.toLocaleString()} tasks: too many to list here.`}</Panel>
      ) : null}
    </>
  );
}
