import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import StatusBadge from '../../components/ui/StatusBadge';
import JsonViewer from '../../components/ui/JsonViewer';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { getFactoryTestRun, type FactoryTestRunDto } from '../../api/factory';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp } from '../../domain';
import FactoryReviewPanel from './FactoryReviewPanel';

export default function FactoryTestRunDetail() {
  const { testRunId } = useParams();
  const navigate = useNavigate();
  const [record, setRecord] = useState<FactoryTestRunDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  useEffect(() => {
    if (!testRunId) {
      setLoading(false);
      setRecord(null);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setRecord(null);
    setError(null);
    getFactoryTestRun(testRunId, controller.signal)
      .then(data => { if (!cancelled) setRecord(data); })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? '실제 Factory 테스트 결과를 조회하지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [testRunId, refresh]);

  const passed = record?.status === 'SUCCEEDED' && record.result === 'PASS';

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Factory 실제 작업', to: '/tool-factory/jobs' }]}
        title="Factory 샌드박스 테스트 결과"
        description="실제 Worker가 기록한 보안·계약 테스트 상태, 결과 및 증빙을 조회합니다."
        actions={record ? (
          <Button variant="outline" onClick={() => navigate(`/tool-factory/artifacts/${record.generated_package_id}`)}>
            패키지 상세
          </Button>
        ) : undefined}
      />
      <div className="p-6 max-w-4xl space-y-4">
        <InlineAlert type="info"
          message="QUEUED/RUNNING은 테스트 대기·진행 상태이며 PASS가 아닙니다. 결과가 PASS이더라도 별도의 검토 및 게시 절차가 남아 있고 MCP 도구는 자동 활성화되지 않습니다." />
        {loading ? (
          <LoadingSkeleton rows={5} />
        ) : error ? (
          <ErrorState message={error.message} requestId={error.requestId} onRetry={() => setRefresh(v => v + 1)} />
        ) : record ? (
          <>
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="outline" onClick={() => setRefresh(v => v + 1)}>상태 새로고침</Button>
            </div>
            <section className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">실제 테스트 진행 상태</h2>
              <dl className="space-y-2 text-sm">
                <div className="flex flex-wrap gap-3"><dt className="text-slate-500">TestRun ID</dt><dd className="font-mono break-all">{record.id}</dd></div>
                <div className="flex flex-wrap items-center gap-3"><dt className="text-slate-500">작업 상태</dt><dd><StatusBadge status={record.status} size="sm" /></dd></div>
                <div className="flex flex-wrap gap-3"><dt className="text-slate-500">검증 결과</dt><dd className={passed ? 'text-green-700 font-semibold' : 'text-slate-700'}>{record.result ?? '미확정'}</dd></div>
                <div className="flex flex-wrap gap-3"><dt className="text-slate-500">보안 정책 버전</dt><dd className="font-mono break-all">{record.security_policy_version}</dd></div>
                <div className="flex flex-wrap gap-3"><dt className="text-slate-500">계약 정책 버전</dt><dd className="font-mono break-all">{record.contract_policy_version}</dd></div>
                <div className="flex flex-wrap gap-3"><dt className="text-slate-500">등록 시각</dt><dd>{formatTimestamp(record.created_at)}</dd></div>
                <div className="flex flex-wrap gap-3"><dt className="text-slate-500">완료 시각</dt><dd>{formatTimestamp(record.finished_at)}</dd></div>
                {record.evidence_hash && (
                  <div className="flex flex-wrap gap-3"><dt className="text-slate-500">증빙 해시</dt><dd className="font-mono text-xs break-all">{record.evidence_hash}</dd></div>
                )}
                {record.error_code && (
                  <div className="flex flex-wrap gap-3"><dt className="text-slate-500">오류 코드</dt><dd className="font-mono text-red-700">{record.error_code}</dd></div>
                )}
                {record.error_message && (
                  <div className="flex flex-wrap gap-3"><dt className="text-slate-500">오류 상세</dt><dd className="break-all">{record.error_message}</dd></div>
                )}
              </dl>
            </section>
            <section className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">보안 검증 결과</h2>
              {record.security_result == null ? <p className="text-sm text-slate-500">아직 보안 검사 결과가 없습니다.</p>
                : <JsonViewer value={record.security_result} />}
            </section>
            <section className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">계약 검증 결과</h2>
              {record.contract_result == null ? <p className="text-sm text-slate-500">아직 계약 검사 결과가 없습니다.</p>
                : <JsonViewer value={record.contract_result} />}
            </section>
            {passed && (
              <p role="status" className="text-sm font-medium text-green-700">
                테스트가 실제 PASS로 완료됐습니다. 아래에서 검토 결정을 별도로 저장할 수 있으며 게시·활성화는 자동 실행되지 않습니다.
              </p>
            )}
            {passed && record.evidence_hash && record.evidence_hash.length === 64 && (
              <FactoryReviewPanel testRunId={record.id} evidenceHash={record.evidence_hash} />
            )}
            {passed && (!record.evidence_hash || record.evidence_hash.length !== 64) && (
              <InlineAlert type="warning" message="검증 증빙 해시가 없어 검토할 수 없습니다. 실제 Backend 결과를 확인하세요." />
            )}
          </>
        ) : (
          <EmptyState title="실제 테스트 결과를 찾을 수 없습니다" />
        )}
      </div>
    </div>
  );
}
