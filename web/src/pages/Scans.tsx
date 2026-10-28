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
import {
  AppLink,
  ErrorNotice,
  Mono,
  PageTitle,
  Panel,
  Secondary,
  StatusTag,
  When,
  changeSummary,
  startedBy,
  ScopeTag,
  tableOverrides,
  useNarrow,
} from '../components/common';
import { Pager, useKeysetPages } from '../components/Pager';
import { useAsync } from '../components/useAsync';
import { useI18n } from '../i18n';
import { statusLabel, typeLabel } from '../i18n/labels';

type Level = 'account' | 'region' | 'type' | 'arn';
type Query = Record<'status' | 'trigger' | 'account_id', string | undefined>;

export function ScansPage() {
  const [css, theme] = useStyletron();
  const { can } = useAuth();
  const { t, lang } = useI18n();
  const narrow = useNarrow();
  const [params, setParams] = useSearchParams();
  const query: Query = {
    status: params.get('status') ?? undefined,
    trigger: params.get('trigger') ?? undefined,
    account_id: params.get('account_id') ?? undefined,
  };
  const key = JSON.stringify(query);
  const setFilter = (name: keyof Query, value: string) => {
    const next = new URLSearchParams(params);
    next.delete('new');
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  };
  const [account, setAccount] = useState(query.account_id ?? '');
  const timer = useRef<ReturnType<typeof setTimeout>>();

  const pages = useKeysetPages(key, (cursor, limit) => api.scans(query, cursor, limit));
  const first = pages.page;
  const items: ScanJob[] = first.data?.items ?? [];

  const modalOpen = params.get('new') === '1' && can('scans:write');
  const closeModal = () => {
    const next = new URLSearchParams(params);
    for (const k of ['new', 'region', 'resource_type', 'arn']) next.delete(k);
    if (!query.account_id) next.delete('account_id');
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageTitle
        title={t('Scans', '扫描')}
        subtitle={t('Scheduled scans and scans started by people or services.', '定时扫描，以及由用户或服务发起的扫描。')}
        actions={
          <Button size={SIZE.compact} onClick={() => setParams(new URLSearchParams({ new: '1' }))}>
            {t('New scan', '发起扫描')}
          </Button>
        }
      />
      <Panel>
        <div
          className={css({
            display: 'grid',
            gridTemplateColumns: narrow ? '1fr' : 'repeat(3, minmax(180px, 1fr))',
            gap: theme.sizing.scale400,
            alignItems: 'center',
          })}
        >
          <Select
            size={SIZE.compact}
            options={JOB_STATUSES.map((s) => ({ id: s, label: statusLabel(s, lang) }))}
            value={query.status ? [{ id: query.status, label: statusLabel(query.status, lang) }] : []}
            placeholder={t('Any status', '全部状态')}
            onChange={({ value }) => setFilter('status', String(value[0]?.id ?? ''))}
          />
          <Select
            size={SIZE.compact}
            options={[
              { id: 'api', label: t('Started by people or services', '由用户或服务发起') },
              { id: 'schedule', label: t('Scheduled', '定时扫描') },
            ]}
            value={query.trigger ? [{ id: query.trigger, label: query.trigger === 'api' ? t('Started by people or services', '由用户或服务发起') : t('Scheduled', '定时扫描') }] : []}
            placeholder={t('Any origin', '全部来源')}
            onChange={({ value }) => setFilter('trigger', String(value[0]?.id ?? ''))}
          />
          <Input
            size={SIZE.compact}
            value={account}
            placeholder={t('Account ID', '账号 ID')}
            clearable
            onChange={(e) => {
              const value = e.currentTarget.value;
              setAccount(value);
              clearTimeout(timer.current);
              timer.current = setTimeout(() => setFilter('account_id', value.trim()), 400);
            }}
          />
        </div>
      </Panel>
      <Panel>
        <ErrorNotice error={first.error} />
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder data={items} isLoading={first.loading} emptyMessage={t('No scans match', '没有符合条件的扫描')} overrides={tableOverrides}>
            <TableBuilderColumn<ScanJob> header={t('Scan ID', '扫描 ID')}>
              {(job) => (
                <AppLink to={`/scans/${encodeURIComponent(job.job_id)}`}>
                  <Mono>{job.job_id}</Mono>
                </AppLink>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Scope', '范围')}>{(job) => <ScopeTag scope={job.scope} />}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Status', '状态')}>
              {(job) => (
                <>
                  <StatusTag status={job.status} />
                  {job.stats.tasks_total && !job.finished_at ? (
                    <Secondary>{t(`${job.stats.tasks_done ?? 0} of ${job.stats.tasks_total} done`, `已完成 ${job.stats.tasks_done ?? 0}/${job.stats.tasks_total}`)}</Secondary>
                  ) : null}
                </>
              )}
            </TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Result', '结果')}>{(job) => (job.finished_at ? changeSummary(job.stats, lang) : '—')}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Started by', '发起人')}>{(job) => startedBy(job, lang)}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Started', '开始时间')}>{(job) => <When value={job.created_at} />}</TableBuilderColumn>
            <TableBuilderColumn<ScanJob> header={t('Finished', '完成时间')}>{(job) => <When value={job.finished_at} />}</TableBuilderColumn>
          </TableBuilder>
        </div>
        <Pager pages={pages} />
      </Panel>
      <NewScanModal isOpen={modalOpen} onClose={closeModal} initial={params} />
    </>
  );
}

