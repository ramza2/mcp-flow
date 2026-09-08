import { Navigate, Outlet, useLocation } from 'react-router';
import { useAuth } from '../auth/useAuth';
import Button from './ui/Button';

export default function RequireAuth() {
  const { status, error, refreshSession } = useAuth();
  const location = useLocation();

  if (status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50">
        <div className="text-sm text-slate-500" role="status">
          세션을 확인하는 중…
        </div>
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50 p-4">
        <div className="w-full max-w-sm bg-white border border-slate-200 rounded-xl p-6 shadow-sm">
          <h1 className="text-sm font-semibold text-slate-800 mb-2">세션을 확인할 수 없습니다</h1>
          <p className="text-sm text-slate-500 mb-4">
            {error?.message ?? '네트워크 오류가 발생했습니다. 잠시 후 다시 시도하세요.'}
          </p>
          <Button type="button" className="w-full justify-center" onClick={() => void refreshSession()}>
            다시 시도
          </Button>
        </div>
      </div>
    );
  }

  if (status === 'unauthenticated') {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <Outlet />;
}
