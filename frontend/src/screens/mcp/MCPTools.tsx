import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Pagination from '../../components/ui/Pagination';
import Button from '../../components/ui/Button';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { activateMCPTool, deactivateMCPTool, getMCPTool, listMCPServers, listMCPTools } from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { MCPServerDto, MCPToolDto } from '../../api/types';
import { formatTimestamp, MCP_TOOL_STATUSES, labelMCPToolStatus } from '../../domain';
import {
  isLifecycleActionDisabled,
  isVersionConflict,
  mergeToolRow,
  toFeedbackError,
  toolLifecycleAction,
  type FeedbackError,
} from './toolLifecycle';

const PAGE_SIZE = 20;

export default function MCPTools() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [serverFilter, setServerFilter] = useState('');
  const [tools, setTools] = useState<MCPToolDto[]>([]);
  const [servers, setServers] = useState<MCPServerDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<FeedbackError | null>(null);
  const [actionError, setActionError] = useState<FeedbackError | null>(null);
  const [mutatingIds, setMutatingIds] = useState<Set<string>>(() => new Set());
  const mutatingIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQ(searchInput);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    const controller = new AbortController();
    listMCPServers({ page_size: 100, signal: controller.signal })
      .then(data => setServers(data.items))
      .catch(err => {
        if (!isAbortError(err)) setServers([]);
      });
    return () => controller.abort();
  }, []);

  const loadTools = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);

    listMCPTools({
      page,
      page_size: PAGE_SIZE,
      sort: '-updated_at',
      q: debouncedQ || undefined,
      status: statusFilter || undefined,
      mcp_server_id: serverFilter || undefined,
      signal: controller.signal,
    })
      .then(data => {
        if (cancelled) return;
        setTools(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        setError(toFeedbackError(err, '도구 목록을 불러오지 못했습니다.'));
        setTools([]);
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
  }, [page, debouncedQ, statusFilter, serverFilter]);

  useEffect(() => {
    return loadTools();
  }, [loadTools]);

  const serverName = (id: string) => servers.find(s => s.id === id)?.name ?? shortenServerId(id);

  const runLifecycle = async (tool: MCPToolDto, action: 'activate' | 'deactivate') => {
    if (mutatingIdsRef.current.has(tool.id)) return;
    mutatingIdsRef.current.add(tool.id);
    setMutatingIds(new Set(mutatingIdsRef.current));
    setActionError(null);
    try {
      const updated =
        action === 'activate'
          ? await activateMCPTool(tool.id, tool.lock_version)
          : await deactivateMCPTool(tool.id, tool.lock_version);
      setTools(prev => mergeToolRow(prev, updated));
    } catch (err) {
      if (isVersionConflict(err)) {
        setActionError({
          message:
            '다른 작업으로 도구 상태가 변경되었습니다. 최신 상태를 다시 불러옵니다. ' +
            (isApiError(err) ? err.message : ''),
          requestId: isApiError(err) ? err.requestId ?? undefined : undefined,
          code: isApiError(err) ? err.code : undefined,
        });
        try {
          const latest = await getMCPTool(tool.id);
          setTools(prev => mergeToolRow(prev, latest));
        } catch {
          loadTools();
        }
      } else {
        setActionError(toFeedbackError(err, `${action === 'activate' ? '활성화' : '비활성화'}에 실패했습니다.`));
      }
    } finally {
      mutatingIdsRef.current.delete(tool.id);
      setMutatingIds(new Set(mutatingIdsRef.current));
    }
  };

  const columns: Column<MCPToolDto>[] = [
    {
      key: 'displayName',
      label: '표시 이름',
      render: r => (
        <span className="font-medium text-slate-800">{r.display_name ?? r.remote_name}</span>
      ),
    },
    {
      key: 'sourceName',
      label: '원본 도구 이름',
      render: r => <span className="font-mono text-xs text-slate-500">{r.remote_name}</span>,
    },
    {
      key: 'serverName',
      label: 'MCP 서버',
      render: r => <span className="text-sm text-slate-600">{serverName(r.mcp_server_id)}</span>,
    },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    {
      key: 'updatedAt',
      label: '수정일',
      render: r => <span className="text-xs text-slate-400">{formatTimestamp(r.updated_at)}</span>,
    },
    {
      key: 'actions',
      label: '작업',
      width: 'w-28',
      render: r => {
        const action = toolLifecycleAction(r.status);
        const disabled = isLifecycleActionDisabled(r.status);
        const busy = mutatingIds.has(r.id);
        if (disabled || !action) {
          return (
            <Button
              variant="ghost"
              size="sm"
              disabled
              title="현재 상태에서는 활성화/비활성화할 수 없습니다."
              onClick={e => e.stopPropagation()}
            >
              —
            </Button>
          );
        }
        return (
          <Button
            variant="outline"
            size="sm"
            loading={busy}
            onClick={e => {
              e.stopPropagation();
              void runLifecycle(r, action);
            }}
          >
            {action === 'activate' ? '활성화' : '비활성화'}
          </Button>
        );
      },
    },
  ];

  const handleFilter = (key: string, value: string) => {
    setPage(1);
    if (key === 'status') setStatusFilter(value);
    if (key === 'server') setServerFilter(value);
  };

  return (
    <div>
      <PageHeader title="MCP 도구 관리" description="서버에서 탐색한 도구의 상태와 사용 정책을 확인합니다." />
      <div className="p-6 space-y-4">
        <section aria-label="MCP 도구 이용 안내" className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
          <p className="font-semibold text-slate-800">도구 사용 전 확인하세요</p>
          <p className="mt-1">서버에서 도구를 탐색한 뒤 필요한 도구만 활성화하세요. 도구 상세에서 설명·위험도·실행 정책과 검증 결과를 먼저 확인하는 것이 좋습니다.</p>
          <p className="mt-1 text-xs text-slate-500">활성화 버튼은 도구 상태를 변경하며, 실제 실행 가능 여부는 서버의 권한 및 정책 검사에 따라 결정됩니다.</p>
        </section>
        <FilterBar
          search
          searchPlaceholder="도구 이름, 서버 이름 검색..."
          onSearch={setSearchInput}
          filters={[
            {
              key: 'status',
              label: '도구 상태',
              options: MCP_TOOL_STATUSES.map(v => ({ value: v, label: labelMCPToolStatus(v) })),
            },
            {
              key: 'server',
              label: 'MCP 서버',
              options: servers.map(s => ({ value: s.id, label: s.name })),
            },
          ]}
          onFilter={handleFilter}
        />
        {actionError && (
          <div className="space-y-1">
            <InlineAlert type="error" message={actionError.message} />
            {actionError.requestId && (
              <p className="font-mono text-xs text-slate-400">Request ID: {actionError.requestId}</p>
            )}
          </div>
        )}
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {loading ? (
            <LoadingSkeleton rows={6} />
          ) : error ? (
            <ErrorState message={error.message} requestId={error.requestId} onRetry={loadTools} />
          ) : tools.length === 0 ? (
            <EmptyState
              title={debouncedQ || statusFilter || serverFilter ? '조건에 맞는 MCP 도구가 없습니다' : '등록된 MCP 도구가 없습니다'}
              description={debouncedQ || statusFilter || serverFilter ? '검색어 또는 필터를 변경해 보세요.' : 'MCP 서버에서 도구 탐색 결과를 미리 확인하고 적용해 주세요.'}
            />
          ) : (
            <>
              <DataTable
                columns={columns}
                data={tools}
                rowKey={r => r.id}
                onRowClick={r => navigate(`/mcp/tools/${r.id}`)}
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

function shortenServerId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}
