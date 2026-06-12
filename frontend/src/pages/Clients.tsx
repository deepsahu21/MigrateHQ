import { useState, useEffect } from 'react'
import { AlertTriangle, Clock } from 'lucide-react'
import StatusBadge from '../components/StatusBadge'
import ConfidenceBadge from '../components/ConfidenceBadge'
import LayerBadge from '../components/LayerBadge'
import { api, ApiClient, ApiMappingResult, ApiRunHistory } from '../lib/api'
import { getClientDisplayName, getClientStatus, formatTimestamp } from '../lib/utils'
import { Session } from '../lib/auth'

interface ClientsProps {
  session: Session
}

export default function Clients({ session }: ClientsProps) {
  const [clients,        setClients]        = useState<ApiClient[]>([])
  const [selectedClient, setSelectedClient] = useState<ApiClient | null>(null)
  const [mappings,       setMappings]       = useState<ApiMappingResult[]>([])
  const [runHistory,     setRunHistory]     = useState<ApiRunHistory[]>([])
  const [loadingList,    setLoadingList]    = useState(true)
  const [loadingDetail,  setLoadingDetail]  = useState(false)
  const [listError,      setListError]      = useState<string | null>(null)
  const [detailError,    setDetailError]    = useState<string | null>(null)

  useEffect(() => {
    setLoadingList(true)
    api.clients()
      .then((data) => {
        // Deduplicate by client_name — keep first occurrence (latest run already returned by API)
        const seen = new Set<string>()
        const unique = data.filter(c => {
          if (seen.has(c.client_name)) return false
          seen.add(c.client_name)
          return true
        })
        setClients(unique)
        if (unique.length > 0) selectClient(unique[0])
      })
      .catch((e: Error) => setListError(e.message))
      .finally(() => setLoadingList(false))
  }, [])

  function selectClient(c: ApiClient) {
    setSelectedClient(c)
    setMappings([])
    setRunHistory([])
    setDetailError(null)
    setLoadingDetail(true)
    Promise.all([
      api.mappings(c.client_name),
      api.runs(c.client_name),
    ])
      .then(([m, r]) => { setMappings(m); setRunHistory(r) })
      .catch((e: Error) => setDetailError(e.message))
      .finally(() => setLoadingDetail(false))
  }

  const isAdmin = session.role === 'admin'
  const reviewQueue = mappings.filter((r) => r.confidence < 0.75)

  if (loadingList) {
    return (
      <div className="page">
        <div className="loading-state"><div className="spinner" />Loading clients…</div>
      </div>
    )
  }

  if (listError) {
    return (
      <div className="page">
        <div className="error-state">
          <div className="error-title">Could not load clients</div>
          <div className="error-message">{listError}</div>
          <div className="error-hint">Make sure the backend is running on port 8000.</div>
        </div>
      </div>
    )
  }

  return (
    <div className="page" style={{ paddingBottom: 48 }}>
      <div className="page-header">
        <h1 className="page-title">Clients</h1>
        <p className="page-subtitle">Select a client to view mapping details</p>
      </div>

      {clients.length === 0 ? (
        <div className="table-card">
          <div className="empty-state">No clients found</div>
        </div>
      ) : (
        <div className="clients-layout">
          {/* Client list — one entry per unique client */}
          <div className="client-list">
            <div className="client-list-header">
              {isAdmin ? 'All Clients' : 'Your Data'}
            </div>
            {clients.map((c) => {
              const status = getClientStatus(c.latest_accuracy_pct)
              return (
                <div
                  key={c.client_name}
                  className={`client-list-item${selectedClient?.client_name === c.client_name ? ' active' : ''}`}
                  onClick={() => selectClient(c)}
                >
                  <div>
                    <div className="client-list-name">
                      {(c as any).display_name || getClientDisplayName(c.client_name)}
                    </div>
                    <div className="client-list-ts">{formatTimestamp(c.last_run_at)}</div>
                  </div>
                  <StatusBadge status={status} />
                </div>
              )
            })}
          </div>

          {/* Client detail */}
          {selectedClient && (
            <div>
              {/* Stat cards from latest run */}
              <div className="detail-stat-cards">
                <div className="detail-stat-card">
                  <div className="detail-stat-value">{selectedClient.total_columns}</div>
                  <div className="detail-stat-label">Total Columns</div>
                </div>
                <div className="detail-stat-card">
                  <div className="detail-stat-value" style={{ color: 'var(--blue-text)' }}>{selectedClient.l1_count}</div>
                  <div className="detail-stat-label">L1 Matches</div>
                </div>
                <div className="detail-stat-card">
                  <div className="detail-stat-value" style={{ color: 'var(--purple-text)' }}>{selectedClient.l2_count}</div>
                  <div className="detail-stat-label">L2 Matches</div>
                </div>
                <div className="detail-stat-card">
                  <div className="detail-stat-value" style={{ color: 'var(--gray-text)' }}>{selectedClient.fallback_count}</div>
                  <div className="detail-stat-label">Fallback</div>
                </div>
                <div className="detail-stat-card">
                  <div className="detail-stat-value" style={{ color: selectedClient.flagged_count > 0 ? 'var(--amber-text)' : 'var(--text-muted)' }}>
                    {selectedClient.flagged_count}
                  </div>
                  <div className="detail-stat-label">Flagged</div>
                </div>
              </div>

              {/* Column mappings from latest run */}
              <div className="table-card section-gap">
                <div className="table-card-header">
                  <span className="table-card-title">Column Mappings</span>
                  {!loadingDetail && (
                    <span className="table-card-count">{mappings.length} columns</span>
                  )}
                </div>
                {loadingDetail ? (
                  <div className="loading-state"><div className="spinner" />Loading mappings…</div>
                ) : detailError ? (
                  <div className="error-state">
                    <div className="error-message">{detailError}</div>
                  </div>
                ) : mappings.length === 0 ? (
                  <div className="empty-state">No mapping results found</div>
                ) : (
                  <table>
                    <thead>
                      <tr>
                        <th>Source Column</th>
                        <th>Target Column</th>
                        <th>Confidence</th>
                        <th>Layer</th>
                        <th>Correct</th>
                        <th>Flagged</th>
                      </tr>
                    </thead>
                    <tbody>
                      {mappings.map((r) => (
                        <tr key={r.source_column}>
                          <td className="font-mono">{r.source_column}</td>
                          <td className="font-mono">{r.target_column ?? <span className="text-muted">— no match —</span>}</td>
                          <td><ConfidenceBadge value={r.confidence} /></td>
                          <td><LayerBadge layer={r.layer} /></td>
                          <td>
                            {r.correct === null
                              ? <span className="text-muted">—</span>
                              : r.correct
                                ? <span style={{ color: 'var(--green-text)', fontWeight: 500 }}>Yes</span>
                                : <span style={{ color: 'var(--red-text)',   fontWeight: 500 }}>No</span>
                            }
                          </td>
                          <td>
                            {r.flagged_for_review
                              ? <span className="flag-icon"><AlertTriangle size={14} /></span>
                              : <span className="text-muted">—</span>
                            }
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Run History */}
              <div className="table-card section-gap">
                <div className="table-card-header">
                  <span className="table-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Clock size={14} /> Run History
                  </span>
                  {!loadingDetail && (
                    <span className="table-card-count">{runHistory.length} runs</span>
                  )}
                </div>
                {loadingDetail ? (
                  <div className="loading-state" style={{ padding: 24 }}><div className="spinner" /></div>
                ) : runHistory.length === 0 ? (
                  <div className="empty-state">No run history found</div>
                ) : (
                  <table>
                    <thead>
                      <tr>
                        <th>Run ID</th>
                        <th>Date</th>
                        <th>Columns</th>
                        <th>L1</th>
                        <th>L2</th>
                        <th>Fallback</th>
                        <th>Accuracy</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runHistory.map((r, idx) => (
                        <tr key={r.run_id}>
                          <td className="font-mono" style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                            {r.run_id.slice(0, 8)}…
                            {idx === 0 && (
                              <span style={{ marginLeft: 6, fontSize: '0.65rem', background: 'var(--blue-bg)', color: 'var(--blue-text)', borderRadius: 4, padding: '1px 5px' }}>
                                latest
                              </span>
                            )}
                          </td>
                          <td>{formatTimestamp(r.created_at)}</td>
                          <td>{r.total_columns ?? '—'}</td>
                          <td style={{ color: 'var(--blue-text)' }}>{r.l1_count ?? '—'}</td>
                          <td style={{ color: 'var(--purple-text)' }}>{r.l2_count ?? '—'}</td>
                          <td style={{ color: 'var(--gray-text)' }}>{r.fallback_count ?? '—'}</td>
                          <td>
                            {r.accuracy_pct != null
                              ? <span style={{ fontWeight: 500 }}>{r.accuracy_pct.toFixed(1)}%</span>
                              : '—'
                            }
                          </td>
                          <td>
                            <span style={{
                              fontSize: '0.75rem',
                              padding: '2px 8px',
                              borderRadius: 4,
                              background: r.status === 'completed' ? 'var(--green-bg)' : 'var(--amber-bg)',
                              color:      r.status === 'completed' ? 'var(--green-text)' : 'var(--amber-text)',
                            }}>
                              {r.status ?? 'unknown'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Review queue */}
              <div className="table-card">
                <div className="table-card-header">
                  <span className="table-card-title">Review Queue</span>
                  {!loadingDetail && (
                    <span className="table-card-count">{reviewQueue.length} items</span>
                  )}
                </div>
                {loadingDetail ? (
                  <div className="loading-state" style={{ padding: 24 }}><div className="spinner" /></div>
                ) : reviewQueue.length === 0 ? (
                  <div className="empty-state">No columns flagged for review</div>
                ) : (
                  <table>
                    <thead>
                      <tr>
                        <th>Source Column</th>
                        <th>Predicted Target</th>
                        <th>Confidence</th>
                        <th>Layer</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {reviewQueue.map((r) => (
                        <tr key={r.source_column}>
                          <td className="font-mono">{r.source_column}</td>
                          <td className="font-mono">{r.target_column ?? '—'}</td>
                          <td><ConfidenceBadge value={r.confidence} /></td>
                          <td><LayerBadge layer={r.layer} /></td>
                          <td>
                            <div className="review-actions">
                              <button className="btn btn-approve">Approve</button>
                              <button className="btn btn-override">Override</button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
