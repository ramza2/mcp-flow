import { useState } from 'react';
import { Outlet, NavLink, useNavigate, useLocation } from 'react-router';
import {
  LayoutDashboard, Play, List, Calendar, CheckSquare,
  Bot, GitBranch, Server, Wrench, Globe, Factory,
  Users, Shield, Clock, ScrollText, Settings, Briefcase,
  ChevronDown, ChevronRight, Menu, X, Bell, User,
  LogOut, ChevronLeft, Zap, Activity
} from 'lucide-react';
import PermissionGate from '../PermissionGate';
import type { Permission } from '../../domain';

interface NavItem {
  label: string;
  path?: string;
  icon?: React.ReactNode;
  children?: NavItem[];
  /** Frontend PermissionGate is UX only. Backend authorization remains authoritative. */
  permission?: Permission;
}

const navConfig: NavItem[] = [
  { label: 'Dashboard', path: '/', icon: <LayoutDashboard size={16} /> },
  {
    label: 'Work',
    icon: <Briefcase size={16} />,
    children: [
      { label: 'Agent Run', path: '/run', icon: <Play size={14} /> },
      { label: 'Executions', path: '/executions', icon: <Activity size={14} />, permission: 'execution.view' },
      { label: 'Schedules', path: '/schedules', icon: <Calendar size={14} />, permission: 'schedule.view' },
      { label: 'Approvals', path: '/approvals', icon: <CheckSquare size={14} />, permission: 'approval.view' },
    ],
  },
  {
    label: 'Build',
    icon: <Zap size={16} />,
    children: [
      { label: 'Agents', path: '/agents', icon: <Bot size={14} />, permission: 'agent.view' },
      { label: 'Workflows', path: '/workflows', icon: <GitBranch size={14} />, permission: 'workflow.view' },
    ],
  },
  {
    label: 'MCP',
    icon: <Server size={16} />,
    permission: 'mcp.view',
    children: [
      { label: 'MCP Servers', path: '/mcp/servers', icon: <Server size={14} />, permission: 'mcp.view' },
      { label: 'MCP Tools', path: '/mcp/tools', icon: <Wrench size={14} />, permission: 'mcp.view' },
      { label: 'External Discovery', path: '/mcp/discovery', icon: <Globe size={14} />, permission: 'mcp.view' },
      { label: 'Tool Factory', path: '/tool-factory', icon: <Factory size={14} />, permission: 'mcp.view' },
    ],
  },
  {
    label: 'Administration',
    icon: <Shield size={16} />,
    permission: 'admin.view',
    children: [
      { label: 'Users & Roles', path: '/admin/users', icon: <Users size={14} />, permission: 'admin.view' },
      { label: 'Roles & Permissions', path: '/admin/roles', icon: <Shield size={14} />, permission: 'admin.view' },
      { label: 'Approval Policies', path: '/admin/approval-policies', icon: <CheckSquare size={14} />, permission: 'admin.view' },
      { label: 'Model Profiles', path: '/admin/model-profiles', icon: <Bot size={14} />, permission: 'admin.view' },
      { label: 'Audit Logs', path: '/admin/audit-logs', icon: <ScrollText size={14} />, permission: 'admin.view' },
      { label: 'Jobs', path: '/admin/jobs', icon: <Clock size={14} />, permission: 'admin.view' },
      { label: 'System Settings', path: '/admin/settings', icon: <Settings size={14} />, permission: 'admin.view' },
    ],
  },
];

