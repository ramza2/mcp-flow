import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { getFactoryBuild, type FactoryBuildDto } from '../../api/factory';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp } from '../../domain';
import FactoryBuildPackages from './FactoryBuildPackages';

export default function FactoryBuildRecord() {
  const { buildId } = useParams();
  const navigate = useNavigate();
  const [record, setRecord] = useState<FactoryBuildDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  useEffect(() => {
    if (!buildId) {
      setRecord(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setRecord(null);

    getFactoryBuild(buildId, controller.signal)
      .then(data => { if (!cancelled) setRecord(data); })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? '실제 Build 정의를 조회하지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [buildId, refreshKey]);

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Factory 실제 작업', to: '/tool-factory/jobs' }]}
        title="Factory Build 정의 상세"
        description="실제 Backend에 저장된 후보 선택 스냅샷을 조회합니다. 패키지 빌드 결과 화면이 아닙니다."
        actions={record ? (
          <Button variant="outline" onClick={() => navigate(`/tool-factory/jobs/${record.job_id}/candidates`)}>
            후보 선택 화면
          </Button>
        ) : undefined}
      />
      <div className="p-6 max-w-4xl space-y-4">
        <InlineAlert
          type="warning"
          message="Build 정의는 패키지와 구분됩니다. 아래에서 명시적으로 패키지를 생성할 수 있지만 보안·계약 테스트, 승인, 게시 및 도구 활성화는 각각 별도 절차가 필요합니다."
        />
        {loading ? (
          <LoadingSkeleton rows={4} />
        ) : error ? (
          <ErrorState message={error.message} requestId={error.requestId}
            onRetry={() => setRefreshKey(v => v + 1)} />
        ) : record ? (
          <>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-2">
              <h2 className="font-semibold text-slate-800">저장된 Build 정의</h2>
              <dl className="space-y-2 text-sm">
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">Build ID</dt><dd className="font-mono break-all">{record.id}</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">분석 작업</dt><dd className="font-mono break-all">{record.job_id}</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">선택한 후보</dt><dd>{record.candidate_count}개</dd></div>
                <div className="flex flex-wrap gap-2"><dt className="text-slate-500">저장 시각</dt><dd>{formatTimestamp(record.created_at)}</dd></div>
              </dl>
            </section>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">선택 후보 스냅샷</h2>
              <ul className="divide-y divide-slate-100">
                {record.candidates_snapshot.map(item => (
                  <li key={item.candidate_id} className="py-2 flex flex-wrap gap-2 text-sm">
                    <span className="font-mono text-xs text-indigo-700">{item.method.toUpperCase()}</span>
                    <span className="font-mono break-all">{item.path}</span>
                    <span className="text-slate-500">{item.summary || item.operation_id || item.operation_key}</span>
                  </li>
                ))}
              </ul>
            </section>
            <FactoryBuildPackages buildId={record.id} />
          </>
        ) : (
          <EmptyState title="Build 기록을 찾을 수 없습니다" />
        )}
      </div>
    </div>
  );
}
