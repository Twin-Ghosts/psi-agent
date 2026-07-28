import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Download, FileText, X } from 'lucide-react'
import type { ChatFile } from '../haitun-agent/model'
import { downloadChatFile } from '../utils/filePreviewUtils'
import { ArtifactFileBody } from './ArtifactFileBody'

/**
 * In-app preview drawer for chat blobs — same render path as 宝箱 ArtifactFileBody.
 */
export default function FilePreview({
  file,
  onClose,
}: {
  file: ChatFile
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div className="preview-drawer-shell">
      <button type="button" className="preview-scrim" aria-label="关闭预览" onClick={onClose} />
      <aside className="file-preview preview-drawer" role="dialog" aria-modal="true" aria-label="文件预览">
        <header className="preview-drawer-header">
          <div className="preview-title-wrap">
            <FileText size={18} />
            <div className="preview-title" title={file.name}>{file.name}</div>
          </div>
          <div className="preview-actions">
            <button type="button" className="preview-icon-btn" title="下载" onClick={() => downloadChatFile(file)}>
              <Download size={16} />
            </button>
            <button type="button" className="preview-icon-btn" title="关闭" onClick={onClose}>
              <X size={16} />
            </button>
          </div>
        </header>
        <div className="preview-drawer-body">
          <ArtifactFileBody key={`${file.name}:${file.data.slice(0, 48)}`} file={file} />
        </div>
      </aside>
    </div>,
    document.body,
  )
}
