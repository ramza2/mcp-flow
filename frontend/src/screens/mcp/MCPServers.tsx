import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, RefreshCw, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import { EmptyState, ErrorState, LoadingSkeleton } from '../../components/ui/EmptyState';
import { connectionTestMCPServer, listMCPServers } from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { ConnectionTestDto, MCPServerDto } from '../../api/types';
import {
  labelDiscoveryMode,
  labelProtocolEra,
  labelTransport,
  labelCheckStatus,
  formatTimestamp,
  MCP_SERVER_STATUSES,
  MCP_TRANSPORT_TYPES,
} from '../../domain';

const PAGE_SIZE = 20;

export default function MCPServers() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [transportFilter, setTransportFilter] = useState('');
  const [servers, setServers] = useState<MCPServerDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ConnectionTestDto>>({});

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQ(searchInput);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const loadServers = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);

    listMCPServers({
      page,
      page_size: PAGE_SIZE,
      sort: '-updated_at',
      q: debouncedQ || undefined,
      status: statusFilter || undefined,
      transport_type: transportFilter || undefined,
      signal: controller.signal,
    })
      .then(data => {
        if (cancelled) return;
        setServers(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? '서버 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setServers([]);
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
  }, [page, debouncedQ, statusFilter, transportFilter]);

  useEffect(() => {
    return loadServers();
  }, [loadServers]);

  const runConnectionTest = async (serverId: string) => {
    setTestingId(serverId);
    try {
      const result = await connectionTestMCPServer(serverId);
      setTestResults(prev => ({ ...prev, [serverId]: result }));
    } catch (err) {
      if (isApiError(err)) {
        setTestResults(prev => ({
          ...prev,
          [serverId]: {
            id: '',
            mcp_server_id: serverId,
            check_type: 'MANUAL',
            status: 'FAILED',
            latency_ms: null,
            protocol_version: null,
            discovery_mode: null,
            error_layer: null,
            error_code: err.code,
            error_message: err.message,
            checked_at: new Date().toISOString(),
          },
        }));
      }
    } finally {
      setTestingId(null);
    }
  };

  const columns: Column<MCPServerDto>[] = [
    {
      key: 'name',
      label: 'Name',
      render: r => <span className="font-medium text-slate-800">{r.name}</span>,
    },
    {
      key: 'transport',
      label: 'Transport',
      render: r => (
        <span className="text-xs font-mono bg-slate-100 px-2 py-0.5 rounded text-slate-600">
          {labelTransport(r.transport_type)}
        </span>
      ),
    },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    {
      key: 'protocol',
      label: 'Protocol era',
      render: r => <span className="text-xs text-slate-600">{labelProtocolEra(r.protocol_era)}</span>,
    },
    {
      key: 'version',
      label: 'Version',
      render: r => (
        <span className="font-mono text-xs text-slate-500">{r.negotiated_protocol_version ?? '—'}</span>
      ),
    },
    {
      key: 'discoveryMode',
      label: 'Discovery Mode',
      render: r => (
        <span className="text-xs text-slate-500">
          {r.discovery_mode ? labelDiscoveryMode(r.discovery_mode) : '—'}
        </span>
      ),
    },
    {
      key: 'lastHealth',
      label: 'Last Health',
      render: r => <span className="text-xs text-slate-400">{formatTimestamp(r.last_healthy_at)}</span>,
    },
    {
      key: 'updatedAt',
      label: 'Updated At',
      render: r => <span className="text-xs text-slate-400">{formatTimestamp(r.updated_at)}</span>,
    },
    {
      key: 'actions',
      label: '',
      render: r => {
        const testResult = testResults[r.id];
        return (
          <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
            <button
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="상세"
              onClick={() => navigate(`/mcp/servers/${r.id}`)}
            >
              <Eye size={13} />
            </button>
            <button
              className="p-1.5 rounded text-slate-400 hover:text-cyan-600 hover:bg-cyan-50 disabled:opacity-50"
              title="연결 테스트"
              disabled={testingId === r.id}
              onClick={() => runConnectionTest(r.id)}
            >
              <RefreshCw size={13} className={testingId === r.id ? 'animate-spin' : ''} />
            </button>
            {testResult && (
              <span
                className={`text-xs font-medium ${
                  testResult.status === 'SUCCEEDED'
                    ? 'text-green-600'
                    : testResult.status === 'TIMED_OUT'
                      ? 'text-amber-600'
                      : 'text-red-600'
                }`}
              >
                {labelCheckStatus(testResult.status)}
              </span>
            )}
          </div>
        );
      },
    },
  ];

  const handleFilter = (key: string, value: string) => {
    setPage(1);
    if (key === 'status') setStatusFilter(value);
    if (key === 'transport') setTransportFilter(value);
  };

  return (
    <div>
      <PageHeader
        title="MCP Servers"
        description="등록된 MCP Server와 Tool Discovery 상태를 관리합니다."
        actions={
          <Button icon={<Plus size={14} />} onClick={() => navigate('/mcp/servers/new')}>
            Register MCP Server
          </Button>
        }
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="서버 이름 검색..."
          onSearch={setSearchInput}
          filters={[
            {
              key: 'status',
              label: '상태',
              options: MCP_SERVER_STATUSES.map(v => ({ value: v, label: v })),
            },
            {
              key: 'transport',
              label: 'Transport',
              options: MCP_TRANSPORT_TYPES.map(v => ({ value: v, label: labelTransport(v) })),
            },
          ]}
          onFilter={handleFilter}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {loading ? (
            <LoadingSkeleton rows={6} />
          ) : error ? (
            <ErrorState message={error.message} requestId={error.requestId} onRetry={loadServers} />
          ) : servers.length === 0 ? (
            <EmptyState title="등록된 MCP Server가 없습니다" description="새 서버를 등록해 보세요." />
          ) : (
            <>
              <DataTable
                columns={columns}
                data={servers}
                rowKey={r => r.id}
                onRowClick={r => navigate(`/mcp/servers/${r.id}`)}
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
