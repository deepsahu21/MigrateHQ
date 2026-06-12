export default function ConfidenceBadge({ value }: { value: number }) {
  const cls =
    value >= 0.9 ? 'badge-conf-high' :
    value >= 0.7 ? 'badge-conf-mid'  :
                   'badge-conf-low'
  return <span className={`badge ${cls}`}>{value.toFixed(2)}</span>
}
