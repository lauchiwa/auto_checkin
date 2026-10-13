import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 本部署挂载在 https://<你的域名>/checkin/ 子路径下（不是域名根）。
// base 让 Vite 把资源引用前缀化（/checkin/assets/...）；'' 时保持上游默认。
// 环境变量在 Dockerfile 里注入：CHECKIN_BASE='/checkin/'
export default defineConfig({
  base: process.env.CHECKIN_BASE || '/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})
