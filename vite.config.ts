import { defineConfig } from 'vitest/config';
export default defineConfig(({ mode }) => ({
  root: 'web',
  base: mode === 'pages' ? './' : '/',
  build: {
    outDir: mode === 'pages' ? '../dist/pages' : '../app/vision/static',
    emptyOutDir: true,
    rollupOptions: {
      input:
        mode === 'pages'
          ? 'web/index.html'
          : { local: 'web/index.html', server: 'web/server.html' },
    },
  },
  server: { proxy: { '/v1/vision': 'http://127.0.0.1:8001' } },
  test: { include: ['**/*.test.ts'], environment: 'node' },
}));
