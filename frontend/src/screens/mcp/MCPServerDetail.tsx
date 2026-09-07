import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import DataTable, { Column } from '../../components/ui/DataTable';
import Pagination from '../../components/ui/Pagination';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import {
  activateMCPServer,
  connectionTestMCPServer,
  createDiscovery,
  deactivateMCPServer,
  getMCPServer,
  listDiscoveries,
  listServerTools,
} from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { ConnectionTestDto, DiscoveryDto, MCPServerDto, MCPToolDto } from '../../api/types';
import {
  labelAuthType,
  labelDiscoveryMode,
  labelProtocolEra,
  labelTransport,
  labelCheckStatus,
  formatTimestamp,
  shortenId,
} from '../../domain';

const DISCOVERY_PAGE_SIZE = 20;
const TOOLS_PAGE_SIZE = 20;

type FeedbackError = { message: string; requestId?: string };

function toFeedbackError(err: unknown, fallback: string): FeedbackError {
  if (isApiError(err)) {
    return {
      message: err.message,
      requestId: err.requestId ?? undefined,
    };
  }
  return { message: fallback };
}

export default function MCPServerDetail() {
  const { serverId } = useParams();
  const navigate = useNavigate();
  const mountedRef = useRef(true);

  const [tab, setTab] = useState('overview');
  const [server, setServer] = useState<MCPServerDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<FeedbackError | null>(null);

  const [tools, setTools] = useState<MCPToolDto[]>([]);
  const [toolsTotal, setToolsTotal] = useState(0);
  const [toolsPage, setToolsPage] = useState(1);
  const [toolsHasNext, setToolsHasNext] = useState(false);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError] = useState<FeedbackError | null>(null);

  const [discoveries, setDiscoveries] = useState<DiscoveryDto[]>([]);
  const [discPage, setDiscPage] = useState(1);
  const [discTotal, setDiscTotal] = useState(0);
  const [discHasNext, setDiscHasNext] = useState(false);
  const [discoveriesLoading, setDiscoveriesLoading] = useState(false);
  const [discoveriesError, setDiscoveriesError] = useState<FeedbackError | null>(null);

  const [mutationError, setMutationError] = useState<FeedbackError | null>(null);
  const [statusMutating, setStatusMutating] = useState(false);
  const [testing, setTesting] = useState(false);
  const [lastTest, setLastTest] = useState<ConnectionTestDto | null>(null);
  const [previewResult, setPreviewResult] = useState<DiscoveryDto | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const reloadServer = useCallback(
    async (signal?: AbortSignal) => {
      if (!serverId) return null;
      const srv = await getMCPServer(serverId, signal);
      if (signal?.aborted || !mountedRef.current) return null;
      setServer(srv);
      return srv;
    },
    [serverId],
  );

  const reloadTools = useCallback(
    async (signal?: AbortSignal) => {
      if (!serverId) return;
      setToolsLoading(true);
      setToolsError(null);
      try {
        const toolsData = await listServerTools(serverId, {
          page: toolsPage,
          page_size: TOOLS_PAGE_SIZE,
          signal,
        });
        if (signal?.aborted || !mountedRef.current) return;
        setTools(toolsData.items);
        setToolsTotal(toolsData.total);
        setToolsHasNext(toolsData.has_next);
      } catch (err) {
        if (isAbortError(err) || signal?.aborted || !mountedRef.current) return;
        setToolsError(toFeedbackError(err, 'Tool 목록을 불러오지 못했습니다.'));
      } finally {
        if (!signal?.aborted && mountedRef.current) setToolsLoading(false);
      }
    },
    [serverId, toolsPage],
  );

  const reloadDiscoveries = useCallback(
    async (signal?: AbortSignal) => {
      if (!serverId) return;
      setDiscoveriesLoading(true);
      setDiscoveriesError(null);
      try {
        const discData = await listDiscoveries(serverId, {
          page: discPage,
          page_size: DISCOVERY_PAGE_SIZE,
          signal,
        });
        if (signal?.aborted || !mountedRef.current) return;
        setDiscoveries(discData.items);
        setDiscTotal(discData.total);
        setDiscHasNext(discData.has_next);
      } catch (err) {
        if (isAbortError(err) || signal?.aborted || !mountedRef.current) return;
        setDiscoveriesError(toFeedbackError(err, 'Discovery 이력을 불러오지 못했습니다.'));
      } finally {
        if (!signal?.aborted && mountedRef.current) setDiscoveriesLoading(false);
      }
    },
    [serverId, discPage],
  );

  // Critical: server detail only
  useEffect(() => {
    if (!serverId) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setNotFound(false);
    setServer(null);
    setToolsPage(1);
    setDiscPage(1);

    getMCPServer(serverId, controller.signal)
      .then(srv => {
        if (controller.signal.aborted || !mountedRef.current) return;
        setServer(srv);
      })
      .catch(err => {
        if (isAbortError(err) || controller.signal.aborted || !mountedRef.current) return;
        if (isApiError(err) && err.status === 404) {
          setNotFound(true);
          setServer(null);
          return;
        }
        setError(toFeedbackError(err, '서버 정보를 불러오지 못했습니다.'));
      })
      .finally(() => {
        if (!controller.signal.aborted && mountedRef.current) setLoading(false);
      });

    return () => controller.abort();
  }, [serverId]);

  // Secondary: tools
  useEffect(() => {
    if (!serverId || !server) return;
    const controller = new AbortController();
    void reloadTools(controller.signal);
    return () => controller.abort();
  }, [serverId, server?.id, toolsPage, reloadTools]);

  // Secondary: discoveries
  useEffect(() => {
    if (!serverId || !server) return;
    const controller = new AbortController();
    void reloadDiscoveries(controller.signal);
    return () => controller.abort();
  }, [serverId, server?.id, discPage, reloadDiscoveries]);

  const handleActivate = async () => {
    if (!serverId) return;
    setStatusMutating(true);
    setMutationError(null);
    try {
      const updated = await activateMCPServer(serverId);
      if (!mountedRef.current) return;
      setServer(updated);
    } catch (err) {
      if (!mountedRef.current) return;
      setMutationError(toFeedbackError(err, 'Activate에 실패했습니다.'));
    } finally {
      if (mountedRef.current) setStatusMutating(false);
    }
  };

  const handleDeactivate = async () => {
    if (!serverId) return;
    setStatusMutating(true);
    setMutationError(null);
    try {
      const updated = await deactivateMCPServer(serverId);
      if (!mountedRef.current) return;
      setServer(updated);
    } catch (err) {
      if (!mountedRef.current) return;
      setMutationError(toFeedbackError(err, 'Deactivate에 실패했습니다.'));
    } finally {
      if (mountedRef.current) setStatusMutating(false);
    }
  };

  const runConnectionTest = async () => {
    if (!serverId) return;
    setTesting(true);
    setMutationError(null);
    try {
      const result = await connectionTestMCPServer(serverId);
      if (!mountedRef.current) return;
      setLastTest(result);
      await reloadServer();
    } catch (err) {
      if (!mountedRef.current) return;
      if (isApiError(err)) {
        setLastTest({
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
        });
      } else {
        setMutationError(toFeedbackError(err, '연결 테스트에 실패했습니다.'));
      }
      try {
        await reloadServer();
      } catch {
        // keep previous server metadata if refetch fails
      }
    } finally {
      if (mountedRef.current) setTesting(false);
    }
  };

  const runDiscoveryPreview = async () => {
    if (!serverId) return;
    setDiscovering(true);
    setPreviewResult(null);
    setMutationError(null);
    try {
      const result = await createDiscovery(serverId, { apply_changes: false });
      if (!mountedRef.current) return;
      setPreviewResult(result);
      await reloadServer();
    } catch (err) {
      if (!mountedRef.current) return;
      if (isApiError(err)) {
        setPreviewResult({
          id: '',
          mcp_server_id: serverId,
          protocol_era: 'CURRENT',
          discovery_mode: null,
          selected_version: null,
          success: false,
          error_code: err.code,
          error_message: err.message,
          apply_changes: false,
          diff: { added: 0, changed: 0, missing: 0, unchanged: 0 },
          started_at: new Date().toISOString(),
          finished_at: null,
          capabilities: null,
        });
      } else {
        setMutationError(toFeedbackError(err, 'Discovery Preview에 실패했습니다.'));
      }
    } finally {
      if (mountedRef.current) setDiscovering(false);
    }
  };

  const runDiscoveryApply = async () => {
    if (!serverId) return;
    setApplying(true);
    setApplyError(null);
    try {
      const result = await createDiscovery(serverId, { apply_changes: true });
      if (!mountedRef.current) return;
      if (!result.success) {
        setApplyError(result.error_message ?? 'Discovery 적용에 실패했습니다.');
        return;
      }
      setApplyOpen(false);
      setPreviewResult(null);
      await Promise.all([reloadServer(), reloadTools(), reloadDiscoveries()]);
    } catch (err) {
      if (!mountedRef.current) return;
      setApplyError(isApiError(err) ? err.message : 'Discovery 적용에 실패했습니다.');
    } finally {
      if (mountedRef.current) setApplying(false);
    }
  };

  if (loading && !server) {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={8} />
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="p-6">
        <ErrorState message="서버를 찾을 수 없습니다." onRetry={() => navigate('/mcp/servers')} />
      </div>
    );
  }

  if (error || !server) {
    return (
      <div className="p-6">
        <ErrorState
          message={error?.message}
          requestId={error?.requestId}
          onRetry={() => {
            if (!serverId) return;
            setLoading(true);
            setError(null);
            getMCPServer(serverId)
              .then(srv => {
                if (!mountedRef.current) return;
                setServer(srv);
              })
              .catch(err => {
                if (!mountedRef.current) return;
                if (isApiError(err) && err.status === 404) {
                  setNotFound(true);
                  return;
                }
                setError(toFeedbackError(err, '서버 정보를 불러오지 못했습니다.'));
              })
              .finally(() => {
                if (mountedRef.current) setLoading(false);
              });
          }}
        />
      </div>
    );
  }

  const canActivate = server.status === 'DRAFT' || server.status === 'INACTIVE';
  const canDeactivate = server.status === 'ACTIVE';

  const toolCols: Column<MCPToolDto>[] = [
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
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    {
      key: 'updatedAt',
      label: 'Updated',
      render: r => <span className="text-xs text-slate-400">{formatTimestamp(r.updated_at)}</span>,
    },
  ];

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button
          onClick={() => navigate('/mcp/servers')}
          className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3"
        >
          <ArrowLeft size={14} /> MCP Servers
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{server.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={server.status} />
              <span className="text-xs text-slate-400 font-mono">{labelTransport(server.transport_type)}</span>
              <span className="text-xs text-slate-400">
                {labelProtocolEra(server.protocol_era)}
                {server.negotiated_protocol_version ? ` · ${server.negotiated_protocol_version}` : ''}
              </span>
            </div>
          </div>
          <div className="flex gap-2 flex-wrap justify-end">
            {canActivate && (
              <Button variant="primary" size="sm" loading={statusMutating} onClick={handleActivate}>
                Activate
              </Button>
            )}
            {canDeactivate && (
              <Button variant="outline" size="sm" loading={statusMutating} onClick={handleDeactivate}>
                Deactivate
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              icon={<RefreshCw size={13} />}
              loading={discovering}
              onClick={runDiscoveryPreview}
            >
              Discovery Preview
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!previewResult?.success}
              onClick={() => setApplyOpen(true)}
            >
              Apply Discovery
            </Button>
            <Button
              variant="secondary"
              size="sm"
              icon={<RefreshCw size={13} />}
              loading={testing}
              onClick={runConnectionTest}
            >
              연결 테스트
            </Button>
          </div>
        </div>
        {mutationError && (
          <div className="mt-3 space-y-1">
            <InlineAlert type="error" message={mutationError.message} />
            {mutationError.requestId && (
              <p className="font-mono text-xs text-slate-400">Request ID: {mutationError.requestId}</p>
            )}
          </div>
        )}
        {lastTest && (
          <div className="mt-3 p-3 rounded-lg border border-slate-200 bg-slate-50 text-sm">
            <span className="font-medium">{labelCheckStatus(lastTest.status)}</span>
            {lastTest.latency_ms != null && (
              <span className="text-slate-500 ml-2">{lastTest.latency_ms}ms</span>
            )}
            {lastTest.protocol_version && (
              <span className="text-slate-500 ml-2">protocol {lastTest.protocol_version}</span>
            )}
            {lastTest.error_message && (
              <p className="text-red-600 text-xs mt-1">{lastTest.error_message}</p>
            )}
          </div>
        )}
        {previewResult && (
          <div className="mt-3 p-3 rounded-lg border border-slate-200 bg-slate-50 text-sm">
            <p className="font-medium">
              Discovery Preview — {previewResult.success ? 'Success' : 'Failed'}
            </p>
            {previewResult.success ? (
              <p className="text-xs text-slate-600 mt-1">
                added {previewResult.diff.added} · changed {previewResult.diff.changed} · missing{' '}
                {previewResult.diff.missing} · unchanged {previewResult.diff.unchanged}
              </p>
            ) : (
              <p className="text-xs text-red-600 mt-1">{previewResult.error_message}</p>
            )}
          </div>
        )}
        {applyError && <InlineAlert type="error" message={applyError} />}
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'tools', label: 'Tools', badge: toolsTotal },
            { id: 'discovery', label: 'Discovery History' },
            { id: 'health', label: 'Health' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4 max-w-2xl">
            <InfoCard title="연결 정보">
              <Row label="Transport">{labelTransport(server.transport_type)}</Row>
              <Row label="Endpoint" mono>{server.endpoint_url ?? '—'}</Row>
              <Row label="Protocol">{labelProtocolEra(server.protocol_era)}</Row>
              <Row label="Version" mono>{server.negotiated_protocol_version ?? '—'}</Row>
              <Row label="Discovery">
                {server.discovery_mode ? labelDiscoveryMode(server.discovery_mode) : '—'}
              </Row>
              <Row label="Last Health">{formatTimestamp(server.last_healthy_at)}</Row>
              <Row label="Last Error">{formatTimestamp(server.last_error_at)}</Row>
            </InfoCard>
            <InfoCard title="인증">
              <Row label="방식">{labelAuthType(server.auth_type)}</Row>
              <Row label="Secret Reference">
                {server.auth_type === 'NONE'
                  ? '—'
                  : server.auth_secret_id
                    ? `configured (${shortenId(server.auth_secret_id)})`
                    : 'not configured'}
              </Row>
            </InfoCard>
          </div>
        )}

        {tab === 'tools' && (
          <div className="max-w-4xl">
            {toolsLoading && <LoadingSkeleton rows={4} />}
            {!toolsLoading && toolsError && (
              <ErrorState
                message={toolsError.message}
                requestId={toolsError.requestId}
                onRetry={() => void reloadTools()}
              />
            )}
            {!toolsLoading && !toolsError && tools.length === 0 && (
              <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <EmptyState title="이 서버에 등록된 Tool이 없습니다" description="Discovery를 실행해 Tool을 가져오세요." />
              </div>
            )}
            {!toolsLoading && !toolsError && tools.length > 0 && (
              <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <DataTable
                  columns={toolCols}
                  data={tools}
                  rowKey={r => r.id}
                  onRowClick={r => navigate(`/mcp/tools/${r.id}`)}
                />
                <Pagination
                  page={toolsPage}
                  pageSize={TOOLS_PAGE_SIZE}
                  total={toolsTotal}
                  hasNext={toolsHasNext}
                  onPageChange={setToolsPage}
                />
              </div>
            )}
          </div>
        )}

        {tab === 'discovery' && (
          <div className="max-w-2xl">
            {discoveriesLoading && <LoadingSkeleton rows={4} />}
            {!discoveriesLoading && discoveriesError && (
              <ErrorState
                message={discoveriesError.message}
                requestId={discoveriesError.requestId}
                onRetry={() => void reloadDiscoveries()}
              />
            )}
            {!discoveriesLoading && !discoveriesError && discoveries.length === 0 && (
              <EmptyState title="Discovery 이력이 없습니다" />
            )}
            {!discoveriesLoading && !discoveriesError && discoveries.length > 0 && (
              <div className="space-y-2">
                {discoveries.map(d => (
                  <div
                    key={d.id}
                    className="bg-white border border-slate-200 rounded-lg p-4 flex justify-between items-center"
                  >
                    <div>
                      <p className="text-sm font-medium text-slate-800">
                        {formatTimestamp(d.finished_at ?? d.started_at)}
                      </p>
                      <p className="text-xs text-slate-400">
                        added {d.diff.added} · changed {d.diff.changed} · missing {d.diff.missing}
                        {d.apply_changes ? ' · applied' : ' · preview'}
                      </p>
                    </div>
                    <StatusBadge status={d.success ? 'SUCCEEDED' : 'FAILED'} size="sm" />
                  </div>
                ))}
                <Pagination
                  page={discPage}
                  pageSize={DISCOVERY_PAGE_SIZE}
                  total={discTotal}
                  hasNext={discHasNext}
                  onPageChange={setDiscPage}
                />
              </div>
            )}
          </div>
        )}

        {tab === 'health' && (
          <div className="max-w-xl space-y-4">
            <InfoCard title="서버 Health 요약">
              <Row label="Last Healthy">{formatTimestamp(server.last_healthy_at)}</Row>
              <Row label="Last Error">{formatTimestamp(server.last_error_at)}</Row>
            </InfoCard>
            {lastTest ? (
              <div className="bg-white border border-slate-200 rounded-lg px-4 py-3 flex items-center justify-between">
                <span className="text-xs font-mono text-slate-500">
                  {formatTimestamp(lastTest.checked_at)} (session)
                </span>
                <span className="text-sm font-mono text-slate-600">
                  {lastTest.latency_ms != null ? `${lastTest.latency_ms}ms` : '—'}
                </span>
                <StatusBadge status={lastTest.status} size="sm" />
              </div>
            ) : null}
            <EmptyState
              title="연결 테스트 이력 API 미제공"
              description="Health check history API는 아직 제공되지 않습니다. 상세 페이지에서 실행한 연결 테스트 결과만 표시됩니다."
            />
          </div>
        )}

        {tab === 'audit' && (
          <EmptyState
            title="Audit 로그 deferred"
            description="서버 Audit 이력 API는 아직 제공되지 않습니다."
          />
        )}
      </div>

      <ConfirmDialog
        open={applyOpen}
        onClose={() => setApplyOpen(false)}
        onConfirm={runDiscoveryApply}
        title="Discovery 변경 적용"
        description="Preview된 Discovery diff를 서버 Tool registry에 적용합니다. 계속하시겠습니까?"
        confirmLabel="Apply"
        loading={applying}
      />
    </div>
  );
}

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex gap-4">
      <span className="text-slate-400 w-24 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
