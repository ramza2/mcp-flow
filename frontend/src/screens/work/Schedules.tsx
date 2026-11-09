import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, RefreshCw } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { type Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton, PermissionDenied } from '../../components/ui/EmptyState';
import { changeScheduleStatus, listSchedules, type ScheduleDto } from '../../api/schedules';
import { isAbortError, isApiError } from '../../api/client';
import { SCHEDULE_STATUSES, formatScheduleTimestamp, labelScheduleTarget, shortenId, type ScheduleStatus } from '../../domain';

const PAGE_SIZE = 20;
const LIVE_REFRESH_MS = 15_000;

function humanScheduleExpression(row: ScheduleDto): string {
  if (row.schedule_type !== 'INTERVAL') return row.schedule_expression;
  const match = /^(?:P([1-9]\d*)D|PT([1-9]\d*)([HMS]))$/.exec(row.schedule_expression);
  if (!match) return row.schedule_expression;
  if (match[1]) return `${Number(match[1]).toLocaleString('ko-KR')}일 간격`;
  const unit = { H: '시간', M: '분', S: '초' }[match[3] as 'H' | 'M' | 'S'];
  return `${Number(match[2]).toLocaleString('ko-KR')}${unit} 간격`;
}

export default function Schedules() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<ScheduleStatus | ''>('');
  const [reload, setReload] = useState(0);
  const [items, setItems] = useState<ScheduleDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; requestId?: string; forbidden?: boolean } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [lastSynced, setLastSynced] = useState<number | null>(null);
  const actionInProgress = useRef(false);

  useEffect(() => {
    const timer = setTimeout(() => { setQuery(searchInput); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let forbidden = false;

    const load = async (quiet: boolean) => {
      if (inFlight || forbidden || actionInProgress.current) return;
      inFlight = true;
      if (!quiet) {
        setLoading(true);
        setError(null);
      }
      try {
        const data = await listSchedules({ page, page_size: PAGE_SIZE, q: query || undefined, status: status || undefined, signal: controller.signal });
        if (controller.signal.aborted) return;
        setItems(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
        setError(null);
        setRefreshError(null);
        setLastSynced(Date.now());
      } catch (err) {
        if (controller.signal.aborted || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        if (apiErr?.status === 403 || !quiet) {
          forbidden = apiErr?.status === 403;
          setError({
            message: apiErr?.message ?? '예약 목록을 불러오지 못했습니다.',
            requestId: apiErr?.requestId ?? undefined,
            forbidden,
          });
        } else {
          setRefreshError('자동 갱신에 실패했습니다. 다음 갱신을 기다리거나 새로고침을 눌러주세요.');
        }
      } finally {
        inFlight = false;
        if (!controller.signal.aborted && !quiet) setLoading(false);
      }
    };

    const refreshVisible = () => {
      if (document.visibilityState === 'visible') void load(true);
    };
    void load(false);
    const timer = window.setInterval(refreshVisible, LIVE_REFRESH_MS);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [page, query, status, reload]);

  const updateStatus = async (row: ScheduleDto) => {
    const action = row.status === 'ACTIVE' ? 'pause'
      : row.last_run_at || row.next_run_at ? 'resume' : 'activate';
    let succeeded = false;
    actionInProgress.current = true;
    setBusyId(row.id);
    setActionError(null);
    try {
      await changeScheduleStatus(row.id, action);
      succeeded = true;
    } catch (err) {
      setActionError(isApiError(err) ? `${err.message}${err.requestId ? ` (Request ID: ${err.requestId})` : ''}` : '예약 상태를 변경하지 못했습니다.');
    } finally {
      actionInProgress.current = false;
      setBusyId(null);
      if (succeeded) setReload(n => n + 1);
    }
  };

  const columns: Column<ScheduleDto>[] = [
    { key: 'name', label: '예약 이름', render: row => <span className="font-medium text-slate-800">{row.name}</span> },
    { key: 'target_type', label: '대상', render: row => (
      <div>
        <p className="text-xs text-slate-600">{labelScheduleTarget(row.target_type)}</p>
        <span className="text-xs font-mono text-slate-400" title={row.target_id}>{shortenId(row.target_id)}</span>
      </div>
    ) },
    { key: 'schedule_type', label: '실행 주기', render: row => (
      <div>
        <p className="text-xs text-slate-700">{row.schedule_type}</p>
        <span className="text-xs text-slate-500" title={row.schedule_expression}>{humanScheduleExpression(row)}</span>
        <p className="text-xs text-slate-400">{row.timezone}</p>
      </div>
    ) },
    { key: 'next_run_at', label: '다음 실행', render: row => <span className="text-xs">{formatScheduleTimestamp(row.next_run_at, row.timezone)}</span> },
    { key: 'last_run_at', label: '최근 실행', render: row => <span className="text-xs">{formatScheduleTimestamp(row.last_run_at, row.timezone)}</span> },
    { key: 'status', label: '상태', render: row => <StatusBadge status={row.status} size="sm" /> },
    { key: 'actions', label: '관리', render: row => (
      <div onClick={event => event.stopPropagation()}>
        {(row.status === 'ACTIVE' || row.status === 'PAUSED') && (
          <Button
            size="sm"
            variant="outline"
            disabled={busyId !== null}
            loading={busyId === row.id}
            onClick={() => void updateStatus(row)}
          >
            {row.status === 'ACTIVE' ? '일시정지' : row.last_run_at || row.next_run_at ? '재개' : '활성화'}
          </Button>
        )}
      </div>
    ) },
  ];

  return (
    <div>
      <PageHeader
        title="예약 실행"
        description="실제 Agent·Workflow 예약을 조회하고 활성화·일시정지할 수 있습니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/schedules/new')}>새 예약</Button>}
      />
      <div className="p-6 space-y-4">
        {actionError && <InlineAlert type="error" message={actionError} />}
        {refreshError && <InlineAlert type="warning" message={refreshError} />}
        <FilterBar
          search
          searchPlaceholder="예약 이름 검색..."
          onSearch={setSearchInput}
          filters={[{ key: 'status', label: '상태', options: SCHEDULE_STATUSES.map(value => ({ value, label: value })) }]}
          onFilter={(_, value) => { setStatus(value as ScheduleStatus | ''); setPage(1); }}
          actions={<Button variant="outline" icon={<RefreshCw size={13} />} onClick={() => setReload(n => n + 1)}>새로고침</Button>}
        />
        <p className="text-xs text-slate-500" role="status">
          {lastSynced ? `마지막 갱신: ${new Date(lastSynced).toLocaleTimeString('ko-KR')} · 화면을 보는 동안 15초마다 자동 갱신` : '예약 목록을 조회 중입니다.'}
        </p>
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {loading ? <LoadingSkeleton rows={6} />
            : error?.forbidden ? <PermissionDenied />
              : error ? <ErrorState message={error.message} requestId={error.requestId} onRetry={() => setReload(n => n + 1)} />
                : items.length === 0 ? <EmptyState title="예약이 없습니다" description="새 예약을 등록하면 먼저 일시정지 상태로 저장됩니다." action={{ label: '새 예약', onClick: () => navigate('/schedules/new') }} />
                  : <>
                    <DataTable columns={columns} data={items} rowKey={row => row.id} onRowClick={row => navigate(`/schedules/${row.id}/edit`)} />
                    <Pagination page={page} pageSize={PAGE_SIZE} total={total} hasNext={hasNext} onPageChange={setPage} />
                  </>}
        </div>
      </div>
    </div>
  );
}
