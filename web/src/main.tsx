import { BaseProvider, DarkTheme, LightTheme } from 'baseui';
import { ToasterContainer } from 'baseui/toast';
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { Client as Styletron } from 'styletron-engine-monolithic';
import { Provider as StyletronProvider } from 'styletron-react';

import { App } from './App';
import { AuthProvider } from './auth/AuthContext';

const engine = new Styletron();
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
  const theme = dark ? DarkTheme : LightTheme;
  useEffect(() => {
    document.body.style.margin = '0';
    document.body.style.backgroundColor = theme.colors.backgroundSecondary;
    document.body.style.color = theme.colors.contentPrimary;
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
          <HashRouter>
            <AuthProvider>
              <App dark={dark} onToggleTheme={toggle} />
            </AuthProvider>
          </HashRouter>
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
