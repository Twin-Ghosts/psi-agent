import { useEffect, useState } from 'react'
import { Upload } from 'lucide-react'
import HubDialog from './HubDialog'
import {
  readAvatarDataUrl,
  readStoredAvatar,
  readStoredName,
  writeStoredProfile,
} from '../../services/userProfile'
import { useI18n } from '../../i18n'

type Props = {
  show: boolean
  onClose: () => void
  onSaved?: (name: string, avatar: string) => void
  onToast?: (message: string) => void
}

export default function HubProfilePanel({ show, onClose, onSaved, onToast }: Props) {
  const { t } = useI18n();
  const [name, setName] = useState('')
  const [avatar, setAvatar] = useState('')

  useEffect(() => {
    if (!show) return
    setName(readStoredName())
    setAvatar(readStoredAvatar())
  }, [show])

  const initial = name.trim().charAt(0).toUpperCase()

  const onFile = async (file: File | null) => {
    if (!file) return
    try {
      setAvatar(await readAvatarDataUrl(file))
    } catch (e) {
      const msg = e instanceof Error ? e.message : ''
      const text = msg === '请选择图片文件'
        ? t('profile.errNotImage')
        : msg === '图片请小于 3MB'
          ? t('profile.errTooLarge')
          : msg === '读取图片失败'
            ? t('profile.errRead')
            : (msg || t('profile.uploadFailed'))
      onToast?.(text)
    }
  }

  const save = () => {
    writeStoredProfile(name, avatar)
    onSaved?.(name.trim(), avatar)
    onClose()
  }

  return (
    <HubDialog
      show={show}
      title={t('app.profile')}
      width={440}
      onClose={onClose}
      actions={(
        <>
          <button type="button" className="hub-btn ghost" onClick={onClose}>{t('profile.cancel')}</button>
          <button type="button" className="hub-btn primary" onClick={save}>{t('profile.save')}</button>
        </>
      )}
    >
      <div className="hub-profile-avatar-row">
        <div className="hub-profile-preview" aria-hidden="true">
          {avatar ? <img src={avatar} alt="" /> : initial ? <span>{initial}</span> : <span className="hub-profile-fallback">?</span>}
        </div>
        <div className="hub-profile-avatar-actions">
          <label className="hub-btn ghost upload">
            <input
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => {
                void onFile(e.target.files?.[0] ?? null)
                e.target.value = ''
              }}
            />
            <Upload size={16} /> {t('profile.uploadAvatar')}
          </label>
          {avatar ? (
            <button type="button" className="hub-link" onClick={() => setAvatar('')}>{t('profile.removeAvatar')}</button>
          ) : null}
        </div>
      </div>
      <label className="hub-field">
        <span>{t('profile.nameLabel')}</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('profile.namePlaceholder')}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              save()
            }
          }}
        />
      </label>
    </HubDialog>
  )
}
