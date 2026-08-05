import { describe, expect, it } from 'vitest'
import {
  appendContentSegment,
  contentSegmentsStart,
  sealContentBeforeTools,
  settleContentSegments,
} from './contentSegments'

describe('contentSegments', () => {
  it('seals prose before each tool_call and keeps last segment as final', () => {
    let seg = contentSegmentsStart()
    seg = appendContentSegment(seg, '第一步计划')
    seg = sealContentBeforeTools(seg)
    seg = appendContentSegment(seg, '第一步完成，下一步读文件')
    seg = sealContentBeforeTools(seg)
    seg = appendContentSegment(seg, '三步全部完成 ✅')
    expect(settleContentSegments(seg)).toEqual({
      finalText: '三步全部完成 ✅',
      processNotes: ['第一步计划', '第一步完成，下一步读文件'],
    })
  })

  it('promotes last sealed note when there is no trailing summary', () => {
    let seg = contentSegmentsStart()
    seg = appendContentSegment(seg, '仅步骤叙述')
    seg = sealContentBeforeTools(seg)
    expect(settleContentSegments(seg)).toEqual({
      finalText: '仅步骤叙述',
      processNotes: [],
    })
  })

  it('ignores blank seals', () => {
    let seg = contentSegmentsStart()
    seg = sealContentBeforeTools(seg)
    seg = appendContentSegment(seg, '  最终  ')
    expect(settleContentSegments(seg)).toEqual({
      finalText: '最终',
      processNotes: [],
    })
  })
})
