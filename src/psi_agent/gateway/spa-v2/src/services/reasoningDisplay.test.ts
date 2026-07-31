import { describe, expect, it } from 'vitest'
import {
  hasDisplayableReasoning,
  hasToolMarkerReasoning,
  stripToolMarkersFromReasoning,
  thinkingHeaderLabel,
} from './reasoningDisplay'

describe('stripToolMarkersFromReasoning', () => {
  it('keeps plain thinking', () => {
    expect(stripToolMarkersFromReasoning('先分析问题')).toBe('先分析问题')
  })

  it('removes tool call and result markers', () => {
    const raw = [
      '先想一步',
      '[Tool Call: list_dir({"path":"."})]',
      '[Tool Result: tools/ AGENTS.md]',
      '再总结',
    ].join('\n')
    expect(stripToolMarkersFromReasoning(raw)).toBe('先想一步\n\n再总结')
  })

  it('hides incomplete trailing tool markers while streaming', () => {
    expect(stripToolMarkersFromReasoning('思考\n[Tool Call: ba')).toBe('思考')
  })

  it('handles nested json in tool args', () => {
    const raw = 'a\n[Tool Call: todo({"items":[{"x":1}]})]\nb'
    expect(stripToolMarkersFromReasoning(raw)).toBe('a\n\nb')
  })

  it('strips Working keepalive markers', () => {
    expect(stripToolMarkersFromReasoning('think\n[Working…]\nmore')).toBe('think\n\nmore')
  })
})

describe('hasDisplayableReasoning', () => {
  it('is false when only tool markers remain', () => {
    expect(hasDisplayableReasoning('[Tool Call: bash({})]\n[Tool Result: ok]')).toBe(false)
    expect(hasDisplayableReasoning('有想法')).toBe(true)
  })
})

describe('hasToolMarkerReasoning', () => {
  it('detects tool markers even when prose is empty', () => {
    expect(hasToolMarkerReasoning('[Tool Call: bash({})]')).toBe(true)
    expect(hasToolMarkerReasoning('纯思考')).toBe(false)
  })
})

describe('thinkingHeaderLabel', () => {
  it('uses Chinese status copy', () => {
    expect(thinkingHeaderLabel({ streaming: true })).toBe('思考中')
    expect(thinkingHeaderLabel({ streaming: false, hasBody: true })).toBe('已思考')
    expect(thinkingHeaderLabel({ slow: true, hasBody: false })).toBe('仍在处理，比平时久一点…')
    expect(thinkingHeaderLabel({ syncing: true })).toBe('正在同步…')
    expect(thinkingHeaderLabel({ stopped: true, streaming: true })).toBe('已停止')
  })
})
