import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Bot, Loader2, RefreshCw, Send } from 'lucide-react';
import Button from '../../components/ui/Button';
import StatusBadge from '../../components/ui/StatusBadge';
import { isAbortError, isApiError } from '../../api/client';
import {
  analyzeAgentRun,
  getConversationLatestAgentRun,
  getAgentRunSelection,
  listRunnableAgents,
  selectToolForAgentRun,
  submitAgentRun,
  type AgentRunRequestDto,
  type AgentRunSelectionDto,
  type RunnableAgentDto,
} from '../../api/agentRun';

function failureMessage(err: unknown): string {
  if (isApiError(err)) return err.message;
  return err instanceof Error ? err.message : '요청 처리 중 오류가 발생했습니다.';
}

export default function AgentRunLive() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const [agents, setAgents] = useState<RunnableAgentDto[]>([]);
  const [agentId, setAgentId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [request, setRequest] = useState<AgentRunRequestDto | null>(null);
  const [selection, setSelection] = useState<AgentRunSelectionDto | null>(null);
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
      const selected = await getAgentRunSelection(latest.id, signal);
      if (signal?.aborted) return;
      setRequest(selected.request);
      setSelection(selected.selection);
      setError(null);
    } catch (err: unknown) {
      if (!isAbortError(err) && !signal?.aborted) setError(failureMessage(err));
    }
  }, [conversationId]);

  useEffect(() => {
    if (!conversationId) {
      setRequest(null);
      setSelection(null);
      return;
    }
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [conversationId, refresh]);

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
    } catch (err: unknown) {
      setError(`Tool 검색·선택 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요.`);
      // Never auto-retry a possibly in-flight embedding / LLM selection.
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
          실제 DB와 ModelOps 분석을 사용합니다. 자연어 분석과 Tool 검색·선택까지 연결되었습니다.
          계획 생성과 실제 실행은 다음 개발 범위입니다.
        </p>
      </header>
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
        <strong>현재 지원 범위:</strong> 실제 Agent 버전으로 요청 등록·LLM 분석·Tool 검색 및 선택.
        RETRIEVING 상태에서 검색·선택 버튼을 눌러야 시작됩니다.
        계획과 Execution은 아직 생성하지 않으며, 외부 MCP Tool은 호출하지 않습니다.
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
          {request.status === 'RETRIEVING' && !working && !selection && (
            <Button size="sm" onClick={() => void handleSelectTool()}>실제 Tool 검색·선택</Button>
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
                <p className="text-amber-700 text-sm">명확화가 필요합니다. 실제 질문 응답 연동은 후속 단계에서 제공합니다.</p>
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
          <div className="border-t pt-3 flex items-center gap-3 text-sm text-slate-500">
            <span>Execution: 생성되지 않음</span>
            <Button size="sm" variant="outline" onClick={() => navigate('/executions')}>실제 Execution 이력 보기</Button>
          </div>
        </section>
      )}
    </div>
  );
}
