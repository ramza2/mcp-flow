import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, CheckCircle2, Info, XCircle } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Dialog, { ConfirmDialog } from '../../components/ui/Dialog';
import {
  EmptyState,
  ErrorState,
  InlineAlert,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import {
  createApprovalDecision,
  getApproval,
  type ApprovalDecisionResultDto,
  type ApprovalDetailDto,
} from '../../api/approvals';
import { isAbortError, isApiError } from '../../api/client';
import {
  formatTimestamp,
  labelApprovalDecisionMode,
  labelApprovalDecisionValue,
  labelApprovalStatus,
  shortenId,
} from '../../domain';

function formatDecisionError(err: unknown): { message: string; requestId?: string } {
  if (!isApiError(err)) {
    return { message: '결정을 제출하지 못했습니다. 네트워크 상태를 확인한 뒤 다시 시도하세요.' };
  }
  if (err.status === 0 || err.code === 'NETWORK_ERROR') {
    return {
      message: '네트워크 오류로 결정을 확인하지 못했습니다. 성공으로 표시하지 않습니다. 목록을 새로고침해 상태를 확인하세요.',
      requestId: err.requestId ?? undefined,
    };
  }
  if (err.status === 403) {
    return {
      message: err.message || '결정 권한이 없거나 정책/범위에 맞지 않습니다.',
      requestId: err.requestId ?? undefined,
    };
  }
  if (err.status === 404) {
    return {
      message: err.message || '승인 요청을 찾을 수 없거나 더 이상 볼 수 없습니다.',
      requestId: err.requestId ?? undefined,
    };
  }
  if (err.status === 409) {
    return {
      message:
        err.message ||
        '이미 결정했거나 상태가 변경되어 처리할 수 없습니다. 상세를 다시 불러오세요.',
      requestId: err.requestId ?? undefined,
    };
  }
  if (err.status === 422) {
    return {
      message: err.message || '입력값이 올바르지 않습니다.',
      requestId: err.requestId ?? undefined,
    };
  }
  return {
    message: err.message || '결정을 제출하지 못했습니다.',
    requestId: err.requestId ?? undefined,
  };
}

export default function ApprovalDetail() {
  const { approvalId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<ApprovalDetailDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [reload, setReload] = useState(0);

  const [approveOpen, setApproveOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectComment, setRejectComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  const [lastResult, setLastResult] = useState<ApprovalDecisionResultDto | null>(null);
  const [refreshWarning, setRefreshWarning] = useState<string | null>(null);
  const submitLock = useRef(false);

  const load = useCallback(
    (opts?: { quiet?: boolean }) => {
      if (!approvalId) {
        setNotFound(true);
        setLoading(false);
        return () => undefined;
      }
      const controller = new AbortController();
      let cancelled = false;
      const quiet = Boolean(opts?.quiet);
      if (!quiet) {
        setLoading(true);
        setError(null);
        setForbidden(false);
        setNotFound(false);
        setDetail(null);
      }

      getApproval(approvalId, controller.signal)
        .then((data) => {
          if (cancelled) return;
          setDetail(data);
          setForbidden(false);
          setNotFound(false);
          setError(null);
        })
        .catch((err: unknown) => {
          if (isAbortError(err) || cancelled) return;
          if (isApiError(err) && err.status === 403) {
            if (!quiet) {
              setForbidden(true);
              setDetail(null);
            }
            return;
          }
          if (isApiError(err) && err.status === 404) {
            if (!quiet) {
              setNotFound(true);
              setDetail(null);
            } else {
              setNotFound(true);
              setDetail(null);
            }
            return;
          }
          if (!quiet) {
            const apiErr = isApiError(err) ? err : null;
            setError({
              message: apiErr?.message ?? '승인 상세를 불러오지 못했습니다.',
              requestId: apiErr?.requestId ?? undefined,
            });
            setDetail(null);
          } else {
            setRefreshWarning(
              '결정은 저장됐을 수 있으나 상세를 다시 불러오지 못했습니다. 새로고침으로 확인해 주세요.',
            );
          }
        })
        .finally(() => {
          if (!cancelled && !quiet) setLoading(false);
        });

      return () => {
        cancelled = true;
        controller.abort();
      };
    },
    [approvalId],
  );

  useEffect(() => {
    setLastResult(null);
    setActionError(null);
    setRefreshWarning(null);
    return load({ quiet: false });
  }, [load, reload]);

  // After a confirmed POST success (decision_id), hide decide controls even if
  // the quiet GET refresh is still in flight or failed — never re-offer decide
  // from stale PENDING/can_decide.
  const canAct =
    detail?.status === 'PENDING' &&
    detail.can_decide === true &&
    !submitting &&
    lastResult == null;

  const submitDecision = async (decision: 'APPROVE' | 'REJECT') => {
    if (!approvalId || !detail || submitLock.current) return;
    if (detail.status !== 'PENDING' || !detail.can_decide) return;

    submitLock.current = true;
    setSubmitting(true);
    setActionError(null);
    setRefreshWarning(null);
    setLastResult(null);

    try {
      const comment =
        decision === 'REJECT' ? rejectComment.trim() || null : null;
      const result = await createApprovalDecision(approvalId, {
        decision,
        comment,
      });
      if (!result?.decision_id) {
        setActionError({
          message:
            '서버 응답이 불완전하여 성공으로 표시하지 않습니다. 목록을 새로고침해 상태를 확인하세요.',
        });
        return;
      }
      setLastResult(result);
      setApproveOpen(false);
      setRejectOpen(false);
      setRejectComment('');
      load({ quiet: true });
    } catch (err: unknown) {
      setActionError(formatDecisionError(err));
      // Do not claim success on any failure / network uncertainty.
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button
          type="button"
          onClick={() => navigate('/approvals')}
          className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3"
        >
          <ArrowLeft size={14} /> 승인 목록
        </button>
        {!loading && detail && (
          <div className="flex items-start justify-between gap-4">
            <div>
              <h1 className="text-lg font-semibold text-slate-900 font-mono" title={detail.id}>
                {shortenId(detail.id, 18)}
              </h1>
              <div className="flex flex-wrap items-center gap-3 mt-1">
                {/* Approval Entity Status — never map PENDING → WAITING_APPROVAL */}
                <span title={detail.status}>
                  <StatusBadge
                    status={detail.status}
                    label={labelApprovalStatus(detail.status)}
                  />
                </span>
                <span className="text-xs text-slate-400">
                  Execution:{' '}
                  <button
                    type="button"
                    className="font-mono text-indigo-600 hover:underline"
                    onClick={() => navigate(`/executions/${detail.execution_id}`)}
                  >
                    {detail.execution_id}
                  </button>
                </span>
              </div>
            </div>
            <div className="flex flex-col items-end gap-2 shrink-0">
              <CanDecideBadge canDecide={detail.can_decide} status={detail.status} />
              {canAct && (
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={submitting}
                    onClick={() => {
                      setActionError(null);
                      setRejectOpen(true);
                    }}
                  >
                    거절
                  </Button>
                  <Button
                    size="sm"
                    disabled={submitting}
                    icon={<CheckCircle2 size={13} />}
                    onClick={() => {
                      setActionError(null);
                      setApproveOpen(true);
                    }}
                  >
                    승인
                  </Button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="p-6 max-w-4xl space-y-4">
        {forbidden ? (
          <div>
            <PermissionDenied />
            <p className="text-center text-sm text-slate-400 -mt-8 mb-4">
              승인 상세를 보려면 관리자에게 승인 결정 권한을 요청하세요.
            </p>
          </div>
        ) : loading ? (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            <LoadingSkeleton rows={8} />
          </div>
        ) : notFound ? (
          <EmptyState
            title="승인 요청을 찾을 수 없습니다"
            description="요청이 없거나, 현재 계정으로는 볼 수 없습니다. (서버는 권한 부족을 404로 반환할 수 있습니다.)"
            action={{ label: '목록으로', onClick: () => navigate('/approvals') }}
          />
        ) : error ? (
          <ErrorState
            message={error.message}
            requestId={error.requestId}
            onRetry={() => setReload((n) => n + 1)}
          />
        ) : detail ? (
          <>
            {actionError && (
              <InlineAlert
                type="error"
                message={`${actionError.message}${
                  actionError.requestId ? ` (Request ID: ${actionError.requestId})` : ''
                }`}
              />
            )}
            {refreshWarning && <InlineAlert type="warning" message={refreshWarning} />}
            {lastResult && (
              <DecisionResultBanner
                result={lastResult}
                onOpenExecution={() => navigate(`/executions/${lastResult.execution_id}`)}
              />
            )}

            {detail.status === 'PENDING' && !detail.can_decide && (
              <InlineAlert
                type="info"
                message="이 요청은 조회만 가능합니다. 이미 결정했거나 정책·만료·범위 때문에 지금은 결정할 수 없습니다."
              />
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <InfoCard title="상태 · 결정 가능 여부">
                <div className="space-y-2 text-sm">
                  <Row label="상태">
                    <span title={detail.status}>
                      <StatusBadge
                        status={detail.status}
                        label={labelApprovalStatus(detail.status)}
                        size="sm"
                      />
                    </span>
                  </Row>
                  <Row label="결정 가능">
                    <span
                      className={
                        detail.can_decide ? 'text-emerald-700 font-medium' : 'text-slate-500'
                      }
                    >
                      {detail.can_decide ? '예 (can_decide=true)' : '아니오 (can_decide=false)'}
                    </span>
                  </Row>
                  <Row label="결정 방식">
                    <span title={detail.decision_mode}>
                      {labelApprovalDecisionMode(detail.decision_mode)}
                    </span>
                  </Row>
                  <Row label="필요 승인 수">
                    <span>{detail.required_approvals}</span>
                  </Row>
                  <Row label="집계">
                    <span>
                      승인 {detail.approve_count} / 거절 {detail.reject_count}
                    </span>
                  </Row>
                </div>
              </InfoCard>

              <InfoCard title="요청 메타">
                <div className="space-y-2 text-sm">
                  <Row label="요청자">
                    <span className="font-mono text-xs" title={detail.requested_by}>
                      {detail.requested_by}
                    </span>
                  </Row>
                  <Row label="요청 시각">
                    <span>{formatTimestamp(detail.requested_at)}</span>
                  </Row>
                  <Row label="만료 시각">
                    <span className="text-amber-700 font-medium">
                      {formatTimestamp(detail.expires_at)}
                    </span>
                  </Row>
                  <Row label="해결 시각">
                    <span>{formatTimestamp(detail.resolved_at)}</span>
                  </Row>
                  <Row label="Step">
                    <span className="font-mono text-xs" title={detail.step_execution_id}>
                      {shortenId(detail.step_execution_id, 12)}
                    </span>
                  </Row>
                </div>
                <p className="text-[10px] text-slate-400 mt-3 flex items-start gap-1.5">
                  <Info size={12} className="shrink-0 mt-0.5" />
                  Approval 거절/만료는 Execution 상태를 REJECTED/EXPIRED로 만들지 않습니다.
                </p>
              </InfoCard>

              <InfoCard title="안전 컨텍스트 (safe_context)">
                <p className="text-xs text-slate-500 mb-2">
                  마스킹된 이력 투영만 표시합니다. 원본 context_snapshot / context_hash / secret_id는
                  노출되지 않습니다.
                </p>
                <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700 overflow-auto max-h-80">
                  {JSON.stringify(detail.safe_context ?? {}, null, 2)}
                </pre>
              </InfoCard>

              <InfoCard title="결정 이력">
                {detail.decisions.length === 0 ? (
                  <p className="text-sm text-slate-400">아직 기록된 결정이 없습니다.</p>
                ) : (
                  <ul className="space-y-3">
                    {detail.decisions.map((decision) => (
                      <li
                        key={decision.decision_id}
                        className="text-sm border border-slate-100 rounded-lg p-3 bg-slate-50/60"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className="font-medium text-slate-800"
                            title={decision.decision}
                          >
                            {labelApprovalDecisionValue(decision.decision)}
                          </span>
                          <span className="text-xs text-slate-500">
                            {formatTimestamp(decision.decided_at)}
                          </span>
                        </div>
                        <p
                          className="font-mono text-[11px] text-slate-500 mt-1"
                          title={decision.decided_by}
                        >
                          결정자: {shortenId(decision.decided_by, 12)}
                        </p>
                        {decision.comment && (
                          <p className="text-xs text-slate-600 mt-2 whitespace-pre-wrap">
                            {decision.comment}
                          </p>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </InfoCard>
            </div>

            {canAct && (
              <div className="bg-white border border-slate-200 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-slate-800 mb-2">승인 / 거절</h3>
                <p className="text-xs text-slate-500 mb-3">
                  최종 권한·정책·만료·중복 결정은 서버가 판정합니다. 자동 승인하지 않습니다.
                </p>
                <div className="flex gap-3">
                  <Button
                    variant="outline"
                    disabled={submitting}
                    onClick={() => {
                      setActionError(null);
                      setRejectOpen(true);
                    }}
                  >
                    거절
                  </Button>
                  <Button
                    disabled={submitting}
                    loading={submitting && approveOpen}
                    icon={<CheckCircle2 size={13} />}
                    onClick={() => {
                      setActionError(null);
                      setApproveOpen(true);
                    }}
                  >
                    승인하기
                  </Button>
                </div>
              </div>
            )}

            <div className="flex gap-2">
              <Button variant="outline" onClick={() => navigate('/approvals')}>
                목록으로
              </Button>
              <Button
                variant="outline"
                disabled={submitting}
                onClick={() => setReload((n) => n + 1)}
              >
                다시 불러오기
              </Button>
            </div>
          </>
        ) : null}
      </div>

      <ConfirmDialog
        open={approveOpen}
        onClose={() => {
          if (!submitting) setApproveOpen(false);
        }}
        onConfirm={() => void submitDecision('APPROVE')}
        title="승인 확인"
        description="이 요청을 승인할까요? 서버가 권한·정책·만료를 다시 검증합니다. 승인 후에도 Execution은 즉시 RUNNING이 되지 않을 수 있습니다."
        confirmLabel="승인 확정"
        loading={submitting}
      />

      <Dialog
        open={rejectOpen}
        onClose={() => {
          if (!submitting) setRejectOpen(false);
        }}
        title="승인 거절"
        description="거절 사유를 입력하세요. 정책에 따라 필수일 수 있으며, 서버가 최종 검증합니다."
        size="sm"
        footer={
          <>
            <Button
              variant="outline"
              disabled={submitting}
              onClick={() => setRejectOpen(false)}
            >
              취소
            </Button>
            <Button
              variant="danger"
              loading={submitting}
              disabled={submitting}
              onClick={() => void submitDecision('REJECT')}
            >
              거절 확정
            </Button>
          </>
        }
      >
        <textarea
          value={rejectComment}
          onChange={(e) => setRejectComment(e.target.value)}
          placeholder="거절 사유 (정책에 따라 필수일 수 있음)"
          rows={3}
          disabled={submitting}
          className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-red-400 disabled:bg-slate-50"
        />
      </Dialog>
    </div>
  );
}

function DecisionResultBanner({
  result,
  onOpenExecution,
}: {
  result: ApprovalDecisionResultDto;
  onOpenExecution: () => void;
}) {
  const approved = result.decision === 'APPROVE';
  return (
    <div
      className={`rounded-xl border p-4 ${
        approved ? 'bg-emerald-50 border-emerald-200' : 'bg-red-50 border-red-200'
      }`}
      role="status"
    >
      <div
        className={`flex items-center gap-2 text-sm font-semibold ${
          approved ? 'text-emerald-800' : 'text-red-800'
        }`}
      >
        {approved ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
        {approved ? '승인 결정이 저장되었습니다' : '거절 결정이 저장되었습니다'}
      </div>
      <dl className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
        <div>
          <dt className="text-slate-500">Approval 상태</dt>
          <dd className="font-medium text-slate-800" title={result.approval_status}>
            {labelApprovalStatus(result.approval_status)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">결정</dt>
          <dd className="font-medium text-slate-800" title={result.decision}>
            {labelApprovalDecisionValue(result.decision)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Execution 상태</dt>
          <dd className="font-mono text-slate-800">{result.execution_status}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Step 상태</dt>
          <dd className="font-mono text-slate-800">{result.step_status}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-slate-500">재개 Outbox</dt>
          <dd className="text-slate-800">
            {result.resume_enqueued
              ? 'EXECUTION_APPROVAL_RESUME 등록됨 (API가 RUNNING으로 올리지 않음)'
              : '등록되지 않음'}
          </dd>
        </div>
      </dl>
      <p className="text-xs text-slate-600 mt-3">
        거절/만료 시 Execution은 REJECTED/EXPIRED가 아니라 계획 정책에 따라 FAILED 등으로
        종료될 수 있습니다.
      </p>
      <div className="mt-3">
        <Button size="sm" variant="outline" onClick={onOpenExecution}>
          Execution 보기
        </Button>
      </div>
    </div>
  );
}

function CanDecideBadge({ canDecide, status }: { canDecide: boolean; status: string }) {
  if (status !== 'PENDING') {
    return (
      <div className="text-sm text-slate-500 font-medium shrink-0">
        결정 종료 ({labelApprovalStatus(status)})
      </div>
    );
  }
  if (canDecide) {
    return (
      <div className="text-sm text-emerald-700 font-medium shrink-0">
        현재 계정으로 결정 가능
      </div>
    );
  }
  return (
    <div className="text-sm text-slate-500 font-medium shrink-0">
      현재 계정으로는 결정할 수 없음
    </div>
  );
}

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <span className="text-slate-400 w-28 shrink-0 text-xs">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
