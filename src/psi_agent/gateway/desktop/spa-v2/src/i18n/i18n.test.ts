import { describe, expect, it } from 'vitest'
import enUS from './en-US.json'
import zhCN from './zh-CN.json'
import zhTW from './zh-TW.json'
import { normalizeLanguage, translate } from './index'

describe('i18n', () => {
  it('normalizes loose language tags', () => {
    expect(normalizeLanguage('en_US')).toBe('en-US')
    expect(normalizeLanguage('en')).toBe('en-US')
    expect(normalizeLanguage('zh_TW')).toBe('zh-TW')
    expect(normalizeLanguage('zh-Hant')).toBe('zh-TW')
    expect(normalizeLanguage('zh')).toBe('zh-CN')
    expect(normalizeLanguage('fr-FR')).toBe('zh-CN')
    expect(normalizeLanguage(null)).toBe('zh-CN')
  })

  it('keeps all dictionaries in parity', () => {
    expect(Object.keys(zhCN).sort()).toEqual(Object.keys(enUS).sort())
    expect(Object.keys(zhCN).sort()).toEqual(Object.keys(zhTW).sort())
  })

  it('translates and falls back to Chinese then key', () => {
    expect(translate('zh-CN', 'app.send')).toBe('发送')
    expect(translate('zh-TW', 'app.send')).toBe('傳送')
    expect(translate('en-US', 'app.send')).toBe('Send')
    expect(translate('en-US', 'missing.key')).toBe('missing.key')
  })

  it('fills placeholders', () => {
    expect(translate('en-US', 'app.composerPlaceholder', { title: 'Report' })).toBe(
      'Tell the agent how to continue “Report”…',
    )
  })
})
