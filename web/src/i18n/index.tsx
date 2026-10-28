import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

// Strings live next to the code that uses them: t('English', '中文'). Two languages do not
// justify a key catalogue.
export type Lang = 'en' | 'zh';
const LANG_KEY = 'inventory.lang';

function initialLang(): Lang {
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved === 'en' || saved === 'zh') return saved;
  } catch {
    // storage unavailable: follow the browser
  }
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
}

interface I18n {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (en: string, zh: string) => string;
}

const I18nContext = createContext<I18n | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(initialLang);
  const setLang = useCallback((next: Lang) => {
    try {
      localStorage.setItem(LANG_KEY, next);
    } catch {
      // not persisted
    }
    document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en';
    setLangState(next);
  }, []);
  const value = useMemo<I18n>(() => ({ lang, setLang, t: (en, zh) => (lang === 'zh' ? zh : en) }), [lang, setLang]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  const value = useContext(I18nContext);
  if (!value) throw new Error('useI18n outside I18nProvider');
  return value;
}

const pad = (n: number) => String(n).padStart(2, '0');

/** "2026-10-27 16:00" in the viewer's time zone; ``null`` → "—". */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
