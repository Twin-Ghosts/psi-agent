import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// ToB 前端脚手架的构建配置。只留「三项能力」所需的东西 (见方案 3.7):
// 能构建 / 能起 dev server / 能连本机 gateway。业务端点的 proxy 由后续开发按需加。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const gateway = (env.GATEWAY_ORIGIN || 'http://127.0.0.1:8765').replace(/\/+$/, '')

  return {
    plugins: [react()],
    // 与后端 add_static 的挂载前缀一致, 否则构建产物里的资源路径会 404。
    base: '/feishu-web/',
    build: {
      outDir: 'dist',
      assetsDir: 'assets',
    },
    server: {
      // 5174 已被 ToC 的 spa-v2 占用, ToB 用 5173。
      port: 5173,
      strictPort: false,
      // 必须显式写 127.0.0.1: vite 默认只监听 ``[::1]``, 于是验收里那个
      // ``http://127.0.0.1:5173`` 直接连不上 (curl 返回 000)，而日志打的是
      // ``localhost``, 看不出差别 —— 实测踩过。
      host: '127.0.0.1',
      proxy: {
        // 连通性验证的唯一端点 —— ``/defaults`` 在 main 上已存在, 不引入业务端点。
        '/defaults': gateway,
      },
    },
  }
})
