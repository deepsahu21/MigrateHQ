import { ClientStatus } from '../types'

const CONFIG: Record<ClientStatus, { label: string; cls: string }> = {
  'healthy':         { label: 'Healthy',         cls: 'badge-healthy'   },
  'warning':         { label: 'Warning',          cls: 'badge-warning'   },
  'needs-attention': { label: 'Needs attention',  cls: 'badge-attention' },
}

export default function StatusBadge({ status }: { status: ClientStatus }) {
  const { label, cls } = CONFIG[status]
  return (
    <span className={`badge ${cls}`}>
      <span className="badge-dot" />
      {label}
    </span>
  )
}
