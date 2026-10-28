import { useStyletron } from 'baseui';
import { AppNavBar, type NavItem } from 'baseui/app-nav-bar';
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { useI18n } from '../i18n';

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
  const { t, lang, setLang } = useI18n();

  // scans are for the people who run them; viewers only see the resources
  const mainItems: NavItem[] = [
    { label: t('Overview', '概览'), info: '/' },
    { label: t('Resources', '资源'), info: '/resources' },
    ...(can('scans:write') ? [{ label: t('Scans', '扫描'), info: '/scans' }] : []),
    // keys can only be managed with authentication enforced (spec 10 §2.2)
    ...(can('api_keys:manage') && me?.auth_mode === 'enforced' ? [{ label: t('API keys', 'API 密钥'), info: '/api-keys' }] : []),
  ];
  const userItems: NavItem[] = [
    { label: lang === 'zh' ? 'English' : '中文', info: 'lang' },
    { label: dark ? t('Light theme', '浅色主题') : t('Dark theme', '深色主题'), info: 'theme' },
    ...(can('scans:write') ? [{ label: t('API documentation', 'API 文档'), info: 'docs' }] : []),
    ...(me?.auth_mode === 'enforced' ? [{ label: t('Sign out', '退出登录'), info: 'sign-out' }] : []),
  ];
  const active = (item: NavItem) =>
    item.info === '/' ? location.pathname === '/' : location.pathname.startsWith(String(item.info));
  const roleNames: Record<string, string> = {
    viewer: t('Viewer', '查看者'),
    operator: t('Operator', '操作员'),
    admin: t('Administrator', '管理员'),
  };
  const topRole = (['admin', 'operator', 'viewer'] as const).find((r) => me?.roles.includes(r));
  const anonymous = me?.kind === 'anonymous';
  const username = anonymous ? t('Local', '本地') : me?.subject ?? '';

  return (
    <div className={css({ minHeight: '100vh', backgroundColor: theme.colors.backgroundSecondary })}>
      <AppNavBar
        title={t('AWS Inventory', 'AWS 资源清单')}
        mainItems={mainItems.map((item) => ({ ...item, active: active(item) }))}
        isMainItemActive={active}
        onMainItemSelect={(item) => navigate(String(item.info))}
        username={username}
        usernameSubtitle={anonymous ? t('Sign-in is off', '未启用登录') : topRole ? roleNames[topRole] : undefined}
        userItems={userItems}
        onUserItemSelect={(item) => {
          if (item.info === 'lang') setLang(lang === 'zh' ? 'en' : 'zh');
          else if (item.info === 'theme') onToggleTheme();
          else if (item.info === 'docs') window.open('/docs', '_blank', 'noopener');
          else if (item.info === 'sign-out') signOut();
        }}
      />
      <main
        className={css({
          maxWidth: '1360px',
          margin: '0 auto',
          padding: `${theme.sizing.scale700} ${theme.sizing.scale500}`,
          [theme.mediaQuery.medium]: { padding: `${theme.sizing.scale800} ${theme.sizing.scale600}` },
        })}
      >
        {children}
      </main>
    </div>
  );
}
