import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiIngestResult } from '../lib/api'
import { Session } from '../lib/auth'

interface UploadProps {
  session: Session
}

export default function Upload({ session: _session }: UploadProps) {
  const navigate = useNavigate()
  const [sourceFile, setSourceFile] = useState<File | null>(null)
  const [targetFile, setTargetFile] = useState<File | null>(null)
  const [clientName, setClientName] = useState('')
  const [loading,    setLoading]    = useState(false)
  const [error,      setError]      = useState<string | null>(null)
  const [result,     setResult]     = useState<ApiIngestResult | null>(null)

  function csvWarning(file: File | null): string | null {
    if (!file) return null
    return file.name.toLowerCase().endsWith('.csv') ? null : 'Expected a .csv file'
  }

  const canSubmit = sourceFile !== null && targetFile !== null && clientName.trim() !== '' && !loading

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return

    const form = new FormData()
    form.append('source_file', sourceFile!)
    form.append('target_file', targetFile!)
    form.append('client_name', clientName.trim())

    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await api.ingest(form)
      setResult(data)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  function handleReset() {
    setResult(null)
    setError(null)
    setClientName('')
    setSourceFile(null)
    setTargetFile(null)
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Upload Dataset</h1>
        <p className="page-subtitle">
          Upload a source CSV and target schema CSV to trigger a new mapping pipeline run.
        </p>
      </div>

      <div className="upload-card">
        <div className="upload-card-header">
          <span className="upload-card-title">New Pipeline Run</span>
        </div>

        {loading ? (
          <div className="upload-running">
            <div className="spinner" />
            <div>
              <div className="upload-running-title">Running pipeline…</div>
              <div className="upload-running-sub">
                This may take several minutes. Do not close this page.
              </div>
            </div>
          </div>
        ) : result ? (
          <SuccessView
            result={result}
            clientName={clientName}
            onNavigate={() => navigate('/clients')}
            onReset={handleReset}
          />
        ) : (
          <form className="upload-form" onSubmit={handleSubmit}>
            <div className="upload-field">
              <label className="upload-label">
                Client name
              </label>
              <input
                className="upload-input"
                type="text"
                placeholder="e.g. acme-corp"
                value={clientName}
                onChange={(e) => setClientName(e.target.value)}
                disabled={loading}
              />
            </div>

            <div className="upload-field">
              <label className="upload-label">
                Source CSV <span className="upload-label-hint">— your data file</span>
              </label>
              <input
                className="upload-file-input"
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => setSourceFile(e.target.files?.[0] ?? null)}
                disabled={loading}
              />
              {csvWarning(sourceFile) && (
                <div className="upload-warning">{csvWarning(sourceFile)}</div>
              )}
            </div>

            <div className="upload-field">
              <label className="upload-label">
                Target schema CSV <span className="upload-label-hint">— WMS column template</span>
              </label>
              <input
                className="upload-file-input"
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => setTargetFile(e.target.files?.[0] ?? null)}
                disabled={loading}
              />
              {csvWarning(targetFile) && (
                <div className="upload-warning">{csvWarning(targetFile)}</div>
              )}
            </div>

            {error && (
              <div className="upload-error">
                <div className="upload-error-title">Upload failed</div>
                <div className="upload-error-body">{error}</div>
              </div>
            )}

            <div className="upload-actions">
              <button type="submit" className="btn-primary" disabled={!canSubmit}>
                Run pipeline
              </button>
              {!canSubmit && (
                <span className="upload-hint">
                  {clientName.trim() === ''
                    ? 'Enter a client name to continue.'
                    : !sourceFile || !targetFile
                    ? 'Select both CSV files to continue.'
                    : ''}
                </span>
              )}
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

function SuccessView({
  result,
  clientName,
  onNavigate,
  onReset,
}: {
  result: ApiIngestResult
  clientName: string
  onNavigate: () => void
  onReset: () => void
}) {
  return (
    <div className="upload-result">
      <div className="upload-result-header">
        <span className="upload-result-badge">Run created</span>
        <span className="upload-result-runid font-mono">{result.run_id}</span>
      </div>

      <div className="upload-result-grid">
        <div className="upload-result-stat">
          <div className="upload-result-value">{result.total_columns}</div>
          <div className="upload-result-label">Total Columns</div>
        </div>
        <div className="upload-result-stat">
          <div className="upload-result-value" style={{ color: 'var(--blue-text)' }}>
            {result.l1_count}
          </div>
          <div className="upload-result-label">L1 Matches</div>
        </div>
        <div className="upload-result-stat">
          <div className="upload-result-value" style={{ color: 'var(--purple-text)' }}>
            {result.l2_count}
          </div>
          <div className="upload-result-label">L2 Matches</div>
        </div>
        <div className="upload-result-stat">
          <div className="upload-result-value" style={{ color: 'var(--gray-text)' }}>
            {result.fallback_count}
          </div>
          <div className="upload-result-label">Fallback</div>
        </div>
      </div>

      <div className="upload-result-actions">
        <button className="btn-primary" onClick={onNavigate}>
          View mappings for {clientName}
        </button>
        <button className="btn upload-btn-secondary" onClick={onReset}>
          Upload another
        </button>
      </div>
    </div>
  )
}
