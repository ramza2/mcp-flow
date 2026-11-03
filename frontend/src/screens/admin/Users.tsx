import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { type Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import Pagination from '../../components/ui/Pagination';
import { ErrorState, LoadingSkeleton, PermissionDenied } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import { listUsers, type AdminUser } from '../../api/administration';

const PAGE_SIZE = 20;

export default function Users() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setForbidden(false);
    listUsers({ page, page_size: PAGE_SIZE, signal: controller.signal })
      .then(data => {
        if (controller.signal.aborted) return;
        setUsers(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || controller.signal.aborted) return;
        setUsers([]);
        if (isApiError(err) && err.status === 403) setForbidden(true);
        else setError(isApiError(err) ? err.message : '사용자 목록을 불러오지 못했습니다.');
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [page, retry]);

  const columns: Column<AdminUser>[] = [
    { key: 'display_name', label: '이름', render: user => <span className="font-medium text-slate-800">{user.display_name}</span> },
    { key: 'username', label: '계정', render: user => <span className="font-mono text-xs">{user.username}</span> },
    { key: 'email', label: '이메일', render: user => <span className="text-xs">{user.email}</span> },
    { key: 'status', label: '상태', render: user => <StatusBadge status={user.status} size="sm" /> },
    { key: 'last_login_at', label: '마지막 로그인', render: user => <span className="text-xs text-slate-500">{user.last_login_at ? new Date(user.last_login_at).toLocaleString('ko-KR') : '기록 없음'}</span> },
  ];

  return (
    <div>
      <PageHeader title="사용자 관리" description="사용자별 역할과 MCP 도구 접근 권한을 관리합니다." />
      <div className="p-6">
        {loading && <LoadingSkeleton />}
        {!loading && forbidden && <PermissionDenied />}
        {!loading && error && <ErrorState message={error} onRetry={() => setRetry(n => n + 1)} />}
        {!loading && !forbidden && !error && (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            <DataTable columns={columns} data={users} rowKey={user => user.id}
              onRowClick={user => navigate(`/admin/users/${user.id}`)} />
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} hasNext={hasNext} onPageChange={setPage} />
          </div>
        )}
      </div>
    </div>
  );
}
