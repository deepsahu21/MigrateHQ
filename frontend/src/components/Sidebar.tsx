import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Users,
  BarChart2,
  Settings,
  ChevronLeft,
  ChevronRight,
  Moon,
  Sun,
  Database,
  LogOut,
} from 'lucide-react'
import { Session } from '../lib/auth'

interface SidebarProps {
  collapsed: boolean
  onToggleCollapse: () => void
  darkMode: boolean
  onToggleDark: () => void
  session: Session
  onLogout: () => void
}

const NAV_ITEMS = [
  { to: '/overview',  label: 'Overview',  icon: LayoutDashboard },
  { to: '/clients',   label: 'Clients',   icon: Users           },
  { to: '/analytics', label: 'Analytics', icon: BarChart2       },
  { to: '/settings',  label: 'Settings',  icon: Settings        },
]

export default function Sidebar({
  collapsed,
  onToggleCollapse,
  darkMode,
  onToggleDark,
  session,
  onLogout,
}: SidebarProps) {
  return (
    <div className={`sidebar${collapsed ? ' collapsed' : ''}`}>
      <div className="sidebar-header">
        <div className="sidebar-logo-icon">
          <Database size={16} />
        </div>
        <span className="sidebar-logo-text">MigrateHQ</span>
      </div>

      <nav className="sidebar-nav">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            title={collapsed ? label : undefined}
          >
            <Icon size={18} className="nav-icon" />
            <span className="nav-label">{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="sidebar-footer">
        {/* User info */}
        {!collapsed && (
          <div className="sidebar-user">
            <div className="sidebar-user-email">{session.email}</div>
            <div className="sidebar-user-role">{session.role === 'admin' ? 'Admin' : 'Customer'}</div>
          </div>
        )}

        <button
          className="sidebar-btn"
          onClick={onToggleDark}
          title={collapsed ? (darkMode ? 'Light mode' : 'Dark mode') : undefined}
        >
          {darkMode ? <Sun size={16} /> : <Moon size={16} />}
          <span>{darkMode ? 'Light mode' : 'Dark mode'}</span>
        </button>

        <button
          className="sidebar-btn"
          onClick={onLogout}
          title={collapsed ? 'Sign out' : undefined}
        >
          <LogOut size={16} />
          <span>Sign out</span>
        </button>

        <button
          className="sidebar-btn"
          onClick={onToggleCollapse}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          <span>Collapse</span>
        </button>
      </div>
    </div>
  )
}
