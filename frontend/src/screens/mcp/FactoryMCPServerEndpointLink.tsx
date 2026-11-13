import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { getMCPServer, setFactoryMCPServerEndpoint } from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { MCPServerDto } from '../../api/types';
import type { FactoryRuntimeBindingDto } from '../../api/factory';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';

interface ErrorInfo { message: string; requestId?: string }

function describeError(err: unknown, fallback: string): ErrorInfo {
  if (isApiError(err)) return { message: err.message, requestId: err.requestId ?? undefined };
  return { message: fallback };
}

/**
 * Explicit manual bridge from READY Factory RuntimeBinding to a DRAFT MCP Server.
 * No implicit network calls beyond GET; only a user-confirmed PATCH may mutate.
 */
export default function FactoryMCPServerEndpointLink({ binding }: { binding: FactoryRuntimeBindingDto }) {
  const navigate = useNavigate();
  const [server, setServer] = useState<MCPServerDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [writeError, setWriteError] = useState<ErrorInfo | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    if (binding.status !== 'READY' || !binding.mcp_endpoint_url) {
      setLoading(false);
      setServer(null);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setServer(null);
    setError(null);
    setWriteError(null);
    setConfirmed(false);
    getMCPServer(binding.mcp_server_id, controller.signal)
      .then(result => {
        if (!cancelled) setServer(result);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        setError(describeError(err, 'MCP 서버의 실제 연결 정보를 조회하지 못했습니다.'));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [binding.status, binding.mcp_endpoint_url, binding.mcp_server_id, refresh]);

  const expectedUrl = binding.mcp_endpoint_url;
  const validUrl = Boolean(expectedUrl && /^https?:\/\/[^\s]+$/i.test(expectedUrl));
  const correctServer = Boolean(server && server.id === binding.mcp_server_id);
  const configured = Boolean(correctServer && validUrl && server?.endpoint_url === expectedUrl);
  const missing = Boolean(correctServer && server?.endpoint_url == null);
  const allowedStatus = server?.status === 'DRAFT' || server?.status === 'INACTIVE';
  const canRegister = binding.status === 'READY' && validUrl && correctServer && missing &&
    allowedStatus && !loading && !saving && !error && confirmed;

  const handleRegister = async () => {
    if (!server || !expectedUrl || !canRegister) return;
    setSaving(true);
    setWriteError(null);
    try {
      const updated = await setFactoryMCPServerEndpoint(
        server.id,
        expectedUrl,
        server.lock_version,
      );
      setServer(updated);
      setConfirmed(false);
    } catch (err) {
      setWriteError(describeError(err,
        'MCP 서버 엔드포인트 등록에 실패했습니다. 최신 서버 정보를 다시 조회한 후 확인하세요.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-3">
      <h3 className="text-sm font-semibold text-slate-800">MCP 서버 주소 등록 — 연결 테스트 전 필수</h3>
      <InlineAlert type="info"
        message="Runtime Binding READY 전환은 MCP 서버 주소를 자동 저장하지 않습니다. 생성된 MCP 엔드포인트를 DRAFT 서버에 별도로 등록해야 연결 테스트와 Discovery를 실행할 수 있습니다." />
      {expectedUrl && (
        <p className="text-xs text-slate-600 break-all">
          등록할 MCP 주소: <span className="font-mono">{expectedUrl}</span>
        </p>
      )}
      {!validUrl && (
        <InlineAlert type="warning" message="Backend가 유효한 MCP 엔드포인트 URL을 반환하지 않아 등록할 수 없습니다." />
      )}
      {loading ? <LoadingSkeleton rows={2} /> : error ? (
        <ErrorState message={error.message} requestId={error.requestId}
          onRetry={() => setRefresh(v => v + 1)} />
      ) : server ? (
        <div className="space-y-2">
          <p className="text-sm text-slate-700">
            MCP 서버 상태: <strong>{server.status}</strong>
            {' · '}설정 버전: {server.lock_version}
          </p>
          {configured ? (
            <InlineAlert type="info"
              message="생성된 MCP 엔드포인트가 서버에 등록되었습니다. 연결 테스트와 Discovery는 아직 별도로 실행해야 합니다." />
          ) : !correctServer ? (
            <InlineAlert type="error"
              message="조회한 서버 ID와 Factory Binding의 서버 ID가 일치하지 않습니다. 등록을 중단했습니다." />
          ) : server.endpoint_url ? (
            <InlineAlert type="warning"
              message="MCP 서버에 다른 엔드포인트가 이미 등록돼 있습니다. 안전을 위해 자동 덮어쓰지 않습니다. 서버의 기존 연결 설정을 확인하세요." />
          ) : !allowedStatus ? (
            <InlineAlert type="warning"
              message="DRAFT 또는 INACTIVE 서버에만 주소 등록이 가능합니다. 현재 서버 상태를 확인하세요." />
          ) : validUrl ? (
            <>
              <label className="flex items-start gap-2 text-sm text-slate-700">
                <input type="checkbox" className="mt-1" checked={confirmed}
                  disabled={saving} onChange={e => setConfirmed(e.target.checked)} />
                <span>위 주소를 실제 MCP 서버에 등록합니다. 자동 연결 테스트·탐색·활성화는 실행하지 않음을 확인했습니다.</span>
              </label>
              <Button disabled={!canRegister} loading={saving} onClick={() => void handleRegister()}>
                MCP 서버 주소 등록
              </Button>
            </>
          ) : null}
          {writeError && (
            <div className="space-y-1">
              <InlineAlert type="error" message={writeError.message} />
              {writeError.requestId && <p className="text-xs font-mono text-slate-500">Request ID: {writeError.requestId}</p>}
              <p className="text-xs text-slate-500">요청 성공 여부가 불확실할 때는 서버를 다시 조회하고 중복 등록 여부를 확인하세요.</p>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={saving} onClick={() => setRefresh(v => v + 1)}>
              MCP 서버 정보 다시 조회
            </Button>
            {configured && (
              <Button variant="outline"
                onClick={() => navigate(`/mcp/servers/${binding.mcp_server_id}`)}>
                연결 테스트·도구 탐색 화면으로
              </Button>
            )}
          </div>
        </div>
      ) : (
        <InlineAlert type="warning" message="Factory MCP 서버의 실제 정보를 확인할 수 없습니다." />
      )}
    </section>
  );
}
