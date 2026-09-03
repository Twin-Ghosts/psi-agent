import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { LanguageProvider } from './i18n'
import './styles/globals.css'
import './styles/highlight.css'

const root = document.getElementById('app')
if (!root) {
  throw new Error('Missing #app mount point')
}

createRoot(root).render(
  <StrictMode>
    <LanguageProvider>
      <App />
    </LanguageProvider>
  </StrictMode>,
)
