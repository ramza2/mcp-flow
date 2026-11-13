import { useEffect, useState } from 'react';
import Button from '../../components/ui/Button';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import FactoryMCPServerEndpointLink from './FactoryMCPServerEndpointLink';
import {
  createFactoryRuntimeBinding,
  getFactoryRuntimeBindingForPublish,
  readyFactoryRuntimeBinding,
  updateFactoryRuntimeBinding,
  type FactoryRuntimeAuthType,
  type FactoryRuntimeBindingDto,
} from '../../api/factory';

interface ErrorInfo {
  message: string;
  requestId?: string;
}
function errorInfo(err: unknown, fallback: string): ErrorInfo {
  const apiErr = isApiError(err) ? err : null;
  return { message: apiErr?.message ?? fallback, requestId: apiErr?.requestId ?? undefined };
}

export default function FactoryRuntimeBindingPanel({
  publishId,
  mcpServerId,
}: {
  publishId: string;
  mcpServerId: string;
}) {
  const [binding, setBinding] = useState<FactoryRuntimeBindingDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [readError, setReadError] = useState<ErrorInfo | null>(null);
  const [actionError, setActionError] = useState<ErrorInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [createConfirmed, setCreateConfirmed] = useState(false);
  const [readyConfirmed, setReadyConfirmed] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  const [authType, setAuthType] = useState<FactoryRuntimeAuthType>('NONE');
  const [secretId, setSecretId] = useState('');

  const populate = (value: FactoryRuntimeBindingDto) => {
    setBinding(value);
    setBaseUrl(value.base_url ?? '');
    setAuthType(value.auth_type);
    setSecretId(value.auth_secret_id ?? '');
    setCreateConfirmed(false);
    setReadyConfirmed(false);
  };

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setReadError(null);
    setBinding(null);
    setActionError(null);
    setCreateConfirmed(false);
    setReadyConfirmed(false);
    getFactoryRuntimeBindingForPublish(publishId, controller.signal)
      .then(value => { if (!cancelled) populate(value); })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        if (isApiError(err) && err.status === 404) {
          setBinding(null);
          return;
        }
        setReadError(errorInfo(err, 'Runtime Binding 정보를 불러오지 못했습니다.'));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [publishId, refresh]);

  const handleCreate = async () => {
    if (loading || busy || readError || binding || !createConfirmed) return;
    setBusy(true);
    setActionError(null);
    try {
      const value = await createFactoryRuntimeBinding(publishId);
      populate(value);
    } catch (err) {
      setActionError(errorInfo(err,
        'Binding 생성 요청이 실패했습니다. 이미 생성되었을 수 있으므로 조회로 상태를 확인하세요.'));
    } finally {
      setBusy(false);
    }
  };

  const canSave = binding?.status === 'DRAFT' && !loading && !busy &&
    baseUrl.trim().length > 0 &&
    (/^https?:\/\//i.test(baseUrl.trim())) &&
    (authType === 'NONE' || secretId.trim().length > 0);
  const dirty = binding != null &&
    (baseUrl.trim() !== (binding.base_url ?? '') ||
     authType !== binding.auth_type ||
     (authType !== 'NONE' && secretId.trim() !== (binding.auth_secret_id ?? '')));

  const handleSave = async () => {
    if (!binding || !canSave || !dirty) return;
    setBusy(true);
    setActionError(null);
    try {
      const value = await updateFactoryRuntimeBinding(binding.id, {
        base_url: baseUrl.trim(),
        auth_type: authType,
        auth_secret_id: authType === 'NONE' ? null : secretId.trim(),
        lock_version: binding.lock_version,
      });
      populate(value);
    } catch (err) {
      setActionError(errorInfo(err, '상위 API 연결 설정 저장에 실패했습니다. 버전 충돌이면 다시 조회하세요.'));
    } finally {
      setBusy(false);
    }
  };

  const handleReady = async () => {
    if (!binding || binding.status !== 'DRAFT' || busy || loading ||
        !readyConfirmed || dirty || !binding.base_url ||
        (binding.auth_type !== 'NONE' && !binding.auth_secret_id)) return;
    setBusy(true);
    setActionError(null);
    try {
      const value = await readyFactoryRuntimeBinding(binding.id, binding.lock_version);
      populate(value);
    } catch (err) {
      setActionError(errorInfo(err, 'Runtime Binding READY 전환에 실패했습니다. 서버 검증 결과를 확인하세요.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-4">
      <h2 className="font-semibold text-slate-800">실제 API 연결 설정 (Factory Runtime Binding)</h2>
      <InlineAlert type="info"
        message="게시된 DRAFT MCP 서버에 연결할 원본 REST API 주소를 설정합니다. 이 단계에서는 외부 API 호출 테스트·MCP 도구 활성화를 자동 실행하지 않습니다. READY는 설정과 증빙의 유효성 검사이며 실제 연결 성공을 의미하지 않습니다." />
      {loading ? (
        <LoadingSkeleton rows={3} />
      ) : readError ? (
        <ErrorState message={readError.message} requestId={readError.requestId}
          onRetry={() => setRefresh(v => v + 1)} />
      ) : !binding ? (
        <div className="space-y-3">
          <EmptyState
            title="연결 설정이 아직 없습니다"
            description="조회만으로 연결 설정을 생성하지 않습니다. 이 게시 결과에 연결 설정을 추가하려면 아래에서 확인 후 실행하세요."
          />
          <label className="flex gap-2 text-sm text-slate-700 items-start">
            <input type="checkbox" className="mt-1" checked={createConfirmed}
              disabled={busy} onChange={event => setCreateConfirmed(event.target.checked)} />
            운영 DB에 DRAFT Runtime Binding 레코드를 생성하는 것을 확인했습니다.
          </label>
          <Button disabled={!createConfirmed || busy} loading={busy}
            onClick={() => void handleCreate()}>
            DRAFT 연결 설정 생성
          </Button>
        </div>
      ) : (
        <div className="space-y-4">
          <dl className="text-sm space-y-2">
            <div className="flex flex-wrap gap-2">
              <dt className="text-slate-500">Binding 상태</dt>
              <dd className="font-semibold">{binding.status}</dd>
            </div>
            <div className="flex flex-wrap gap-2">
              <dt className="text-slate-500">Binding ID</dt>
              <dd className="font-mono text-xs break-all">{binding.id}</dd>
            </div>
            <div className="flex flex-wrap gap-2">
              <dt className="text-slate-500">설정 버전</dt>
              <dd>{binding.lock_version}</dd>
            </div>
            {binding.last_error_code && (
              <div className="flex flex-wrap gap-2">
                <dt className="text-slate-500">최근 오류</dt>
                <dd className="text-red-700 break-all">{binding.last_error_code} {binding.last_error_message}</dd>
              </div>
            )}
          </dl>
          {binding.status === 'DRAFT' ? (
            <div className="space-y-3 border-t border-slate-100 pt-3">
              <div className="space-y-1">
                <label htmlFor="factory-runtime-base-url" className="block text-sm font-semibold text-slate-700">
                  원본 REST API Base URL
                </label>
                <input id="factory-runtime-base-url" type="url" value={baseUrl}
                  disabled={busy} placeholder="https://api.example.com"
                  onChange={event => { setBaseUrl(event.target.value); setReadyConfirmed(false); setActionError(null); }}
                  className="w-full border border-slate-300 rounded-lg p-2 text-sm" />
                <p className="text-xs text-slate-500">
                  OpenAPI에 정의된 도구가 실제 요청을 보내는 상위 API 주소입니다.
                  이 값은 MCP 서버 주소가 아닙니다. 서버의 허용 호스트 정책에 의해 거부될 수 있습니다.
                </p>
              </div>
              <div className="space-y-1">
                <label htmlFor="factory-runtime-auth-type" className="block text-sm font-semibold text-slate-700">상위 API 인증</label>
                <select id="factory-runtime-auth-type" value={authType}
                  disabled={busy} onChange={event => {
                    setAuthType(event.target.value as FactoryRuntimeAuthType);
                    setReadyConfirmed(false);
                    setActionError(null);
                  }}
                  className="w-full border border-slate-300 rounded-lg p-2 text-sm">
                  <option value="NONE">인증 없음</option>
                  <option value="BEARER">Bearer (기존 Secret ID 필요)</option>
                  <option value="BASIC">Basic (기존 Secret ID 필요)</option>
                </select>
              </div>
              {authType !== 'NONE' && (
                <div className="space-y-1">
                  <label htmlFor="factory-runtime-secret-id" className="block text-sm font-semibold text-slate-700">
                    기존 Secret ID
                  </label>
                  <input id="factory-runtime-secret-id" value={secretId}
                    disabled={busy} placeholder="등록된 Secret UUID"
                    onChange={event => { setSecretId(event.target.value); setReadyConfirmed(false); setActionError(null); }}
                    className="w-full border border-slate-300 rounded-lg p-2 font-mono text-sm" />
                  <p className="text-xs text-slate-500">
                    실제 비밀번호·API Key·토큰은 입력하지 마세요. 사전 등록된 Secret의 UUID만 지정합니다.
                  </p>
                </div>
              )}
              <Button variant="outline" disabled={!canSave || !dirty} loading={busy}
                onClick={() => void handleSave()}>
                연결 설정 저장
              </Button>
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <InlineAlert type="warning"
                  message="READY는 설정·상위 주소 정책·패키지 증빙을 검사합니다. 실제 원격 API 접속 성공이나 MCP Tool 활성화로 처리하지 않습니다. 설정 저장 후에만 전환할 수 있습니다." />
                <label className="flex items-start gap-2 text-sm text-slate-700">
                  <input type="checkbox" className="mt-1" checked={readyConfirmed}
                    disabled={busy || dirty || !binding.base_url}
                    onChange={event => setReadyConfirmed(event.target.checked)} />
                  상위 API 연결 설정이 저장되었으며 READY 전환을 실행할 것을 확인했습니다.
                </label>
                <Button disabled={busy || dirty || !readyConfirmed || !binding.base_url ||
                  (binding.auth_type !== 'NONE' && !binding.auth_secret_id)}
                  loading={busy} onClick={() => void handleReady()}>
                  Runtime Binding READY 전환
                </Button>
              </div>
            </div>
          ) : binding.status === 'READY' ? (
            <div className="space-y-3 border-t border-slate-100 pt-3">
              <InlineAlert type="info"
                message="Runtime Binding이 READY입니다. MCP 서버 연결 테스트·Discovery·도구 검증·활성화는 아래의 별도 서버 화면에서 사용자가 단계별로 실행해야 합니다." />
              <p className="text-sm text-slate-700">
                원본 API 주소: <span className="font-mono break-all">{binding.base_url}</span>
              </p>
              {binding.mcp_endpoint_url && (
                <p className="text-sm text-slate-700">
                  MCP 엔드포인트: <span className="font-mono break-all">{binding.mcp_endpoint_url}</span>
                </p>
              )}
              {binding.mcp_server_id === mcpServerId ? (
                <FactoryMCPServerEndpointLink binding={binding} />
              ) : (
                <InlineAlert type="error"
                  message="게시 결과와 Runtime Binding의 MCP 서버 ID가 다릅니다. 연결 설정 등록을 중단합니다." />
              )}
            </div>
          ) : (
            <InlineAlert type="warning" message="이 Runtime Binding은 DISABLED입니다. 기존 연결 설정을 활성화할 수 없습니다." />
          )}
          <div>
            <Button variant="outline" disabled={busy} onClick={() => setRefresh(v => v + 1)}>
              연결 설정 다시 조회
            </Button>
          </div>
        </div>
      )}
      {actionError && (
        <div className="space-y-1">
          <InlineAlert type="error" message={actionError.message} />
          {actionError.requestId && (
            <p className="font-mono text-xs text-slate-500">Request ID: {actionError.requestId}</p>
          )}
        </div>
      )}
    </section>
  );
}
