import {
  CheckCircle2, XCircle, Clock, AlertTriangle, Minus,
  Loader2, Pause, Ban, RefreshCw, AlertCircle, Eye
} from 'lucide-react';

type StatusCategory = 'neutral' | 'processing' | 'waiting' | 'success' | 'warning' | 'error' | 'disabled';

const STATUS_MAP: Record<string, { category: StatusCategory; label: string }> = {
  // Presentation labels only. Status keys and all API/domain values stay canonical.
  DRAFT: { category: 'neutral', label: '초안' },
  CREATED: { category: 'neutral', label: '생성됨' },
  QUEUED: { category: 'neutral', label: '실행 대기' },
  PENDING: { category: 'neutral', label: '대기' },
  DISCOVERED: { category: 'neutral', label: '발견됨' },
  RECEIVED: { category: 'neutral', label: '접수됨' },
  READY: { category: 'neutral', label: '준비 완료' },
  ANALYZING: { category: 'processing', label: '분석 중' },
  RETRIEVING: { category: 'processing', label: '검색 중' },
  SELECTING: { category: 'processing', label: '선택 중' },
  BUILDING_PARAMETERS: { category: 'processing', label: '입력값 구성 중' },
  PLANNING: { category: 'processing', label: '계획 수립 중' },
  VALIDATING: { category: 'processing', label: '검증 중' },
  RUNNING: { category: 'processing', label: '실행 중' },
  WAITING_INPUT: { category: 'waiting', label: '입력 대기' },
  WAITING_CONFIRMATION: { category: 'waiting', label: '확인 대기' },
  WAITING_APPROVAL: { category: 'waiting', label: '승인 대기' },
  CANCEL_REQUESTED: { category: 'waiting', label: '취소 요청됨' },
  ACTIVE: { category: 'success', label: '활성' },
  PUBLISHED: { category: 'success', label: '게시됨' },
  SUCCEEDED: { category: 'success', label: '성공' },
  APPROVED: { category: 'success', label: '승인됨' },
  VERIFIED: { category: 'success', label: '검증 완료' },
  PARTIALLY_SUCCEEDED: { category: 'warning', label: '일부 성공' },
  WARNING: { category: 'warning', label: '주의' },
  MISSING: { category: 'warning', label: '누락' },
  UNKNOWN_OUTCOME: { category: 'warning', label: '결과 확인 필요' },
  FAILED: { category: 'error', label: '실패' },
  REJECTED: { category: 'error', label: '거절됨' },
  TIMED_OUT: { category: 'error', label: '시간 초과' },
  BLOCKED: { category: 'error', label: '차단됨' },
  INVALID: { category: 'error', label: '검증 실패' },
  PAUSED: { category: 'waiting', label: '일시정지' },
  COMPLETED: { category: 'success', label: '완료' },
  ERROR: { category: 'error', label: '오류' },
  PLANNED: { category: 'neutral', label: '실행 예정' },
  ENQUEUED: { category: 'processing', label: '대기열 등록' },
  SKIPPED: { category: 'disabled', label: '건너뜀' },
  INACTIVE: { category: 'disabled', label: '비활성' },
  CANCELLED: { category: 'disabled', label: '취소됨' },
  EXPIRED: { category: 'disabled', label: '만료됨' },
  ARCHIVED: { category: 'disabled', label: '보관됨' },
  DEPRECATED: { category: 'disabled', label: '이전 버전' },
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
  processing: <Loader2 size={10} />,
  waiting: <Pause size={10} />,
  success: <CheckCircle2 size={10} />,
  warning: <AlertTriangle size={10} />,
  error: <XCircle size={10} />,
  disabled: <Ban size={10} />,
};

interface StatusBadgeProps {
  status: string;
  /** Presentation-only label override; canonical status is unchanged. */
  label?: string;
  size?: 'sm' | 'md';
  /** Disable live progress animation when displaying an immutable history entry. */
  animate?: boolean;
}

export default function StatusBadge({ status, label: labelOverride, size = 'md', animate = true }: StatusBadgeProps) {
  const info = STATUS_MAP[status] ?? { category: 'neutral' as StatusCategory };
  const styles = CATEGORY_STYLES[info.category];
  const icon = CATEGORY_ICONS[info.category];
  const label = labelOverride ?? info.label ?? status.replace(/_/g, ' ');
  const isProcessing = info.category === 'processing';

  return (
    <span
      title={status}
      className={`inline-flex items-center gap-1 rounded-full font-medium
        ${size === 'sm' ? 'text-[10px] px-1.5 py-0.5' : 'text-xs px-2 py-0.5'}
        ${styles.bg} ${styles.text}`}
    >
      <span className={`shrink-0 ${isProcessing && animate ? 'animate-spin' : ''}`}>{icon}</span>
      {label}
    </span>
  );
}

export function RiskBadge({ risk }: { risk: string }) {
  const styles: Record<string, { bg: string; text: string; label: string }> = {
    READ_ONLY: { bg: 'bg-green-50', text: 'text-green-700', label: '읽기 전용' },
    IDEMPOTENT_WRITE: { bg: 'bg-blue-50', text: 'text-blue-700', label: '멱등 쓰기' },
    NON_IDEMPOTENT_WRITE: { bg: 'bg-orange-50', text: 'text-orange-700', label: '중복 실행 위험' },
    DESTRUCTIVE: { bg: 'bg-red-50', text: 'text-red-700', label: '파괴적 작업' },
    UNKNOWN: { bg: 'bg-slate-100', text: 'text-slate-600', label: '위험도 미확인' },
  };
  const s = styles[risk] ?? styles.UNKNOWN;
  return (
    <span title={risk} className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${s.bg} ${s.text}`}>
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
  const label = STATUS_MAP[status]?.label ?? status.replace(/_/g, ' ');
  return (
    <span title={status} className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${s.bg} ${s.text}`}>
      {s.icon} {label}
    </span>
  );
}
