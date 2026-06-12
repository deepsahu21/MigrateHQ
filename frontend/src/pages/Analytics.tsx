import { useState } from 'react'

const STORAGE_KEY = 'migratehq-bi-url'

export default function Analytics() {
  const [inputUrl, setInputUrl] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '')
  const [savedUrl, setSavedUrl] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '')

  function handleSave() {
    localStorage.setItem(STORAGE_KEY, inputUrl)
    setSavedUrl(inputUrl)
  }

  return (
    <div className="analytics-page">
      <div className="analytics-toolbar">
        <div className="analytics-input-group">
          <div className="analytics-label">BI Report URL</div>
          <input
            className="url-input"
            type="url"
            placeholder="Paste your Looker Studio, PowerBI, or Tableau report URL"
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSave()}
          />
          <div className="helper-text">Supports Looker Studio, Microsoft PowerBI, and Tableau</div>
        </div>
        <button className="btn-primary" onClick={handleSave}>
          Save
        </button>
      </div>

      {savedUrl ? (
        <div className="analytics-embed">
          <iframe
            src={savedUrl}
            title="BI Report"
            allowFullScreen
            sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
          />
        </div>
      ) : (
        <div className="analytics-empty">
          <div className="empty-logo">MigrateHQ</div>
          <div className="empty-title">Connect your BI tool to visualize your data</div>
          <div className="empty-sub">
            Paste a Looker Studio, Microsoft PowerBI, or Tableau report URL above to embed your dashboard here.
          </div>
        </div>
      )}
    </div>
  )
}
