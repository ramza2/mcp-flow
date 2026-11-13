import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import StatusBadge from '../../components/ui/StatusBadge';
import JsonViewer from '../../components/ui/JsonViewer';
import { EmptyState, ErrorState, LoadingSkeleton } from '../../components/ui/EmptyState';
import { getFactoryJob, type FactoryJobDetailDto } from '../../api/factory';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp } from '../../domain';

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-[140px_1fr] gap-1 sm:gap-3 text-sm">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-slate-800 break-all">{value}</dd>
    </div>
  );
}

export default function FactoryJobDetail() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const [refreshKey, setRefreshKey] = useState(0);
  const [detail, setDetail] = useState<FactoryJobDetailDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  const load = useCallback(() => {
    if (!jobId) {
      setDetail(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setDetail(null);
    setLoading(true);
    setError(null);
    getFactoryJob(jobId, controller.signal)
      .then(data => {
        if (cancelled) return;
        setDetail(data);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Factory 분석 상세를 불러오지 못했습니다.',
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
  }, [jobId, refreshKey]);

  useEffect(() => load(), [load]);

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: '실제 Factory 분석 작업', to: '/tool-factory/jobs' }]}
        title="Factory 분석 작업 상세"
        description="실제 Factory API의 분석 작업 정보를 읽기 전용으로 조회합니다."
        actions={<Button variant="outline" onClick={() => navigate('/tool-factory/jobs')}>작업 목록</Button>}
      />
      <div className="p-6 max-w-4xl space-y-4">
        {loading ? (
          <LoadingSkeleton rows={6} />
        ) : error ? (
          <ErrorState
            message={error.message}
            requestId={error.requestId}
            onRetry={() => setRefreshKey(key => key + 1)}
          />
        ) : detail ? (
          <>
            <div className="flex justify-end">
              <Button variant="outline" onClick={() => setRefreshKey(key => key + 1)}>새로고침</Button>
            </div>
            <section className="bg-white border border-slate-200 rounded-xl p-4">
              <h2 className="font-semibold text-slate-800 mb-4">실제 분석 상태</h2>
              <dl className="space-y-3">
                <DetailRow label="작업 ID" value={<span className="font-mono text-xs">{detail.id}</span>} />
                <DetailRow label="소스 파일" value={detail.source_name} />
                <DetailRow label="작업 유형" value={detail.job_type} />
                <DetailRow label="작업 상태" value={<StatusBadge status={detail.status} size="sm" />} />
                <DetailRow label="분석 단계" value={detail.current_phase ?? '—'} />
                <DetailRow label="진행률" value={`${detail.progress_current} / ${detail.progress_total}`} />
                <DetailRow label="발견된 작업 수" value={detail.operation_count} />
                <DetailRow label="저장된 후보 수" value={detail.candidate_count} />
                <DetailRow label="테스트 결과 수" value={detail.test_result_count} />
                <DetailRow label="등록 시각" value={formatTimestamp(detail.created_at)} />
                <DetailRow label="완료 시각" value={formatTimestamp(detail.finished_at)} />
                {detail.error_code && (
                  <DetailRow label="오류 코드" value={<span className="font-mono">{detail.error_code}</span>} />
                )}
                {detail.error_message && (
                  <DetailRow label="오류 내용" value={detail.error_message} />
                )}
              </dl>
            </section>
            <section className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">분석 결과 (읽기 전용)</h2>
              {detail.analysis == null ? (
                <p className="text-sm text-slate-500">
                  저장된 분석 결과가 없습니다. 작업이 완료되지 않았거나 결과를 조회할 수 없는 상태일 수 있습니다.
                </p>
              ) : (
                <JsonViewer value={detail.analysis} />
              )}
            </section>
            <p className="text-xs text-slate-500">
              이 화면에서 후보 생성·빌드·검토·게시를 실행하지 않습니다. 분석 작업 성공만으로 MCP 서버나 도구가 활성화되지 않습니다.
            </p>
          </>
        ) : (
          <EmptyState title="분석 작업을 선택해 주세요" />
        )}
      </div>
    </div>
  );
}
