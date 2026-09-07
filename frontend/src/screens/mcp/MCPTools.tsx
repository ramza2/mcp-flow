import { useCallback, useEffect, useState } from 'react';
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
import { formatTimestamp, MCP_TOOL_STATUSES } from '../../domain';
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
  const [mutatingId, setMutatingId] = useState<string | null>(null);

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
        setError(toFeedbackError(err, 'Tool 목록을 불러오지 못했습니다.'));
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
    setMutatingId(tool.id);
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
            '다른 작업으로 Tool 상태가 변경되었습니다. 최신 상태를 다시 불러옵니다. ' +
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
        setActionError(toFeedbackError(err, `${action}에 실패했습니다.`));
      }
    } finally {
      setMutatingId(null);
    }
  };

  const columns: Column<MCPToolDto>[] = [
    {
      key: 'displayName',
      label: 'Display Name',
      render: r => (
        <span className="font-medium text-slate-800">{r.display_name ?? r.remote_name}</span>
      ),
    },
    {
      key: 'sourceName',
      label: 'Source Name',
      render: r => <span className="font-mono text-xs text-slate-500">{r.remote_name}</span>,
    },
    {
      key: 'serverName',
      label: 'Server',
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
      label: 'Action',
      width: 'w-28',
      render: r => {
        const action = toolLifecycleAction(r.status);
        const disabled = isLifecycleActionDisabled(r.status);
        const busy = mutatingId === r.id;
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
            {action === 'activate' ? 'Activate' : 'Deactivate'}
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
      <PageHeader title="MCP Tools" description="등록된 MCP Tool의 상태와 정책을 관리합니다." />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="Tool 이름, 서버 검색..."
          onSearch={setSearchInput}
          filters={[
            {
              key: 'status',
              label: 'Tool Status',
              options: MCP_TOOL_STATUSES.map(v => ({ value: v, label: v })),
            },
            {
              key: 'server',
              label: 'Server',
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
            <EmptyState title="등록된 MCP Tool이 없습니다" description="서버에서 Discovery를 실행해 Tool을 가져오세요." />
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
