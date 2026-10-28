import { api } from '../api/client';
import type { Account } from '../api/types';
import { useAsync } from './useAsync';

/** "payments-dev (123456789012)", or just the ID when the directory gives no name. */
export function accountLabel(accountId: string, name: string | null | undefined): string {
  return name ? `${name} (${accountId})` : accountId;
}

/** The account directory, keyed by account ID (a few thousand rows, loaded once per page). */
export function useAccounts() {
  const state = useAsync(() => api.accounts().then((page) => page.items), []);
  const items: Account[] = state.data ?? [];
  const byId = new Map(items.map((a) => [a.account_id, a]));
  const eims = [...new Set(items.map((a) => a.eim).filter((e): e is string => Boolean(e)))].sort();
  return { ...state, items, byId, eims };
}
