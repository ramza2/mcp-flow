import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import {
  ArrowLeft,
  X,
  CheckCircle2,
  Loader2,
  Circle,
  AlertTriangle,
  Clock,
} from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import {
  EmptyState,
  ErrorState,
  InlineAlert,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import MrtrInputPanel from '../../components/MrtrInputPanel';
import PermissionGate from '../../components/PermissionGate';
import {
  cancelExecution,
  getExecution,
  getExecutionStep,
  listExecutionSteps,
  type ExecutionDetailDto,
  type ExecutionStepDetailDto,
  type ExecutionStepListItemDto,
  type StepAttemptSafeDto,
} from '../../api/executions';
import {
  MAX_TIMELINE_EVENTS,
  SNAPSHOT_REFRESH_DEBOUNCE_MS,
  SSE_MAX_CONSECUTIVE_ERRORS,
  isEventSourceAvailable,
  openExecutionEventsStream,
  parseSseEventId,
  type ExecutionEventEnvelope,
  type ExecutionEventMessage,
  type SseConnectionState,
} from '../../api/executionEvents';
import {
  getAuditEvent,
  listAuditEvents,
  type AuditEventDetailDto,
  type AuditEventListItemDto,
} from '../../api/audit';
import { isAbortError, isApiError } from '../../api/client';
import {
  formatDurationMs,
  formatTimestamp,
  labelExecutionSource,
  type ExecutionStatus,
} from '../../domain';

const ACTIVE_STATUSES = new Set<ExecutionStatus>([
  'CREATED',
  'QUEUED',
  'RUNNING',
  'WAITING_INPUT',
  'WAITING_APPROVAL',
  'CANCEL_REQUESTED',
]);

const POLL_MS = 4000;

/** Timeline row: wire envelope + durable SSE bigint id (decimal string). */
export interface ExecutionTimelineItem extends ExecutionEventEnvelope {
  sseId: string;
}

function connectionLabel(state: SseConnectionState): string {
  switch (state) {
    case 'connecting':
      return 'Connecting';
    case 'live':
      return 'Live';
    case 'reconnecting':
      return 'Reconnecting';
    case 'polling':
      return 'Polling fallback';
    case 'unavailable':
      return 'Unavailable';
    case 'disconnected':
    default:
      return 'Disconnected';
  }
}

function JsonBlock({ title, value, note }: { title: string; value: unknown; note?: string }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      {note && <p className="text-xs text-slate-400 mb-2">{note}</p>}
      <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700 overflow-x-auto max-h-80">
        {value == null ? 'null' : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

export default function ExecutionDetail() {
  const { executionId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const [execution, setExecution] = useState<ExecutionDetailDto | null>(null);
  const [steps, setSteps] = useState<ExecutionStepListItemDto[]>([]);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [stepDetail, setStepDetail] = useState<ExecutionStepDetailDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [stepDetailLoading, setStepDetailLoading] = useState(false);
  const [hasSnapshot, setHasSnapshot] = useState(false);
  const [usePollingFallback, setUsePollingFallback] = useState(false);
  const [connectionState, setConnectionState] =
    useState<SseConnectionState>('disconnected');
  const [timeline, setTimeline] = useState<ExecutionTimelineItem[]>([]);
  const [snapshotVersion, setSnapshotVersion] = useState(0);

  const pollInFlight = useRef(false);
  const snapshotSeq = useRef(0);
  const stepDetailSeq = useRef(0);
  const lastAcceptedSseId = useRef<bigint | null>(null);
  const statusRef = useRef<ExecutionStatus | null>(null);
  const refreshTimer = useRef<number | null>(null);
  const refreshInFlight = useRef(false);
  const refreshAgain = useRef(false);
  const loadSnapshotRef = useRef<
    (signal?: AbortSignal, opts?: { quiet?: boolean }) => Promise<void>
  >(async () => undefined);

  statusRef.current = execution?.status ?? null;

  const loadSnapshot = useCallback(
    async (signal?: AbortSignal, opts?: { quiet?: boolean }) => {
      if (!executionId) return;
      const seq = ++snapshotSeq.current;
      if (!opts?.quiet) {
        setLoading(true);
        setError(null);
        setForbidden(false);
        setNotFound(false);
      }
      try {
        const [detail, stepList] = await Promise.all([
          getExecution(executionId, signal),
          listExecutionSteps(executionId, signal),
        ]);
        if (signal?.aborted || seq !== snapshotSeq.current) return;
        setExecution(detail);
        setSteps(stepList.items);
        setHasSnapshot(true);
        setSnapshotVersion((v) => v + 1);
      } catch (err: unknown) {
        if (isAbortError(err) || signal?.aborted || seq !== snapshotSeq.current) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setExecution(null);
          setHasSnapshot(false);
          return;
        }
        if (isApiError(err) && err.status === 404) {
          setNotFound(true);
          setExecution(null);
          setHasSnapshot(false);
          return;
        }
        if (!opts?.quiet) {
          const apiErr = isApiError(err) ? err : null;
          setError({
            message: apiErr?.message ?? 'Execution을 불러오지 못했습니다.',
            requestId: apiErr?.requestId ?? undefined,
          });
        }
      } finally {
        if (!opts?.quiet && !signal?.aborted && seq === snapshotSeq.current) {
          setLoading(false);
        }
      }
    },
    [executionId],
  );

  loadSnapshotRef.current = loadSnapshot;

  const runQuietRefresh = useCallback(async () => {
    if (refreshInFlight.current) {
      refreshAgain.current = true;
      return;
    }
    refreshInFlight.current = true;
    try {
      do {
        refreshAgain.current = false;
        await loadSnapshotRef.current(undefined, { quiet: true });
      } while (refreshAgain.current);
    } finally {
      refreshInFlight.current = false;
    }
  }, []);

  const scheduleQuietRefresh = useCallback(() => {
    if (refreshTimer.current != null) {
      window.clearTimeout(refreshTimer.current);
    }
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      void runQuietRefresh();
    }, SNAPSHOT_REFRESH_DEBOUNCE_MS);
  }, [runQuietRefresh]);

  const acceptSseEvent = useCallback(
    (message: ExecutionEventMessage) => {
      const id = parseSseEventId(message.sseId);
      if (id === null) return;
      if (lastAcceptedSseId.current !== null && id <= lastAcceptedSseId.current) {
        return;
      }
      lastAcceptedSseId.current = id;
      const item: ExecutionTimelineItem = {
        sseId: message.sseId,
        ...message.envelope,
      };
      setTimeline((prev) => {
        if (prev.some((row) => row.sseId === item.sseId)) return prev;
        const next = [...prev, item];
        return next.length > MAX_TIMELINE_EVENTS
          ? next.slice(-MAX_TIMELINE_EVENTS)
          : next;
      });
      // Invalidate REST snapshot — never apply SSE payload status directly.
      scheduleQuietRefresh();
    },
    [scheduleQuietRefresh],
  );

  // Reset transport/timeline when route execution changes.
  useEffect(() => {
    setHasSnapshot(false);
    setUsePollingFallback(false);
    setConnectionState('disconnected');
    setTimeline([]);
    lastAcceptedSseId.current = null;
    snapshotSeq.current = 0;
    setSelectedStepId(null);
    setStepDetail(null);
  }, [executionId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadSnapshot(controller.signal);
    return () => controller.abort();
  }, [loadSnapshot]);

  // Snapshot → SSE (native EventSource reconnect preserves Last-Event-ID).
  useEffect(() => {
    if (!executionId || !hasSnapshot || usePollingFallback) return;

    if (!isEventSourceAvailable()) {
      setConnectionState('unavailable');
      const status = statusRef.current;
      if (status && ACTIVE_STATUSES.has(status)) {
        setUsePollingFallback(true);
      }
      return;
    }

    let closed = false;
    let consecutiveErrors = 0;
    setConnectionState('connecting');

    const stream = openExecutionEventsStream({
      executionId,
      onOpen: () => {
        if (closed) return;
        consecutiveErrors = 0;
        setConnectionState('live');
      },
      onError: () => {
        if (closed) return;
        consecutiveErrors += 1;
        setConnectionState('reconnecting');
        if (consecutiveErrors >= SSE_MAX_CONSECUTIVE_ERRORS) {
          closed = true;
          stream.close();
          const status = statusRef.current;
          if (status && ACTIVE_STATUSES.has(status)) {
            setUsePollingFallback(true);
            setConnectionState('polling');
          } else {
            setConnectionState('disconnected');
          }
        }
      },
      onEvent: (message) => {
        if (closed) return;
        acceptSseEvent(message);
      },
    });

    return () => {
      closed = true;
      stream.close();
    };
  }, [executionId, hasSnapshot, usePollingFallback, acceptSseEvent]);

  // Polling fallback for active executions only (after sustained SSE failure).
  useEffect(() => {
    if (!executionId || !usePollingFallback) return;
    if (!execution || !ACTIVE_STATUSES.has(execution.status)) {
      setConnectionState((prev) => (prev === 'polling' ? 'disconnected' : prev));
      return;
    }
    setConnectionState('polling');
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      if (pollInFlight.current || controller.signal.aborted) return;
      pollInFlight.current = true;
      void loadSnapshot(controller.signal, { quiet: true }).finally(() => {
        pollInFlight.current = false;
      });
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [executionId, usePollingFallback, execution?.status, loadSnapshot]);

  // Selected Step detail — refresh when selection or snapshot version changes.
  useEffect(() => {
    if (!executionId || !selectedStepId) {
      setStepDetail(null);
      return;
    }
    const seq = ++stepDetailSeq.current;
    const controller = new AbortController();
    setStepDetailLoading(true);
    getExecutionStep(executionId, selectedStepId, controller.signal)
      .then((d) => {
        if (seq !== stepDetailSeq.current || controller.signal.aborted) return;
        setStepDetail(d);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || seq !== stepDetailSeq.current) return;
        setStepDetail(null);
      })
      .finally(() => {
        if (!controller.signal.aborted && seq === stepDetailSeq.current) {
          setStepDetailLoading(false);
        }
      });
    return () => controller.abort();
  }, [executionId, selectedStepId, snapshotVersion]);

  useEffect(() => {
    return () => {
      if (refreshTimer.current != null) {
        window.clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
    };
  }, []);

  const handleCancel = async () => {
    if (!executionId || cancelling) return;
    setCancelling(true);
    setCancelError(null);
    try {
      const result = await cancelExecution(executionId);
      setExecution((prev) =>
        prev
          ? {
              ...prev,
              status: result.status,
              cancel_requested_at: result.cancel_requested_at,
              finished_at: result.finished_at,
            }
          : prev,
      );
      await loadSnapshot(undefined, { quiet: true });
    } catch (err: unknown) {
      setCancelError(isApiError(err) ? err.message : '취소 요청에 실패했습니다.');
    } finally {
      setCancelling(false);
    }
  };

  if (!executionId) {
    return (
      <div className="p-6">
        <EmptyState title="Execution ID가 없습니다." />
      </div>
    );
  }

  if (loading && !execution) {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={8} />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div className="p-6">
        <PermissionDenied />
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="p-6">
        <EmptyState
          title="Execution을 찾을 수 없습니다."
          description="권한이 없거나 삭제된 실행일 수 있습니다."
          action={{ label: '목록으로', onClick: () => navigate('/executions') }}
        />
      </div>
    );
  }

  if (error && !execution) {
    return (
      <div className="p-6">
        <ErrorState
          message={error.message}
          requestId={error.requestId}
          onRetry={() => void loadSnapshot()}
        />
      </div>
    );
  }

  if (!execution) return null;

  const status = execution.status;
  const canCancel =
    status === 'RUNNING' ||
    status === 'WAITING_APPROVAL' ||
    status === 'WAITING_INPUT' ||
    status === 'QUEUED' ||
    status === 'CREATED';
  const hasUnknown = steps.some((s) => s.status === 'UNKNOWN_OUTCOME');

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button
          onClick={() => navigate('/executions')}
          className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3"
        >
          <ArrowLeft size={14} /> Executions
        </button>
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h1 className="text-lg font-semibold text-slate-900 font-mono">
                {execution.id}
              </h1>
              <StatusBadge status={status} />
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
              <span>
                Source:{' '}
                <span className="text-slate-700">
                  {labelExecutionSource(execution.source_type)}
                  {execution.source?.name ? ` · ${execution.source.name}` : ''}
                </span>
              </span>
              <span>
                Requester:{' '}
                <span className="text-slate-700 font-mono">{execution.requester_id}</span>
              </span>
              <span>
                Requested:{' '}
                <span className="text-slate-700">
                  {formatTimestamp(execution.requested_at)}
                </span>
              </span>
              <span>
                Started:{' '}
                <span className="text-slate-700">
                  {formatTimestamp(execution.started_at)}
                </span>
              </span>
              <span>
                Duration:{' '}
                <span className="text-slate-700 font-mono">
                  {formatDurationMs(execution.duration_ms)}
                </span>
              </span>
            </div>
          </div>
          <div className="flex gap-2 shrink-0">
            {(canCancel || status === 'CANCEL_REQUESTED') && (
              <PermissionGate permission="execution.cancel">
                <Button
                  variant="danger"
                  size="sm"
                  icon={<X size={13} />}
                  loading={cancelling || status === 'CANCEL_REQUESTED'}
                  onClick={() => void handleCancel()}
                  disabled={status === 'CANCEL_REQUESTED' || cancelling}
                >
                  {status === 'CANCEL_REQUESTED' ? '취소 요청됨' : '실행 취소'}
                </Button>
              </PermissionGate>
            )}
          </div>
        </div>
        {cancelError && (
          <div className="mt-3">
            <InlineAlert type="error" message={cancelError} />
          </div>
        )}
        {status === 'CANCEL_REQUESTED' && (
          <div className="mt-3">
            <InlineAlert
              type="warning"
              message="Cancel requested — 진행 중 Step 정리 후 CANCELLED로 전환됩니다."
            />
          </div>
        )}
        {hasUnknown && (
          <div className="mt-3">
            <InlineAlert
              type="warning"
              message="UNKNOWN_OUTCOME Step이 있습니다. 자동 Retry CTA는 제공하지 않습니다. 외부 시스템 결과를 운영 확인하세요."
            />
          </div>
        )}
        {execution.error_code && (
          <div className="mt-3">
            <InlineAlert
              type="error"
              message={`error_code: ${execution.error_code}${
                execution.error_category ? ` (${execution.error_category})` : ''
              }`}
            />
          </div>
        )}
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'steps', label: 'Steps' },
            { id: 'events', label: 'Events' },
            { id: 'io', label: 'Inputs / Outputs' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && (
          <OverviewTab
            execution={execution}
            steps={steps}
            onLiveMrtrResolved={() => void loadSnapshot(undefined, { quiet: true })}
          />
        )}
        {tab === 'steps' && (
          <StepsTab
            steps={steps}
            selectedStepId={selectedStepId}
            onSelectStep={(id) =>
              setSelectedStepId((prev) => (prev === id ? null : id))
            }
            stepDetail={stepDetail}
            stepDetailLoading={stepDetailLoading}
          />
        )}
        {tab === 'events' && (
          <EventsTab timeline={timeline} connectionState={connectionState} />
        )}
        {tab === 'io' && <IOTab resultSummary={execution.result_summary} />}
        {tab === 'audit' && <AuditTab executionId={execution.id} />}
      </div>
    </div>
  );
}

