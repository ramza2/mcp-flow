import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { type Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import {
  EmptyState,
  ErrorState,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import {
  listExecutions,
  type ExecutionListItemDto,
  type ExecutionSort,
} from '../../api/executions';
import { isAbortError, isApiError } from '../../api/client';
import {
  EXECUTION_SOURCE_TYPES,
  EXECUTION_STATUSES,
  formatDurationMs,
  formatTimestamp,
  labelExecutionSource,
  shortenId,
} from '../../domain';

const PAGE_SIZE = 20;

export default function Executions() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [sort, setSort] = useState<ExecutionSort>('-requested_at');
  const [items, setItems] = useState<ExecutionListItemDto[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQ(searchInput);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const load = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setForbidden(false);

    listExecutions({
      page,
      page_size: PAGE_SIZE,
      q: debouncedQ || undefined,
      status: statusFilter || undefined,
      source_type: sourceFilter || undefined,
      sort,
      signal: controller.signal,
    })
      .then((data) => {
        if (cancelled) return;
        setItems(data.items);
        setTotal(data.total);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setItems([]);
          setTotal(0);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Execution 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setItems([]);
        setTotal(0);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [page, debouncedQ, statusFilter, sourceFilter, sort]);

  useEffect(() => load(), [load]);

  const hasNext = page * PAGE_SIZE < total;
  const hasFilters = Boolean(debouncedQ || statusFilter || sourceFilter);

  const columns: Column<ExecutionListItemDto>[] = [
    {
      key: 'id',
      label: 'Execution ID',
      render: (r) => (
        <span className="font-mono text-xs text-slate-500">{shortenId(r.id, 12)}</span>
      ),
    },
    {
      key: 'source',
      label: 'Source',
      render: (r) => (
        <div className="min-w-0">
          <div className="text-sm font-medium text-slate-800 truncate">
            {r.source?.name ?? '—'}
          </div>
          <span className="text-xs bg-slate-100 px-2 py-0.5 rounded text-slate-600">
            {labelExecutionSource(r.source_type)}
          </span>
        </div>
      ),
    },
    {
      key: 'requester',
      label: 'Requester',
      render: (r) => (
        <span className="text-slate-600 font-mono text-xs">{shortenId(r.requester_id, 10)}</span>
      ),
    },
    {
      key: 'status',
      label: '상태',
      render: (r) => <StatusBadge status={r.status} size="sm" />,
    },
    {
      key: 'steps',
      label: 'Steps',
      align: 'center',
      render: (r) => (
        <span className="text-sm text-slate-500">
          {r.completed_step_count} / {r.step_count}
        </span>
      ),
    },
    {
      key: 'duration',
      label: 'Duration',
      render: (r) => (
        <span className="font-mono text-xs text-slate-500">
          {formatDurationMs(r.duration_ms)}
        </span>
      ),
    },
    {
      key: 'requested',
      label: 'Requested',
      render: (r) => (
        <span className="text-xs text-slate-500">{formatTimestamp(r.requested_at)}</span>
      ),
    },
    {
      key: 'started',
      label: 'Started',
      render: (r) => (
        <span className="text-xs text-slate-500">{formatTimestamp(r.started_at)}</span>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="Executions"
        description="실행 기록을 조회하고 상태를 모니터링합니다."
        actions={
          <Button icon={<Plus size={14} />} onClick={() => navigate('/run')}>
            새 실행
          </Button>
        }
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="ID, trace, error_code..."
          onSearch={setSearchInput}
          filters={[
            {
              key: 'status',
              label: '상태',
              options: EXECUTION_STATUSES.map((v) => ({ value: v, label: v })),
            },
            {
              key: 'sourceType',
              label: 'Source',
              options: EXECUTION_SOURCE_TYPES.map((v) => ({
                value: v,
                label: labelExecutionSource(v),
              })),
            },
            {
              key: 'sort',
              label: 'Sort',
              options: [
                { value: '-requested_at', label: 'Requested ↓' },
                { value: 'requested_at', label: 'Requested ↑' },
                { value: '-started_at', label: 'Started ↓' },
                { value: 'started_at', label: 'Started ↑' },
                { value: '-finished_at', label: 'Finished ↓' },
                { value: 'status', label: 'Status ↑' },
              ],
            },
          ]}
          onFilter={(key, value) => {
            setPage(1);
            if (key === 'status') setStatusFilter(value);
            if (key === 'sourceType') setSourceFilter(value);
            if (key === 'sort') setSort((value || '-requested_at') as ExecutionSort);
          }}
        />

        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {forbidden ? (
            <PermissionDenied />
          ) : loading ? (
            <LoadingSkeleton rows={6} />
          ) : error ? (
            <ErrorState
              message={error.message}
              requestId={error.requestId}
              onRetry={() => load()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              title={hasFilters ? '조건에 맞는 Execution이 없습니다.' : 'Execution이 없습니다.'}
              description={
                hasFilters
                  ? '필터를 변경하거나 검색어를 비워 보세요.'
                  : 'Agent/Workflow/Schedule로 실행을 시작하면 여기에 표시됩니다.'
              }
            />
          ) : (
            <>
              <DataTable
                columns={columns}
                data={items}
                rowKey={(r) => r.id}
                onRowClick={(r) => navigate(`/executions/${r.id}`)}
                emptyMessage="조건에 맞는 Execution이 없습니다."
              />
              <Pagination
                page={page}
                pageSize={PAGE_SIZE}
                total={total}
                hasNext={hasNext}
                onPageChange={setPage}
                disabled={loading}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
