import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const gateway = (env.GATEWAY_ORIGIN || 'http://127.0.0.1:8765').replace(/\/+$/, '')

  return {
    plugins: [vue()],
    base: '/spa/',
    build: {
      outDir: 'dist',
      assetsDir: 'assets',
    },
    resolve: {
      alias: { '@': '/src' },
    },
    server: {
      port: 5173,
      strictPort: false,
      proxy: {
        '/ais': gateway,
        '/sessions': gateway,
        '/titles': gateway,
        '/workspace': gateway,
        '/ui': gateway,
        '/openapi.json': gateway,
      },
    },
    test: {
      environment: 'node',
      include: ['src/**/*.test.js'],
    },
  }
})
