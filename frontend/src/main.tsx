import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AuthGate } from './components/auth/AuthGate'
import { installFetchGuard, preferredLanguage, rememberLanguage } from './auth'
import { useProjectStore } from './store/projectStore'
import { loadTranslations } from './i18n'
import './styles/globals.css'

// before anything fetches: a 401 from /api shows the login screen
installFetchGuard()

// the login screen renders before /api/settings is readable: keep the app's language for it
useProjectStore.subscribe((s) => { if (s.settings.language) rememberLanguage(s.settings.language) })

const render = () => createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthGate />
  </StrictMode>,
)

// English is bundled; another language is a separate chunk. Fetch it before the
// first paint so the page doesn't flash English (and render anyway if it fails).
const language = useProjectStore.getState().settings.language || preferredLanguage()
if (language === 'en') render()
else loadTranslations(language).catch(console.error).finally(render)
