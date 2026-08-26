import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// Vite 构建面向 Electron renderer。
// base 使用 './' 以兼容 Electron 通过 file:// 直接加载 dist/index.html。
export default defineConfig({
  root: '.',
  plugins: [react()],
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src/renderer', import.meta.url)),
    },
  },
  build: {
    // 只构建 renderer（index.html 入口）；main/preload 由 electron 直接运行，不参与打包。
    outDir: 'dist',
    emptyOutDir: true,
    target: 'chrome120',
    rollupOptions: {
      input: fileURLToPath(new URL('./index.html', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    host: true,
  },
});