function NewScanModal({ isOpen, onClose, initial }: { isOpen: boolean; onClose: () => void; initial: URLSearchParams }) {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const { t, lang } = useI18n();
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
  const regions = ['global', ...new Set(allTypes.flatMap((x) => x.enabled_regions))].filter((r, i, a) => a.indexOf(r) === i);
  const typeOptions = allTypes
    .filter((x) => !form.region || x.enabled_regions.includes(form.region))
    .map((x) => ({ id: x.resource_type, label: typeLabel(x.resource_type, lang) }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const regionValue: Value = form.region ? [{ id: form.region, label: form.region }] : [];
  const typeValue: Value = form.resource_type ? [{ id: form.resource_type, label: typeLabel(form.resource_type, lang) }] : [];

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
        ? t('Account ID', '账号 ID')
        : level === 'region' && !scope.region
          ? t('Region', '区域')
          : level === 'type' && !scope.resource_type
            ? t('Resource type', '资源类型')
            : null;

  const submit = async () => {
    if (missing) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.startScan(scope);
      if (job.deduplicated) toaster.info(t('The same scan is already running; showing it.', '相同范围的扫描正在进行，已为你打开。'), {});
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
      <ModalHeader>{t('New scan', '发起扫描')}</ModalHeader>
      <ModalBody>
        <FormControl label={t('What to scan', '扫描什么')}>
          <RadioGroup value={level} onChange={(e) => setLevel(e.currentTarget.value as Level)}>
            <Radio value="account">{t('A whole account', '整个账号')}</Radio>
            <Radio value="region">{t('One region of an account', '账号的某个区域')}</Radio>
            <Radio value="type">{t('One resource type of an account', '账号的某类资源')}</Radio>
            <Radio value="arn">{t('A single resource (ARN)', '单个资源（ARN）')}</Radio>
          </RadioGroup>
        </FormControl>
        <div className={css({ marginTop: theme.sizing.scale300 })}>
          {level === 'arn' ? (
            <FormControl label="ARN">
              <Input value={form.arn} onChange={(e) => setForm({ ...form, arn: e.currentTarget.value })} placeholder="arn:aws:ec2:eu-west-1:123456789012:vpc/vpc-0abc" />
            </FormControl>
          ) : (
            <FormControl label={t('Account ID', '账号 ID')} caption={t('12 digits', '12 位数字')}>
              <Input value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.currentTarget.value })} />
            </FormControl>
          )}
          {level === 'region' || level === 'type' ? (
            <FormControl
              label={t('Region', '区域')}
              caption={level === 'type' ? t('Leave empty to scan every region the account uses', '留空则扫描该账号启用的所有区域') : undefined}
            >
              <Select
                options={regions.map((r) => ({ id: r, label: r }))}
                value={regionValue}
                placeholder={level === 'type' ? t('All enabled regions', '所有启用的区域') : t('Select a region', '选择区域')}
                onChange={({ value }) => setForm({ ...form, region: String(value[0]?.id ?? '') })}
              />
            </FormControl>
          ) : null}
          {level === 'type' ? (
            <FormControl label={t('Resource type', '资源类型')}>
              <Select
                options={typeOptions}
                value={typeValue}
                placeholder={t('Select a type', '选择类型')}
                onChange={({ value }) => setForm({ ...form, resource_type: String(value[0]?.id ?? '') })}
              />
            </FormControl>
          ) : null}
        </div>
        <ErrorNotice error={error} />
      </ModalBody>
      <ModalFooter>
        <ModalButton kind={KIND.tertiary} onClick={onClose}>
          {t('Cancel', '取消')}
        </ModalButton>
        <ModalButton disabled={Boolean(missing)} isLoading={busy} onClick={() => void submit()}>
          {missing ? t(`Enter the ${missing.toLowerCase()}`, `请填写${missing}`) : t('Start scan', '开始扫描')}
        </ModalButton>
      </ModalFooter>
    </Modal>
  );
}
