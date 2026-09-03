import { AlertCircle, Lock, Inbox, ServerCrash, RefreshCw } from 'lucide-react';
import Button from './Button';

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void; icon?: React.ReactNode };
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="w-12 h-12 rounded-xl bg-slate-100 flex items-center justify-center text-slate-400 mb-4">
        {icon ?? <Inbox size={22} />}
      </div>
      <h3 className="text-sm font-semibold text-slate-700">{title}</h3>
      {description && <p className="text-sm text-slate-400 mt-1 max-w-xs">{description}</p>}
      {action && (
        <div className="mt-4">
          <Button onClick={action.onClick} icon={action.icon} size="sm">{action.label}</Button>
        </div>
      )}
    </div>
  );
}

export function ErrorState({ message, requestId, onRetry }: { message?: string; requestId?: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="w-12 h-12 rounded-xl bg-red-50 flex items-center justify-center text-red-400 mb-4">
        <ServerCrash size={22} />
      </div>
      <h3 className="text-sm font-semibold text-slate-700">데이터를 불러오지 못했습니다</h3>
      <p className="text-sm text-slate-400 mt-1">{message ?? '일시적인 오류가 발생했습니다.'}</p>
      {requestId && <p className="font-mono text-xs text-slate-300 mt-2">Request ID: {requestId}</p>}
      {onRetry && (
        <div className="mt-4">
          <Button variant="secondary" icon={<RefreshCw size={13} />} size="sm" onClick={onRetry}>
            다시 시도
          </Button>
        </div>
      )}
    </div>
  );
}

export function PermissionDenied() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="w-12 h-12 rounded-xl bg-amber-50 flex items-center justify-center text-amber-400 mb-4">
        <Lock size={22} />
      </div>
      <h3 className="text-sm font-semibold text-slate-700">접근 권한이 없습니다</h3>
      <p className="text-sm text-slate-400 mt-1">이 기능을 사용하려면 관리자에게 권한을 요청하세요.</p>
    </div>
  );
}

export function LoadingSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-4 animate-pulse">
          <div className="h-4 bg-slate-200 rounded flex-1" style={{ width: `${60 + (i * 7) % 30}%` }} />
          <div className="h-4 bg-slate-100 rounded w-20" />
          <div className="h-4 bg-slate-100 rounded w-16" />
        </div>
      ))}
    </div>
  );
}

export function InlineAlert({ type, message }: { type: 'info' | 'warning' | 'error'; message: string }) {
  const styles = {
    info: { bg: 'bg-blue-50 border-blue-200', text: 'text-blue-800', icon: <AlertCircle size={14} className="text-blue-500" /> },
    warning: { bg: 'bg-amber-50 border-amber-200', text: 'text-amber-800', icon: <AlertCircle size={14} className="text-amber-500" /> },
    error: { bg: 'bg-red-50 border-red-200', text: 'text-red-800', icon: <AlertCircle size={14} className="text-red-500" /> },
  };
  const s = styles[type];
  return (
    <div className={`flex items-start gap-2 p-3 rounded-lg border ${s.bg}`}>
      <span className="shrink-0 mt-0.5">{s.icon}</span>
      <p className={`text-sm ${s.text}`}>{message}</p>
    </div>
  );
}