function NavSection({ item, collapsed, level = 0 }: { item: NavItem; collapsed: boolean; level?: number }) {
  const location = useLocation();
  const [open, setOpen] = useState(() => {
    if (!item.children) return false;
    return item.children.some(c => c.path && location.pathname.startsWith(c.path));
  });

  const wrapPermission = (node: React.ReactNode) => {
    if (!item.permission) return node;
    // Frontend PermissionGate is UX only. Backend authorization remains authoritative.
    return <PermissionGate permission={item.permission}>{node}</PermissionGate>;
  };

  if (item.path) {
    const isActive = item.path === '/'
      ? location.pathname === '/'
      : location.pathname.startsWith(item.path);
    return wrapPermission(
      <NavLink
        to={item.path}
        title={collapsed ? item.label : undefined}
        className={`flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors duration-100
          ${level > 0 ? 'ml-4 pl-2.5' : ''}
          ${isActive
            ? 'bg-indigo-600 text-white font-medium'
            : 'text-slate-400 hover:text-slate-100 hover:bg-slate-800'
          }`}
      >
        {item.icon && <span className="shrink-0">{item.icon}</span>}
        {!collapsed && <span className="truncate">{item.label}</span>}
      </NavLink>
    );
  }

  const hasActiveChild = item.children?.some(c => c.path && location.pathname.startsWith(c.path));

  return wrapPermission(
    <div>
      <button
        onClick={() => setOpen(v => !v)}
        title={collapsed ? item.label : undefined}
        className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors duration-100
          ${hasActiveChild ? 'text-slate-100' : 'text-slate-400 hover:text-slate-100 hover:bg-slate-800'}`}
      >
        {item.icon && <span className="shrink-0">{item.icon}</span>}
        {!collapsed && (
          <>
            <span className="flex-1 truncate text-left font-medium">{item.label}</span>
            {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </>
        )}
      </button>
      {!collapsed && open && item.children && (
        <div className="mt-0.5 space-y-0.5">
          {item.children.map(child => (
            <NavSection key={child.label} item={child} collapsed={collapsed} level={level + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function AppShell() {
  const [collapsed, setCollapsed] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const navigate = useNavigate();

  const handleLogout = () => {
    navigate('/login');
  };

  return (
    <div className="flex h-full bg-slate-50">
      {/* Sidebar */}
      <aside
        className={`sidebar-transition flex-shrink-0 flex flex-col bg-slate-900 border-r border-slate-700/50 ${collapsed ? 'w-14' : 'w-60'}`}
        style={{ height: '100vh', position: 'sticky', top: 0 }}
      >
        {/* Logo */}
        <div className="flex items-center gap-2.5 px-3 py-4 border-b border-slate-700/50">
          <div className="w-7 h-7 rounded-lg bg-indigo-600 flex items-center justify-center shrink-0">
            <Zap size={14} className="text-white" />
          </div>
          {!collapsed && (
            <div className="min-w-0">
              <div className="text-sm font-semibold text-white">MCPFlow</div>
              <div className="text-[10px] text-slate-400 truncate">AI Agent Platform</div>
            </div>
          )}
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
          {navConfig.map(item => (
            <NavSection key={item.label} item={item} collapsed={collapsed} />
          ))}
        </nav>

        {/* Collapse toggle */}
        <div className="border-t border-slate-700/50 p-2">
          <button
            onClick={() => setCollapsed(v => !v)}
            className="w-full flex items-center justify-center p-2 rounded-md text-slate-400 hover:text-slate-100 hover:bg-slate-800 transition-colors"
          >
            {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
          </button>
        </div>
      </aside>

      {/* Main area */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Top bar */}
        <header className="flex items-center justify-between h-12 bg-white border-b border-slate-200 px-4 shrink-0">
          <div className="flex items-center gap-2 text-sm text-slate-500">
            {/* breadcrumb injected by screens */}
          </div>
          <div className="flex items-center gap-2">
            <button className="p-1.5 rounded-md text-slate-500 hover:text-slate-700 hover:bg-slate-100 relative">
              <Bell size={16} />
              <span className="absolute top-1 right-1 w-1.5 h-1.5 bg-red-500 rounded-full" />
            </button>
            <div className="relative">
              <button
                onClick={() => setUserMenuOpen(v => !v)}
                className="flex items-center gap-2 px-2.5 py-1.5 rounded-md text-sm text-slate-700 hover:bg-slate-100 transition-colors"
              >
                <div className="w-6 h-6 rounded-full bg-indigo-600 flex items-center justify-center text-white text-xs font-medium">A</div>
                <div className="text-left hidden sm:block">
                  <div className="text-xs font-medium leading-none">Admin</div>
                  <div className="text-[10px] text-slate-400 leading-none mt-0.5">KST</div>
                </div>
                <ChevronDown size={12} className="text-slate-400" />
              </button>
              {userMenuOpen && (
                <div className="absolute right-0 top-full mt-1 w-48 bg-white rounded-lg shadow-lg border border-slate-200 py-1 z-50">
                  <div className="px-3 py-2 border-b border-slate-100">
                    <div className="text-xs font-semibold text-slate-800">Administrator</div>
                    <div className="text-xs text-slate-500">admin@mcpflow.io</div>
                    <div className="text-xs text-indigo-600 mt-0.5">Super Admin</div>
                  </div>
                  <button className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
                    <User size={13} /> 프로필
                  </button>
                  <button
                    onClick={handleLogout}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-600 hover:bg-red-50"
                  >
                    <LogOut size={13} /> 로그아웃
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
