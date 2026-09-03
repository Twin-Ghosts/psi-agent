import { useEffect } from 'react'
import { BarChart3, Bot, FileCode, FileText, Plus, Settings2, Sparkles, X } from 'lucide-react'
import './first-run-guide.css'
import { useI18n } from '../i18n'

type Props = {
  onClose: () => void
  onConfigureModels: () => void
  onStartTask: () => void
}

export default function FirstRunGuide({
  onClose,
  onConfigureModels,
  onStartTask,
}: Props) {
  const { t } = useI18n();
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="first-run-layer" role="dialog" aria-modal="true" aria-label={t('firstRun.aria')}>
      <button className="first-run-backdrop" type="button" onClick={onClose} aria-label={t('firstRun.closeAria')} />
      <div className="first-run-dialog">
        <button type="button" className="first-run-close" onClick={onClose} aria-label={t('firstRun.close')}>
          <X size={16} />
        </button>
        <div className="first-run-head">
          <div>
            <span className="first-run-eyebrow">
              <Sparkles size={13} /> {t('firstRun.eyebrow')}
            </span>
            <h2>{t('firstRun.title')}</h2>
          </div>
        </div>
        <div className="first-run-sections">
          <section className="first-run-section">
            <div className="first-run-capability">
              <div className="first-run-capability-intro">
                <Sparkles size={15} />
                <p>{t('firstRun.desc')}</p>
              </div>
              <div className="first-run-capability-tags">
                <span><FileText size={14} /> {t('firstRun.tagDoc')}</span>
                <span><BarChart3 size={14} /> {t('firstRun.tagData')}</span>
                <span><FileCode size={14} /> {t('firstRun.tagCode')}</span>
              </div>
            </div>
          </section>
        </div>
        <div className="first-run-foot">
          <div className="first-run-actions">
            <button type="button" className="first-run-btn secondary" onClick={onConfigureModels}>
              <Bot size={15} /> {t('firstRun.configureModels')}
            </button>
            <button type="button" className="first-run-btn primary" onClick={onStartTask}>
              <Plus size={15} /> {t('app.newTask')}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
