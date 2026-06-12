import { ClientStatus } from '../types'

export function getClientDisplayName(clientName: string): string {
  const lower = clientName.toLowerCase()
  if (lower.includes('olist')) return 'Olist (test)'
  return clientName.replace(/\.csv$/i, '').replace(/_/g, ' ')
}

export function getClientStatus(accuracyPct: number): ClientStatus {
  if (accuracyPct >= 90) return 'healthy'
  if (accuracyPct >= 70) return 'warning'
  return 'needs-attention'
}

export function formatTimestamp(ts: string): string {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    })
  } catch {
    return ts
  }
}
