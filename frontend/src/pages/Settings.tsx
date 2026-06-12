import { useState, useEffect } from 'react'

const STORAGE_KEY = 'migratehq-bi-url'

export default function Settings() {
  const [url, setUrl] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '')
  const [toast, setToast] = useState(false)

  function handleSave() {
    localStorage.setItem(STORAGE_KEY, url)
    setToast(true)
  }

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(false), 2500)
    return () => clearTimeout(t)
  }, [toast])

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Settings</h1>
        <p className="page-subtitle">Configure your MigrateHQ workspace</p>
      </div>

      <div className="settings-section">
        <div className="settings-section-header">BI Integration</div>
        <div className="settings-body">
          <div className="settings-field-label">Report URL</div>
          <input
            className="url-input"
            type="url"
            placeholder="Paste your Looker Studio, PowerBI, or Tableau report URL"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSave()}
          />
          <div className="settings-field-helper">
            Supports Looker Studio, Microsoft PowerBI, and Tableau. This URL is also shown on the Analytics page.
          </div>
          <div className="settings-actions">
            <button className="btn-primary" onClick={handleSave}>Save URL</button>
          </div>
        </div>
        <div className="settings-footer-note">More settings coming soon</div>
      </div>

      {toast && (
        <div className="toast">BI URL saved</div>
      )}
    </div>
  )
}
