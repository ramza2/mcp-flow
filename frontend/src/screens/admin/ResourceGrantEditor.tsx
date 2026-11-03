import { useCallback, useEffect, useState } from 'react';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton, PermissionDenied } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import { listMCPTools } from '../../api/mcp';
import type { MCPToolDto } from '../../api/types';
import {
  createResourceGrant,
  deleteResourceGrant,
  listResourceGrants,
  type GrantOwner,
  type ResourceGrant,
} from '../../api/administration';

interface Props {
  owner: GrantOwner;
  ownerId: string;
}

function errorMessage(error: unknown) {
  return isApiError(error) ? error.message : '요청 처리 중 오류가 발생했습니다.';
}

/** ResourceGrants are exact positive scopes, never wildcard or inherited. */
export default function ResourceGrantEditor({ owner, ownerId }: Props) {
  const [grants, setGrants] = useState<ResourceGrant[]>([]);
  const [tools, setTools] = useState<MCPToolDto[]>([]);
  const [selectedTool, setSelectedTool] = useState('');
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toolError, setToolError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [removeId, setRemoveId] = useState<string | null>(null);

  const reload = useCallback(async (signal?: AbortSignal) => {
    const next = await listResourceGrants(owner, ownerId, signal);
    setGrants(next);
  }, [owner, ownerId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setForbidden(false);
    setError(null);
    setToolError(null);
    setSelectedTool('');
    setFeedback(null);
    setGrants([]);
    setTools([]);
    reload(controller.signal)
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        if (isApiError(err) && err.status === 403) setForbidden(true);
        else setError(errorMessage(err));
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });

    async function loadTools() {
      try {
        const all: MCPToolDto[] = [];
        for (let page = 1; ; page += 1) {
          const result = await listMCPTools({ page, page_size: 100, signal: controller.signal });
          all.push(...result.items);
          if (!result.has_next) break;
        }
        if (!controller.signal.aborted) setTools(all);
      } catch (err) {
        if (!controller.signal.aborted && !isAbortError(err)) setToolError(errorMessage(err));
      }
    }
    void loadTools();
    return () => controller.abort();
  }, [reload]);

  const existing = new Set(grants.filter(g => g.resource_type === 'MCP_TOOL').map(g => g.resource_id));
  const available = tools.filter(t => t.status === 'ACTIVE' && !existing.has(t.id));
  const toolNames = new Map(tools.map(t => [t.id, t.display_name || t.remote_name]));
  const assigned = grants.filter(g => g.resource_type === 'MCP_TOOL');

  async function addGrant() {
    if (!selectedTool || saving) return;
    setSaving(true);
    setFeedback(null);
    try {
      await createResourceGrant(owner, ownerId, 'MCP_TOOL', selectedTool);
      await reload();
      setSelectedTool('');
      setFeedback('도구 접근 권한을 부여했습니다.');
    } catch (err) {
      setFeedback(`권한 부여 실패: ${errorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  }

  async function removeGrant(grantId: string) {
    if (saving) return;
    setSaving(true);
    setFeedback(null);
    try {
      await deleteResourceGrant(owner, ownerId, grantId);
      await reload();
      setFeedback('도구 접근 권한을 해제했습니다.');
    } catch (err) {
      setFeedback(`권한 해제 실패: ${errorMessage(err)}`);
    } finally {
      setRemoveId(null);
      setSaving(false);
    }
  }

  if (loading) return <LoadingSkeleton rows={3} />;
  if (forbidden) return <PermissionDenied />;
  if (error) return <ErrorState message={error} onRetry={() => {
    setLoading(true);
    setError(null);
    void reload().catch(err => setError(errorMessage(err))).finally(() => setLoading(false));
  }} />;

  return (
    <section className="space-y-4">
      <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
        <h3 className="font-semibold text-sm text-slate-800">MCP 도구 접근 권한</h3>
        <p className="text-xs text-slate-500">
          선택한 도구 하나에 대한 접근 권한만 부여합니다. 도구를 실제 실행하려면
          역할에 <code>mcp.tool.execute</code> 권한도 필요합니다.
          {owner === 'user' ? ' 역할을 통해 받은 권한은 별도로 적용됩니다.' : ''}
        </p>
        {toolError && <InlineAlert type="warning" message={`도구 목록을 불러오지 못했습니다: ${toolError}`} />}
        <div className="flex flex-wrap gap-2 items-center">
          <select
            aria-label="권한을 부여할 MCP 도구"
            className="min-w-0 flex-1 border border-slate-300 rounded-md px-3 py-2 text-sm"
            value={selectedTool}
            onChange={e => setSelectedTool(e.target.value)}
            disabled={saving || Boolean(toolError)}
          >
            <option value="">활성 MCP 도구 선택</option>
            {available.map(tool => (
              <option key={tool.id} value={tool.id}>{tool.display_name || tool.remote_name} ({tool.remote_name})</option>
            ))}
          </select>
          <Button size="sm" disabled={!selectedTool || saving} loading={saving} onClick={() => void addGrant()}>
            접근 권한 부여
          </Button>
        </div>
        {feedback && <InlineAlert type={feedback.includes('실패') ? 'error' : 'info'} message={feedback} />}
      </div>
      <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
        <h3 className="font-semibold text-sm text-slate-800">직접 부여한 MCP 도구 ({assigned.length}개)</h3>
        {assigned.length === 0 && <p className="text-sm text-slate-500">직접 부여한 도구 접근 권한이 없습니다.</p>}
        {assigned.map(grant => (
          <div key={grant.id} className="flex gap-3 items-center justify-between border-t border-slate-100 pt-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-slate-800">{toolNames.get(grant.resource_id) ?? '현재 목록에서 확인할 수 없는 도구'}</div>
              <div className="text-xs font-mono text-slate-500 break-all">{grant.resource_id}</div>
            </div>
            {removeId === grant.id ? (
              <div className="flex gap-1 shrink-0">
                <Button size="sm" variant="danger" loading={saving} onClick={() => void removeGrant(grant.id)}>해제 확인</Button>
                <Button size="sm" variant="outline" disabled={saving} onClick={() => setRemoveId(null)}>취소</Button>
              </div>
            ) : (
              <Button size="sm" variant="outline" disabled={saving} onClick={() => setRemoveId(grant.id)}>해제</Button>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
