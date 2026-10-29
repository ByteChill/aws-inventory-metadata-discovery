import { BaseProvider, DarkTheme, LightTheme, type Theme } from 'baseui';
import { ToasterContainer } from 'baseui/toast';
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { Client as Styletron } from 'styletron-engine-monolithic';
import { Provider as StyletronProvider } from 'styletron-react';

import { App } from './App';
import { AuthProvider } from './auth/AuthContext';
import { I18nProvider } from './i18n';

const engine = new Styletron();

// baseui names Uber Move first, which is not shipped; name the system faces outright, with
// CJK faces before the generic fallback so Chinese never lands on a serif default (SimSun)
const SANS = 'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif';
const MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace';

function withSystemFonts(base: Theme): Theme {
  const typography = Object.fromEntries(
    Object.entries(base.typography).map(([role, style]) => [
      role,
      typeof style === 'object' && style && 'fontFamily' in style
        ? { ...style, fontFamily: String(style.fontFamily).includes('Mono') ? MONO : SANS }
        : style,
    ]),
  );
  return { ...base, typography: typography as Theme['typography'] };
}
const LIGHT = withSystemFonts(LightTheme);
const DARK = withSystemFonts(DarkTheme);
const THEME_KEY = 'inventory.theme';

function initialDark(): boolean {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved) return saved === 'dark';
  } catch {
    // storage unavailable: follow the system preference
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

function Root() {
  const [dark, setDark] = useState(initialDark);
  const theme = dark ? DARK : LIGHT;
  useEffect(() => {
    document.body.style.margin = '0';
    document.body.style.backgroundColor = theme.colors.backgroundSecondary;
    document.body.style.color = theme.colors.contentPrimary;
    // layers (popovers, drawers) render under <body>: give it the theme font too
    document.body.style.fontFamily = String(theme.typography.ParagraphMedium.fontFamily);
  }, [theme]);
  const toggle = () =>
    setDark((value) => {
      try {
        localStorage.setItem(THEME_KEY, value ? 'light' : 'dark');
      } catch {
        // not persisted
      }
      return !value;
    });
  return (
    <StyletronProvider value={engine}>
      <BaseProvider theme={theme}>
        <ToasterContainer autoHideDuration={5000} placement="bottomRight">
          <I18nProvider>
            <HashRouter>
              <AuthProvider>
                <App dark={dark} onToggleTheme={toggle} />
              </AuthProvider>
            </HashRouter>
          </I18nProvider>
        </ToasterContainer>
      </BaseProvider>
    </StyletronProvider>
  );
}

const container = document.getElementById('root');
if (container) {
  createRoot(container).render(
    <StrictMode>
      <Root />
    </StrictMode>,
  );
}
