import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import Button from '../../components/ui/Button';
import JsonViewer from '../../components/ui/JsonViewer';
import { InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { createManualToolTest, getManualToolTestResult } from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { JsonValue, ManualToolTestCreateResult, ManualToolTestResultDto, MCPToolVersionDto } from '../../api/types';
import { shortenId } from '../../domain';
import { toFeedbackError, type FeedbackError } from './toolLifecycle';

function defaultArgumentsJson(version: MCPToolVersionDto | null): string {
  if (!version?.input_schema || typeof version.input_schema !== 'object') {
    return '{\n}\n';
  }
  const schema = version.input_schema as {
    properties?: Record<string, unknown>;
    required?: string[];
  };
  const props = schema.properties ?? {};
  const seed: Record<string, unknown> = {};
  for (const key of Object.keys(props)) {
    seed[key] = null;
  }
  return `${JSON.stringify(seed, null, 2)}\n`;
}

interface Props {
  toolId: string;
  selectedVersion: MCPToolVersionDto | null;
  currentVersionId: string | null;
  active: boolean;
}

export default function ToolTestCallTab({
  toolId,
  selectedVersion,
  currentVersionId,
  active,
}: Props) {
  const navigate = useNavigate();
  const [argumentsText, setArgumentsText] = useState('{\n}\n');
  const [parseError, setParseError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<FeedbackError | null>(null);
  const [result, setResult] = useState<ManualToolTestCreateResult | null>(null);
  const [responsePreview, setResponsePreview] = useState<ManualToolTestResultDto | null>(null);
  const [checkingResponse, setCheckingResponse] = useState(false);
  const [responseError, setResponseError] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    setArgumentsText(defaultArgumentsJson(selectedVersion));
    setParseError(null);
    setError(null);
    setResult(null);
    setResponsePreview(null);
    setResponseError(null);
  }, [active, selectedVersion?.id]);

  const canRun = useMemo(() => {
    if (!selectedVersion) return false;
    if (selectedVersion.validation_status !== 'VALID') return false;
    if (currentVersionId && selectedVersion.id !== currentVersionId) return false;
    return true;
  }, [selectedVersion, currentVersionId]);

  async function onRun() {
    if (!selectedVersion || !canRun) return;
    let parsed: Record<string, unknown>;
    try {
      const value = JSON.parse(argumentsText) as unknown;
      if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        setParseError('실행 인수는 JSON 객체 형태여야 합니다.');
        return;
      }
      parsed = value as Record<string, unknown>;
      setParseError(null);
    } catch {
      setParseError('실행 인수의 JSON 형식이 올바르지 않습니다.');
      return;
    }

    setSubmitting(true);
    setError(null);
    setResult(null);
    setResponsePreview(null);
    setResponseError(null);
    try {
      const created = await createManualToolTest(
        toolId,
        {
          mcp_tool_version_id: selectedVersion.id,
          arguments: parsed as Record<string, JsonValue>,
        },
        { idempotencyKey: crypto.randomUUID() },
      );
      setResult(created);
    } catch (err) {
      if (isAbortError(err)) return;
      setError(
        isApiError(err) && err.status === 403
          ? {
            message: '도구 실행 권한이 없습니다. 관리자에게 mcp.tool.execute 기능 권한과 해당 MCP 도구의 접근 권한(ResourceGrant)을 요청하세요.',
            requestId: err.requestId ?? undefined,
            code: err.code,
          }
          : toFeedbackError(err, '도구 실행 테스트에 실패했습니다.'),
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function onCheckResponse() {
    if (!result || checkingResponse) return;
    setCheckingResponse(true);
    setResponseError(null);
    try {
      const preview = await getManualToolTestResult(toolId, result.id);
      setResponsePreview(preview);
    } catch (err) {
      if (isAbortError(err)) return;
      setResponseError(
        isApiError(err) && err.status === 403
          ? '응답 조회 권한이 없습니다. 현재 mcp.tool.execute 및 해당 도구의 ResourceGrant를 확인해 주세요.'
          : toFeedbackError(err, '실행 응답을 확인하지 못했습니다.').message,
      );
    } finally {
      setCheckingResponse(false);
    }
  }

  if (!active) return null;

  if (!selectedVersion) {
    return <LoadingSkeleton rows={3} />;
  }

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-slate-800">도구 실행 테스트</h3>
            <p className="text-xs text-slate-500 mt-1">
              선택한 도구 버전으로 실제 수동 실행 작업(MANUAL_TOOL_TEST)을 생성합니다.
              실행 테스트만으로 검증 완료(VERIFIED) 기록이 자동 생성되지는 않습니다.
            </p>
          </div>
          <Button
            size="sm"
            disabled={!canRun || submitting}
            onClick={() => void onRun()}
          >
            {submitting ? '실행 중…' : '테스트 실행'}
          </Button>
        </div>

        <dl className="grid grid-cols-2 gap-2 text-xs text-slate-600">
          <div>
            <dt className="text-slate-400">도구 버전</dt>
            <dd className="font-mono">v{selectedVersion.version_no}</dd>
          </div>
          <div>
            <dt className="text-slate-400">유효성 검사</dt>
            <dd>{selectedVersion.validation_status}</dd>
          </div>
          <div className="col-span-2">
            <dt className="text-slate-400">버전 ID</dt>
            <dd className="font-mono">{shortenId(selectedVersion.id)}</dd>
          </div>
        </dl>

        <p className="text-xs text-slate-500">
          실행 전 입력 스키마의 필수값을 확인하고, JSON 인수를 실제 값으로 수정하세요.
          실행 테스트는 외부 MCP 도구를 호출할 수 있으므로 데이터 변경 가능성과 실행 정책을 먼저 확인하세요.
        </p>

        {!canRun && (
          <InlineAlert
            type="warning"
            message={
              selectedVersion.validation_status !== 'VALID'
                ? '유효성 검사를 통과한(VALID) 도구 버전만 실행 테스트를 수행할 수 있습니다.'
                : '현재 도구의 최신 활성 버전(current_version)만 실행 테스트를 수행할 수 있습니다.'
            }
          />
        )}

        <div>
          <label className="block text-xs font-medium text-slate-600 mb-1">
            실행 인수 (JSON 객체)
          </label>
          <textarea
            className="w-full min-h-[180px] font-mono text-xs border border-slate-200 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-slate-300"
            value={argumentsText}
            onChange={(e) => setArgumentsText(e.target.value)}
            spellCheck={false}
            disabled={submitting}
          />
          {parseError && <InlineAlert type="error" message={parseError} />}
        </div>

        {error && (
          <div className="space-y-1">
            <InlineAlert type="error" message={error.message} />
            {error.requestId && (
              <p className="font-mono text-xs text-slate-400">Request ID: {error.requestId}</p>
            )}
          </div>
        )}

        {result && (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 space-y-2">
            <p className="text-sm text-emerald-900">
              실행 작업 생성됨: <span className="font-mono">{result.id}</span>
            </p>
            <p className="text-xs text-emerald-800">
              상태={result.status} · 출처={result.source_type} · 실행 유형=
              {result.trigger_type}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                disabled={checkingResponse}
                onClick={() => void onCheckResponse()}
              >
                {checkingResponse ? '응답 확인 중…' : '실행 응답 확인 / 새로고침'}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => navigate(`/executions/${result.id}`)}
              >
                실행 상세로 이동
              </Button>
            </div>
            {responseError && <InlineAlert type="error" message={responseError} />}
            {responsePreview && (
              <div className="rounded-lg border border-slate-200 bg-white p-3 space-y-2">
                <p className="text-xs text-slate-700">
                  실행 상태: <strong>{responsePreview.status}</strong>
                  {responsePreview.message_echoed === true && (
                    <span className="ml-2 text-emerald-700 font-semibold">
                      입력 메시지 응답 포함 확인
                    </span>
                  )}
                  {responsePreview.message_echoed === false && (
                    <span className="ml-2 text-amber-700 font-semibold">
                      입력 메시지와 응답 불일치
                    </span>
                  )}
                </p>
                {responsePreview.available ? (
                  <div className="space-y-2">
                    <p className="text-xs font-medium text-slate-700">도구 응답 텍스트</p>
                    {responsePreview.text_items.map((item, index) => (
                      <pre
                        key={index}
                        className="max-h-64 overflow-auto rounded border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700 whitespace-pre-wrap break-all"
                      >
                        {item}
                      </pre>
                    ))}
                    {responsePreview.truncated && (
                      <p className="text-xs text-amber-700">일부 응답은 길이 제한으로 생략됐습니다.</p>
                    )}
                  </div>
                ) : (
                  <p className="text-xs text-slate-600">
                    {responsePreview.status !== 'SUCCEEDED'
                      ? '아직 실행 중이거나 완료되지 않았습니다. 잠시 후 다시 응답 확인을 눌러 주세요.'
                      : '표시 가능한 텍스트 결과가 없습니다. (' +
                        (responsePreview.unavailable_reason ?? 'EMPTY') + ')'}
                  </p>
                )}
                <p className="text-xs text-slate-500">
                  보안을 위해 텍스트 응답만 제한적으로 표시하며 메타데이터와 민감정보는 제외합니다.
                </p>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">입력 스키마</h3>
        <JsonViewer
          value={selectedVersion.input_schema ?? null}
          emptyLabel="입력 스키마가 없습니다."
        />
      </div>
    </div>
  );
}
