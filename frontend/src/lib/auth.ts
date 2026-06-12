export type Role = 'admin' | 'customer'

export interface Session {
  email: string
  role: Role
  tenant: string
  isAuthenticated: boolean
}

const SESSION_KEY = 'migratehq-session'

const USERS: Array<{ email: string; password: string; role: Role; tenant: string }> = [
  { email: 'admin@migratehq.com', password: 'migratehq2024', role: 'admin',    tenant: 'migratehq' },
  { email: 'admin@olist.com',     password: 'olist2024',     role: 'customer', tenant: 'olist'     },
]

export function login(email: string, password: string): Session | null {
  const user = USERS.find((u) => u.email === email && u.password === password)
  if (!user) return null
  const session: Session = {
    email: user.email,
    role: user.role,
    tenant: user.tenant,
    isAuthenticated: true,
  }
  localStorage.setItem(SESSION_KEY, JSON.stringify(session))
  return session
}

export function getSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}

export function logout(): void {
  localStorage.removeItem(SESSION_KEY)
}
