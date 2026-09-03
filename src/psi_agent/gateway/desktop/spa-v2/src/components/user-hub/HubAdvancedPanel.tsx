import { useEffect, useMemo, useState } from 'react'
import { createAi, listAis, type AiInfo } from '../../services/api'
import { writeStoredAiId } from '../../services/bootstrapAi'
import { PROVIDERS } from '../../services/providers'
import { useI18n } from '../../i18n'
import HubDialog from './HubDialog'

type Props = {
  show: boolean
  onClose: () => void
  onBackToModels?: () => void
  onSelectAi: (id: string) => void
  onToast?: (message: string) => void
  onAisChanged?: (ais: AiInfo[]) => void
  requireAi?: boolean
}

export default function HubAdvancedPanel({
  show,
  onClose,
  onBackToModels,
  onSelectAi,
  onToast,
  onAisChanged,
  requireAi = false,
}: Props) {
  const { t } = useI18n();
  const [provider, setProvider] = useState(PROVIDERS[0]?.v ?? 'openai')
  const [model, setModel] = useState(PROVIDERS[0]?.models[0] ?? '')
  const [baseUrl, setBaseUrl] = useState(PROVIDERS[0]?.base ?? '')
  const [apiKey, setApiKey] = useState('')
  const [connecting, setConnecting] = useState(false)

  const current = useMemo(() => PROVIDERS.find((p) => p.v === provider), [provider])

  useEffect(() => {
    if (!show) return
    const first = PROVIDERS[0]
    if (!first) return
    setProvider(first.v)
    setModel(first.models[0] ?? '')
    setBaseUrl(first.base)
    setApiKey('')
  }, [show])

  const selectProvider = (v: string) => {
    const p = PROVIDERS.find((item) => item.v === v)
    if (!p) return
    setProvider(p.v)
    setBaseUrl(p.base)
    setModel(p.models[0] ?? '')
  }

  const connect = async () => {
    if (!apiKey.trim() || !model.trim() || !baseUrl.trim() || connecting) return
    setConnecting(true)
    try {
      const info = await createAi({
        provider,
        model: model.trim(),
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
      })
      const list = await listAis()
      onAisChanged?.(list)
      onSelectAi(info.id)
      writeStoredAiId(info.id)
      onToast?.(t('advanced.connected'))
      onClose()
    } catch (e) {
      onToast?.(e instanceof Error ? e.message : t('models.connectFailed'))
    } finally {
      setConnecting(false)
    }
  }

  const handleClose = () => {
    if (requireAi) {
      onToast?.(t('advanced.requireAi'))
      return
    }
    onClose()
  }

  const backToModels = () => {
    if (onBackToModels) {
      onBackToModels()
      return
    }
    handleClose()
  }

  return (
    <HubDialog
      show={show}
      title={(
        <div className="hub-models-title">
          <span>{t('advanced.title')}</span>
          <button type="button" className="hub-link" onClick={backToModels}>
            {t('advanced.backToModels')}
          </button>
        </div>
      )}
      width={480}
      onClose={handleClose}
      actions={(
        <>
          <button type="button" className="hub-btn ghost" onClick={backToModels}>{t('advanced.backToModels')}</button>
          <button
            type="button"
            className="hub-btn primary"
            disabled={!apiKey.trim() || !model.trim() || connecting}
            onClick={() => void connect()}
          >
            {connecting ? t('models.connecting') : t('advanced.link')}
          </button>
        </>
      )}
    >
      <label className="hub-field">
        <span>{t('advanced.providerLabel')}</span>
        <select value={provider} onChange={(e) => selectProvider(e.target.value)}>
          {PROVIDERS.map((p) => (
            <option key={p.v} value={p.v}>{t(`provider.${p.v}`)}</option>
          ))}
        </select>
      </label>
      <label className="hub-field">
        <span>{t('advanced.modelLabel')}</span>
        <input
          value={model}
          list="hub-advanced-models"
          onChange={(e) => setModel(e.target.value)}
          placeholder={t('advanced.modelPlaceholder')}
        />
        <datalist id="hub-advanced-models">
          {(current?.models ?? []).map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      </label>
      <label className="hub-field">
        <span>{t('advanced.baseUrlLabel')}</span>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://..." />
      </label>
      <label className="hub-field">
        <span>{t('advanced.apiKeyLabel')}</span>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="sk-..."
          autoComplete="off"
        />
      </label>
    </HubDialog>
  )
}
