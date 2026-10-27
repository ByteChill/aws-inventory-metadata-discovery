import { useCallback, useEffect, useRef, useState } from 'react';

export interface AsyncState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** Runs ``load`` when ``deps`` change; responses from superseded runs are ignored. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const mine = ++seq.current;
    setLoading(true);
    load().then(
      (value) => {
        if (mine !== seq.current) return;
        setData(value);
        setError(null);
        setLoading(false);
      },
      (failure: unknown) => {
        if (mine !== seq.current) return;
        setError(failure);
        setLoading(false);
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
