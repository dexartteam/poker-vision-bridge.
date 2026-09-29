import { defineConfig } from 'vitest/config';
export default defineConfig(({ mode }) => ({
  root: 'web',
  base: mode === 'pages' ? './' : '/',
  build: {
    outDir: mode === 'pages' ? '../dist/pages' : '../app/vision/static',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        local: 'web/index.html',
        replay: 'web/replay.html',
        ...(mode === 'pages' ? {} : { server: 'web/server.html' }),
      } as Record<string, string>,
    },
  },
  server: {
    proxy: {
      '/v1/vision': 'http://127.0.0.1:8001',
      '/v1/replay': 'http://127.0.0.1:8002',
    },
  },
  test: { include: ['**/*.test.ts'], environment: 'node' },
}));