function OverviewTab({
  execution,
  steps,
  onLiveMrtrResolved,
}: {
  execution: ExecutionDetailDto;
  steps: ExecutionStepListItemDto[];
  onLiveMrtrResolved: () => void;
}) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Source</h3>
        <div className="space-y-1.5 text-sm text-slate-700">
          <p>
            Type:{' '}
            <span className="font-medium">
              {labelExecutionSource(execution.source_type)}
            </span>
          </p>
          <p>Name: {execution.source?.name ?? '—'}</p>
          <p className="font-mono text-xs text-slate-500">
            version: {execution.source?.version_id ?? '—'}
          </p>
          <p className="font-mono text-xs text-slate-500">
            logical: {execution.source?.logical_id ?? '—'}
          </p>
        </div>
      </div>
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Plan limits</h3>
        {execution.plan_limits ? (
          <div className="space-y-1 text-sm text-slate-700">
            <p>max_steps: {execution.plan_limits.max_steps ?? '—'}</p>
            <p>
              max_duration_seconds:{' '}
              {execution.plan_limits.max_duration_seconds ?? '—'}
            </p>
            <p>max_parallelism: {execution.plan_limits.max_parallelism ?? '—'}</p>
            <p>
              max_loop_iterations:{' '}
              {execution.plan_limits.max_loop_iterations ?? '—'}
            </p>
            <p className="text-xs text-slate-400 font-mono mt-2">
              schema {execution.plan_schema_version} · hash{' '}
              {execution.plan_hash.slice(0, 12)}…
            </p>
          </div>
        ) : (
          <p className="text-sm text-slate-400">limits summary 없음</p>
        )}
        <p className="text-xs text-slate-400 mt-3">
          Plan snapshot은 Operations API에서 노출되지 않습니다.
        </p>
      </div>

      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Steps summary</h3>
        <p className="text-sm text-slate-700">
          {execution.completed_step_count} completed / {execution.step_count} total
          {execution.failed_step_count > 0
            ? ` · ${execution.failed_step_count} failed`
            : ''}
        </p>
        <ul className="mt-2 space-y-1">
          {steps.map((s) => (
            <li key={s.id} className="flex items-center justify-between text-sm">
              <span className="text-slate-700 truncate font-mono text-xs">{s.step_key}</span>
              <StatusBadge status={s.status} size="sm" />
            </li>
          ))}
        </ul>
      </div>

      {execution.status === 'WAITING_APPROVAL' && (
        <div className="col-span-full bg-amber-50 border border-amber-200 rounded-xl p-4 flex items-start gap-3">
          <AlertTriangle size={16} className="text-amber-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800">
              Execution WAITING_APPROVAL
            </p>
            <p className="text-sm text-amber-700 mt-0.5">
              승인 대기 중입니다. Approval Entity status는 PENDING과 별개입니다.
            </p>
          </div>
        </div>
      )}

      {execution.status === 'WAITING_INPUT' && (
        <div className="col-span-full">
          <MrtrInputPanel
            executionId={execution.id}
            onResolved={() => onLiveMrtrResolved()}
          />
        </div>
      )}
    </div>
  );
}

