import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import StatCard from '../components/StatCard'
import StatusBadge from '../components/StatusBadge'
import { api, ApiOverview, ApiActivity, ApiClient } from '../lib/api'
import { getClientDisplayName, getClientStatus, formatTimestamp } from '../lib/utils'
import { Session } from '../lib/auth'

interface OverviewProps {
  session: Session
}

export default function Overview({ session }: OverviewProps) {
  const navigate = useNavigate()

  const [stats,    setStats]    = useState<ApiOverview | null>(null)
  const [activity, setActivity] = useState<ApiActivity[]>([])
  const [clients,  setClients]  = useState<ApiClient[]>([])
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    Promise.all([api.overview(), api.activity(), api.clients()])
      .then(([ov, act, cl]) => {
        setStats(ov)
        setActivity(act)
        setClients(cl)
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const isAdmin = session.role === 'admin'

  if (loading) return <LoadingState />
  if (error)   return <ErrorState message={error} />

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Overview</h1>
        <p className="page-subtitle">
          {isAdmin ? 'All clients and recent mapping activity' : 'Your mapping activity'}
        </p>
      </div>

      <div className="stat-cards">
        <StatCard
          label="Total Clients"
          value={stats?.total_clients ?? 0}
          sub="Active migrations"
        />
        <StatCard
          label="Overall Accuracy"
          value={`${stats?.overall_accuracy_pct ?? 0}%`}
          sub="Across all clients"
        />
        <StatCard
          label="Total Columns Mapped"
          value={stats?.total_columns_mapped ?? 0}
          sub="All runs combined"
        />
        <StatCard
          label="Flagged for Review"
          value={stats?.flagged_count ?? 0}
          sub="Confidence < 0.75"
        />
      </div>

      {clients.length === 0 ? (
        <div className="overview-grid">
          <div className="table-card">
            <div className="table-card-header">
              <span className="table-card-title">Clients</span>
            </div>
            <div className="empty-state">No clients found</div>
          </div>
          <div className="table-card">
            <div className="table-card-header">
              <span className="table-card-title">Recent Activity</span>
            </div>
            <div className="empty-state">No recent activity</div>
          </div>
        </div>
      ) : (
        <div className="overview-grid">
          <div className="table-card">
            <div className="table-card-header">
              <span className="table-card-title">Clients</span>
              <span className="table-card-count">{clients.length} client{clients.length !== 1 ? 's' : ''}</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Client</th>
                  {isAdmin && <th>Dataset</th>}
                  <th>Last Run</th>
                  <th>Accuracy</th>
                  <th>L1</th>
                  <th>L2</th>
                  <th>Flagged</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {clients.map((c) => {
                  const status = getClientStatus(c.latest_accuracy_pct)
                  return (
                    <tr
                      key={c.client_name}
                      className="clickable"
                      onClick={() => navigate('/clients')}
                    >
                      <td style={{ fontWeight: 500 }}>
                        {getClientDisplayName(c.client_name)}
                      </td>
                      {isAdmin && (
                        <td className="font-mono text-secondary" style={{ fontSize: 11 }}>
                          {c.client_name}
                        </td>
                      )}
                      <td className="text-secondary">{formatTimestamp(c.last_run_at)}</td>
                      <td>
                        <span style={{
                          color: c.latest_accuracy_pct >= 90 ? 'var(--green-text)' :
                                 c.latest_accuracy_pct >= 70 ? 'var(--amber-text)' :
                                 'var(--red-text)',
                          fontWeight: 600,
                        }}>
                          {c.latest_accuracy_pct.toFixed(1)}%
                        </span>
                      </td>
                      <td className="text-secondary">{c.l1_count}</td>
                      <td className="text-secondary">{c.l2_count}</td>
                      <td>
                        {c.flagged_count > 0
                          ? <span style={{ color: 'var(--amber-text)', fontWeight: 500 }}>{c.flagged_count}</span>
                          : <span className="text-muted">0</span>
                        }
                      </td>
                      <td><StatusBadge status={status} /></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="table-card">
            <div className="table-card-header">
              <span className="table-card-title">Recent Activity</span>
              <span className="table-card-count">Last {activity.length} runs</span>
            </div>
            {activity.length === 0 ? (
              <div className="empty-state">No recent activity</div>
            ) : (
              <div className="activity-feed">
                {activity.map((run) => (
                  <div key={run.run_id} className="activity-item">
                    <div className="activity-run-id">{run.run_id}</div>
                    <div className="activity-client">{getClientDisplayName(run.source_dataset)}</div>
                    <div className="activity-meta">
                      <span>{formatTimestamp(run.last_run_at)}</span>
                      <span className="activity-dot" />
                      <span>{run.total_columns} cols</span>
                      <span className="activity-dot" />
                      <span style={{
                        color: run.accuracy_pct >= 90 ? 'var(--green-text)' :
                               run.accuracy_pct >= 70 ? 'var(--amber-text)' :
                               'var(--red-text)',
                      }}>
                        {run.accuracy_pct.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function LoadingState() {
  return (
    <div className="page">
      <div className="loading-state">
        <div className="spinner" />
        Loading overview…
      </div>
    </div>
  )
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="page">
      <div className="error-state">
        <div className="error-title">Could not load data</div>
        <div className="error-message">{message}</div>
        <div className="error-hint">Make sure the backend is running on port 8000.</div>
      </div>
    </div>
  )
}
