import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { createFactoryReview, getFactoryReviewForTestRun, type FactoryReviewDecision, type FactoryReviewDto } from '../../api/factory';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp } from '../../domain';

function newKey(): string {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return 'factory-review-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

export default function FactoryReviewPanel({ testRunId, evidenceHash }: { testRunId: string; evidenceHash: string }) {
  const navigate = useNavigate();
  const [review, setReview] = useState<FactoryReviewDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [readError, setReadError] = useState<{ message: string; requestId?: string } | null>(null);
  const [writeError, setWriteError] = useState<{ message: string; requestId?: string } | null>(null);
  const [decision, setDecision] = useState<FactoryReviewDecision | ''>('');
  const [comment, setComment] = useState('');
  const [confirm, setConfirm] = useState(false);
  const [saving, setSaving] = useState(false);
  const keysRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setReadError(null);
    getFactoryReviewForTestRun(testRunId, controller.signal)
      .then(data => {
        if (!cancelled) setReview(data);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        if (isApiError(err) && err.status === 404) {
          setReview(null);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setReadError({
          message: apiErr?.message ?? '검토 이력을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [testRunId, refresh]);

  const normalizedComment = comment.trim() || null;
  const eligible = Boolean(decision) && comment.length <= 500 && confirm && !saving && !loading && !readError;

  const submit = async () => {
    if (!eligible || !decision || review) return;
    setSaving(true);
    setWriteError(null);
    try {
      const fingerprint = JSON.stringify([testRunId, decision, normalizedComment]);
      let key = keysRef.current.get(fingerprint);
      if (!key) {
        key = newKey();
        keysRef.current.set(fingerprint, key);
      }
      const result = await createFactoryReview(testRunId, decision, normalizedComment, key);
      navigate(`/tool-factory/reviews/${result.id}`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setWriteError({
        message: apiErr?.message ?? '검토 결정 저장에 실패했습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
      <h2 className="font-semibold text-slate-800">Factory 검토·승인/반려</h2>
      <InlineAlert type="warning"
        message="이 검토는 실제 PASS TestRun의 증빙에 대한 1회성 불변 결정입니다. 승인해도 MCP 서버/도구가 게시되거나 활성화되지 않습니다. 반려한 결정도 되돌릴 수 없습니다." />
      <p className="text-xs font-mono break-all text-slate-500">검토 대상 증빙 해시: {evidenceHash}</p>
      {loading ? (
        <LoadingSkeleton rows={2} />
      ) : readError ? (
        <ErrorState message={readError.message} requestId={readError.requestId}
          onRetry={() => setRefresh(v => v + 1)} />
      ) : review ? (
        <div className="space-y-2">
          <p className="text-sm text-slate-800">
            이미 저장된 결정: <strong>{review.decision === 'APPROVE' ? '승인' : '반려'}</strong>
            {' · '}{formatTimestamp(review.reviewed_at)}
          </p>
          {review.comment && <p className="text-sm text-slate-600 break-all">{review.comment}</p>}
          <Button variant="outline" onClick={() => navigate(`/tool-factory/reviews/${review.id}`)}>
            검토 결정 상세
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          <fieldset className="space-y-2" disabled={saving}>
            <legend className="text-sm font-semibold text-slate-700">결정 선택</legend>
            <label className="flex gap-2 items-center text-sm text-slate-700">
              <input type="radio" name="factory-review-decision" value="APPROVE"
                checked={decision === 'APPROVE'}
                onChange={() => { setDecision('APPROVE'); setConfirm(false); setWriteError(null); }} />
              승인 — 게시 가능 상태로 검토 결과 저장 (게시 자체는 별도 단계)
            </label>
            <label className="flex gap-2 items-center text-sm text-slate-700">
              <input type="radio" name="factory-review-decision" value="REJECT"
                checked={decision === 'REJECT'}
                onChange={() => { setDecision('REJECT'); setConfirm(false); setWriteError(null); }} />
              반려 — 해당 테스트 결과는 게시 불가
            </label>
          </fieldset>
          <div className="space-y-1">
            <label htmlFor="factory-review-comment" className="text-sm font-semibold text-slate-700">
              검토 의견 (선택 사항, 최대 500자)
            </label>
            <textarea id="factory-review-comment" rows={3} maxLength={500} disabled={saving}
              value={comment} onChange={e => { setComment(e.target.value); setConfirm(false); setWriteError(null); }}
              className="w-full border border-slate-300 rounded-lg p-3 text-sm text-slate-700"
              placeholder="승인 또는 반려 사유를 기록할 수 있습니다." />
          </div>
          <label className="flex items-start gap-2 text-sm text-slate-700">
            <input type="checkbox" className="mt-1" checked={confirm}
              disabled={!decision || saving}
              onChange={e => setConfirm(e.target.checked)} />
            <span>
              선택한 <strong>{decision === 'APPROVE' ? '승인' : decision === 'REJECT' ? '반려' : '결정'}</strong> 결과가
              실제 운영 DB에 변경 불가능한 기록으로 저장됨을 확인했습니다.
            </span>
          </label>
          {writeError && (
            <div className="space-y-1">
              <InlineAlert type="error" message={writeError.message} />
              {writeError.requestId && <p className="font-mono text-xs text-slate-500">Request ID: {writeError.requestId}</p>}
              <p className="text-xs text-slate-500">응답이 불확실한 경우에도 동일한 결정·의견으로 재시도할 때 같은 멱등성 키를 사용합니다.</p>
            </div>
          )}
          <Button disabled={!eligible} loading={saving} onClick={() => void submit()}>
            {decision === 'APPROVE' ? '승인 결정 저장' : decision === 'REJECT' ? '반려 결정 저장' : '검토 결정 선택 필요'}
          </Button>
        </div>
      )}
    </section>
  );
}
