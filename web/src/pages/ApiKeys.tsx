import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { Checkbox } from 'baseui/checkbox';
import { FormControl } from 'baseui/form-control';
import { Input } from 'baseui/input';
import { Modal, ModalBody, ModalButton, ModalFooter, ModalHeader, ROLE } from 'baseui/modal';
import { KIND as NOTIFICATION_KIND, Notification } from 'baseui/notification';
import { TableBuilder, TableBuilderColumn } from 'baseui/table-semantic';
import { useState } from 'react';

import { api } from '../api/client';
import { ROLES, type ApiKey, type ApiKeyCreated, type RoleName } from '../api/types';
import { ErrorNotice, Mono, PageTitle, Panel, StatusTag, When, tableOverrides } from '../components/common';
import { useAsync } from '../components/useAsync';
import { useI18n } from '../i18n';

function keyState(key: ApiKey): string {
  if (key.revoked_at) return 'revoked';
  if (key.expires_at && new Date(key.expires_at) <= new Date()) return 'expired';
  return 'active';
}

export function ApiKeysPage() {
  const [css, theme] = useStyletron();
  const { t } = useI18n();
  const keys = useAsync(() => api.apiKeys(), []);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

  const revoke = async () => {
    if (!revoking) return;
    setError(null);
    try {
      await api.revokeApiKey(revoking.key_id);
      if (created?.key.key_id === revoking.key_id) setCreated(null);
      keys.reload();
    } catch (failure) {
      setError(failure);
    } finally {
      setRevoking(null);
    }
  };

  return (
    <>
      <PageTitle
        title={t('API keys', 'API 密钥')}
        subtitle={t('Keys let other services call the API (header X-API-Key). Each key has its own roles.', '供其他服务调用 API 使用（请求头 X-API-Key），每个密钥有自己的角色。')}
        actions={
          <Button size={SIZE.compact} onClick={() => setCreating(true)}>
            {t('Create key', '创建密钥')}
          </Button>
        }
      />
      {created ? (
        <Notification kind={NOTIFICATION_KIND.positive} overrides={{ Body: { style: { width: 'auto' } } }}>
          <div className={css({ marginBottom: theme.sizing.scale300 })}>
            {t('Key created. Copy it now: it is not shown again.', '密钥已创建，请立即复制：之后不会再显示。')} <strong>{created.key.name}</strong>
          </div>
          <Mono>{created.secret}</Mono>
          <div className={css({ marginTop: theme.sizing.scale300 })}>
            <Button size={SIZE.mini} kind={KIND.secondary} onClick={() => void navigator.clipboard?.writeText(created.secret)}>
              {t('Copy', '复制')}
            </Button>{' '}
            <Button size={SIZE.mini} kind={KIND.tertiary} onClick={() => setCreated(null)}>
              {t('Done', '完成')}
            </Button>
          </div>
        </Notification>
      ) : null}
      <ErrorNotice error={error} />
      <ErrorNotice error={keys.error} />
      <Panel>
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder data={keys.data?.items ?? []} isLoading={keys.loading} emptyMessage={t('No API keys yet', '还没有 API 密钥')} overrides={tableOverrides}>
            <TableBuilderColumn<ApiKey> header={t('Name', '名称')}>{(k) => k.name}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header={t('Key ID', '密钥 ID')}>{(k) => <Mono>{k.key_id}</Mono>}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header={t('Roles', '角色')}>{(k) => k.roles.join(', ')}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header={t('State', '状态')}>{(k) => <StatusTag status={keyState(k)} />}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header={t('Created', '创建')}>{(k) => (<><When value={k.created_at} /> · {k.created_by}</>)}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header={t('Expires', '过期时间')}>{(k) => <When value={k.expires_at} />}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header={t('Last used', '最近使用')}>{(k) => <When value={k.last_used_at} />}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="">
              {(k) =>
                keyState(k) === 'revoked' ? null : (
                  <Button size={SIZE.mini} kind={KIND.secondary} onClick={() => setRevoking(k)}>
                    {t('Revoke', '撤销')}
                  </Button>
                )
              }
            </TableBuilderColumn>
          </TableBuilder>
        </div>
      </Panel>
      <CreateKeyModal
        isOpen={creating}
        onClose={() => setCreating(false)}
        onCreated={(result) => {
          setCreated(result);
          setCreating(false);
          keys.reload();
        }}
      />
      <Modal isOpen={Boolean(revoking)} onClose={() => setRevoking(null)} role={ROLE.alertdialog}>
        <ModalHeader>{t('Revoke API key?', '撤销 API 密钥？')}</ModalHeader>
        <ModalBody>
          <strong>{revoking?.name}</strong>: {t('calls with this key will be rejected from now on. This cannot be undone.', '使用该密钥的调用将被拒绝，且无法恢复。')}
        </ModalBody>
        <ModalFooter>
          <ModalButton kind={KIND.tertiary} onClick={() => setRevoking(null)}>
            {t('Cancel', '取消')}
          </ModalButton>
          <ModalButton onClick={() => void revoke()}>{t('Revoke', '撤销')}</ModalButton>
        </ModalFooter>
      </Modal>
    </>
  );
}

function CreateKeyModal({ isOpen, onClose, onCreated }: { isOpen: boolean; onClose: () => void; onCreated: (k: ApiKeyCreated) => void }) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [roles, setRoles] = useState<RoleName[]>(['viewer']);
  const [expires, setExpires] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const expiresAt = expires ? new Date(`${expires}T23:59:59`).toISOString() : null;
      onCreated(await api.createApiKey(name.trim(), roles, expiresAt));
      setName('');
      setRoles(['viewer']);
      setExpires('');
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} role={ROLE.dialog}>
      <ModalHeader>{t('Create API key', '创建 API 密钥')}</ModalHeader>
      <ModalBody>
        <FormControl label={t('Name', '名称')} caption={t('Which service uses it, e.g. cmdb-sync', '使用它的服务，例如 cmdb-sync')}>
          <Input value={name} onChange={(e) => setName(e.currentTarget.value)} />
        </FormControl>
        <FormControl label={t('Roles', '角色')}>
          <div>
            {ROLES.map((role) => (
              <Checkbox
                key={role}
                checked={roles.includes(role)}
                onChange={(e) =>
                  setRoles(e.currentTarget.checked ? [...roles, role] : roles.filter((r) => r !== role))
                }
              >
                {role}
              </Checkbox>
            ))}
          </div>
        </FormControl>
        <FormControl label={t('Expires on (optional)', '过期日期（可选）')}>
          <Input type="date" value={expires} onChange={(e) => setExpires(e.currentTarget.value)} />
        </FormControl>
        <ErrorNotice error={error} />
      </ModalBody>
      <ModalFooter>
        <ModalButton kind={KIND.tertiary} onClick={onClose}>
          Cancel
        </ModalButton>
        <ModalButton disabled={!name.trim() || !roles.length} isLoading={busy} onClick={() => void submit()}>
          {t('Create', '创建')}
        </ModalButton>
      </ModalFooter>
    </Modal>
  );
}
