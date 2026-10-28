import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Bot, ChevronDown, ChevronUp, Loader2, RefreshCw, Send } from 'lucide-react';
import Button from '../../components/ui/Button';
import StatusBadge from '../../components/ui/StatusBadge';
import { isAbortError, isApiError } from '../../api/client';
import {
  analyzeAgentRun,
  buildAgentRunParameters,
  createAgentRunExecution,
  generateAgentRunPlan,
  getAgentRunChatResult,
  getAgentRunProgress,
  getConversationLatestAgentRun,
  getAgentRunSelection,
  listRunnableAgents,
  respondToAgentRunClarification,
  selectToolForAgentRun,
  submitAgentRun,
  validateAgentRunPlan,
  type AgentRunChatResultDto,
  type AgentRunProgressDto,
  type AgentRunQuestionFieldDto,
  type AgentRunRequestDto,
  type AgentRunSelectionDto,
  type RunnableAgentDto,
} from '../../api/agentRun';
import {
  getExecution,
  listExecutionSteps,
  listExecutions,
  type ExecutionStepListItemDto,
} from '../../api/executions';
import {
  isEventSourceAvailable,
  openExecutionEventsStream,
} from '../../api/executionEvents';
import type { JsonValue } from '../../api/types';

type ChatRole = 'user' | 'assistant' | 'system';

interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
}

const TERMINAL_EXECUTION = new Set([
  'SUCCEEDED',
  'PARTIALLY_SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'TIMED_OUT',
]);

const AUTO_PLAN_STATUSES = new Set([
  'RECEIVED',
  'RETRIEVING',
  'BUILDING_PARAMETERS',
  'PLANNING',
  'VALIDATING',
]);

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

function statusPlainKorean(status: string): string {
  const map: Record<string, string> = {
    RECEIVED: '요청을 접수했습니다.',
    ANALYZING: '요청을 분석하고 있습니다.',
    RETRIEVING: '사용할 기능을 찾고 있습니다.',
    SELECTING: '기능을 선택하고 있습니다.',
    BUILDING_PARAMETERS: '필요한 입력값을 준비하고 있습니다.',
    PLANNING: '실행 계획을 만들고 있습니다.',
    VALIDATING: '실행 계획을 확인하고 있습니다.',
    WAITING_INPUT: '추가 정보가 필요합니다.',
    WAITING_CONFIRMATION: '실행 전 확인이 필요합니다.',
    READY: '실행 준비가 완료되었습니다.',
    REJECTED: '요청이 거절되었습니다.',
    FAILED: '요청 처리에 실패했습니다.',
    CANCELLED: '요청이 취소되었습니다.',
    CREATED: '실행이 생성되었습니다.',
    QUEUED: '실행 대기 중입니다.',
    RUNNING: '업무를 실행하고 있습니다.',
    WAITING_APPROVAL: '승인이 필요합니다.',
    CANCEL_REQUESTED: '실행 취소가 요청되었습니다.',
    SUCCEEDED: '실행이 완료되었습니다.',
    PARTIALLY_SUCCEEDED: '실행이 일부 완료되었습니다.',
    TIMED_OUT: '실행 시간이 초과되었습니다.',
  };
  return map[status] ?? `상태: ${status}`;
}

