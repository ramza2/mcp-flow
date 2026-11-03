import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton, PermissionDenied } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import { getUser, listRoles, listUserRoles, replaceUserRoles, type AdminRole, type AdminUser } from '../../api/administration';
import ResourceGrantEditor from './ResourceGrantEditor';

export default function UserDetail() {
  const { userId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('profile');
  const [user, setUser] = useState<AdminUser | null>(null);
  const [roles, setRoles] = useState<AdminRole[]>([]);
  const [selectedRoles, setSelectedRoles] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  const reload = useCallback(async (signal?: AbortSignal) => {
    if (!userId) throw new Error('잘못된 사용자 주소입니다.');
    const [nextUser, assigned, allRoles] = await Promise.all([
      getUser(userId, signal), listUserRoles(userId, signal), listRoles(signal),
    ]);
    setUser(nextUser);
    setRoles(allRoles);
    setSelectedRoles(assigned.map(role => role.id));
  }, [userId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setForbidden(false);
    setError(null);
    setFeedback(null);
    setUser(null);
    reload(controller.signal)
      .catch((err: unknown) => {
        if (isAbortError(err) || controller.signal.aborted) return;
        if (isApiError(err) && err.status === 403) setForbidden(true);
        else setError(isApiError(err) ? err.message : '사용자 정보를 불러오지 못했습니다.');
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [reload]);

  async function saveRoles() {
    if (!user || !userId || saving) return;
    setSaving(true);
    setFeedback(null);
    try {
      await replaceUserRoles(userId, selectedRoles, user.lock_version);
      await reload();
      setFeedback('사용자 역할을 저장했습니다.');
    } catch (err) {
      setFeedback(isApiError(err) ? `역할 저장 실패: ${err.message}` : '역할 저장에 실패했습니다.');
    } finally {
      setSaving(false);
    }
  }

  function toggleRole(id: string) {
    setSelectedRoles(current => current.includes(id) ? current.filter(v => v !== id) : [...current, id]);
  }

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/admin/users')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> 사용자 목록
        </button>
        <h1 className="text-lg font-semibold text-slate-900">사용자 상세 및 권한</h1>
        {user && <div className="flex items-center gap-2 mt-1">
          <span className="text-sm text-slate-700">{user.display_name}</span>
          <StatusBadge status={user.status} size="sm" />
          <span className="font-mono text-xs text-slate-400">{user.username}</span>
        </div>}
      </div>
      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'profile', label: '기본 정보' },
            { id: 'roles', label: '역할 관리' },
            { id: 'grants', label: 'MCP 도구 접근 권한' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>
      <div className="p-6 max-w-3xl space-y-3">
        {loading && <LoadingSkeleton rows={4} />}
        {!loading && forbidden && <PermissionDenied />}
        {!loading && error && <ErrorState message={error} onRetry={() => {
          setLoading(true);
          setError(null);
          void reload().catch(err => setError(isApiError(err) ? err.message : '다시 불러오지 못했습니다.')).finally(() => setLoading(false));
        }} />}
        {!loading && user && !error && !forbidden && (
          <>
            {tab === 'profile' && (
              <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3 text-sm">
                <Row label="이름">{user.display_name}</Row>
                <Row label="계정" mono>{user.username}</Row>
                <Row label="이메일" mono>{user.email}</Row>
                <Row label="상태"><StatusBadge status={user.status} size="sm" /></Row>
                <Row label="마지막 로그인">{user.last_login_at ? new Date(user.last_login_at).toLocaleString('ko-KR') : '기록 없음'}</Row>
              </div>
            )}
            {tab === 'roles' && (
              <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
                <div className="flex justify-between items-center gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-slate-800">사용자 역할</h3>
                    <p className="text-xs text-slate-500 mt-1">역할의 기능 권한은 [역할 및 권한] 메뉴에서 설정합니다.</p>
                  </div>
                  <Button size="sm" loading={saving} onClick={() => void saveRoles()}>역할 저장</Button>
                </div>
                {roles.length === 0 && <p className="text-sm text-slate-500">등록된 역할이 없습니다.</p>}
                {roles.map(role => (
                  <label key={role.id} className="flex items-center gap-2 p-2.5 rounded-lg bg-slate-50 text-sm">
                    <input type="checkbox" checked={selectedRoles.includes(role.id)} disabled={saving} onChange={() => toggleRole(role.id)} />
                    <span>{role.name}</span>
                    <span className="text-xs text-slate-400 font-mono">({role.code})</span>
                  </label>
                ))}
                {feedback && <InlineAlert type={feedback.includes('실패') ? 'error' : 'info'} message={feedback} />}
              </div>
            )}
            {tab === 'grants' && userId && <ResourceGrantEditor owner="user" ownerId={userId} />}
          </>
        )}
      </div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-28 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
