import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { fetchLanguage, saveLanguage } from '../services/api'
import enUS from './en-US.json'
import zhCN from './zh-CN.json'
import zhTW from './zh-TW.json'

export type Language = 'zh-CN' | 'zh-TW' | 'en-US'
export const DEFAULT_LANGUAGE: Language = 'zh-CN'
export const LANGUAGES: Language[] = ['zh-CN', 'zh-TW', 'en-US']

export type TranslateFn = (key: string, vars?: Record<string, string | number>) => string

export function normalizeLanguage(raw: string | null | undefined): Language {
  const code = (raw || '').trim().toLowerCase().replace(/_/g, '-')
  if (code === 'en' || code === 'en-us') return 'en-US'
  if (code === 'zh-tw' || code === 'zh-hant' || code === 'zh-hk' || code === 'zh-mo') return 'zh-TW'
  return 'zh-CN'
}

export function translate(
  language: Language,
  key: string,
  vars?: Record<string, string | number>,
): string {
  const zh = zhCN as unknown as Record<string, string>
  const en = enUS as unknown as Record<string, string>
  const tw = zhTW as unknown as Record<string, string>
  let text = (language === 'en-US' ? en[key] : language === 'zh-TW' ? tw[key] : zh[key])
    || (language === 'en-US' ? zh[key] : tw[key])
    || zh[key]
    || key
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{${name}}`).join(String(value))
    }
  }
  return text
}

type I18nValue = {
  language: Language
  setLanguage: (language: Language) => void
  t: TranslateFn
}

const I18nContext = createContext<I18nValue | null>(null)

const FALLBACK_I18N: I18nValue = {
  language: DEFAULT_LANGUAGE,
  setLanguage: () => {},
  t: (key, vars) => translate(DEFAULT_LANGUAGE, key, vars),
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(DEFAULT_LANGUAGE)

  useEffect(() => {
    let cancelled = false
    fetchLanguage()
      .then((res) => {
        if (!cancelled) setLanguageState(normalizeLanguage(res.language))
      })
      .catch(() => {
        // Old gateway / transient failure: keep the Chinese default.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next)
    void saveLanguage(next).catch(() => {})
  }, [])

  const t = useCallback<TranslateFn>(
    (key, vars) => translate(language, key, vars),
    [language],
  )

  const value = useMemo<I18nValue>(() => ({ language, setLanguage, t }), [language, setLanguage, t])
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  return useContext(I18nContext) ?? FALLBACK_I18N
}
