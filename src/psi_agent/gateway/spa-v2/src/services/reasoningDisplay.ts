/**
 * DeepSeek / Cursor-style thinking display helpers.
 * Session packs model thinking + tool markers into SSE ``reasoning``;
 * the expandable panel shows only thinking prose (tool markers stripped).
 */

const TOOL_CALL_PREFIX = '[Tool Call:'
const TOOL_RESULT_PREFIX = '[Tool Result:'

function findMatchingParen(s: string, openIdx: number): number {
  let depth = 0
  for (let i = openIdx; i < s.length; i++) {
    const ch = s[i]
    if (ch === '(') depth++
    else if (ch === ')') {
      depth--
      if (depth === 0) return i
    }
  }
  return -1
}

function matchToolCall(buf: string): { end: number } | null {
  if (!buf.startsWith(TOOL_CALL_PREFIX)) return null
  let i = TOOL_CALL_PREFIX.length
  while (i < buf.length && /\s/.test(buf[i]!)) i++
  const nameStart = i
  while (i < buf.length && /[A-Za-z0-9_.-]/.test(buf[i]!)) i++
  if (i === nameStart) return null
  while (i < buf.length && /\s/.test(buf[i]!)) i++
  if (buf[i] !== '(') return null
  const close = findMatchingParen(buf, i)
  if (close < 0 || buf[close + 1] !== ']') return null
  return { end: close + 2 }
}

function matchToolResult(buf: string): { end: number } | null {
  if (!buf.startsWith(TOOL_RESULT_PREFIX)) return null
  let i = TOOL_RESULT_PREFIX.length
  while (i < buf.length && /\s/.test(buf[i]!)) i++
  const nextCall = buf.indexOf(TOOL_CALL_PREFIX, i)
  const nextResult = buf.indexOf(TOOL_RESULT_PREFIX, i)
  let limit = buf.length
  if (nextCall >= 0) limit = Math.min(limit, nextCall)
  if (nextResult >= 0) limit = Math.min(limit, nextResult)
  const close = buf.lastIndexOf(']', limit - 1)
  if (close < i) return null
  return { end: close + 1 }
}

function isPartialToolPrefix(s: string): boolean {
  if (!s.startsWith('[')) return false
  return (
    TOOL_CALL_PREFIX.startsWith(s)
    || TOOL_RESULT_PREFIX.startsWith(s)
    || s.startsWith(TOOL_CALL_PREFIX)
    || s.startsWith(TOOL_RESULT_PREFIX)
  )
}

/**
 * Strip ``[Tool Call:…]`` / ``[Tool Result:…]`` from reasoning for display.
 * Incomplete trailing markers (still streaming) are held back so they do not flash.
 */
export function stripToolMarkersFromReasoning(raw: string): string {
  let buf = typeof raw === 'string' ? raw : ''
  let out = ''

  while (buf) {
    const callIdx = buf.indexOf(TOOL_CALL_PREFIX)
    const resultIdx = buf.indexOf(TOOL_RESULT_PREFIX)
    let idx = -1
    let kind: 'call' | 'result' | null = null
    if (callIdx >= 0 && (resultIdx < 0 || callIdx <= resultIdx)) {
      idx = callIdx
      kind = 'call'
    } else if (resultIdx >= 0) {
      idx = resultIdx
      kind = 'result'
    }

    if (idx < 0) {
      const bracket = buf.lastIndexOf('[')
      if (bracket >= 0 && isPartialToolPrefix(buf.slice(bracket))) {
        out += buf.slice(0, bracket)
        break
      }
      out += buf
      break
    }

    out += buf.slice(0, idx)
    const rest = buf.slice(idx)
    const matched = kind === 'call' ? matchToolCall(rest) : matchToolResult(rest)
    if (!matched) {
      // Incomplete marker — omit from display until it completes.
      break
    }
    buf = rest.slice(matched.end)
  }

  return out
    .replace(/\[Working…\]/g, '')
    .replace(/\[Working\.\.\.\]/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/** Whether cleaned thinking text is worth showing. */
export function hasDisplayableReasoning(raw: string): boolean {
  return !!stripToolMarkersFromReasoning(raw)
}

/** Raw reasoning has tool activity even if prose was stripped for display. */
export function hasToolMarkerReasoning(raw: string): boolean {
  const s = typeof raw === 'string' ? raw : ''
  return s.includes('[Tool Call:') || s.includes('[Tool Result:')
}

/** Header label for the thinking panel (Chinese only). */
export function thinkingHeaderLabel(opts: {
  streaming?: boolean
  slow?: boolean
  hasBody?: boolean
  stopped?: boolean
  syncing?: boolean
} = {}): string {
  if (opts.stopped) return '已停止'
  if (opts.syncing) return '正在同步…'
  if (opts.slow && !opts.hasBody) return '仍在处理，比平时久一点…'
  if (opts.streaming) return '思考中'
  return '已思考'
}
