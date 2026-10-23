import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Bot, Loader2, RefreshCw, Send } from 'lucide-react';
import Button from '../../components/ui/Button';
import StatusBadge from '../../components/ui/StatusBadge';
import { isAbortError, isApiError } from '../../api/client';
import {
  analyzeAgentRun,
  buildAgentRunParameters,
  createAgentRunExecution,
  generateAgentRunPlan,
  getAgentRunProgress,
  getConversationLatestAgentRun,
  getAgentRunSelection,
  listRunnableAgents,
  respondToAgentRunClarification,
  selectToolForAgentRun,
  submitAgentRun,
  validateAgentRunPlan,
  type AgentRunProgressDto,
  type AgentRunQuestionFieldDto,
  type AgentRunRequestDto,
  type AgentRunSelectionDto,
  type RunnableAgentDto,
} from '../../api/agentRun';
import type { JsonValue } from '../../api/types';
import { listExecutions } from '../../api/executions';

function failureMessage(err: unknown): string {
  if (isApiError(err)) return err.message;
  return err instanceof Error ? err.message : '요청 처리 중 오류가 발생했습니다.';
}


function fieldValue(field: AgentRunQuestionFieldDto, raw: string): JsonValue {
  const value = raw.trim();
  if (!value) throw new Error('필수 입력값을 입력해 주세요: ' + field.name);
  if (field.value_type === 'boolean') {
    if (value === 'true') return true;
    if (value === 'false') return false;
    throw new Error('예 또는 아니오를 선택해 주세요: ' + field.name);
  }
  if (field.value_type === 'number' || field.value_type === 'integer') {
    const numberValue = Number(value);
    if (!Number.isFinite(numberValue) || (field.value_type === 'integer' && !Number.isInteger(numberValue))) {
      throw new Error('유효한 숫자를 입력해 주세요: ' + field.name);
    }
    return numberValue;
  }
  if (field.value_type === 'object' || field.value_type === 'array') {
    let parsed: JsonValue;
    try {
      parsed = JSON.parse(value) as JsonValue;
    } catch {
      throw new Error('올바른 JSON 형식으로 입력해 주세요: ' + field.name);
    }
    if (field.value_type === 'array' ? !Array.isArray(parsed) : !parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      throw new Error('입력 형식을 확인해 주세요: ' + field.name);
    }
    return parsed;
  }
  return raw;
}

