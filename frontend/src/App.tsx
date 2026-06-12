import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import Login from './pages/Login'
import Overview from './pages/Overview'
import Clients from './pages/Clients'
import Analytics from './pages/Analytics'
import Settings from './pages/Settings'
import { getSession, logout, Session } from './lib/auth'

export default function App() {
  const [session, setSession] = useState<Session | null>(() => getSession())
  const [collapsed, setCollapsed] = useState(false)
  const [darkMode, setDarkMode] = useState(() => {
    return localStorage.getItem('migratehq-theme') === 'dark'
  })

  useEffect(() => {
    document.documentElement.dataset.theme = darkMode ? 'dark' : 'light'
    localStorage.setItem('migratehq-theme', darkMode ? 'dark' : 'light')
  }, [darkMode])

  function handleLogout() {
    logout()
    setSession(null)
  }

  return (
    <BrowserRouter>
      <Routes>
        {/* Public route */}
        <Route
          path="/login"
          element={
            session
              ? <Navigate to="/overview" replace />
              : <Login onLogin={(s) => setSession(s)} />
          }
        />

        {/* Protected shell */}
        <Route
          path="/*"
          element={
            !session ? (
              <Navigate to="/login" replace />
            ) : (
              <div className="app">
                <Sidebar
                  collapsed={collapsed}
                  onToggleCollapse={() => setCollapsed((c) => !c)}
                  darkMode={darkMode}
                  onToggleDark={() => setDarkMode((d) => !d)}
                  session={session}
                  onLogout={handleLogout}
                />
                <main className="main-content">
                  <Routes>
                    <Route path="/"          element={<Navigate to="/overview" replace />} />
                    <Route path="/overview"  element={<Overview  session={session} />} />
                    <Route path="/clients"   element={<Clients   session={session} />} />
                    <Route path="/analytics" element={<Analytics />} />
                    <Route path="/settings"  element={<Settings />} />
                  </Routes>
                </main>
              </div>
            )
          }
        />
      </Routes>
    </BrowserRouter>
  )
}
