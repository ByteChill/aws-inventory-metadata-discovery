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
import { ErrorNotice, Mono, PageTitle, Panel, StatusTag, formatTime } from '../components/common';
import { useAsync } from '../components/useAsync';

function keyState(key: ApiKey): string {
  if (key.revoked_at) return 'revoked';
  if (key.expires_at && new Date(key.expires_at) <= new Date()) return 'expired';
  return 'active';
}

export function ApiKeysPage() {
  const [css, theme] = useStyletron();
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
        title="API keys"
        subtitle="Keys let other services call the API (header X-API-Key). Each key has its own roles."
        actions={
          <Button size={SIZE.compact} onClick={() => setCreating(true)}>
            Create key
          </Button>
        }
      />
      {created ? (
        <Notification kind={NOTIFICATION_KIND.positive} overrides={{ Body: { style: { width: 'auto' } } }}>
          <div className={css({ marginBottom: theme.sizing.scale300 })}>
            Key <strong>{created.key.name}</strong> created. Copy it now: it is not shown again.
          </div>
          <Mono>{created.secret}</Mono>
          <div className={css({ marginTop: theme.sizing.scale300 })}>
            <Button size={SIZE.mini} kind={KIND.secondary} onClick={() => void navigator.clipboard?.writeText(created.secret)}>
              Copy
            </Button>{' '}
            <Button size={SIZE.mini} kind={KIND.tertiary} onClick={() => setCreated(null)}>
              Done
            </Button>
          </div>
        </Notification>
      ) : null}
      <ErrorNotice error={error} />
      <ErrorNotice error={keys.error} />
      <Panel>
        <div className={css({ overflowX: 'auto' })}>
          <TableBuilder data={keys.data?.items ?? []} isLoading={keys.loading} emptyMessage="No API keys yet">
            <TableBuilderColumn<ApiKey> header="Name">{(k) => k.name}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="Key ID">{(k) => <Mono>{k.key_id}</Mono>}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="Roles">{(k) => k.roles.join(', ')}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="State">{(k) => <StatusTag status={keyState(k)} />}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="Created">{(k) => `${formatTime(k.created_at)} by ${k.created_by}`}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="Expires">{(k) => formatTime(k.expires_at)}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="Last used">{(k) => formatTime(k.last_used_at)}</TableBuilderColumn>
            <TableBuilderColumn<ApiKey> header="">
              {(k) =>
                keyState(k) === 'revoked' ? null : (
                  <Button size={SIZE.mini} kind={KIND.secondary} onClick={() => setRevoking(k)}>
                    Revoke
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
        <ModalHeader>Revoke API key?</ModalHeader>
        <ModalBody>
          Calls with <strong>{revoking?.name}</strong> will be rejected from now on. This cannot be undone.
        </ModalBody>
        <ModalFooter>
          <ModalButton kind={KIND.tertiary} onClick={() => setRevoking(null)}>
            Cancel
          </ModalButton>
          <ModalButton onClick={() => void revoke()}>Revoke</ModalButton>
        </ModalFooter>
      </Modal>
    </>
  );
}

function CreateKeyModal({ isOpen, onClose, onCreated }: { isOpen: boolean; onClose: () => void; onCreated: (k: ApiKeyCreated) => void }) {
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
      <ModalHeader>Create API key</ModalHeader>
      <ModalBody>
        <FormControl label="Name" caption="Which service uses it, e.g. cmdb-sync">
          <Input value={name} onChange={(e) => setName(e.currentTarget.value)} />
        </FormControl>
        <FormControl label="Roles">
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
        <FormControl label="Expires on (optional)">
          <Input type="date" value={expires} onChange={(e) => setExpires(e.currentTarget.value)} />
        </FormControl>
        <ErrorNotice error={error} />
      </ModalBody>
      <ModalFooter>
        <ModalButton kind={KIND.tertiary} onClick={onClose}>
          Cancel
        </ModalButton>
        <ModalButton disabled={!name.trim() || !roles.length} isLoading={busy} onClick={() => void submit()}>
          Create
        </ModalButton>
      </ModalFooter>
    </Modal>
  );
}
