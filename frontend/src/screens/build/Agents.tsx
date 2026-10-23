import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, Bot, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import { VersionBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import { EmptyState, ErrorState, LoadingSkeleton } from '../../components/ui/EmptyState';
import { createAgent, listAgents } from '../../api/agents';
import { isAbortError, isApiError } from '../../api/client';
import type { AgentDto } from '../../api/types';
import { AGENT_STATUSES, formatTimestamp } from '../../domain';

const PAGE_SIZE = 20;

export default function Agents() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [agents, setAgents] = useState<AgentDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQ(searchInput);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const loadAgents = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);

    listAgents({
      page,
      page_size: PAGE_SIZE,
      sort: '-updated_at',
      q: debouncedQ || undefined,
      status: statusFilter || undefined,
      signal: controller.signal,
    })
      .then(data => {
        if (cancelled) return;
        setAgents(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Agent 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setAgents([]);
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
  }, [page, debouncedQ, statusFilter]);

  useEffect(() => {
    return loadAgents();
  }, [loadAgents]);

  const handleFilter = (key: string, value: string) => {
    if (key === 'status') {
      setStatusFilter(value);
      setPage(1);
    }
  };

  const handleNewAgent = async () => {
    setCreating(true);
    setError(null);
    try {
      const created = await createAgent({
        name: 'New Agent',
        description: null,
        visibility: 'PRIVATE',
      });
      navigate(`/agents/${created.id}/versions/new/edit`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setError({
        message: apiErr?.message ?? 'Agent를 생성하지 못했습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      setCreating(false);
    }
  };

  const columns: Column<AgentDto>[] = [
    { key: 'name', label: 'Name', render: r => (
      <div className="flex items-center gap-2">
        <span className="w-7 h-7 rounded-md bg-indigo-50 text-indigo-600 flex items-center justify-center shrink-0"><Bot size={14} /></span>
        <span className="font-medium text-slate-800">{r.name}</span>
      </div>
    )},
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'visibility', label: 'Visibility', render: r => (
      <span className="text-xs text-slate-600">{r.visibility}</span>
    ) },
    { key: 'publishedVersion', label: 'Current Version', render: r => r.current_version_id
      ? <VersionBadge version={`${r.current_version_id.slice(0, 8)}…`} status="PUBLISHED" />
      : <span className="text-xs text-slate-400">–</span> },
    { key: 'owner', label: 'Owner', render: r => (
      <span className="text-xs text-slate-500 font-mono">{r.owner_id ? `${r.owner_id.slice(0, 8)}…` : '—'}</span>
    ) },
    { key: 'updatedAt', label: 'Updated', render: r => (
      <span className="text-xs text-slate-400">{formatTimestamp(r.updated_at)}</span>
    ) },
    { key: 'actions', label: '', render: r => (
      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
        <button onClick={() => navigate(`/agents/${r.id}`)} className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50" title="상세"><Eye size={13} /></button>
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader
        title="Agents"
        description="자연어 요청을 처리하는 AI Agent와 버전을 관리합니다."
        actions={
          <Button icon={<Plus size={14} />} loading={creating} onClick={handleNewAgent}>
            New Agent
          </Button>
        }
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="Agent 이름 검색..."
          onSearch={setSearchInput}
          filters={[
            { key: 'status', label: '상태', options: AGENT_STATUSES.map(v => ({
              value: v,
              label: v.charAt(0) + v.slice(1).toLowerCase(),
            })) },
          ]}
          onFilter={handleFilter}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {loading ? (
            <LoadingSkeleton rows={6} />
          ) : error ? (
            <ErrorState message={error.message} requestId={error.requestId} onRetry={() => { loadAgents(); }} />
          ) : agents.length === 0 ? (
            <EmptyState
              title="등록된 Agent가 없습니다"
              description="New Agent로 본인 소유의 PRIVATE Agent를 생성한 뒤 버전을 게시·활성화하세요."
            />
          ) : (
            <>
              <DataTable
                columns={columns}
                data={agents}
                rowKey={r => r.id}
                onRowClick={r => navigate(`/agents/${r.id}`)}
                emptyMessage="등록된 Agent가 없습니다."
              />
              <Pagination
                page={page}
                pageSize={PAGE_SIZE}
                total={total}
                hasNext={hasNext}
                onPageChange={setPage}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
