import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Info } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import {
  EmptyState,
  ErrorState,
  InlineAlert,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import { getApproval, type ApprovalDetailDto } from '../../api/approvals';
import { isAbortError, isApiError } from '../../api/client';
import {
  formatTimestamp,
  labelApprovalDecisionMode,
  labelApprovalDecisionValue,
  labelApprovalStatus,
  shortenId,
} from '../../domain';

export default function ApprovalDetail() {
  const { approvalId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<ApprovalDetailDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [reload, setReload] = useState(0);

  const load = useCallback(() => {
    if (!approvalId) {
      setNotFound(true);
      setLoading(false);
      return () => undefined;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setForbidden(false);
    setNotFound(false);
    setDetail(null);

    getApproval(approvalId, controller.signal)
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          return;
        }
        if (isApiError(err) && err.status === 404) {
          setNotFound(true);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? '승인 상세를 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [approvalId, reload]);

  useEffect(() => load(), [load]);

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
            <CanDecideBadge canDecide={detail.can_decide} status={detail.status} />
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
            <InlineAlert
              type="info"
              message="이 화면은 조회만 지원합니다. 승인·거절 처리는 이후 단계에서 제공됩니다."
            />

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
                        <p className="font-mono text-[11px] text-slate-500 mt-1" title={decision.decided_by}>
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

            <div className="flex gap-2">
              <Button variant="outline" onClick={() => navigate('/approvals')}>
                목록으로
              </Button>
              <Button variant="outline" onClick={() => setReload((n) => n + 1)}>
                다시 불러오기
              </Button>
            </div>
          </>
        ) : null}
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
