import { useEffect, useState } from 'react'

/** 后端连通性的三种状态 —— 页面上唯一的动态内容。 */
type Probe =
  | { kind: 'pending' }
  | { kind: 'ok'; status: number }
  | { kind: 'failed'; detail: string }

/**
 * ToB 前端脚手架的占位页面。
 *
 * 这里**故意只有一次 ``fetch('/defaults')``**: 它是方案 3.7 第三项能力
 * (「能连本地服务端」) 的判据 —— dev 期间经 vite proxy 打到本机 gateway 拿 200。
 * 登录、会话列表、对话收发等业务由其他同事后续开发, 本轮不碰。
 */
export function App() {
  const [probe, setProbe] = useState<Probe>({ kind: 'pending' })

  useEffect(() => {
    const abort = new AbortController()
    fetch('/defaults', { signal: abort.signal })
      .then((res) => setProbe({ kind: 'ok', status: res.status }))
      .catch((err: unknown) => {
        if (abort.signal.aborted) return
        setProbe({ kind: 'failed', detail: err instanceof Error ? err.message : String(err) })
      })
    return () => abort.abort()
  }, [])

  return (
    <main style={{ fontFamily: 'system-ui, sans-serif', padding: '2rem', lineHeight: 1.6 }}>
      <h1 style={{ fontSize: '1.25rem' }}>psi-agent 飞书前端脚手架</h1>
      <p>
        本页是占位。脚手架只验证三件事: 能构建、能起 dev server、能连本地服务端。
      </p>
      <p>
        后端连通性 (<code>GET /defaults</code>):{' '}
        {probe.kind === 'pending' && <span>探测中…</span>}
        {probe.kind === 'ok' && <strong>HTTP {probe.status}</strong>}
        {probe.kind === 'failed' && <span>连不上 —— {probe.detail}</span>}
      </p>
    </main>
  )
}
