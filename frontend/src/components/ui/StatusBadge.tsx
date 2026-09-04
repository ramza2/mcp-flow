import {
  CheckCircle2, XCircle, Clock, AlertTriangle, Minus,
  Loader2, Pause, Ban, RefreshCw, AlertCircle, Eye
} from 'lucide-react';

type StatusCategory = 'neutral' | 'processing' | 'waiting' | 'success' | 'warning' | 'error' | 'disabled';

const STATUS_MAP: Record<string, { category: StatusCategory; label?: string }> = {
  // Neutral
  DRAFT: { category: 'neutral' },
  CREATED: { category: 'neutral' },
  QUEUED: { category: 'neutral' },
  PENDING: { category: 'neutral' },
  DISCOVERED: { category: 'neutral' },
  RECEIVED: { category: 'neutral' },
  READY: { category: 'neutral' },
  // Processing
  ANALYZING: { category: 'processing' },
  RETRIEVING: { category: 'processing' },
  SELECTING: { category: 'processing' },
  BUILDING_PARAMETERS: { category: 'processing', label: 'Building Params' },
  PLANNING: { category: 'processing' },
  VALIDATING: { category: 'processing' },
  RUNNING: { category: 'processing' },
  // Waiting
  WAITING_INPUT: { category: 'waiting', label: 'Waiting Input' },
  WAITING_CONFIRMATION: { category: 'waiting', label: 'Waiting Confirmation' },
  WAITING_APPROVAL: { category: 'waiting', label: 'Waiting Approval' },
  CANCEL_REQUESTED: { category: 'waiting', label: 'Cancel Requested' },
  // Success
  ACTIVE: { category: 'success' },
  PUBLISHED: { category: 'success' },
  SUCCEEDED: { category: 'success' },
  APPROVED: { category: 'success' },
  VERIFIED: { category: 'success' },
  // Warning
  PARTIALLY_SUCCEEDED: { category: 'warning', label: 'Partial Success' },
  WARNING: { category: 'warning' },
  MISSING: { category: 'warning' },
  UNKNOWN_OUTCOME: { category: 'warning', label: 'Unknown Outcome' },
  // Error
  FAILED: { category: 'error' },
  REJECTED: { category: 'error' },
  TIMED_OUT: { category: 'error', label: 'Timed Out' },
  BLOCKED: { category: 'error' },
  INVALID: { category: 'error' },
  // Schedule / Occurrence
  PAUSED: { category: 'waiting' },
  COMPLETED: { category: 'success' },
  ERROR: { category: 'error' },
  PLANNED: { category: 'neutral' },
  ENQUEUED: { category: 'processing' },
  SKIPPED: { category: 'disabled' },
  // Disabled
  INACTIVE: { category: 'disabled' },
  CANCELLED: { category: 'disabled' },
  EXPIRED: { category: 'disabled' },
  ARCHIVED: { category: 'disabled' },
  DEPRECATED: { category: 'disabled' },
};

const CATEGORY_STYLES: Record<StatusCategory, { bg: string; text: string; dot: string }> = {
  neutral: { bg: 'bg-slate-100', text: 'text-slate-600', dot: 'bg-slate-400' },
  processing: { bg: 'bg-cyan-50', text: 'text-cyan-700', dot: 'bg-cyan-500' },
  waiting: { bg: 'bg-amber-50', text: 'text-amber-700', dot: 'bg-amber-500' },
  success: { bg: 'bg-green-50', text: 'text-green-700', dot: 'bg-green-500' },
  warning: { bg: 'bg-orange-50', text: 'text-orange-700', dot: 'bg-orange-500' },
  error: { bg: 'bg-red-50', text: 'text-red-700', dot: 'bg-red-500' },
  disabled: { bg: 'bg-slate-100', text: 'text-slate-400', dot: 'bg-slate-300' },
};

const CATEGORY_ICONS: Record<StatusCategory, React.ReactNode> = {
  neutral: <Minus size={10} />,
  processing: <Loader2 size={10} className="animate-spin" />,
  waiting: <Pause size={10} />,
  success: <CheckCircle2 size={10} />,
  warning: <AlertTriangle size={10} />,
  error: <XCircle size={10} />,
  disabled: <Ban size={10} />,
};

interface StatusBadgeProps {
  status: string;
  size?: 'sm' | 'md';
}

export default function StatusBadge({ status, size = 'md' }: StatusBadgeProps) {
  const info = STATUS_MAP[status] ?? { category: 'neutral' as StatusCategory };
  const styles = CATEGORY_STYLES[info.category];
  const icon = CATEGORY_ICONS[info.category];
  const label = info.label ?? status.replace(/_/g, ' ');
  const isProcessing = info.category === 'processing';

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full font-medium
        ${size === 'sm' ? 'text-[10px] px-1.5 py-0.5' : 'text-xs px-2 py-0.5'}
        ${styles.bg} ${styles.text}`}
    >
      <span className={`shrink-0 ${isProcessing ? 'animate-spin' : ''}`}>{icon}</span>
      {label}
    </span>
  );
}

export function RiskBadge({ risk }: { risk: string }) {
  const styles: Record<string, { bg: string; text: string; label: string }> = {
    READ_ONLY: { bg: 'bg-green-50', text: 'text-green-700', label: 'Read Only' },
    IDEMPOTENT_WRITE: { bg: 'bg-blue-50', text: 'text-blue-700', label: 'Idempotent Write' },
    NON_IDEMPOTENT_WRITE: { bg: 'bg-orange-50', text: 'text-orange-700', label: 'Non-Idempotent Write' },
    DESTRUCTIVE: { bg: 'bg-red-50', text: 'text-red-700', label: 'Destructive' },
    UNKNOWN: { bg: 'bg-slate-100', text: 'text-slate-600', label: 'Unknown Risk' },
  };
  const s = styles[risk] ?? styles.UNKNOWN;
  return (
    <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${s.bg} ${s.text}`}>
      {risk === 'DESTRUCTIVE' && <AlertCircle size={10} />}
      {risk === 'NON_IDEMPOTENT_WRITE' && <AlertTriangle size={10} />}
      {s.label}
    </span>
  );
}

export function VersionBadge({ version, status }: { version: string; status: string }) {
  const statusStyles: Record<string, string> = {
    DRAFT: 'bg-amber-50 text-amber-700 border-amber-200',
    PUBLISHED: 'bg-green-50 text-green-700 border-green-200',
    DEPRECATED: 'bg-slate-100 text-slate-500 border-slate-200',
  };
  const s = statusStyles[status] ?? statusStyles.DRAFT;
  return (
    <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border font-mono font-medium ${s}`}>
      {version}
    </span>
  );
}

export function VerificationBadge({ status }: { status: string }) {
  const styles: Record<string, { bg: string; text: string; icon: React.ReactNode }> = {
    PENDING: { bg: 'bg-slate-100', text: 'text-slate-500', icon: <Clock size={10} /> },
    VERIFIED: { bg: 'bg-green-50', text: 'text-green-700', icon: <CheckCircle2 size={10} /> },
    FAILED: { bg: 'bg-red-50', text: 'text-red-700', icon: <XCircle size={10} /> },
    EXPIRED: { bg: 'bg-slate-100', text: 'text-slate-400', icon: <RefreshCw size={10} /> },
  };
  const s = styles[status] ?? styles.PENDING;
  return (
    <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${s.bg} ${s.text}`}>
      {s.icon} {status}
    </span>
  );
}
