import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { RefreshCw } from 'lucide-react';
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
  listApprovals,
  type ApprovalListItemDto,
  type ApprovalSort,
} from '../../api/approvals';
import { isAbortError, isApiError } from '../../api/client';
import {
  APPROVAL_STATUSES,
  formatTimestamp,
  labelApprovalDecisionMode,
  labelApprovalStatus,
  shortenId,
  type ApprovalStatus,
} from '../../domain';
import {
  APPROVAL_REVIEW_FLOW_LABEL,
  APPROVAL_REVIEW_STEPS,
  approvalVsExecutionNote,
  approvalsForbiddenGuidance,
  approvalsListEmptyGuidance,
} from './approvalUx';

const PAGE_SIZE = 20;
const DEFAULT_STATUS: ApprovalStatus = 'PENDING';

export default function Approvals() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<ApprovalStatus | ''>('');
  const [sort, setSort] = useState<ApprovalSort>('expires_at');
  const [reload, setReload] = useState(0);
  const [items, setItems] = useState<ApprovalListItemDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  const effectiveStatus: ApprovalStatus = statusFilter || DEFAULT_STATUS;

  const load = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setForbidden(false);

    listApprovals({
      page,
      page_size: PAGE_SIZE,
      status: effectiveStatus,
      sort,
      signal: controller.signal,
    })
      .then((data) => {
        if (cancelled) return;
        setItems(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setItems([]);
          setTotal(0);
          setHasNext(false);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? '승인 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setItems([]);
        setTotal(0);
        setHasNext(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [page, effectiveStatus, sort, reload]);

  useEffect(() => load(), [load]);

  const columns: Column<ApprovalListItemDto>[] = [
    {
      key: 'id',
      label: '승인 ID',
      render: (row) => (
        <span className="font-mono text-xs text-slate-600" title={row.id}>
          {shortenId(row.id, 12)}
        </span>
      ),
    },
    {
      key: 'status',
      label: '상태',
      render: (row) => (
        <span title={row.status}>
          <StatusBadge status={row.status} label={labelApprovalStatus(row.status)} size="sm" />
        </span>
      ),
    },
    {
      key: 'can_decide',
      label: '결정 가능',
      render: (row) => (
        <span className={`text-xs font-medium ${row.can_decide ? 'text-emerald-700' : 'text-slate-400'}`}>
          {row.can_decide ? '가능' : '불가'}
        </span>
      ),
    },
    {
      key: 'execution_id',
      label: '실행',
      render: (row) => (
        <button
          type="button"
          className="font-mono text-xs text-indigo-600 hover:underline"
          title={row.execution_id}
          onClick={(event) => {
            event.stopPropagation();
            navigate(`/executions/${row.execution_id}`);
          }}
        >
          {shortenId(row.execution_id, 12)}
        </button>
      ),
    },
    {
      key: 'decision_mode',
      label: '결정 방식',
      render: (row) => (
        <span className="text-xs text-slate-600" title={row.decision_mode}>
          {labelApprovalDecisionMode(row.decision_mode)}
          <span className="text-slate-400"> · {row.required_approvals}명</span>
        </span>
      ),
    },
    {
      key: 'votes',
      label: '집계',
      render: (row) => (
        <span className="text-xs text-slate-500">
          승인 {row.approve_count} / 거절 {row.reject_count}
        </span>
      ),
    },
    {
      key: 'requested_by',
      label: '요청자',
      render: (row) => (
        <span className="font-mono text-xs text-slate-500" title={row.requested_by}>
          {shortenId(row.requested_by, 8)}
        </span>
      ),
    },
    {
      key: 'requested_at',
      label: '요청 시각',
      render: (row) => <span className="text-xs text-slate-500">{formatTimestamp(row.requested_at)}</span>,
    },
    {
      key: 'expires_at',
      label: '만료 시각',
      render: (row) => <span className="text-xs text-amber-700">{formatTimestamp(row.expires_at)}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="승인"
        description="승인 권한이 있는 요청을 조회합니다. 기본 목록은 대기 중(PENDING)입니다. 상세에서 PENDING이고 결정 가능할 때만 승인·거절할 수 있습니다."
        actions={
          <Button
            variant="outline"
            icon={<RefreshCw size={13} />}
            onClick={() => setReload((n) => n + 1)}
          >
            새로고침
          </Button>
        }
      />
      <div className="p-6 space-y-4">
        <section
          aria-label="승인 처리 안내"
          className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600"
          data-testid="approval-setup-progress"
        >
          <p className="font-semibold text-slate-800">처리 순서: {APPROVAL_REVIEW_FLOW_LABEL}</p>
          <ol className="mt-2 flex flex-wrap gap-2 text-xs">
            {APPROVAL_REVIEW_STEPS.map((step, index) => (
              <li
                key={step.id}
                className="rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-700"
                data-testid={`approval-setup-step-${step.id}`}
              >
                {index + 1}. {step.label}
              </li>
            ))}
          </ol>
          <p className="mt-2 text-xs text-slate-500" data-testid="approval-vs-execution-note">
            {approvalVsExecutionNote()}
          </p>
        </section>
        <FilterBar
          filters={[
            {
              key: 'status',
              label: '상태',
              options: APPROVAL_STATUSES.map((value) => ({
                value,
                label: labelApprovalStatus(value),
              })),
            },
            {
              key: 'sort',
              label: '정렬',
              options: [
                { value: 'expires_at', label: '만료 빠른 순' },
                { value: '-expires_at', label: '만료 늦은 순' },
                { value: 'requested_at', label: '요청 오래된 순' },
                { value: '-requested_at', label: '요청 최신 순' },
              ],
            },
          ]}
          onFilter={(key, value) => {
            setPage(1);
            if (key === 'status') setStatusFilter((value || '') as ApprovalStatus | '');
            if (key === 'sort') setSort((value || 'expires_at') as ApprovalSort);
          }}
        />
        <p className="text-xs text-slate-500" role="status">
          {statusFilter
            ? `필터: ${labelApprovalStatus(statusFilter)}`
            : '상태 미선택 시 대기 중(PENDING) 승인함을 조회합니다.'}
          {' · '}권한은 서버의 승인 결정(`approval.decide`)을 따릅니다.
        </p>

        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {forbidden ? (
            <div className="py-4">
              <PermissionDenied />
              <p
                className="text-center text-sm text-slate-400 -mt-8 mb-10 px-6"
                data-testid="approvals-forbidden-guidance"
              >
                {approvalsForbiddenGuidance()}
              </p>
            </div>
          ) : loading ? (
            <LoadingSkeleton rows={6} />
          ) : error ? (
            <ErrorState
              message={error.message}
              requestId={error.requestId}
              onRetry={() => setReload((n) => n + 1)}
            />
          ) : items.length === 0 ? (
            <EmptyState
              title={approvalsListEmptyGuidance(effectiveStatus).title}
              description={approvalsListEmptyGuidance(effectiveStatus).description}
            />
          ) : (
            <>
              <DataTable
                columns={columns}
                data={items}
                rowKey={(row) => row.id}
                onRowClick={(row) => navigate(`/approvals/${row.id}`)}
                emptyMessage="조건에 맞는 승인 요청이 없습니다."
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
