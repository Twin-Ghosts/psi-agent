import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Download, FileText, FolderOpen, X } from 'lucide-react'
import type { ChatFile } from '../haitun-agent/model'
import { downloadChatFile, revealDeliverableInFolder } from '../utils/filePreviewUtils'
import { ArtifactFileBody } from './ArtifactFileBody'
import { useI18n } from '../i18n'

/**
 * In-app preview drawer for chat blobs — same render path as 宝箱 ArtifactFileBody.
 */
export default function FilePreview({
  file,
  workspaceRoot = '',
  onClose,
}: {
  file: ChatFile
  workspaceRoot?: string
  onClose: () => void
}) {
  const { t } = useI18n();
  const [revealBusy, setRevealBusy] = useState(false)
  const [revealError, setRevealError] = useState('')
  const canReveal = Boolean(file.path?.trim())

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleReveal = () => {
    const path = file.path?.trim()
    if (!path || revealBusy) return
    setRevealBusy(true)
    setRevealError('')
    void revealDeliverableInFolder(path, workspaceRoot)
      .catch((e) => {
        setRevealError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setRevealBusy(false))
  }

  return createPortal(
    <div className="preview-drawer-shell">
      <button type="button" className="preview-scrim" aria-label={t('preview.closeAria')} onClick={onClose} />
      <aside className="file-preview preview-drawer" role="dialog" aria-modal="true" aria-label={t('preview.aria')}>
        <header className="preview-drawer-header">
          <div className="preview-title-wrap">
            <FileText size={18} />
            <div className="preview-title" title={file.path || file.name}>{file.name}</div>
          </div>
          <div className="preview-actions">
            <button
              type="button"
              className="preview-icon-btn"
              title={canReveal ? (revealBusy ? t('chat.opening') : t('chat.showInFolder')) : t('preview.noPath')}
              disabled={!canReveal || revealBusy}
              onClick={handleReveal}
              aria-label={t('chat.showInFolder')}
            >
              <FolderOpen size={16} />
            </button>
            <button type="button" className="preview-icon-btn" title={t('drawer.download')} onClick={() => downloadChatFile(file)}>
              <Download size={16} />
            </button>
            <button type="button" className="preview-icon-btn" title={t('preview.close')} onClick={onClose}>
              <X size={16} />
            </button>
          </div>
        </header>
        {revealError ? <div className="preview-reveal-error" role="alert">{revealError}</div> : null}
        <div className="preview-drawer-body">
          <ArtifactFileBody key={`${file.name}:${file.data.slice(0, 48)}`} file={file} />
        </div>
      </aside>
    </div>,
    document.body,
  )
}
