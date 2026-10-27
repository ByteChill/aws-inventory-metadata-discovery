import { useStyletron } from 'baseui';
import { Button } from 'baseui/button';
import { FormControl } from 'baseui/form-control';
import { Textarea } from 'baseui/textarea';
import { HeadingMedium, ParagraphSmall } from 'baseui/typography';
import { useState } from 'react';

import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { ErrorNotice } from '../components/common';

export function LoginPage({ error }: { error: unknown }) {
  const [css, theme] = useStyletron();
  const { signIn, retry } = useAuth();
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
        <HeadingMedium marginTop={0}>AWS Inventory</HeadingMedium>
        <ParagraphSmall color="contentSecondary">
          Sign in with your company account (8-digit employee ID).
        </ParagraphSmall>
        <Button disabled overrides={{ BaseButton: { style: { width: '100%' } } }}>
          Sign in with SSO (coming soon)
        </Button>
        {error && !unauthenticated ? (
          <>
            <ErrorNotice error={error} />
            <Button kind="tertiary" onClick={retry}>
              Retry
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
            label="Access token"
            caption="Until SSO is connected: paste a token issued by the SSO service. It is kept for this browser tab only."
          >
            <Textarea value={token} onChange={(e) => setToken(e.currentTarget.value)} rows={4} />
          </FormControl>
          <Button type="submit" kind="secondary" disabled={!token.trim()}>
            Use token
          </Button>
        </form>
      </div>
    </div>
  );
}
