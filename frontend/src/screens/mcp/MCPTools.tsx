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
import { useAuth } from '../../auth/useAuth';
import { formatTimestamp, MCP_TOOL_STATUSES, labelMCPToolStatus } from '../../domain';
import {
  isLifecycleActionDisabled,
  isVersionConflict,
  mergeToolRow,
  toFeedbackError,
  toolLifecycleAction,
  type FeedbackError,
} from './toolLifecycle';
import {
  MCP_TOOL_SETUP_FLOW_LABEL,
  lifecycleMutationNextAction,
  listEmptyGuidance,
  listLoadFailureGuidance,
  toolLifecycleDisabledReason,
  toolStatusMeaningNote,
} from './mcpToolSetup';

const PAGE_SIZE = 20;

export default function MCPTools() {
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const canEdit = hasPermission('mcp.server.manage');
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
        const feedback = toFeedbackError(err, '도구 목록을 불러오지 못했습니다.');
        if (isApiError(err) && err.status === 403) {
          feedback.code = feedback.code ?? 'FORBIDDEN';
        }
        setError(feedback);
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
        const base =
          '다른 작업으로 도구 상태가 변경되었습니다. 최신 상태를 다시 불러옵니다. ' +
          (isApiError(err) ? err.message : '');
        setActionError({
          message: `${base} ${lifecycleMutationNextAction(action, { code: 'RESOURCE_VERSION_CONFLICT' })}`,
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
        const feedback = toFeedbackError(
          err,
          `${action === 'activate' ? '활성화' : '비활성화'}에 실패했습니다.`,
        );
        setActionError({
          ...feedback,
          message: `${feedback.message} ${lifecycleMutationNextAction(action, feedback)}`,
        });
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
      width: 'w-44',
      render: r => {
        const action = toolLifecycleAction(r.status);
        const reason = toolLifecycleDisabledReason(r, { canEdit });
        const statusBlocked = isLifecycleActionDisabled(r.status) || !action;
        const busy = mutatingIds.has(r.id);
        if (reason || statusBlocked || !action) {
          return (
            <span className="block max-w-[11rem]" onClick={e => e.stopPropagation()}>
              <Button variant="ghost" size="sm" disabled>
                —
              </Button>
              <p className="mt-1 text-[11px] leading-snug text-slate-500" data-testid="mcp-tool-list-disabled-reason">
                {reason ?? '현재 상태에서는 활성화/비활성화할 수 없습니다.'}
              </p>
            </span>
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
        <section
          aria-label="MCP 도구 이용 안내"
          className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600"
          data-testid="mcp-tool-setup-progress"
        >
          <p className="font-semibold text-slate-800">이용 준비: {MCP_TOOL_SETUP_FLOW_LABEL}</p>
          <p className="mt-1">검색으로 도구를 찾은 뒤 상태를 확인하고, 상세에서 실행 정책·권한을 검토한 다음 필요한 도구만 활성화하세요.</p>
          <p className="mt-1 text-xs text-slate-500">{toolStatusMeaningNote()}</p>
          <p className="mt-1 text-xs text-slate-500">활성화는 자동으로 수행되지 않으며, 실행 가능 여부는 서버의 권한·정책 검사가 최종 결정합니다.</p>
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
            <div className="space-y-2 p-4">
              <ErrorState message={error.message} requestId={error.requestId} onRetry={loadTools} />
              <p className="text-xs text-slate-500" data-testid="mcp-tool-list-error-next">
                {listLoadFailureGuidance(error.code)}
              </p>
            </div>
          ) : tools.length === 0 ? (
            <EmptyState
              {...listEmptyGuidance({
                hasFilters: Boolean(debouncedQ || statusFilter || serverFilter),
              })}
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
