import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  Activity,
  CheckCircle2,
  XCircle,
  Clock,
  AlertTriangle,
  Pause,
  Server,
  Wrench,
  ChevronRight,
  Lock,
} from 'lucide-react';
import StatusBadge from '../components/ui/StatusBadge';
import {
  EmptyState,
  ErrorState,
  InlineAlert,
  LoadingSkeleton,
} from '../components/ui/EmptyState';
import { listExecutions, type ExecutionListItemDto } from '../api/executions';
import { getDashboardSummary, type DashboardSummaryDto } from '../api/operations';
import { isAbortError, isApiError } from '../api/client';
import {
  formatDurationMs,
  formatTimestamp,
  labelExecutionSource,
  shortenId,
} from '../domain';

function MetricCard({
  label,
  value,
  sub,
  icon,
  color,
}: {
  label: string;
  value: string | number;
  sub?: string;
  icon: React.ReactNode;
  color: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-medium text-slate-500">{label}</span>
        <span className={`p-1.5 rounded-lg ${color}`}>{icon}</span>
      </div>
      <div className="text-2xl font-semibold text-slate-900">{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-1">{sub}</div>}
    </div>
  );
}

function SectionCard({
  title,
  children,
  linkTo,
  linkLabel,
}: {
  title: string;
  children: React.ReactNode;
  linkTo?: string;
  linkLabel?: string;
}) {
  const navigate = useNavigate();
  return (
    <div className="bg-white rounded-xl border border-slate-200">
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100">
        <span className="text-sm font-semibold text-slate-800">{title}</span>
        {linkTo && (
          <button
            onClick={() => navigate(linkTo)}
            className="text-xs text-indigo-600 hover:text-indigo-700 flex items-center gap-0.5"
          >
            {linkLabel ?? '전체 보기'} <ChevronRight size={12} />
          </button>
        )}
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

function RecentList({
  items,
  emptyLabel,
}: {
  items: ExecutionListItemDto[];
  emptyLabel: string;
}) {
  const navigate = useNavigate();
  if (items.length === 0) {
    return <EmptyState title={emptyLabel} />;
  }
  return (
    <div className="space-y-2">
      {items.map((exe) => (
        <div
          key={exe.id}
          onClick={() => navigate(`/executions/${exe.id}`)}
          className="flex items-center justify-between p-2.5 rounded-lg hover:bg-slate-50 cursor-pointer transition-colors"
        >
          <div className="min-w-0">
            <div className="text-sm font-medium text-slate-800 truncate">
              {exe.source?.name ?? labelExecutionSource(exe.source_type)}
            </div>
            <div className="text-xs text-slate-400 font-mono">{shortenId(exe.id, 12)}</div>
          </div>
          <StatusBadge status={exe.status} size="sm" />
        </div>
      ))}
    </div>
  );
}

type Mode = 'loading' | 'ops' | 'own' | 'error';

export default function Dashboard() {
  const [mode, setMode] = useState<Mode>('loading');
  const [summary, setSummary] = useState<DashboardSummaryDto | null>(null);
  const [ownRecent, setOwnRecent] = useState<ExecutionListItemDto[]>([]);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setMode('loading');
    setError(null);

    getDashboardSummary({ recent_limit: 5, signal: controller.signal })
      .then((data) => {
        if (cancelled) return;
        setSummary(data);
        setMode('ops');
      })
      .catch(async (err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          try {
            const list = await listExecutions({
              page: 1,
              page_size: 5,
              sort: '-requested_at',
              signal: controller.signal,
            });
            if (cancelled) return;
            setOwnRecent(list.items);
            setSummary(null);
            setMode('own');
          } catch (fallbackErr: unknown) {
            if (isAbortError(fallbackErr) || cancelled) return;
            const apiErr = isApiError(fallbackErr) ? fallbackErr : null;
            setError({
              message: apiErr?.message ?? 'Dashboard를 불러오지 못했습니다.',
              requestId: apiErr?.requestId ?? undefined,
            });
            setMode('error');
          }
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Dashboard를 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setMode('error');
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  if (mode === 'loading') {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={8} />
      </div>
    );
  }

  if (mode === 'error') {
    return (
      <div className="p-6">
        <ErrorState message={error?.message} requestId={error?.requestId} />
      </div>
    );
  }

  if (mode === 'own') {
    return (
      <div className="p-6 space-y-6">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Dashboard</h1>
          <p className="text-sm text-slate-500 mt-0.5">내 최근 Execution</p>
        </div>
        <InlineAlert
          type="info"
          message="전역 운영 지표는 execution.read 권한이 필요합니다. 아래는 현재 사용자의 최근 Execution입니다."
        />
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <SectionCard title="내 최근 Execution" linkTo="/executions">
            <RecentList items={ownRecent} emptyLabel="최근 Execution이 없습니다." />
          </SectionCard>
          <SectionCard title="전역 Operator 위젯">
            <div className="flex flex-col items-center py-8 text-center">
              <Lock size={20} className="text-amber-400 mb-2" />
              <p className="text-sm text-slate-600">권한이 없습니다</p>
              <p className="text-xs text-slate-400 mt-1">
                Approval / Schedule / MCP 집계는 Operator Dashboard에서만 표시됩니다.
              </p>
            </div>
          </SectionCard>
        </div>
      </div>
    );
  }

  const s = summary!;
  const waiting = s.executions.waiting_input + s.executions.waiting_approval;
  const successPct =
    s.success_rate == null ? '—' : `${Math.round(s.success_rate * 100)}%`;

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Dashboard</h1>
        <p className="text-sm text-slate-500 mt-0.5">
          {formatTimestamp(s.window_from)} – {formatTimestamp(s.window_to)} 집계 · recent는 전역 최신
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <MetricCard
          label="Total Executions"
          value={s.executions.total}
          sub="window"
          icon={<Activity size={14} />}
          color="bg-slate-100 text-slate-600"
        />
        <MetricCard
          label="Success Rate"
          value={successPct}
          sub={`terminal ${s.terminal_total}`}
          icon={<CheckCircle2 size={14} />}
          color="bg-green-100 text-green-600"
        />
        <MetricCard
          label="Running"
          value={s.executions.running}
          icon={<Activity size={14} className="animate-pulse" />}
          color="bg-cyan-100 text-cyan-600"
        />
        <MetricCard
          label="Waiting"
          value={waiting}
          icon={<Pause size={14} />}
          color="bg-amber-100 text-amber-600"
        />
        <MetricCard
          label="Failed"
          value={s.executions.failed}
          icon={<XCircle size={14} />}
          color="bg-red-100 text-red-600"
        />
        <MetricCard
          label="Avg Duration"
          value={formatDurationMs(s.avg_duration_ms)}
          sub={`p95: ${formatDurationMs(s.p95_duration_ms)}`}
          icon={<Clock size={14} />}
          color="bg-indigo-100 text-indigo-600"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SectionCard title="최근 Execution" linkTo="/executions">
          <RecentList items={s.recent_executions} emptyLabel="최근 Execution이 없습니다." />
        </SectionCard>

        <SectionCard title="승인 대기" linkTo="/approvals">
          <div className="grid grid-cols-2 gap-3">
            <MetricCard
              label="Pending"
              value={s.approvals.pending}
              icon={<Pause size={14} />}
              color="bg-amber-100 text-amber-600"
            />
            <MetricCard
              label="Overdue"
              value={s.approvals.overdue}
              icon={<AlertTriangle size={14} />}
              color="bg-red-100 text-red-600"
            />
          </div>
          <p className="text-xs text-slate-400 mt-3">
            Approval 상세는 별도 권한이 필요합니다. 여기서는 집계만 표시합니다.
          </p>
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <SectionCard title="MCP Server (aggregate)" linkTo="/mcp/servers">
          <div className="space-y-2 text-sm text-slate-700">
            <div className="flex justify-between">
              <span className="flex items-center gap-2">
                <Server size={13} className="text-slate-400" /> Total
              </span>
              <span className="font-medium">{s.mcp_servers.total}</span>
            </div>
            <div className="flex justify-between">
              <span>Active</span>
              <span>{s.mcp_servers.active}</span>
            </div>
            <div className="flex justify-between">
              <span>Inactive</span>
              <span>{s.mcp_servers.inactive}</span>
            </div>
            <div className="flex justify-between">
              <span>Error</span>
              <span>{s.mcp_servers.error}</span>
            </div>
            <div className="flex justify-between">
              <span>Draft</span>
              <span>{s.mcp_servers.draft}</span>
            </div>
          </div>
          {s.mcp_servers.inactive + s.mcp_servers.error > 0 && (
            <div className="mt-3 p-2.5 bg-amber-50 border border-amber-100 rounded-lg flex items-center gap-2">
              <AlertTriangle size={13} className="text-amber-600 shrink-0" />
              <span className="text-xs text-amber-700">
                Inactive/Error 서버 {s.mcp_servers.inactive + s.mcp_servers.error}건
              </span>
            </div>
          )}
        </SectionCard>

        <SectionCard title="Tool (aggregate)" linkTo="/mcp/tools">
          <div className="space-y-2 text-sm text-slate-700">
            <div className="flex justify-between">
              <span className="flex items-center gap-2">
                <Wrench size={13} className="text-slate-400" /> Total
              </span>
              <span className="font-medium">{s.mcp_tools.total}</span>
            </div>
            <div className="flex justify-between">
              <span>Missing</span>
              <span>{s.mcp_tools.missing}</span>
            </div>
            <div className="flex justify-between">
              <span>Blocked</span>
              <span>{s.mcp_tools.blocked}</span>
            </div>
            <div className="flex justify-between">
              <span>Problematic</span>
              <span className="font-medium text-orange-600">{s.mcp_tools.problematic}</span>
            </div>
          </div>
        </SectionCard>

        <SectionCard title="Schedules (aggregate)" linkTo="/schedules">
          <div className="space-y-2 text-sm text-slate-700">
            <div className="flex justify-between">
              <span>Active</span>
              <span>{s.schedules.active}</span>
            </div>
            <div className="flex justify-between">
              <span>Paused</span>
              <span>{s.schedules.paused}</span>
            </div>
            <div className="flex justify-between">
              <span>Error</span>
              <span>{s.schedules.error}</span>
            </div>
            <div className="flex justify-between">
              <span>Overdue</span>
              <span className="font-medium text-orange-600">{s.schedules.overdue}</span>
            </div>
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
