import { FolderOpen, Loader2, Package } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'
import { BrandLogo } from '../haitun-agent/primitives'
import { fetchCwd, fetchDefaults } from '../services/api'
import PathPickerDialog from './PathPickerDialog'
import { useI18n } from '../i18n'

export type PathPickKind = 'workspace' | 'agent'

type Props = {
  /** Prefill when switching from an existing path. */
  initialPath?: string
  /** workspace = user open folder; agent = capability pack (tools/schedules/systems). */
  kind?: PathPickKind
  onReady: (path: string) => void
  /** Return without changing (settings → 切换). */
  onCancel?: () => void
}

/** Pick / confirm a directory (workspace gate or agent-package gate). */
export default function WorkspaceGate({
  initialPath = '',
  kind = 'workspace',
  onReady,
  onCancel,
}: Props) {
  const { t } = useI18n();
  const isAgent = kind === 'agent'
  const [path, setPath] = useState(initialPath)
  const [loading, setLoading] = useState(!initialPath)
  const [error, setError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)

  useEffect(() => {
    if (initialPath.trim()) {
      setPath(initialPath.trim())
      setLoading(false)
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        const d = await fetchDefaults().catch(() => null)
        if (isAgent) {
          if (!cancelled && d?.agent) setPath(d.agent)
          else if (!cancelled) setPath('')
        } else if (!cancelled && d?.workspace) {
          setPath(d.workspace)
        } else {
          const cwd = await fetchCwd()
          if (!cancelled) setPath(cwd?.cwd || '')
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [initialPath, isAgent])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const clean = path.trim()
    if (!clean) {
      setError(isAgent ? t('gate.errorAgent') : t('gate.errorWorkspace'))
      return
    }
    onReady(clean)
  }

  const ConfirmIcon = isAgent ? Package : FolderOpen

  return (
    <div className="workspace-gate">
      <div className="workspace-gate-card">
        <BrandLogo size="hero" />
        <span className="eyebrow">HaiTun Agent</span>
        <h1>{isAgent ? t('gate.agentTitle') : t('gate.workspaceTitle')}</h1>
        <p>
          {isAgent ? (
            <>
              {t('gate.agentDescPrefix')}<code>tools/</code>、<code>schedules/</code>、<code>systems/</code>{t('gate.agentDescSuffix')}<strong>{t('app.newTask')}</strong>{t('gate.agentDescTail')}
            </>
          ) : (
            <>
              {t('gate.workspaceDesc')}
            </>
          )}
        </p>
        {loading ? (
          <div className="workspace-gate-loading"><Loader2 className="spin" size={22} /> {t('app.connecting')}</div>
        ) : (
          <form onSubmit={submit}>
            <label>
              <span>{isAgent ? t('gate.agentPathLabel') : t('gate.workspacePathLabel')}</span>
              <div className="workspace-gate-path-row">
                <button
                  type="button"
                  className="workspace-gate-browse"
                  onClick={() => setPickerOpen(true)}
                  aria-label={t('gate.browseAria')}
                  title={t('gate.browseAria')}
                >
                  <FolderOpen size={18} />
                </button>
                <input
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  placeholder={
                    isAgent
                      ? t('gate.agentPlaceholder')
                      : t('gate.workspacePlaceholder')
                  }
                  autoFocus
                />
              </div>
            </label>
            {error && <div className="workspace-gate-error" role="alert">{error}</div>}
            <div className="workspace-gate-actions">
              {onCancel && (
                <button type="button" className="secondary-button" onClick={onCancel}>
                  {t('gate.cancel')}
                </button>
              )}
              <button type="submit" className="primary-button" disabled={!path.trim()}>
                <ConfirmIcon size={16} /> {isAgent ? t('gate.useAgent') : t('gate.enterWorkspace')}
              </button>
            </div>
          </form>
        )}
      </div>

      <PathPickerDialog
        open={pickerOpen}
        initialPath={path}
        title={isAgent ? t('gate.agentTitle') : t('gate.workspaceTitle')}
        confirmLabel={isAgent ? t('gate.pickAgent') : t('gate.pickWorkspace')}
        hint={
          isAgent
            ? t('gate.agentHint')
            : t('gate.workspaceHint')
        }
        onCancel={() => setPickerOpen(false)}
        onConfirm={(picked) => {
          setPath(picked)
          setPickerOpen(false)
          setError(null)
        }}
      />
    </div>
  )
}
