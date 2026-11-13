import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { getFactoryPublishForReview, getFactoryReview, publishFactoryReview, type FactoryPublishDto, type FactoryReviewDto } from '../../api/factory';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp } from '../../domain';
import FactoryRuntimeBindingPanel from './FactoryRuntimeBindingPanel';

function freshPublishKey(): string {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return 'factory-publish-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

export default function FactoryReviewDetail() {
  const { reviewId } = useParams();
  const navigate = useNavigate();
  const [review, setReview] = useState<FactoryReviewDto | null>(null);
  const [published, setPublished] = useState<FactoryPublishDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [loadError, setLoadError] = useState<{ message: string; requestId?: string } | null>(null);
  const [writeError, setWriteError] = useState<{ message: string; requestId?: string } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const publishKeyRef = useRef<string | null>(null);

  useEffect(() => {
    publishKeyRef.current = null;
    setConfirmed(false);
  }, [reviewId]);

  useEffect(() => {
    if (!reviewId) {
      setReview(null);
      setPublished(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      getFactoryReview(reviewId, controller.signal),
      getFactoryPublishForReview(reviewId, controller.signal).catch(err => {
        if (isApiError(err) && err.status === 404) return null;
        throw err;
      }),
    ])
      .then(([reviewData, publishData]) => {
        if (cancelled) return;
        setReview(reviewData);
        setPublished(publishData);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setReview(null);
        setPublished(null);
        setLoadError({
          message: apiErr?.message ?? 'Factory 검토·게시 기록을 불러올 수 없습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [reviewId, refresh]);

  const handlePublish = async () => {
    if (!reviewId || !review || review.decision !== 'APPROVE' ||
        published || !confirmed || loading || publishing || loadError) return;

    setPublishing(true);
    setWriteError(null);
    try {
      const key = publishKeyRef.current ?? freshPublishKey();
      publishKeyRef.current = key;
      const result = await publishFactoryReview(reviewId, key);
      setPublished(result);
      setConfirmed(false);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setWriteError({
        message: apiErr?.message ?? 'Factory Publish/Import 요청에 실패했습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      setPublishing(false);
    }
  };

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Factory 실제 작업', to: '/tool-factory/jobs' }]}
        title="Factory 검토·게시 상세"
        description="실제 불변 검토 기록과 Publish/Import 결과를 조회합니다."
        actions={review ? (
          <Button variant="outline" onClick={() => navigate(`/tool-factory/test-runs/${review.test_run_id}`)}>
            테스트 결과
          </Button>
        ) : undefined}
      />
      <div className="p-6 max-w-4xl space-y-4">
        <InlineAlert type="warning"
          message="게시(Publish/Import)는 실제 운영 DB에 DRAFT MCP 서버와 INACTIVE 도구를 등록합니다. 외부 MCP 연결 테스트·Discovery·검증·ACTIVE 전환은 수행하지 않습니다." />
        {loading ? (
          <LoadingSkeleton rows={5} />
        ) : loadError ? (
          <ErrorState message={loadError.message} requestId={loadError.requestId}
            onRetry={() => setRefresh(v => v + 1)} />
        ) : review ? (
          <>
            <div className="flex justify-end">
              <Button variant="outline" disabled={publishing} onClick={() => setRefresh(v => v + 1)}>
                게시 상태 새로고침
              </Button>
            </div>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">저장된 검토 결정</h2>
              <dl className="text-sm space-y-2">
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">검토 ID</dt><dd className="font-mono break-all">{review.id}</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">결정</dt><dd className="font-semibold">{review.decision === 'APPROVE' ? '승인' : '반려'}</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">검토 시각</dt><dd>{formatTimestamp(review.reviewed_at)}</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">테스트 증빙 해시</dt><dd className="font-mono text-xs break-all">{review.test_evidence_hash}</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">검토 증빙 해시</dt><dd className="font-mono text-xs break-all">{review.review_evidence_hash}</dd></div>
                {review.comment && (
                  <div className="space-y-1"><dt className="text-slate-500">검토 의견</dt><dd className="break-all">{review.comment}</dd></div>
                )}
              </dl>
            </section>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">Publish/Import — MCP 초안 등록</h2>
              {published ? (
                <>
                  <InlineAlert type="info"
                    message="Publish/Import 기록이 있습니다. DRAFT 서버 및 INACTIVE 도구가 등록되었으며, MCP 도구가 ACTIVE 상태인 것은 아닙니다." />
                  <dl className="text-sm space-y-2">
                    <div className="flex flex-wrap gap-2"><dt className="text-slate-500">Publish ID</dt><dd className="font-mono break-all">{published.id}</dd></div>
                    <div className="flex flex-wrap gap-2"><dt className="text-slate-500">DRAFT MCP 서버 ID</dt><dd className="font-mono break-all">{published.mcp_server_id}</dd></div>
                    <div className="flex flex-wrap gap-2"><dt className="text-slate-500">등록 도구 수</dt><dd>{published.tool_count}개</dd></div>
                    <div className="flex flex-wrap gap-2"><dt className="text-slate-500">게시 증빙 해시</dt><dd className="font-mono text-xs break-all">{published.publish_evidence_hash}</dd></div>
                    <div className="flex flex-wrap gap-2"><dt className="text-slate-500">등록 시각</dt><dd>{formatTimestamp(published.created_at)}</dd></div>
                  </dl>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" onClick={() => navigate(`/mcp/servers/${published.mcp_server_id}`)}>
                      DRAFT MCP 서버 상세
                    </Button>
                    <Button variant="outline" onClick={() => navigate('/mcp/tools')}>
                      MCP 도구 목록
                    </Button>
                  </div>
                  <p className="text-xs text-slate-500">
                    서버 연결·도구 검증·활성화는 별도 운영 절차로 확인해야 합니다.
                  </p>
                </>
              ) : review.decision === 'REJECT' ? (
                <InlineAlert type="warning"
                  message="반려된 검토 기록은 게시할 수 없습니다. 새로운 유효한 테스트와 별도의 검토가 필요합니다." />
              ) : (
                <div className="space-y-3">
                  <InlineAlert type="warning"
                    message="승인 결정만으로 게시하지 않습니다. 이 작업은 실제 DRAFT MCP 서버와 INACTIVE Tool 레코드를 생성합니다. 이미 생성된 등록 정보를 임의로 되돌리지 않습니다." />
                  <label className="flex gap-2 items-start text-sm text-slate-700">
                    <input type="checkbox" className="mt-1" checked={confirmed}
                      disabled={publishing}
                      onChange={e => { setConfirmed(e.target.checked); setWriteError(null); }} />
                    <span>실제 운영 DB에 DRAFT 서버와 INACTIVE 도구를 등록하며 자동 활성화되지 않는다는 점을 확인했습니다.</span>
                  </label>
                  {writeError && (
                    <div className="space-y-1">
                      <InlineAlert type="error" message={writeError.message} />
                      {writeError.requestId && (
                        <p className="font-mono text-xs text-slate-500">Request ID: {writeError.requestId}</p>
                      )}
                      <p className="text-xs text-slate-500">게시 응답이 불확실해도 같은 검토 기록 재시도에는 동일한 멱등성 키를 사용합니다.</p>
                    </div>
                  )}
                  <Button disabled={!confirmed || publishing || loading} loading={publishing}
                    onClick={() => void handlePublish()}>
                    DRAFT 서버·도구 게시 실행
                  </Button>
                </div>
              )}
            </section>
            {published && (
              <FactoryRuntimeBindingPanel
                publishId={published.id}
                mcpServerId={published.mcp_server_id}
              />
            )}
          </>
        ) : (
          <EmptyState title="검토 기록을 찾을 수 없습니다" />
        )}
      </div>
    </div>
  );
}
