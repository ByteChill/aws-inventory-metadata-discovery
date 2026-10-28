import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Served by the API under /ui/; `npm run dev` proxies the API to a local uvicorn.
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 2000 },
  server: { proxy: { '/v1': 'http://localhost:8080' } },
});
