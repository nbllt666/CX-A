import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

/**
 * Test1 单元测试配置（s0402 三重闸第一关）。
 * environment: jsdom —— 组件级渲染测试与 localStorage / window 桥降级测试所需。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src/renderer', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/unit/setupTests.ts'],
    include: ['tests/unit/**/*.{test,spec}.{ts,tsx}'],
  },
});
