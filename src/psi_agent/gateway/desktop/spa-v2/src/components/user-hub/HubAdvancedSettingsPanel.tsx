import { ChevronRight, Package } from 'lucide-react'
import { useI18n } from '../../i18n'
import HubDialog from './HubDialog'

type Props = {
  show: boolean
  onClose: () => void
  onBackToSettings?: () => void
  agent?: string
  onChangeAgent?: () => void
}

function pathLabel(path: string, fallback = '未选择'): string {
  const p = path.replace(/\\/g, '/').replace(/\/+$/, '')
  const parts = p.split('/').filter(Boolean)
  return parts[parts.length - 1] || p || fallback
}

/** Advanced settings page of the settings dialog: Agent package path switch. */
export default function HubAdvancedSettingsPanel({
  show,
  onClose,
  onBackToSettings,
  agent,
  onChangeAgent,
}: Props) {
  const { t } = useI18n();
  const backToSettings = () => {
    if (onBackToSettings) {
      onBackToSettings()
      return
    }
    onClose()
  }

  return (
    <HubDialog
      show={show}
      title={(
        <div className="hub-models-title">
          <span>{t('app.advancedSettings')}</span>
          <button type="button" className="hub-link" onClick={backToSettings}>
            {t('advanced.backToSettings')}
          </button>
        </div>
      )}
      width={480}
      onClose={onClose}
      actions={(
        <>
          <button type="button" className="hub-btn ghost" onClick={backToSettings}>{t('advanced.backToSettings')}</button>
          <button type="button" className="hub-btn primary" onClick={onClose}>{t('app.close')}</button>
        </>
      )}
    >
      <section className="hub-settings-section">
        {onChangeAgent ? (
          <button
            type="button"
            className="hub-settings-row hub-settings-workspace"
            onClick={() => {
              onClose()
              onChangeAgent()
            }}
          >
            <span className="hub-settings-workspace-icon" aria-hidden="true">
              <Package size={18} />
            </span>
            <span>
              <strong>{t('advanced.switchAgent')}</strong>
              <em title={agent || undefined}>
                {agent ? pathLabel(agent, t('advanced.unselected')) : t('advanced.chooseAgentDir')}
              </em>
            </span>
            <ChevronRight size={16} className="hub-settings-row-chevron" />
          </button>
        ) : null}
        {agent && onChangeAgent ? (
          <p className="hub-settings-workspace-path" title={agent}>{agent}</p>
        ) : null}
        <p className="hub-settings-foot">
          {t('advanced.agentFoot')}
        </p>
      </section>
    </HubDialog>
  )
}
