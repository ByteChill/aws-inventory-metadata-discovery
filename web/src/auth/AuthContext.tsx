import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { api, configureAuth } from '../api/client';
import type { Me, Permission } from '../api/types';

// The bearer token will come from the company SSO. Until that is wired in, a token can be
// pasted on the sign-in page (kept for this browser tab only). Only getToken/setToken change
// when SSO is integrated.
const TOKEN_KEY = 'inventory.token';

function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function writeToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage unavailable (private mode): the token lives in memory only
  }
}

type AuthState =
  | { status: 'loading' }
  | { status: 'signed-in'; me: Me }
  | { status: 'signed-out'; error: unknown };

interface AuthValue {
  state: AuthState;
  me: Me | null;
  can: (permission: Permission) => boolean;
  signIn: (token: string) => Promise<void>;
  signOut: () => void;
  retry: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading' });
  const [token, setTokenState] = useState<string | null>(readToken);
  const [attempt, setAttempt] = useState(0);

  const load = useCallback(async () => {
    try {
      setState({ status: 'signed-in', me: await api.me() });
    } catch (error) {
      setState({ status: 'signed-out', error });
    }
  }, []);

  useEffect(() => {
    // any 401 (expired or revoked token) sends the user back to the sign-in page
    configureAuth(
      () => token,
      () => setState((current) => (current.status === 'signed-out' ? current : { status: 'signed-out', error: null })),
    );
    void load();
  }, [token, attempt, load]);

  const value = useMemo<AuthValue>(() => {
    const me = state.status === 'signed-in' ? state.me : null;
    return {
      state,
      me,
      can: (permission) => Boolean(me?.permissions.includes(permission)),
      signIn: async (next) => {
        writeToken(next);
        setState({ status: 'loading' });
        setTokenState(next);
        setAttempt((n) => n + 1); // reload even when the same token is pasted again
      },
      signOut: () => {
        writeToken(null);
        setTokenState(null);
      },
      retry: () => {
        setState({ status: 'loading' });
        setAttempt((n) => n + 1);
      },
    };
  }, [state]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth outside AuthProvider');
  return value;
}
