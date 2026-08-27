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
    // 构建 renderer 双入口：index.html（主界面）+ pet-overlay.html（桌宠透明悬浮窗）；
    // electron 主进程文件不参与打包，由 electron 直接运行。
    outDir: 'dist',
    emptyOutDir: true,
    target: 'chrome120',
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        'pet-overlay': fileURLToPath(new URL('./pet-overlay.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    host: true,
  },
});