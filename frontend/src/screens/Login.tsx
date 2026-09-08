import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router';
import { Zap, Eye, EyeOff } from 'lucide-react';
import Button from '../components/ui/Button';
import { useAuth } from '../auth/useAuth';
import { isApiError } from '../api/client';

function resolveIntendedPath(state: unknown): string {
  if (!state || typeof state !== 'object') return '/';
  const from = (state as { from?: { pathname?: unknown } }).from;
  const pathname = from?.pathname;
  if (typeof pathname !== 'string') return '/';
  // Only trust internal React Router locations — reject absolute/external URLs.
  if (!pathname.startsWith('/') || pathname.startsWith('//')) return '/';
  if (pathname === '/login') return '/';
  return pathname;
}

export default function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const { status, login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPw, setShowPw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const intended = resolveIntendedPath(location.state);

  if (status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50">
        <div className="text-sm text-slate-500" role="status">
          세션을 확인하는 중…
        </div>
      </div>
    );
  }

  if (status === 'authenticated') {
    return <Navigate to={intended} replace />;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    if (!username.trim() || !password) {
      setError('아이디와 비밀번호를 입력하세요.');
      return;
    }
    if (loading) return;
    setLoading(true);
    try {
      await login(username.trim(), password);
      navigate(intended, { replace: true });
    } catch (err) {
      if (isApiError(err) && err.status === 401 && err.code === 'AUTH_INVALID_CREDENTIALS') {
        setError('아이디 또는 비밀번호를 확인하세요.');
      } else if (isApiError(err)) {
        setError(err.message || '로그인에 실패했습니다.');
      } else {
        setError('로그인에 실패했습니다.');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-3 mb-8">
          <div className="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center">
            <Zap size={20} className="text-white" />
          </div>
          <div>
            <div className="text-xl font-semibold text-slate-900">MCPFlow</div>
            <div className="text-xs text-slate-500">MCP-based AI Agent Automation Platform</div>
          </div>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
          <h2 className="text-sm font-semibold text-slate-800 mb-5">로그인</h2>

          {error && (
            <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{error}</div>
          )}

          <form onSubmit={e => void handleSubmit(e)} className="space-y-4">
            <div>
              <label htmlFor="login-username" className="block text-xs font-medium text-slate-600 mb-1.5">
                사용자 아이디
              </label>
              <input
                id="login-username"
                type="text"
                name="username"
                autoComplete="username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                placeholder="admin"
                disabled={loading}
                className="w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent disabled:bg-slate-50"
              />
            </div>
            <div>
              <label htmlFor="login-password" className="block text-xs font-medium text-slate-600 mb-1.5">
                비밀번호
              </label>
              <div className="relative">
                <input
                  id="login-password"
                  type={showPw ? 'text' : 'password'}
                  name="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="••••••••"
                  disabled={loading}
                  className="w-full h-9 px-3 pr-9 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent disabled:bg-slate-50"
                />
                <button
                  type="button"
                  onClick={() => setShowPw(v => !v)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                  aria-label={showPw ? 'Hide password' : 'Show password'}
                >
                  {showPw ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              </div>
            </div>
            <Button type="submit" className="w-full justify-center" loading={loading} disabled={loading}>
              Sign In
            </Button>
          </form>
        </div>

        <p className="text-center text-xs text-slate-400 mt-6">
          MCPFlow v1.0.0 · Enterprise AI Agent Platform
        </p>
      </div>
    </div>
  );
}
