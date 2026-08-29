import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// ToB 前端的构建配置。骨架期的三项能力 (能构建 / 能起 dev server / 能连本机 gateway)
// 原样保留, 只把业务用到的端点加进 proxy 表。
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
        // 每一项都对应后端已存在的路由 (``gateway`` 下的 add_get/add_post/add_delete)。
        '/defaults': gateway,
        '/ais': gateway,
        // ``/sessions/{id}/chat`` 是 SSE, 必须关掉缓冲否则流式变成一次性返回。
        '/sessions': { target: gateway, changeOrigin: true, ws: false },
        '/titles': gateway,
        '/summaries': gateway,
        '/workspace': gateway,
        // 飞书免登(任务 5fef7 已落地): ``/auth/feishu`` ``/auth/me`` ``/auth/logout``, 全部是
        // 一次性 JSON 请求/响应, 不是 SSE, 用普通字符串写法即可。
        '/auth': gateway,
        // ``/feishu/*``(``_routes.py`` 里的 ``register_feishu_routes``): app-id、按身份过滤的
        // sessions/titles/summaries、以及 ``/feishu/route`` ``/feishu/routes`` 都是普通 JSON。
        // 聊天流式仍然打骨架的 ``/sessions/{id}/chat``(上面那条), ``/feishu`` 下**没有**注册
        // 任何 SSE 端点, 所以不需要 ``{ target, changeOrigin, ws: false }`` 的特殊处理。
        '/feishu': gateway,
      },
    },
  }
})