export default function AgentRunLive() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const [agents, setAgents] = useState<RunnableAgentDto[]>([]);
  const [agentId, setAgentId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [request, setRequest] = useState<AgentRunRequestDto | null>(null);
  const [selection, setSelection] = useState<AgentRunSelectionDto | null>(null);
  const [progress, setProgress] = useState<AgentRunProgressDto | null>(null);
  const [execution, setExecution] = useState<{ id: string; status: string } | null>(null);
  const [executionLoadedForRequest, setExecutionLoadedForRequest] = useState('');
  const [executionConfirmed, setExecutionConfirmed] = useState(false);
  const [questionValues, setQuestionValues] = useState<Record<string, string>>({});
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    void listRunnableAgents(controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      setAgents(response.items);
      setAgentId((old) => old || response.items[0]?.id || '');
    }).catch((err: unknown) => {
      if (!isAbortError(err)) setError(failureMessage(err));
    }).finally(() => {
      if (!controller.signal.aborted) setLoadingAgents(false);
    });
    return () => controller.abort();
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!conversationId) return;
    try {
      const latest = await getConversationLatestAgentRun(conversationId, signal);
      if (signal?.aborted) return;
      setRequest(latest);
      const [selected, stage, history] = await Promise.all([
        getAgentRunSelection(latest.id, signal),
        getAgentRunProgress(latest.id, signal),
        listExecutions({
          agent_request_id: latest.id,
          source_type: 'AGENT_REQUEST',
          page_size: 1,
          signal,
        }),
      ]);
      if (signal?.aborted) return;
      setRequest(stage.request);
      setSelection(selected.selection);
      setProgress(stage);
      setExecution(history.items[0] ?? null);
      setExecutionLoadedForRequest(latest.id);
      setError(null);
    } catch (err: unknown) {
      if (!isAbortError(err) && !signal?.aborted) setError(failureMessage(err));
    }
  }, [conversationId]);

  useEffect(() => {
    if (!conversationId) {
      setRequest(null);
      setSelection(null);
      setProgress(null);
      setExecution(null);
      setExecutionLoadedForRequest('');
      return;
    }
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [conversationId, refresh]);

  useEffect(() => {
    setQuestionValues({});
  }, [progress?.open_clarification?.id]);

  useEffect(() => {
    setExecutionConfirmed(false);
  }, [request?.id]);

  const startAnalysis = async (requestId: string) => {
    try {
      const analyzed = await analyzeAgentRun(requestId);
      setRequest(analyzed);
      setError(null);
    } catch (err: unknown) {
      // HTTP timeout / disconnect does not mean Analyzer failed: read persisted state.
      setError(`분석 요청 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요.`);
      // Do not retry a potentially in-flight LLM call.
    }
  };

  const handleSend = async () => {
    const text = prompt.trim();
    if (inFlight.current || !text || !agentId) return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      const created = await submitAgentRun(agentId, text);
      setPrompt('');
      setRequest(created);
      setSelection(null);
      setProgress(null);
      setExecution(null);
      setExecutionLoadedForRequest('');
      navigate(`/run/${created.conversation_id}`);
      await startAnalysis(created.id);
    } catch (err: unknown) {
      setError(failureMessage(err));
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  };

  const handleResumeAnalysis = async () => {
    if (inFlight.current || !request || request.status !== 'RECEIVED') return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      await startAnalysis(request.id);
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  };

  const handleSelectTool = async () => {
    if (inFlight.current || !request || request.status !== 'RETRIEVING') return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      const selected = await selectToolForAgentRun(request.id);
      setRequest(selected.request);
      setSelection(selected.selection);
      setProgress(await getAgentRunProgress(request.id));
    } catch (err: unknown) {
      setError(`Tool 검색·선택 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요.`);
      // Never auto-retry a possibly in-flight embedding / LLM selection.
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  };


  const handlePlanningStep = async () => {
    if (inFlight.current || !request) return;
    const status = request.status;
    if (!['BUILDING_PARAMETERS', 'PLANNING', 'VALIDATING'].includes(status)) return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      const result = status === 'BUILDING_PARAMETERS'
        ? await buildAgentRunParameters(request.id)
        : status === 'PLANNING'
          ? await generateAgentRunPlan(request.id)
          : await validateAgentRunPlan(request.id);
      setRequest(result.request);
      setProgress(result);
    } catch (err: unknown) {
      setError('계획 처리 결과 확인 필요: ' + failureMessage(err) + '. 상태 새로고침으로 확인하세요.');
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  };

  const handleClarification = async (confirmation?: boolean) => {
    const clarification = progress?.open_clarification;
    if (inFlight.current || !request || !clarification) return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      let payload: Record<string, JsonValue>;
      if (clarification.request_type === 'MISSING_PARAMETER') {
        payload = {};
        for (const field of clarification.fields) {
          const raw = questionValues[field.name] ?? '';
          if (!raw.trim() && !field.required) continue;
          payload[field.name] = fieldValue(field, raw);
        }
      } else {
        if (confirmation === undefined) throw new Error('확인 여부를 선택해 주세요.');
        payload = { confirmed: confirmation };
      }
      await respondToAgentRunClarification(request.id, clarification.id, payload);
      setQuestionValues({});
      await refresh();
    } catch (err: unknown) {
      setError('추가 입력/확인 처리 실패: ' + failureMessage(err));
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  };

  const handleCreateExecution = async () => {
    if (
      inFlight.current || !request || request.status !== 'READY'
      || executionLoadedForRequest !== request.id || execution || !executionConfirmed
    ) return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      const created = await createAgentRunExecution(request.id);
      setExecution({ id: created.id, status: created.status });
      setExecutionConfirmed(false);
      navigate(`/executions/${created.id}`);
    } catch (err: unknown) {
      // A timeout/disconnect might occur after the server committed the Execution.
      // Never retry automatically; re-read history first. Manual repeats use the same key.
      setError(`실행 생성 결과 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요. 자동 재시도하지 않습니다.`);
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  };

  const selectionLabel: Record<AgentRunSelectionDto['decision'], string> = {
    AUTO_SELECT: '자동 선택',
    CONFIRM: '선택 확인 필요',
    CLARIFY: '추가 입력 필요',
    NO_MATCH: '일치하는 Tool 없음',
  };

  const data = request?.structured_request;

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-5">
      <header>
        <h1 className="text-xl font-semibold text-slate-900 flex items-center gap-2">
          <Bot size={20} /> Agent 업무 지시
        </h1>
        <p className="text-sm text-slate-500 mt-1">
          실제 DB와 ModelOps 분석을 사용합니다. Tool 선택부터 파라미터 구성·계획 생성·검증·확인까지 연결됩니다.
          검증 완료 후 명시적으로 실행을 시작하면 별도 Execution에서 진행됩니다.
        </p>
      </header>
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
        <strong>현재 지원 범위:</strong> 실제 Agent 요청 분석 → Tool 선택 → 입력값 구성 → 단일 Tool 실행계획 → 정책 검증/사용자 확인 → 실행 생성.
        실행 시작을 누르면 별도 Execution이 생성되고, 이후 외부 MCP Tool 호출은 실행 Worker에서 비동기로 처리됩니다.
      </div>

      <section className="rounded-xl border border-slate-200 bg-white p-5 space-y-3">
        <label className="block text-sm font-semibold text-slate-700" htmlFor="agent-run-selection">
          사용 가능한 Agent
        </label>
        <select
          id="agent-run-selection"
          className="w-full rounded-md border border-slate-300 p-2 text-sm"
          value={agentId}
          disabled={working || loadingAgents}
          onChange={(event) => setAgentId(event.target.value)}
        >
          {agents.length === 0 && <option value="">사용 가능한 Agent 없음</option>}
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name} (v{agent.version_no})
            </option>
          ))}
        </select>
        <label htmlFor="agent-run-prompt" className="block text-sm font-semibold text-slate-700">
          업무 지시
        </label>
        <textarea
          id="agent-run-prompt"
          className="w-full rounded-md border border-slate-300 p-3 text-sm"
          placeholder="분석할 자연어 업무 지시를 입력하세요."
          rows={4}
          maxLength={4000}
          disabled={working}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
        />
        <div className="flex gap-2">
          <Button icon={<Send size={14} />} loading={working} disabled={!agentId || !prompt.trim() || working} onClick={() => void handleSend()}>
            실제 분석 시작
          </Button>
          <Button variant="outline" onClick={() => navigate('/run')} disabled={working}>
            새 요청 화면
          </Button>
        </div>
      </section>

      {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

      {request && (
        <section className="rounded-xl border border-slate-200 bg-white p-5 space-y-4">
          <div className="flex justify-between items-center flex-wrap gap-2">
            <h2 className="text-base font-semibold text-slate-800">저장된 AgentRequest</h2>
            <div className="flex items-center gap-2">
              <StatusBadge status={request.status} size="sm" />
              <Button variant="outline" size="sm" icon={<RefreshCw size={13} />} onClick={() => void refresh()} disabled={working}>상태 새로고침</Button>
            </div>
          </div>
          <p className="text-xs text-slate-500 font-mono break-all">요청 ID: {request.id}</p>
          <p className="text-sm text-slate-800 whitespace-pre-wrap">{request.raw_request_text}</p>
          {working && <div role="status" className="flex gap-2 items-center text-sm text-indigo-700"><Loader2 size={15} className="animate-spin" />{request.status === 'RETRIEVING' ? '실제 Tool 검색·선택 중입니다.' : '실제 LLM 분석을 요청 중입니다.'}</div>}
          {request.status === 'RECEIVED' && !working && (
            <Button size="sm" onClick={() => void handleResumeAnalysis()}>저장된 요청 분석 시작</Button>
          )}
          {request.status === 'RETRIEVING' && !working && (
            <Button size="sm" onClick={() => void handleSelectTool()}>실제 Tool 검색·선택</Button>
          )}
          {['BUILDING_PARAMETERS', 'PLANNING', 'VALIDATING'].includes(request.status) && !working && (
            <Button size="sm" onClick={() => void handlePlanningStep()}>
              {request.status === 'BUILDING_PARAMETERS'
                ? '입력 파라미터 구성'
                : request.status === 'PLANNING' ? '실행계획 생성' : '실행계획 검증'}
            </Button>
          )}
          {request.status === 'READY' && (
            <p className="text-sm text-green-800">
              계획 검증이 완료됐습니다. 실제 업무 실행은 별도 확인 후 시작할 수 있습니다.
            </p>
          )}
          {request.status === 'READY' && executionLoadedForRequest === request.id && !execution && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3">
              <p className="text-sm text-amber-900">
                실제 실행을 시작하면 외부 MCP Tool이 호출되며, 외부 데이터 변경이나 업무 처리가 발생할 수 있습니다.
              </p>
              <label className="flex gap-2 items-start text-sm text-amber-900">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={executionConfirmed}
                  disabled={working}
                  onChange={(event) => setExecutionConfirmed(event.target.checked)}
                />
                실제 시스템에서 실행되는 작업임을 확인했습니다.
              </label>
              <Button size="sm" disabled={working || !executionConfirmed} onClick={() => void handleCreateExecution()}>
                실제 업무 실행 시작
              </Button>
            </div>
          )}
          {request.status === 'SELECTING' && !working && (
            <p className="text-sm text-amber-700">
              Tool 평가가 진행 중이거나 중단됐을 수 있습니다. 저장 상태를 새로고침해 확인하세요.
            </p>
          )}
          {request.status === 'ANALYZING' && !working && (
            <p className="text-sm text-amber-700">
              분석이 진행 중이거나 중단됐을 수 있습니다. 상태를 새로고침해 확인하세요.
              안전을 위해 자동 재시도하지 않습니다.
            </p>
          )}
          {request.status === 'FAILED' && (
            <p className="text-sm text-red-700">요청 처리가 실패했습니다. 저장된 상태와 이력은 보존됩니다.</p>
          )}
          {data && (
            <div className="space-y-3">
              <div className="rounded-lg bg-slate-50 p-3">
                <h3 className="text-sm font-semibold mb-1">업무 의도</h3>
                <p className="text-sm">{String(data.intent ?? '판별 안 됨')}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-3">
                <h3 className="text-sm font-semibold mb-1">필요 업무 기능</h3>
                <pre className="text-xs whitespace-pre-wrap break-words">{JSON.stringify(data.required_capabilities ?? [], null, 2)}</pre>
              </div>
              <div className="rounded-lg bg-slate-50 p-3">
                <h3 className="text-sm font-semibold mb-1">추출 정보 · 위험 · 추가 질문</h3>
                <pre className="text-xs whitespace-pre-wrap break-words">{JSON.stringify({
                  entities: data.entities,
                  risk_hints: data.risk_hints,
                  needs_clarification: data.needs_clarification,
                  missing_inputs: data.missing_inputs,
                  ambiguities: data.ambiguities,
                }, null, 2)}</pre>
              </div>
              {request.status === 'WAITING_INPUT' ? (
                <p className="text-amber-700 text-sm">추가 입력이 필요합니다. 아래 입력 항목을 확인해 주세요.</p>
              ) : (
                <p className="text-sm text-slate-500">
                  {selection
                    ? 'Tool 선택 결과가 DB에 저장됐습니다. 계획 생성과 실행은 시작되지 않았습니다.'
                    : '분석 결과가 DB에 저장됐습니다. Tool 검색·선택은 버튼을 눌러 시작할 수 있습니다.'}
                </p>
              )}
            </div>
          )}
          {selection && (
            <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-3 space-y-2">
              <h3 className="text-sm font-semibold text-indigo-900">실제 Tool 선택 결과</h3>
              <p className="text-sm text-indigo-900">{selectionLabel[selection.decision]}</p>
              {selection.selected_tool_version_id && (
                <p className="text-sm text-indigo-900">
                  선택 Tool: {selection.selected_tool_name ?? '이름 확인 불가'}
                  <span className="block text-xs font-mono break-all">
                    Version ID: {selection.selected_tool_version_id}
                  </span>
                </p>
              )}
              {selection.confidence !== null && (
                <p className="text-xs text-indigo-800">
                  선택 신뢰도: {Math.round(selection.confidence * 100)}%
                </p>
              )}
              {selection.reason_summary && (
                <p className="text-sm text-indigo-800 whitespace-pre-wrap">
                  {selection.reason_summary}
                </p>
              )}
              <p className="text-xs text-indigo-700">
                이 결과는 검색·선택 기록이며 MCP 호출 또는 업무 실행 완료를 의미하지 않습니다.
              </p>
            </div>
          )}

          {progress?.parameter_build && (
            <div className="rounded-lg bg-slate-50 p-3 space-y-1">
              <h3 className="text-sm font-semibold">입력 파라미터 구성 결과</h3>
              <p className="text-sm">{progress.parameter_build.is_complete ? '필수 입력 완료' : '추가 입력 필요'}</p>
              {progress.parameter_build.missing_fields.length > 0 && (
                <p className="text-xs">미입력 항목: {progress.parameter_build.missing_fields.join(', ')}</p>
              )}
            </div>
          )}
          {progress?.plan && (
            <div className="rounded-lg bg-slate-50 p-3 space-y-2">
              <h3 className="text-sm font-semibold">저장된 실행계획 요약</h3>
              <p className="text-sm">목적: {progress.plan.goal ?? '목적 미지정'}</p>
              <p className="text-sm">단계 수: {progress.plan.step_count}</p>
              <p className="text-sm">단계: {progress.plan.step_names.join(', ') || '표시할 단계 없음'}</p>
              <p className="text-xs font-mono break-all">Plan Hash: {progress.plan.plan_hash}</p>
              <p className="text-xs text-slate-500">입력값·Secret 참조·원본 binding은 화면에 표시하지 않습니다.</p>
            </div>
          )}
          {progress?.validation && (
            <div className="rounded-lg bg-slate-50 p-3 space-y-1">
              <h3 className="text-sm font-semibold">계획 검증 결과</h3>
              <p className="text-sm">{progress.validation.decision}</p>
              <p className="text-xs">
                오류 {progress.validation.error_count}건 · 경고 {progress.validation.warning_count}건
              </p>
            </div>
          )}
          {progress?.open_clarification && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3">
              <h3 className="text-sm font-semibold text-amber-900">
                {progress.open_clarification.request_type === 'MISSING_PARAMETER'
                  ? '추가 정보 입력' : progress.open_clarification.request_type === 'TOOL_CONFIRMATION'
                    ? '선택 Tool 확인' : '실행계획 확인'}
              </h3>
              <p className="text-sm text-amber-900">{progress.open_clarification.prompt_text}</p>
              {progress.open_clarification.request_type === 'MISSING_PARAMETER' ? (
                <>
                  <p className="text-xs text-amber-800">비밀번호·토큰 등 원문 비밀정보를 직접 입력하지 마세요.</p>
                  {progress.open_clarification.fields.map((field) => (
                    <div key={field.name} className="space-y-1">
                      <label className="block text-sm font-medium text-amber-900" htmlFor={'question-' + field.name}>
                        {field.name}{field.required ? ' (필수)' : ''}
                      </label>
                      {field.description && <p className="text-xs">{field.description}</p>}
                      {field.value_type === 'boolean' ? (
                        <select
                          id={'question-' + field.name}
                          className="w-full border rounded p-2 text-sm"
                          disabled={working}
                          value={questionValues[field.name] ?? ''}
                          onChange={(event) => setQuestionValues((prev) => ({ ...prev, [field.name]: event.target.value }))}
                        >
                          <option value="">선택해 주세요</option>
                          <option value="true">예</option>
                          <option value="false">아니오</option>
                        </select>
                      ) : (
                        <input
                          id={'question-' + field.name}
                          type={field.value_type === 'integer' || field.value_type === 'number' ? 'number' : 'text'}
                          step={field.value_type === 'integer' ? '1' : 'any'}
                          className="w-full border rounded p-2 text-sm"
                          disabled={working}
                          placeholder={field.value_type === 'object' || field.value_type === 'array'
                            ? 'JSON 형식으로 입력' : '값을 입력해 주세요'}
                          value={questionValues[field.name] ?? ''}
                          onChange={(event) => setQuestionValues((prev) => ({ ...prev, [field.name]: event.target.value }))}
                        />
                      )}
                    </div>
                  ))}
                  <Button size="sm" disabled={working} onClick={() => void handleClarification()}>
                    입력 내용 제출
                  </Button>
                </>
              ) : (
                <div className="flex gap-2">
                  <Button size="sm" disabled={working} onClick={() => void handleClarification(true)}>
                    확인하고 계속
                  </Button>
                  <Button size="sm" variant="outline" disabled={working} onClick={() => void handleClarification(false)}>
                    거절하고 종료
                  </Button>
                </div>
              )}
            </div>
          )}
          <div className="border-t pt-3 flex items-center gap-3 flex-wrap text-sm text-slate-500">
            {execution && executionLoadedForRequest === request.id ? (
              <>
                <span>Execution: {execution.status} · {execution.id}</span>
                <Button size="sm" onClick={() => navigate(`/executions/${execution.id}`)}>
                  실행 결과·단계 이력 보기
                </Button>
              </>
            ) : (
              <span>Execution: {executionLoadedForRequest === request.id ? '생성되지 않음' : '확인 중'}</span>
            )}
            <Button size="sm" variant="outline" onClick={() => navigate('/executions')}>전체 실행 이력 보기</Button>
          </div>
        </section>
      )}
    </div>
  );
}
