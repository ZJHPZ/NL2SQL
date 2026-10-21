import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时用 Vite dev server（5173），它把 /api 代理到 FastAPI（8000）。
// 构建产物由 FastAPI 直接托管（见 backend/main.py），生产只跑一个进程。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // 个人本地工具，不做代码分割优化：单文件更易排查
    chunkSizeWarningLimit: 1500,
  },
})
