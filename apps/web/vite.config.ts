import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    // 5173 常被其他本地项目占用，固定 5178 专用端口（strictPort 直接报错而非静默换端口）
    host: '127.0.0.1',
    port: 5178,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
