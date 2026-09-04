import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import JsonViewer from '../../components/ui/JsonViewer';
import { EmptyState, ErrorState, LoadingSkeleton } from '../../components/ui/EmptyState';
import { getMCPTool, getToolVersion, listToolVersions } from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { MCPToolDto, MCPToolVersionDto } from '../../api/types';
import { formatTimestamp, shortenId } from '../../domain';

export default function MCPToolDetail() {
  const { toolId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const [tool, setTool] = useState<MCPToolDto | null>(null);
  const [versions, setVersions] = useState<MCPToolVersionDto[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<MCPToolVersionDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  const loadTool = useCallback(() => {
    if (!toolId) return () => {};
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setNotFound(false);

    getMCPTool(toolId, controller.signal)
      .then(async t => {
        if (cancelled) return;
        setTool(t);
        const vers = await listToolVersions(toolId, { signal: controller.signal });
        if (cancelled) return;
        setVersions(vers.items);
        const currentId = t.current_version_id ?? vers.items[0]?.id ?? null;
        setSelectedVersionId(currentId);
        if (currentId) {
          const ver = await getToolVersion(toolId, currentId, controller.signal);
          if (!cancelled) setSelectedVersion(ver);
        } else {
          setSelectedVersion(null);
        }
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 404) {
          setNotFound(true);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Tool 정보를 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [toolId]);

  useEffect(() => {
    return loadTool();
  }, [loadTool]);

  const selectVersion = async (versionId: string) => {
    if (!toolId) return;
    setSelectedVersionId(versionId);
    try {
      const ver = await getToolVersion(toolId, versionId);
      setSelectedVersion(ver);
    } catch {
      setSelectedVersion(versions.find(v => v.id === versionId) ?? null);
    }
  };

  if (loading && !tool) {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={8} />
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="p-6">
        <ErrorState message="Tool을 찾을 수 없습니다." onRetry={() => navigate('/mcp/tools')} />
      </div>
    );
  }

  if (error || !tool) {
    return (
      <div className="p-6">
        <ErrorState message={error?.message} requestId={error?.requestId} onRetry={loadTool} />
      </div>
    );
  }

  const displayName = tool.display_name ?? tool.remote_name;
  const validationStatus = selectedVersion?.validation_status;

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button
          onClick={() => navigate('/mcp/tools')}
          className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3"
        >
          <ArrowLeft size={14} /> MCP Tools
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{displayName}</h1>
            <p className="text-xs font-mono text-slate-400 mt-0.5">{tool.remote_name}</p>
            <div className="flex items-center gap-3 mt-2">
              <StatusBadge status={tool.status} />
              {validationStatus && (
                <ValidationBadge status={validationStatus} />
              )}
              <span className="text-xs text-slate-400 font-mono">{shortenId(tool.mcp_server_id)}</span>
            </div>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'schema', label: 'Input Schema' },
            { id: 'output', label: 'Output Schema' },
            { id: 'policy', label: 'Policy' },
            { id: 'verification', label: 'Verification' },
            { id: 'test', label: 'Test Call' },
            { id: 'usedby', label: 'Used By' },
            { id: 'versions', label: 'Versions' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6 max-w-3xl">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4">
            <InfoCard title="Tool 정보">
              <Row label="Source Name" mono>{tool.remote_name}</Row>
              <Row label="Display Name">{tool.display_name ?? '—'}</Row>
              <Row label="Server ID" mono>{shortenId(tool.mcp_server_id)}</Row>
              <Row label="Tool Status"><StatusBadge status={tool.status} size="sm" /></Row>
              <Row label="Current Version">
                {selectedVersion ? `v${selectedVersion.version_no}` : '—'}
              </Row>
              <Row label="Version Validation">
                {validationStatus ? <ValidationBadge status={validationStatus} /> : '—'}
              </Row>
              <Row label="First Seen">{formatTimestamp(tool.first_seen_at)}</Row>
              <Row label="Last Seen">{formatTimestamp(tool.last_seen_at)}</Row>
            </InfoCard>
            {selectedVersion?.validation_errors && selectedVersion.validation_errors.length > 0 && (
              <InfoCard title="Validation Errors">
                <JsonViewer value={selectedVersion.validation_errors} />
              </InfoCard>
            )}
          </div>
        )}

        {tab === 'schema' && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">Input Schema</h3>
            <JsonViewer value={selectedVersion?.input_schema ?? null} emptyLabel="Input schema가 없습니다." />
          </div>
        )}

        {tab === 'output' && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">Output Schema</h3>
            <JsonViewer value={selectedVersion?.output_schema ?? null} emptyLabel="Output schema가 없습니다." />
          </div>
        )}

        {tab === 'policy' && (
          <EmptyState title="Tool Policy deferred" description="Policy API는 아직 제공되지 않습니다." />
        )}

        {tab === 'verification' && (
          <EmptyState title="Verification deferred" description="Verification API는 아직 제공되지 않습니다." />
        )}

        {tab === 'test' && (
          <EmptyState title="Test Call deferred" description="Manual Tool Test API는 아직 제공되지 않습니다." />
        )}

        {tab === 'usedby' && (
          <EmptyState title="Used By deferred" description="Agent/Workflow 사용 이력 API는 아직 제공되지 않습니다." />
        )}

        {tab === 'versions' && (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            {versions.length === 0 ? (
              <EmptyState title="버전 이력이 없습니다" />
            ) : (
              versions.map(v => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => selectVersion(v.id)}
                  className={`w-full flex items-center justify-between px-4 py-3 border-b last:border-0 text-left hover:bg-slate-50 ${
                    selectedVersionId === v.id ? 'bg-indigo-50' : ''
                  }`}
                >
                  <div>
                    <p className="text-sm font-mono font-medium text-slate-800">v{v.version_no}</p>
                    <p className="text-xs text-slate-400">{formatTimestamp(v.discovered_at)}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    {tool.current_version_id === v.id && (
                      <span className="text-xs text-indigo-600 font-medium">current</span>
                    )}
                    <ValidationBadge status={v.validation_status} />
                  </div>
                </button>
              ))
            )}
          </div>
        )}

        {tab === 'audit' && (
          <EmptyState title="Audit deferred" description="Tool Audit 이력 API는 아직 제공되지 않습니다." />
        )}
      </div>
    </div>
  );
}

function ValidationBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    VALID: 'text-green-700 bg-green-50',
    WARNING: 'text-amber-700 bg-amber-50',
    INVALID: 'text-red-700 bg-red-50',
  };
  const s = styles[status] ?? 'text-slate-500 bg-slate-100';
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s}`}>{status}</span>;
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
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-36 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
