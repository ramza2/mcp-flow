import { useCallback, useEffect, useMemo, useState } from 'react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton, PermissionDenied } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import {
  getRole,
  listPermissions,
  listRolePermissions,
  listRoles,
  replaceRolePermissions,
  type AdminPermission,
  type AdminRole,
} from '../../api/administration';
import ResourceGrantEditor from './ResourceGrantEditor';

export default function Roles() {
  const [roles, setRoles] = useState<AdminRole[]>([]);
  const [permissions, setPermissions] = useState<AdminPermission[]>([]);
  const [roleId, setRoleId] = useState('');
  const [role, setRole] = useState<AdminRole | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [tab, setTab] = useState<'permissions' | 'grants'>('permissions');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setForbidden(false);
    setError(null);
    Promise.all([listRoles(controller.signal), listPermissions(controller.signal)])
      .then(([nextRoles, nextPermissions]) => {
        if (controller.signal.aborted) return;
        setRoles(nextRoles);
        setPermissions(nextPermissions);
        setRoleId(current => nextRoles.some(r => r.id === current) ? current : nextRoles[0]?.id ?? '');
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || controller.signal.aborted) return;
        if (isApiError(err) && err.status === 403) setForbidden(true);
        else setError(isApiError(err) ? err.message : '역할 목록을 불러오지 못했습니다.');
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [retry]);

  const reloadRole = useCallback(async (id: string, signal?: AbortSignal) => {
    const [nextRole, granted] = await Promise.all([getRole(id, signal), listRolePermissions(id, signal)]);
    if (signal?.aborted) return;
    setRole(nextRole);
    setSelected(granted.map(item => item.id));
  }, []);

  useEffect(() => {
    if (!roleId) return;
    const controller = new AbortController();
    setDetailLoading(true);
    setDetailError(null);
    setRole(null);
    setFeedback(null);
    reloadRole(roleId, controller.signal)
      .catch((err: unknown) => {
        if (isAbortError(err) || controller.signal.aborted) return;
        setDetailError(isApiError(err) ? err.message : '역할 권한을 불러오지 못했습니다.');
      })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [roleId, reloadRole]);

  const groups = useMemo(() => {
    const result = new Map<string, AdminPermission[]>();
    for (const permission of permissions) {
      const category = permission.code.split('.')[0].toUpperCase();
      result.set(category, [...(result.get(category) ?? []), permission]);
    }
    return [...result.entries()];
  }, [permissions]);

  function togglePermission(id: string) {
    setSelected(previous => previous.includes(id) ? previous.filter(value => value !== id) : [...previous, id]);
    setFeedback(null);
  }

  async function savePermissions() {
    if (!role || saving) return;
    setSaving(true);
    setFeedback(null);
    try {
      await replaceRolePermissions(role.id, selected, role.lock_version);
      await reloadRole(role.id);
      setFeedback('역할의 기능 권한을 저장했습니다.');
    } catch (err) {
      setFeedback(isApiError(err) ? `저장 실패: ${err.message}` : '권한 저장에 실패했습니다.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <PageHeader title="역할 및 권한" description="실제 등록된 역할의 기능 권한과 MCP 도구 접근 권한을 관리합니다." />
      {loading && <LoadingSkeleton />}
      {!loading && forbidden && <PermissionDenied />}
      {!loading && error && <ErrorState message={error} onRetry={() => setRetry(value => value + 1)} />}
      {!loading && !forbidden && !error && (
        <div className="flex min-h-[60vh]">
          <nav aria-label="역할 선택" className="w-56 border-r border-slate-200 bg-white flex-shrink-0 py-3">
            {roles.map(item => (
              <button
                key={item.id}
                onClick={() => { setRoleId(item.id); setTab('permissions'); }}
                className={`w-full text-left px-4 py-2.5 text-sm transition-colors ${roleId === item.id
                  ? 'bg-indigo-50 text-indigo-700 font-medium' : 'text-slate-600 hover:bg-slate-50'}`}
              >
                {item.name}
                <span className="block text-xs text-slate-400 font-mono">{item.code}</span>
              </button>
            ))}
            {roles.length === 0 && <p className="text-sm text-slate-500 p-4">등록된 역할이 없습니다.</p>}
          </nav>
          <main className="flex-1 min-w-0 p-4 space-y-4">
            {detailLoading && <LoadingSkeleton rows={3} />}
            {!detailLoading && detailError && <ErrorState message={detailError} onRetry={() => {
              setDetailLoading(true);
              setDetailError(null);
              void reloadRole(roleId).catch(err => setDetailError(isApiError(err) ? err.message : '다시 불러오지 못했습니다.')).finally(() => setDetailLoading(false));
            }} />}
            {!detailLoading && role && !detailError && (
              <>
                <div className="flex gap-2 border-b border-slate-200 pb-2">
                  <Button size="sm" variant={tab === 'permissions' ? 'primary' : 'outline'} onClick={() => setTab('permissions')}>기능 권한</Button>
                  <Button size="sm" variant={tab === 'grants' ? 'primary' : 'outline'} onClick={() => setTab('grants')}>MCP 도구 접근 권한</Button>
                </div>
                {tab === 'permissions' && (
                  <>
                    <div className="flex flex-wrap justify-between items-center gap-2">
                      <div>
                        <h3 className="text-sm font-semibold text-slate-800">{role.name} — 기능 권한</h3>
                        <p className="text-xs text-slate-500 mt-1">도구 실행에는 <code>mcp.tool.execute</code> 및 해당 MCP_TOOL ResourceGrant가 모두 필요합니다.</p>
                      </div>
                      <div className="flex gap-2">
                        <Button size="sm" variant="outline" disabled={saving} onClick={() => setSelected(permissions.map(p => p.id))}>전체 선택</Button>
                        <Button size="sm" variant="outline" disabled={saving} onClick={() => setSelected([])}>전체 해제</Button>
                        <Button size="sm" loading={saving} onClick={() => void savePermissions()}>저장</Button>
                      </div>
                    </div>
                    {feedback && <InlineAlert type={feedback.includes('실패') ? 'error' : 'info'} message={feedback} />}
                    {groups.map(([category, items]) => (
                      <section key={category} className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                        <h4 className="bg-slate-50 px-4 py-2.5 text-xs font-semibold text-slate-600">{category}</h4>
                        <div className="p-3 grid grid-cols-1 lg:grid-cols-2 gap-2">
                          {items.map(permission => (
                            <label key={permission.id} className="flex items-start gap-2 p-2 rounded hover:bg-slate-50 cursor-pointer">
                              <input type="checkbox" className="mt-1" checked={selected.includes(permission.id)}
                                disabled={saving} onChange={() => togglePermission(permission.id)} />
                              <span className="min-w-0">
                                <span className="font-mono text-xs text-slate-700">{permission.code}</span>
                                <span className="block text-xs text-slate-500">{permission.name}</span>
                              </span>
                            </label>
                          ))}
                        </div>
                      </section>
                    ))}
                  </>
                )}
                {tab === 'grants' && <ResourceGrantEditor owner="role" ownerId={role.id} />}
              </>
            )}
          </main>
        </div>
      )}
    </div>
  );
}
