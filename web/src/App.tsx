import { Navigate, Route, Routes } from 'react-router-dom';

import { useAuth } from './auth/AuthContext';
import { Loading } from './components/common';
import { Layout } from './components/Layout';
import { ApiKeysPage } from './pages/ApiKeys';
import { CoveragePage } from './pages/Coverage';
import { DashboardPage } from './pages/Dashboard';
import { JobPage } from './pages/Job';
import { LoginPage } from './pages/Login';
import { ResourcesPage } from './pages/Resources';
import { ScansPage } from './pages/Scans';

export function App({ dark, onToggleTheme }: { dark: boolean; onToggleTheme: () => void }) {
  const { state, can } = useAuth();
  if (state.status === 'loading') return <Loading />;
  if (state.status === 'signed-out') return <LoginPage error={state.error} />;
  return (
    <Layout dark={dark} onToggleTheme={onToggleTheme}>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/resources" element={<ResourcesPage />} />
        {can('scans:write') ? <Route path="/scans" element={<ScansPage />} /> : null}
        {can('scans:write') ? <Route path="/scans/:jobId" element={<JobPage />} /> : null}
        <Route path="/accounts/:accountId" element={<CoveragePage />} />
        {can('api_keys:manage') && state.me.auth_mode === 'enforced' ? <Route path="/api-keys" element={<ApiKeysPage />} /> : null}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
