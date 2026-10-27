import { useStyletron } from 'baseui';
import { AppNavBar, type NavItem } from 'baseui/app-nav-bar';
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';

interface Props {
  dark: boolean;
  onToggleTheme: () => void;
  children: ReactNode;
}

export function Layout({ dark, onToggleTheme, children }: Props) {
  const [css, theme] = useStyletron();
  const navigate = useNavigate();
  const location = useLocation();
  const { me, can, signOut } = useAuth();

  const mainItems: NavItem[] = [
    { label: 'Dashboard', info: '/' },
    { label: 'Resources', info: '/resources' },
    { label: 'Scans', info: '/scans' },
    // keys can only be managed with authentication enforced (spec 10 §2.2)
    ...(can('api_keys:manage') && me?.auth_mode === 'enforced' ? [{ label: 'API keys', info: '/api-keys' }] : []),
  ];
  const userItems: NavItem[] = [
    { label: dark ? 'Light theme' : 'Dark theme', info: 'theme' },
    { label: 'API docs', info: 'docs' },
    ...(me?.auth_mode === 'enforced' ? [{ label: 'Sign out', info: 'sign-out' }] : []),
  ];
  const active = (item: NavItem) =>
    item.info === '/' ? location.pathname === '/' : location.pathname.startsWith(String(item.info));

  return (
    <div className={css({ minHeight: '100vh', backgroundColor: theme.colors.backgroundSecondary })}>
      <AppNavBar
        title="AWS Inventory"
        mainItems={mainItems.map((item) => ({ ...item, active: active(item) }))}
        isMainItemActive={active}
        onMainItemSelect={(item) => navigate(String(item.info))}
        username={me?.subject ?? ''}
        usernameSubtitle={me ? me.roles.join(', ') || 'no role' : undefined}
        userItems={userItems}
        onUserItemSelect={(item) => {
          if (item.info === 'theme') onToggleTheme();
          else if (item.info === 'docs') window.open('/docs', '_blank', 'noopener');
          else if (item.info === 'sign-out') signOut();
        }}
      />
      <main
        className={css({
          maxWidth: '1360px',
          margin: '0 auto',
          padding: `${theme.sizing.scale800} ${theme.sizing.scale600}`,
          [theme.mediaQuery.small]: { padding: theme.sizing.scale800 },
        })}
      >
        {children}
      </main>
    </div>
  );
}
