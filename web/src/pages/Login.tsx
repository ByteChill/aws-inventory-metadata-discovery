import { useStyletron } from 'baseui';
import { Button } from 'baseui/button';
import { FormControl } from 'baseui/form-control';
import { Textarea } from 'baseui/textarea';
import { HeadingMedium, ParagraphSmall } from 'baseui/typography';
import { useState } from 'react';

import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { ErrorNotice } from '../components/common';
import { useI18n } from '../i18n';

export function LoginPage({ error }: { error: unknown }) {
  const [css, theme] = useStyletron();
  const { signIn, retry } = useAuth();
  const { t, lang, setLang } = useI18n();
  const unauthenticated = error instanceof ApiError && error.status === 401;
  const [token, setToken] = useState('');

  return (
    <div
      className={css({
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: theme.colors.backgroundSecondary,
        padding: theme.sizing.scale600,
      })}
    >
      <div
        className={css({
          width: '100%',
          maxWidth: '440px',
          backgroundColor: theme.colors.backgroundPrimary,
          border: `1px solid ${theme.colors.borderOpaque}`,
          borderRadius: theme.borders.radius400,
          padding: theme.sizing.scale800,
        })}
      >
        <HeadingMedium marginTop={0}>{t('AWS Inventory', 'AWS 资源清单')}</HeadingMedium>
        <ParagraphSmall color="contentSecondary">
          {t('Sign in with your company account (8-digit employee ID).', '请使用公司账号登录（8 位工号）。')}
        </ParagraphSmall>
        <Button disabled overrides={{ BaseButton: { style: { width: '100%' } } }}>
          {t('Sign in with SSO (coming soon)', '使用 SSO 登录（即将开放）')}
        </Button>
        {error && !unauthenticated ? (
          <>
            <ErrorNotice error={error} />
            <Button kind="tertiary" onClick={retry}>
              {t('Retry', '重试')}
            </Button>
          </>
        ) : null}
        <form
          className={css({ marginTop: theme.sizing.scale800 })}
          onSubmit={(event) => {
            event.preventDefault();
            if (token.trim()) void signIn(token.trim());
          }}
        >
          <FormControl
            label={t('Access token', '访问令牌')}
            caption={t('Until SSO is connected: paste a token issued by the SSO service. It is kept for this browser tab only.', 'SSO 接入前：粘贴 SSO 签发的令牌，只保存在当前浏览器标签页。')}
          >
            <Textarea value={token} onChange={(e) => setToken(e.currentTarget.value)} rows={4} />
          </FormControl>
          <Button type="submit" kind="secondary" disabled={!token.trim()}>
            {t('Use token', '使用令牌')}
          </Button>
        </form>
        <Button kind="tertiary" size="compact" onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}>
          {lang === 'zh' ? 'English' : '中文'}
        </Button>
      </div>
    </div>
  );
}
