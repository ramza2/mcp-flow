import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, GitBranch, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge, { VersionBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import { EmptyState, ErrorState, LoadingSkeleton } from '../../components/ui/EmptyState';
import { listWorkflows, type WorkflowDto } from '../../api/workflows';
import { isAbortError, isApiError } from '../../api/client';
import { WORKFLOW_STATUSES, formatTimestamp, labelWorkflowStatus, labelWorkflowVisibility } from '../../domain';
import type { WorkflowStatus } from '../../domain/types';

const PAGE_SIZE = 20;

export default function Workflows() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<WorkflowStatus | ''>('');
  const [page, setPage] = useState(1);
  const [workflows, setWorkflows] = useState<WorkflowDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => { setDebouncedQuery(query); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const load = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    listWorkflows({
      page, page_size: PAGE_SIZE,
      q: debouncedQuery || undefined,
      status: statusFilter || undefined,
      signal: controller.signal,
    }).then(result => {
      if (cancelled) return;
      setWorkflows(result.items);
      setTotal(result.total);
      setHasNext(result.has_next);
    }).catch(err => {
      if (cancelled || isAbortError(err)) return;
      const apiErr = isApiError(err) ? err : null;
      setError({ message: apiErr?.message ?? '워크플로 목록을 불러오지 못했습니다.', requestId: apiErr?.requestId ?? undefined });
      setWorkflows([]);
      setTotal(0);
      setHasNext(false);
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [page, debouncedQuery, statusFilter]);

  useEffect(() => load(), [load]);

  const columns: Column<WorkflowDto>[] = [
    { key: 'name', label: '이름', render: row => (
      <div className="flex items-center gap-2">
        <span className="w-7 h-7 rounded-md bg-cyan-50 text-cyan-600 flex items-center justify-center shrink-0"><GitBranch size={14} /></span>
        <span className="font-medium text-slate-800">{row.name}</span>
      </div>
    ) },
    { key: 'status', label: '상태', render: row => <StatusBadge status={row.status} label={labelWorkflowStatus(row.status)} size="sm" /> },
    { key: 'visibility', label: '공개 범위', render: row => <span title={row.visibility} className="text-xs text-slate-600">{labelWorkflowVisibility(row.visibility)}</span> },
    { key: 'current', label: '현재 게시 버전', render: row => row.current_version_id
      ? <VersionBadge version={`${row.current_version_id.slice(0, 8)}…`} status="PUBLISHED" />
      : <span className="text-xs text-slate-400">—</span> },
    { key: 'updated', label: '수정일', render: row => <span className="text-xs text-slate-400">{formatTimestamp(row.updated_at)}</span> },
    { key: 'actions', label: '', render: row => (
      <button onClick={event => { event.stopPropagation(); navigate(`/workflows/${row.id}`); }} className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50" title="상세"><Eye size={13} /></button>
    ) },
  ];

  return (
    <div>
      <PageHeader
        title="워크플로 관리"
        description="저장된 워크플로를 조회하고 실행 계획 초안을 관리합니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/workflows/new')}>새 워크플로</Button>}
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search searchPlaceholder="워크플로 이름 검색..." onSearch={setQuery}
          onFilter={(key, value) => { if (key === 'status') { setStatusFilter(value as WorkflowStatus | ''); setPage(1); } }}
          filters={[{ key: 'status', label: '상태', options: WORKFLOW_STATUSES.map(value => ({ value, label: labelWorkflowStatus(value) })) }]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {loading ? <LoadingSkeleton rows={6} /> : error ? (
            <ErrorState message={error.message} requestId={error.requestId} onRetry={() => { load(); }} />
          ) : workflows.length === 0 ? (
            <EmptyState title="등록된 워크플로가 없습니다" description="새 워크플로를 등록하고 실행 계획 초안을 작성하세요." />
          ) : (
            <>
              <DataTable columns={columns} data={workflows} rowKey={row => row.id} onRowClick={row => navigate(`/workflows/${row.id}`)} />
              <Pagination page={page} pageSize={PAGE_SIZE} total={total} hasNext={hasNext} onPageChange={setPage} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
