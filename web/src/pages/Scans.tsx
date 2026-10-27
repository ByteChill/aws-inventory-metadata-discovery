import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { FormControl } from 'baseui/form-control';
import { Input } from 'baseui/input';
import { Modal, ModalBody, ModalButton, ModalFooter, ModalHeader, ROLE } from 'baseui/modal';
import { Radio, RadioGroup } from 'baseui/radio';
import { Select, type Value } from 'baseui/select';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { toaster } from 'baseui/toast';
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { api } from '../api/client';
import { JOB_STATUSES, type ScanJob, type ScanScope } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { AppLink, ErrorNotice, Mono, PageTitle, Panel, StatusTag, formatTime, scopeText, shortType } from '../components/common';
import { useAsync } from '../components/useAsync';

type Level = 'account' | 'region' | 'type' | 'arn';

export function ScansPage() {
  const [css, theme] = useStyletron();
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState<{ status?: string; trigger?: string; account_id?: string }>({});
  const [draft, setDraft] = useState(query);
  const key = JSON.stringify(query);
  const currentKey = useRef(key);
  currentKey.current = key;
  const first = useAsync(() => api.scans(query), [key]);
  const [items, setItems] = useState<ScanJob[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [more, setMore] = useState<{ loading: boolean; error: unknown }>({ loading: false, error: null });
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

  // the cursor belongs to the query it was issued for: "Load more" reuses `query`, and a page
  // that arrives after the filters changed is dropped
  const loadMore = async () => {
    if (!cursor) return;
    const issuedFor = key;
    setMore({ loading: true, error: null });
    try {
      const page = await api.scans(query, cursor);
      if (currentKey.current !== issuedFor) return;
      setItems((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      setMore({ loading: false, error: null });
    } catch (error) {
      if (currentKey.current === issuedFor) setMore({ loading: false, error });
    }
  };

  const modalOpen = params.get('new') === '1' && can('scans:write');
  const closeModal = () => setParams(new URLSearchParams());

  return (
    <>
      <PageTitle
        title="Scans"
        subtitle="Scan jobs from the schedule and from people or services, with their progress."
        actions={
          can('scans:write') ? (
            <Button size={SIZE.compact} onClick={() => setParams(new URLSearchParams({ new: '1' }))}>
              New scan
            </Button>
          ) : null
        }
      />
      <Panel>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setQuery(draft);
          }}
          className={css({
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            columnGap: theme.sizing.scale500,
            alignItems: 'end',
          })}
        >
          <div>
          <FormControl label="Status">
            <Select
              size={SIZE.compact}
              options={JOB_STATUSES.map((s) => ({ id: s, label: s }))}
              value={draft.status ? [{ id: draft.status, label: draft.status }] : []}
              placeholder="Any status"
              onChange={({ value }) => setDraft({ ...draft, status: value[0] ? String(value[0].id) : undefined })}
            />
          </FormControl>
          </div>
          <div>
          <FormControl label="Trigger">
            <Select
              size={SIZE.compact}
              options={[
                { id: 'api', label: 'api' },
                { id: 'schedule', label: 'schedule' },
              ]}
              value={draft.trigger ? [{ id: draft.trigger, label: draft.trigger }] : []}
              placeholder="Any trigger"
              onChange={({ value }) => setDraft({ ...draft, trigger: value[0] ? String(value[0].id) : undefined })}
            />
          </FormControl>
          </div>
          <div>
          <FormControl label="Account">
            <Input
              size={SIZE.compact}
              value={draft.account_id ?? ''}
              onChange={(e) => setDraft({ ...draft, account_id: e.currentTarget.value.trim() || undefined })}
            />
          </FormControl>
          </div>
          <div className={css({ marginBottom: theme.sizing.scale600 })}>
            <Button size={SIZE.compact} type="submit">
              Filter
            </Button>
          </div>
        </form>
      </Panel>
      <Panel title="Jobs">
        <ErrorNotice error={first.error} />
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder data={items} isLoading={first.loading} emptyMessage="No jobs match">
            <TableBuilderColumn<ScanJob> header="Job">
              {(job) => (
                <AppLink to={`/scans/${encodeURIComponent(job.job_id)}`}>
                  <Mono>{job.job_id}</Mono>
                </AppLink>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Scope">{(job) => scopeText(job.scope)}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Status">{(job) => <StatusTag status={job.status} />}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Tasks" numeric>
              {(job) => (job.stats.tasks_total != null ? `${job.stats.tasks_done ?? 0} / ${job.stats.tasks_total}` : '—')}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Requested by">{(job) => job.requested_by ?? job.trigger}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Created">{(job) => formatTime(job.created_at)}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header="Finished">{(job) => formatTime(job.finished_at)}</TableBuilderColumn>
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
      <NewScanModal isOpen={modalOpen} onClose={closeModal} initial={params} />
    </>
  );
}

function NewScanModal({ isOpen, onClose, initial }: { isOpen: boolean; onClose: () => void; initial: URLSearchParams }) {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const types = useAsync(() => api.resourceTypes(), []);
  const [level, setLevel] = useState<Level>('account');
  const [form, setForm] = useState({ account_id: '', region: '', resource_type: '', arn: '' });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    const next = {
      account_id: initial.get('account_id') ?? '',
      region: initial.get('region') ?? '',
      resource_type: initial.get('resource_type') ?? '',
      arn: initial.get('arn') ?? '',
    };
    setForm(next);
    setLevel(next.arn ? 'arn' : next.resource_type ? 'type' : next.region ? 'region' : 'account');
    setError(null);
  }, [isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  const allTypes = types.data?.items ?? [];
  const regions = ['global', ...new Set(allTypes.flatMap((t) => t.enabled_regions))].filter((r, i, a) => a.indexOf(r) === i);
  const typeOptions = allTypes
    .filter((t) => !form.region || t.enabled_regions.includes(form.region))
    .map((t) => ({ id: t.resource_type, label: shortType(t.resource_type) }));
  const pick = (value: string): Value => (value ? [{ id: value, label: value.startsWith('AWS::') ? shortType(value) : value }] : []);

  const scope: ScanScope =
    level === 'arn'
      ? { arn: form.arn.trim() }
      : {
          account_id: form.account_id.trim(),
          region: level === 'region' || level === 'type' ? form.region || undefined : undefined,
          resource_type: level === 'type' ? form.resource_type || undefined : undefined,
        };
  const missing =
    level === 'arn'
      ? !scope.arn && 'ARN'
      : !scope.account_id
        ? 'Account ID'
        : level === 'region' && !scope.region
          ? 'Region'
          : level === 'type' && !scope.resource_type
            ? 'Resource type'
            : null;

  const submit = async () => {
    if (missing) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.startScan(scope);
      if (job.deduplicated) toaster.info('A job with the same scope is already running; showing it.', {});
      onClose();
      navigate(`/scans/${encodeURIComponent(job.job_id)}`);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} role={ROLE.dialog} size="default">
      <ModalHeader>New scan</ModalHeader>
      <ModalBody>
        <RadioGroup value={level} onChange={(e) => setLevel(e.currentTarget.value as Level)} align="horizontal">
          <Radio value="account">Account</Radio>
          <Radio value="region">Account + region</Radio>
          <Radio value="type">Account + type</Radio>
          <Radio value="arn">Single ARN</Radio>
        </RadioGroup>
        <div className={css({ marginTop: theme.sizing.scale500 })}>
          {level === 'arn' ? (
            <div>
            <FormControl label="ARN">
              <Input value={form.arn} onChange={(e) => setForm({ ...form, arn: e.currentTarget.value })} placeholder="arn:aws:ec2:eu-west-1:123456789012:vpc/vpc-0abc" />
            </FormControl>
            </div>
          ) : (
            <div>
            <FormControl label="Account ID">
              <Input value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.currentTarget.value })} placeholder="123456789012" />
            </FormControl>
            </div>
          )}
          {level === 'region' || level === 'type' ? (
            <div>
            <FormControl
              label="Region"
              caption={level === 'type' ? 'Leave empty to scan every region the account has enabled' : undefined}
            >
              <Select
                options={regions.map((r) => ({ id: r, label: r }))}
                value={pick(form.region)}
                placeholder={level === 'type' ? 'All enabled regions' : 'Select a region'}
                onChange={({ value }) => setForm({ ...form, region: String(value[0]?.id ?? '') })}
              />
            </FormControl>
            </div>
          ) : null}
          {level === 'type' ? (
            <div>
            <FormControl label="Resource type">
              <Select
                options={typeOptions}
                value={pick(form.resource_type)}
                placeholder="Select a type"
                onChange={({ value }) => setForm({ ...form, resource_type: String(value[0]?.id ?? '') })}
              />
            </FormControl>
            </div>
          ) : null}
        </div>
        <ErrorNotice error={error} />
      </ModalBody>
      <ModalFooter>
        <ModalButton kind={KIND.tertiary} onClick={onClose}>
          Cancel
        </ModalButton>
        <ModalButton disabled={Boolean(missing)} isLoading={busy} onClick={() => void submit()}>
          {missing ? `${missing} is required` : 'Start scan'}
        </ModalButton>
      </ModalFooter>
    </Modal>
  );
}
