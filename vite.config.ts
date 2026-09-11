import { defineConfig } from 'vitest/config';
export default defineConfig({
  root: 'web',
  build: { outDir: '../app/vision/static', emptyOutDir: true },
  server: { proxy: { '/v1/vision': 'http://127.0.0.1:8001' } },
  test: { include: ['**/*.test.ts'], environment: 'node' },
});