let messageSeq = 0;
function nextMessageId(): string {
  messageSeq += 1;
  return `msg-${messageSeq}`;
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
  const [chatResult, setChatResult] = useState<AgentRunChatResultDto | null>(null);
  const [executionSteps, setExecutionSteps] = useState<ExecutionStepListItemDto[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [questionValues, setQuestionValues] = useState<Record<string, string>>({});
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const inFlight = useRef(false);
  const autoPipelineLock = useRef(false);
  const autoExecAttemptedFor = useRef<string>('');
  const lastStatusMessage = useRef<string>('');
  /** Dedupes final chat answer when SSE + polling both observe terminal. */
  const chatResultAnnouncedFor = useRef<string>('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const refreshExecutionSteps = useCallback(async (executionId: string, signal?: AbortSignal) => {
    try {
      const listed = await listExecutionSteps(executionId, signal);
      if (signal?.aborted) return;
      setExecutionSteps(listed.items);
    } catch {
      // Keep prior step snapshot; chat-result also carries terminal steps.
    }
  }, []);

  const pushMessage = useCallback((role: ChatRole, text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (role === 'system' && lastStatusMessage.current === trimmed) return;
    if (role === 'system') lastStatusMessage.current = trimmed;
    setMessages((prev) => [...prev, { id: nextMessageId(), role, text: trimmed }]);
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, progress?.open_clarification?.id, chatResult, execution?.status]);

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

  const loadChatResult = useCallback(async (requestId: string, signal?: AbortSignal) => {
    try {
      const result = await getAgentRunChatResult(requestId, signal);
      // Always apply the projection once fetched; ignoring a stale abort avoids
      // dropping terminal results under React Strict Mode double-invoke.
      setChatResult(result);
      if (signal?.aborted) return;
      const announceKey = result.execution_id ?? requestId;
      const terminalStatus = result.execution_status;
      const shouldAnnounce =
        result.available
        || (
          Boolean(result.summary_text)
          && terminalStatus != null
          && TERMINAL_EXECUTION.has(terminalStatus)
        );
      if (!shouldAnnounce) return;
      if (chatResultAnnouncedFor.current === announceKey) return;
      chatResultAnnouncedFor.current = announceKey;
      if (result.steps && result.steps.length > 0) {
        setExecutionSteps((prev) => {
          if (prev.length > 0) return prev;
          return result.steps!.map((step, index) => ({
            id: `${result.execution_id ?? requestId}-${step.step_key}`,
            execution_id: result.execution_id ?? '',
            step_key: step.step_key,
            step_type: 'TOOL',
            parent_step_id: null,
            sequence_hint: index,
            mcp_tool_version_id: null,
            iteration_no: null,
            status: step.status,
            attempt_count: 0,
            condition_result: null,
            ready_at: null,
            started_at: null,
            finished_at: null,
            error_code: null,
            error_category: null,
            duration_ms: null,
          }));
        });
      }
      if (result.available && result.display_items.length > 0) {
        const stepLine = (result.steps && result.steps.length > 1)
          ? `\n단계: ${result.steps.map((s) => `${s.tool_name ?? s.step_key}(${s.status})`).join(' → ')}`
          : '';
        const lines = result.display_items.map(
          (item) => `· ${item.name} / ${item.status}`,
        );
        pushMessage(
          'assistant',
          `${result.summary_text ?? '조회 결과입니다.'}${stepLine}\n${lines.join('\n')}`,
        );
      } else if (result.summary_text) {
        pushMessage('assistant', result.summary_text);
      }
    } catch (err: unknown) {
      if (!isAbortError(err) && !signal?.aborted) {
        setError(`결과 조회 실패: ${failureMessage(err)}`);
      }
    }
  }, [pushMessage]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!conversationId) return null as AgentRunProgressDto | null;
    try {
      const latest = await getConversationLatestAgentRun(conversationId, signal);
      if (signal?.aborted) return null;
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
      if (signal?.aborted) return null;
      setRequest(stage.request);
      setSelection(selected.selection);
      setProgress(stage);
      const existing = history.items[0] ?? null;
      setExecution(existing);
      setExecutionLoadedForRequest(latest.id);
      setError(null);
      if (existing && TERMINAL_EXECUTION.has(existing.status)) {
        await loadChatResult(latest.id, signal);
      }
      return stage;
    } catch (err: unknown) {
      if (!isAbortError(err) && !signal?.aborted) setError(failureMessage(err));
      return null;
    }
  }, [conversationId, loadChatResult]);

  useEffect(() => {
    if (!conversationId) {
      setRequest(null);
      setSelection(null);
      setProgress(null);
      setExecution(null);
      setExecutionLoadedForRequest('');
      setChatResult(null);
      setExecutionSteps([]);
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
    autoExecAttemptedFor.current = '';
    chatResultAnnouncedFor.current = '';
    setExecutionSteps([]);
  }, [request?.id]);

  // Execution SSE / polling while non-terminal.
  useEffect(() => {
    if (!execution?.id || !request?.id) return;
    if (TERMINAL_EXECUTION.has(execution.status)) return;

    let cancelled = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const stream = isEventSourceAvailable()
      ? openExecutionEventsStream({
          executionId: execution.id,
          onEvent: (message) => {
            if (cancelled) return;
            const type = message.envelope.event_type;
            if (!type.startsWith('execution.')) return;
            const status = type.slice('execution.'.length).toUpperCase();
            // Map catalog names like succeeded → SUCCEEDED; keep known statuses only.
            const normalized = status.replace(/\./g, '_');
            const mapped = normalized === 'PARTIAL_SUCCEEDED'
              ? 'PARTIALLY_SUCCEEDED'
              : normalized;
            const known = [
              'CREATED', 'QUEUED', 'STARTED', 'RUNNING',
              'WAITING_INPUT', 'WAITING_APPROVAL', 'CANCEL_REQUESTED',
              'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT',
            ];
            let next = execution.status;
            if (mapped === 'STARTED') next = 'RUNNING';
            else if (known.includes(mapped)) next = mapped;
            setExecution((prev) => (prev ? { ...prev, status: next } : prev));
            void refreshExecutionSteps(execution.id);
            pushMessage('system', statusPlainKorean(next));
            if (TERMINAL_EXECUTION.has(next) && request) {
              void loadChatResult(request.id);
            }
          },
        })
      : null;

    const poll = async () => {
      if (cancelled || !execution.id) return;
      try {
        const detail = await getExecution(execution.id);
        if (cancelled) return;
        setExecution({ id: detail.id, status: detail.status });
        await refreshExecutionSteps(execution.id);
        if (TERMINAL_EXECUTION.has(detail.status) && request) {
          pushMessage('system', statusPlainKorean(detail.status));
          await loadChatResult(request.id);
          if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
        }
      } catch {
        // Keep prior status; user can refresh.
      }
    };

    pollTimer = setInterval(() => {
      void poll();
    }, 2500);
    void poll();

    return () => {
      cancelled = true;
      stream?.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [execution?.id, execution?.status, request, loadChatResult, pushMessage, refreshExecutionSteps]);

  const startAnalysis = async (requestId: string) => {
    try {
      const analyzed = await analyzeAgentRun(requestId);
      setRequest(analyzed);
      pushMessage('system', statusPlainKorean(analyzed.status));
      setError(null);
      return analyzed;
    } catch (err: unknown) {
      setError(`분석 요청 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요.`);
      return null;
    }
  };

  const runAutoPipeline = useCallback(async (
    seed?: AgentRunProgressDto | null,
    opts?: { allowCreate?: boolean },
  ) => {
    if (autoPipelineLock.current || inFlight.current) return;
    autoPipelineLock.current = true;
    setWorking(true);
    try {
      let stage = seed ?? null;
      if (!stage && request) {
        stage = await getAgentRunProgress(request.id);
        setProgress(stage);
        setRequest(stage.request);
      }
      // Cap steps to avoid infinite loops on unexpected status oscillation.
      for (let i = 0; i < 8; i += 1) {
        if (!stage) break;
        const status = stage.request.status;
        const requestId = stage.request.id;

        if (status === 'RECEIVED') {
          const analyzed = await startAnalysis(requestId);
          if (!analyzed) break;
          stage = await getAgentRunProgress(requestId);
          setProgress(stage);
          setRequest(stage.request);
          continue;
        }
        if (status === 'RETRIEVING') {
          pushMessage('system', '사용할 기능을 찾고 있습니다…');
          const selected = await selectToolForAgentRun(requestId);
          setRequest(selected.request);
          setSelection(selected.selection);
          if (selected.selection?.decision === 'NO_MATCH') {
            pushMessage('assistant', '요청에 맞는 기능을 찾지 못했습니다. 다른 표현으로 다시 시도해 주세요.');
            stage = await getAgentRunProgress(requestId);
            setProgress(stage);
            break;
          }
          if (selected.selection?.decision === 'CLARIFY' || selected.selection?.decision === 'CONFIRM') {
            pushMessage('assistant', selected.selection.reason_summary
              || '기능을 확정하려면 추가 확인이 필요합니다.');
          }
          stage = await getAgentRunProgress(requestId);
          setProgress(stage);
          setRequest(stage.request);
          continue;
        }
        if (status === 'BUILDING_PARAMETERS') {
          pushMessage('system', '필요한 입력값을 준비하고 있습니다…');
          stage = await buildAgentRunParameters(requestId);
          setProgress(stage);
          setRequest(stage.request);
          continue;
        }
        if (status === 'PLANNING') {
          pushMessage('system', '실행 계획을 만들고 있습니다…');
          stage = await generateAgentRunPlan(requestId);
          setProgress(stage);
          setRequest(stage.request);
          continue;
        }
        if (status === 'VALIDATING') {
          pushMessage('system', '실행 계획을 확인하고 있습니다…');
          stage = await validateAgentRunPlan(requestId);
          setProgress(stage);
          setRequest(stage.request);
          continue;
        }

        if (status === 'WAITING_INPUT' || status === 'WAITING_CONFIRMATION') {
          const promptText = stage.open_clarification?.prompt_text;
          pushMessage(
            'assistant',
            promptText
              || (status === 'WAITING_INPUT'
                ? '추가로 알려주셔야 하는 정보가 있습니다.'
                : '실행하기 전에 확인이 필요합니다.'),
          );
          break;
        }
        if (status === 'REJECTED' || status === 'FAILED' || status === 'CANCELLED') {
          pushMessage('assistant', statusPlainKorean(status));
          break;
        }
        if (status === 'READY') {
          if (stage.safe_auto_execute) {
            const historyChecked = executionLoadedForRequest === requestId;
            const canCreate = Boolean(
              opts?.allowCreate || (historyChecked && !execution),
            );
            if (autoExecAttemptedFor.current !== requestId && canCreate) {
              autoExecAttemptedFor.current = requestId;
              const stepHint = stage.plan && stage.plan.step_count > 1
                ? ` (${stage.plan.step_count}단계 조회)`
                : '';
              pushMessage('system', `안전한 조회 작업이라 바로 실행합니다${stepHint}…`);
              try {
                const created = await createAgentRunExecution(requestId);
                setExecution({ id: created.id, status: created.status });
                void refreshExecutionSteps(created.id);
              } catch (err: unknown) {
                setError(
                  `실행 생성 결과 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요. 자동 재시도하지 않습니다.`,
                );
              }
            }
          } else {
            pushMessage(
              'assistant',
              '이 작업은 바로 자동 실행하지 않습니다. 아래에서 내용을 확인한 뒤 실행해 주세요.',
            );
          }
          break;
        }
        // Stuck ANALYZING / SELECTING — never auto-retry.
        if (status === 'ANALYZING' || status === 'SELECTING') {
          pushMessage('assistant', statusPlainKorean(status) + ' 상태 새로고침으로 확인해 주세요. 안전을 위해 자동 재시도하지 않습니다.');
          break;
        }
        if (!AUTO_PLAN_STATUSES.has(status)) break;
      }
    } catch (err: unknown) {
      setError(`자동 진행 확인 필요: ${failureMessage(err)}. 상태 새로고침으로 확인하세요.`);
    } finally {
      autoPipelineLock.current = false;
      setWorking(false);
    }
  // startAnalysis is stable enough via closure; include key deps only.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [execution, executionLoadedForRequest, pushMessage, request]);

  // After refresh loads a conversation mid-pipeline, continue auto steps once.
  const autoStartedForRequest = useRef<string>('');
  useEffect(() => {
    if (!request || !progress) return;
    if (executionLoadedForRequest !== request.id) return;
    if (autoStartedForRequest.current === request.id) return;
    if (!AUTO_PLAN_STATUSES.has(request.status) && request.status !== 'READY') return;
    // Do not auto-kick ANALYZING/SELECTING (may be in-flight elsewhere).
    if (request.status === 'ANALYZING' || request.status === 'SELECTING') return;
    autoStartedForRequest.current = request.id;
    void runAutoPipeline(progress);
  }, [request, progress, executionLoadedForRequest, runAutoPipeline]);

  const handleSend = async () => {
    const text = prompt.trim();
    if (inFlight.current || !text || !agentId) return;
    inFlight.current = true;
    setWorking(true);
    setError(null);
    try {
      pushMessage('user', text);
      const created = await submitAgentRun(agentId, text);
      setPrompt('');
      setRequest(created);
      setSelection(null);
      setProgress(null);
      setExecution(null);
      setExecutionLoadedForRequest('');
      setChatResult(null);
      lastStatusMessage.current = '';
      autoStartedForRequest.current = created.id;
      navigate(`/run/${created.conversation_id}`);
      pushMessage('system', '요청을 접수했습니다. 분석을 시작합니다…');
      const analyzed = await startAnalysis(created.id);
      if (analyzed) {
        const stage = await getAgentRunProgress(created.id);
        setProgress(stage);
        setRequest(stage.request);
        setExecutionLoadedForRequest(created.id);
        inFlight.current = false;
        // allowCreate: history is known empty for this brand-new request (setState
        // has not flushed executionLoadedForRequest into the pipeline closure yet).
        await runAutoPipeline(stage, { allowCreate: true });
        return;
      }
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
      const stage = await getAgentRunProgress(request.id);
      setProgress(stage);
      setRequest(stage.request);
      inFlight.current = false;
      await runAutoPipeline(stage);
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
      const stage = await refresh();
      inFlight.current = false;
      if (stage) await runAutoPipeline(stage);
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
      pushMessage('system', statusPlainKorean(created.status));
    } catch (err: unknown) {
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
  const showManualReadyConfirm = Boolean(
    request
    && request.status === 'READY'
    && executionLoadedForRequest === request.id
    && !execution
    && !progress?.safe_auto_execute,
  );

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold text-slate-900 flex items-center gap-2">
          <Bot size={20} /> Agent 업무 실행
        </h1>
        <p className="text-sm text-slate-500">
          자연어로 업무를 지시하면, 안전한 조회 작업은 자동으로 진행되고 결과를 이 화면에서 확인할 수 있습니다.
        </p>
      </header>

      <section className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
        <label className="block text-sm font-semibold text-slate-700" htmlFor="agent-run-selection">
          Agent 선택
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
      </section>

      <section
        className="rounded-xl border border-slate-200 bg-slate-50 min-h-[280px] max-h-[52vh] overflow-y-auto p-4 space-y-3"
        aria-live="polite"
      >
        {messages.length === 0 && !request && (
          <p className="text-sm text-slate-500">
            예: &quot;반려동물 목록을 조회해 줘&quot;처럼 원하는 업무를 입력한 뒤 실행을 눌러 주세요.
          </p>
        )}
        {messages.map((message) => (
          <div
            key={message.id}
            className={
              message.role === 'user'
                ? 'ml-8 rounded-lg bg-indigo-600 text-white px-3 py-2 text-sm whitespace-pre-wrap'
                : message.role === 'assistant'
                  ? 'mr-8 rounded-lg bg-white border border-slate-200 px-3 py-2 text-sm text-slate-800 whitespace-pre-wrap'
                  : 'text-xs text-slate-500 px-1'
            }
          >
            {message.text}
          </div>
        ))}

        {progress?.open_clarification && (
          <div className="mr-4 rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3">
            <h3 className="text-sm font-semibold text-amber-900">
              {progress.open_clarification.request_type === 'MISSING_PARAMETER'
                ? '추가 정보 입력'
                : progress.open_clarification.request_type === 'TOOL_CONFIRMATION'
                  ? '기능 확인'
                  : '실행 계획 확인'}
            </h3>
            <p className="text-sm text-amber-900">{progress.open_clarification.prompt_text}</p>
            {progress.open_clarification.request_type === 'MISSING_PARAMETER' ? (
              <>
                <p className="text-xs text-amber-800">비밀번호·토큰 등 비밀정보는 직접 입력하지 마세요.</p>
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

        {showManualReadyConfirm && (
          <div className="mr-4 rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3">
            <p className="text-sm text-amber-900">
              이 작업은 자동 실행 대상이 아닙니다. 외부 시스템에 영향을 줄 수 있으니 확인 후 실행해 주세요.
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

        {executionSteps.length > 0 && (
          <div className="mr-4 rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-2">
            <h3 className="text-sm font-semibold text-slate-800">실행 단계</h3>
            <ol className="text-sm text-slate-700 space-y-1 list-decimal list-inside">
              {executionSteps.map((step) => (
                <li key={step.id}>
                  {step.step_key}
                  <span className="ml-2 text-xs text-slate-500">{step.status}</span>
                </li>
              ))}
            </ol>
          </div>
        )}

        {working && (
          <div role="status" className="flex gap-2 items-center text-sm text-indigo-700">
            <Loader2 size={15} className="animate-spin" />
            처리 중입니다…
          </div>
        )}
        <div ref={messagesEndRef} />
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
        <label htmlFor="agent-run-prompt" className="block text-sm font-semibold text-slate-700">
          업무 지시
        </label>
        <textarea
          id="agent-run-prompt"
          className="w-full rounded-md border border-slate-300 p-3 text-sm"
          placeholder="무엇을 도와드릴까요? 예: 반려동물 목록을 조회해 줘"
          rows={3}
          maxLength={4000}
          disabled={working}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void handleSend();
            }
          }}
        />
        <div className="flex flex-wrap gap-2">
          <Button
            icon={<Send size={14} />}
            loading={working}
            disabled={!agentId || !prompt.trim() || working}
            onClick={() => void handleSend()}
          >
            실행
          </Button>
          <Button variant="outline" onClick={() => navigate('/run')} disabled={working}>
            새 대화
          </Button>
          {request && (
            <Button
              variant="outline"
              size="sm"
              icon={<RefreshCw size={13} />}
              onClick={() => void refresh()}
              disabled={working}
            >
              상태 새로고침
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            icon={advanced ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            onClick={() => setAdvanced((value) => !value)}
          >
            {advanced ? '간단히 보기' : '고급 보기'}
          </Button>
        </div>
      </section>

      {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

      {request && (
        <div className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <StatusBadge status={request.status} size="sm" />
          {execution && executionLoadedForRequest === request.id ? (
            <>
              <span>Execution: {execution.status} · {execution.id}</span>
              <Button size="sm" variant="outline" onClick={() => navigate(`/executions/${execution.id}`)}>
                실행 상세 보기
              </Button>
            </>
          ) : (
            <span>Execution: {executionLoadedForRequest === request.id ? '생성되지 않음' : '확인 중'}</span>
          )}
          <Button size="sm" variant="outline" onClick={() => navigate('/executions')}>전체 실행 이력 보기</Button>
        </div>
      )}

      {advanced && request && (
        <section className="rounded-xl border border-slate-200 bg-white p-5 space-y-4">
          <h2 className="text-base font-semibold text-slate-800">고급 상세</h2>
          <p className="text-xs text-slate-500 font-mono break-all">요청 ID: {request.id}</p>
          <p className="text-sm text-slate-800 whitespace-pre-wrap">{request.raw_request_text}</p>
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
            </div>
          )}
          {progress?.validation && (
            <div className="rounded-lg bg-slate-50 p-3 space-y-1">
              <h3 className="text-sm font-semibold">계획 검증 결과</h3>
              <p className="text-sm">{progress.validation.decision}</p>
              <p className="text-xs">
                오류 {progress.validation.error_count}건 · 경고 {progress.validation.warning_count}건
              </p>
              {progress.safe_auto_execute && (
                <p className="text-xs text-emerald-700">안전한 자동 실행 대상(READ_ONLY)입니다.</p>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