function StepsTab({
  steps,
  selectedStepId,
  onSelectStep,
  stepDetail,
  stepDetailLoading,
}: {
  steps: ExecutionStepListItemDto[];
  selectedStepId: string | null;
  onSelectStep: (id: string) => void;
  stepDetail: ExecutionStepDetailDto | null;
  stepDetailLoading: boolean;
}) {
  if (steps.length === 0) {
    return <EmptyState title="Step이 없습니다." />;
  }
  return (
    <div className="flex gap-4 max-w-5xl">
      <div className="flex-1 bg-white rounded-xl border border-slate-200 p-6">
        <h3 className="text-sm font-semibold text-slate-700 mb-6">Steps</h3>
        <div className="flex flex-col items-center gap-0">
          {steps.map((step, i) => (
            <div key={step.id} className="flex flex-col items-center">
              <button
                onClick={() => onSelectStep(step.id)}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl border-2 transition-all w-80
                  ${
                    selectedStepId === step.id
                      ? 'border-indigo-500 bg-indigo-50'
                      : 'border-slate-200 bg-white hover:border-slate-300'
                  }`}
              >
                <StepIcon status={step.status} />
                <div className="text-left min-w-0 flex-1">
                  <p className="text-sm font-medium text-slate-800 truncate font-mono">
                    {step.step_key}
                  </p>
                  <p className="text-xs text-slate-400">
                    {step.step_type}
                    {step.iteration_no != null ? ` · iter ${step.iteration_no}` : ''}
                  </p>
                </div>
                <StatusBadge status={step.status} size="sm" />
              </button>
              {i < steps.length - 1 && <div className="w-px h-6 bg-slate-200 my-1" />}
            </div>
          ))}
        </div>
      </div>

      {selectedStepId && (
        <div className="w-96 bg-white rounded-xl border border-slate-200 p-4">
          <div className="flex items-center justify-between mb-3">
            <p className="text-sm font-semibold text-slate-800 font-mono">
              {stepDetail?.step_key ?? 'Step'}
            </p>
            <button
              onClick={() => onSelectStep(selectedStepId)}
              className="text-slate-400 hover:text-slate-600"
            >
              <X size={14} />
            </button>
          </div>
          {stepDetailLoading && !stepDetail ? (
            <LoadingSkeleton rows={4} />
          ) : stepDetail ? (
            <div className="space-y-2 text-xs">
              <Row label="Type" value={stepDetail.step_type} />
              <Row label="Status">
                <StatusBadge status={stepDetail.status} size="sm" />
              </Row>
              <Row label="Attempts" value={String(stepDetail.attempt_count)} />
              <Row label="Duration" value={formatDurationMs(stepDetail.duration_ms)} />
              <Row label="Started" value={formatTimestamp(stepDetail.started_at)} />
              <Row label="Finished" value={formatTimestamp(stepDetail.finished_at)} />
              {stepDetail.error_code && (
                <Row label="Error" value={stepDetail.error_code} mono />
              )}
              {stepDetail.status === 'UNKNOWN_OUTCOME' && (
                <div className="mt-3 p-2.5 bg-orange-50 border border-orange-200 rounded-lg">
                  <p className="text-xs font-semibold text-orange-700 mb-1">
                    Unknown Outcome
                  </p>
                  <p className="text-xs text-orange-600">
                    자동 Retry CTA 없음. 외부 시스템 결과를 운영 확인하세요.
                  </p>
                </div>
              )}
              <div className="mt-4 space-y-3">
                <p className="text-xs font-semibold text-slate-600">Attempts</p>
                {stepDetail.attempts.length === 0 ? (
                  <p className="text-xs text-slate-400">Attempt 없음</p>
                ) : (
                  stepDetail.attempts.map((a) => <AttemptCard key={a.id} attempt={a} />)
                )}
              </div>
            </div>
          ) : (
            <p className="text-xs text-slate-400">상세를 불러오지 못했습니다.</p>
          )}
        </div>
      )}
    </div>
  );
}

function AttemptCard({ attempt }: { attempt: StepAttemptSafeDto }) {
  return (
    <div className="border border-slate-200 rounded-lg p-2.5 space-y-1">
      <div className="flex justify-between">
        <span className="font-medium text-slate-700">#{attempt.attempt_no}</span>
        <StatusBadge status={attempt.status} size="sm" />
      </div>
      <p className="text-slate-500">
        {formatDurationMs(attempt.duration_ms)} · {formatTimestamp(attempt.started_at)}
      </p>
      {attempt.error_code && (
        <p className="font-mono text-slate-600">{attempt.error_code}</p>
      )}
      {attempt.tool_calls.length > 0 && (
        <div className="mt-1 space-y-1">
          {attempt.tool_calls.map((tc) => (
            <div
              key={tc.id}
              className="bg-slate-50 rounded px-2 py-1 text-[11px] text-slate-600"
            >
              <div className="flex justify-between gap-2">
                <span>{tc.normalized_status}</span>
                <span className="font-mono">{formatDurationMs(tc.duration_ms)}</span>
              </div>
              <div className="text-slate-400">
                ttfb {formatDurationMs(tc.time_to_first_byte_ms)} · req{' '}
                {tc.request_bytes ?? '—'}B / res {tc.response_bytes ?? '—'}B
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EventsTab({
  timeline,
  connectionState,
}: {
  timeline: ExecutionTimelineItem[];
  connectionState: SseConnectionState;
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // Durable id ASC (append-only increasing accepts).
  const rows = timeline;

  return (
    <div className="max-w-3xl space-y-3">
      <div className="flex items-center justify-between px-1">
        <p className="text-sm text-slate-600">
          Connection:{' '}
          <span className="font-medium text-slate-800" data-testid="sse-connection-state">
            {connectionLabel(connectionState)}
          </span>
        </p>
        <p className="text-xs text-slate-400">
          {rows.length} event{rows.length === 1 ? '' : 's'} (max {MAX_TIMELINE_EVENTS})
        </p>
      </div>
      {rows.length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 p-6">
          <EmptyState
            title="아직 수신된 Execution Event가 없습니다."
            description="SSE로 durable events가 도착하면 여기에 표시됩니다."
          />
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {rows.map((row) => {
            const statusValue = row.payload.status;
            const statusText =
              typeof statusValue === 'string' ? statusValue : null;
            const expanded = expandedId === row.sseId;
            return (
              <div
                key={row.sseId}
                className="border-b border-slate-100 last:border-0 px-4 py-3"
                data-testid="execution-event-row"
              >
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                  <span className="font-mono text-xs text-slate-400">
                    {formatTimestamp(row.occurred_at)}
                  </span>
                  <span className="font-mono text-xs text-indigo-600">
                    {row.event_type}
                  </span>
                  <span className="font-mono text-[11px] text-slate-400">
                    id {row.sseId}
                  </span>
                  {row.step_execution_id && (
                    <span className="font-mono text-[11px] text-slate-500">
                      step {row.step_execution_id}
                    </span>
                  )}
                  {statusText && (
                    <StatusBadge status={statusText} size="sm" />
                  )}
                  <button
                    type="button"
                    className="ml-auto text-xs text-slate-500 hover:text-slate-700"
                    onClick={() =>
                      setExpandedId((prev) => (prev === row.sseId ? null : row.sseId))
                    }
                  >
                    {expanded ? 'Hide payload' : 'Show payload'}
                  </button>
                </div>
                {expanded && (
                  <pre className="mt-2 text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700 overflow-x-auto max-h-64">
                    {JSON.stringify(row.payload, null, 2)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function IOTab({
  resultSummary,
}: {
  resultSummary: ExecutionDetailDto['result_summary'];
}) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
      <JsonBlock
        title="Execution 입력"
        note="input_snapshot / plan_snapshot은 Operations API에서 의도적으로 미노출입니다."
        value={null}
      />
      <JsonBlock
        title="result_summary (safe)"
        note="Tool content / structured_content / metadata는 포함되지 않습니다."
        value={resultSummary}
      />
    </div>
  );
}

function AuditTab({ executionId }: { executionId: string }) {
  const [items, setItems] = useState<AuditEventListItemDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditEventDetailDto | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setForbidden(false);
    setError(null);
    listAuditEvents({ execution_id: executionId, limit: 50, signal: controller.signal })
      .then((data) => setItems(data.items))
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          return;
        }
        setError(isApiError(err) ? err.message : 'Audit를 불러오지 못했습니다.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [executionId]);

  useEffect(() => {
    if (!expanded) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    getAuditEvent(expanded, controller.signal)
      .then(setDetail)
      .catch(() => setDetail(null));
    return () => controller.abort();
  }, [expanded]);

  if (forbidden) return <PermissionDenied />;
  if (loading) return <LoadingSkeleton rows={4} />;
  if (error) return <ErrorState message={error} />;
  if (items.length === 0) {
    return <EmptyState title="이 Execution에 연결된 Audit event가 없습니다." />;
  }

  return (
    <div className="max-w-3xl bg-white rounded-xl border border-slate-200 overflow-hidden">
      {items.map((log) => (
        <div key={log.event_id} className="border-b border-slate-100 last:border-0">
          <button
            className="w-full flex gap-4 px-4 py-3 text-sm text-left hover:bg-slate-50"
            onClick={() =>
              setExpanded((prev) => (prev === log.event_id ? null : log.event_id))
            }
          >
            <span className="font-mono text-xs text-slate-400 shrink-0">
              {formatTimestamp(log.occurred_at)}
            </span>
            <span className="text-slate-600">{log.actor_type}</span>
            <span className="font-mono text-xs text-indigo-600">{log.action}</span>
            <span className="ml-auto text-xs font-medium text-slate-600">
              {log.result}
            </span>
          </button>
          {expanded === log.event_id && detail && (
            <div className="px-4 pb-3 grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
              <JsonBlock title="Before" value={detail.before_data} />
              <JsonBlock title="After" value={detail.after_data} />
              <JsonBlock title="Change set" value={detail.change_set} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function StepIcon({ status }: { status: string }) {
  if (status === 'SUCCEEDED') {
    return <CheckCircle2 size={18} className="text-green-500 shrink-0" />;
  }
  if (status === 'RUNNING') {
    return <Loader2 size={18} className="animate-spin text-indigo-500 shrink-0" />;
  }
  if (status === 'WAITING_APPROVAL' || status === 'WAITING_INPUT') {
    return <Clock size={18} className="text-amber-500 shrink-0" />;
  }
  if (status === 'FAILED' || status === 'UNKNOWN_OUTCOME') {
    return <AlertTriangle size={18} className="text-red-500 shrink-0" />;
  }
  if (status === 'CANCELLED') {
    return <X size={18} className="text-slate-400 shrink-0" />;
  }
  return <Circle size={18} className="text-slate-300 shrink-0" />;
}

function Row({
  label,
  value,
  mono,
  children,
}: {
  label: string;
  value?: string;
  mono?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-400 shrink-0">{label}</span>
      {children ?? (
        <span className={`text-slate-700 text-right ${mono ? 'font-mono' : ''}`}>
          {value}
        </span>
      )}
    </div>
  );
}
