import { getSession } from './auth'

async function apiFetch<T>(path: string): Promise<T> {
  const session = getSession()
  const headers: Record<string, string> = {}
  if (session?.tenant) headers['X-Tenant'] = session.tenant

  const res = await fetch(path, { headers })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status}: ${body || res.statusText}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  overview: ()                         => apiFetch<ApiOverview>('/api/overview'),
  activity: ()                         => apiFetch<ApiActivity[]>('/api/activity'),
  clients:  ()                         => apiFetch<ApiClient[]>('/api/clients'),
  mappings: (clientName: string)       => apiFetch<ApiMappingResult[]>(
    `/api/clients/${encodeURIComponent(clientName)}/mappings`
  ),
  runs: (clientName: string)           => apiFetch<ApiRunHistory[]>(
    `/api/clients/${encodeURIComponent(clientName)}/runs`
  ),
}

// ── Response types ───────────────────────────────────────────────────────────

export interface ApiOverview {
  total_clients: number
  overall_accuracy_pct: number
  total_columns_mapped: number
  flagged_count: number
}

export interface ApiActivity {
  run_id: string
  source_dataset: string
  last_run_at: string
  total_columns: number
  l1_count: number
  l2_count: number
  accuracy_pct: number
}

export interface ApiClient {
  client_name: string
  last_run_at: string
  total_runs: number
  latest_accuracy_pct: number
  total_columns: number
  l1_count: number
  l2_count: number
  fallback_count: number
  flagged_count: number
}

export interface ApiRunHistory {
  run_id: string
  created_at: string
  total_columns: number | null
  l1_count: number | null
  l2_count: number | null
  fallback_count: number | null
  accuracy_pct: number | null
  status: string | null
}

export interface ApiMappingResult {
  source_column: string
  target_column: string | null
  confidence: number
  layer: 'L1' | 'L2' | 'L1-fallback' | 'none'
  correct: boolean | null
  flagged_for_review: boolean
}
