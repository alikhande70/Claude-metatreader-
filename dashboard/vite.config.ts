import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The built bundle is served by the FastAPI app from src/atlas/api/static, so `atlas serve`
// needs no separate web server. In development, `npm run dev` proxies /api and /ws to the
// running engine instead.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/atlas/api/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
})
