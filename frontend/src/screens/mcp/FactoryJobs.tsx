import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { type Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import { EmptyState, ErrorState, LoadingSkeleton } from '../../components/ui/EmptyState';
import { listFactoryJobs, type FactoryJobDto } from '../../api/factory';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp } from '../../domain';

const PAGE_SIZE = 20;

export default function FactoryJobs() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [items, setItems] = useState<FactoryJobDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);

  const load = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);

    listFactoryJobs({ page, page_size: PAGE_SIZE, signal: controller.signal })
      .then(data => {
        if (cancelled) return;
        setItems(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Factory 분석 작업을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setItems([]);
        setTotal(0);
        setHasNext(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [page, refreshKey]);

  useEffect(() => load(), [load]);

  const columns: Column<FactoryJobDto>[] = [
    { key: 'source_name', label: '소스 파일', render: job => (
      <span className="font-medium text-slate-800 break-all">{job.source_name}</span>
    ) },
    { key: 'job_type', label: '작업 유형', render: job => (
      <span className="font-mono text-xs text-slate-600">{job.job_type}</span>
    ) },
    { key: 'status', label: '상태', render: job => <StatusBadge status={job.status} size="sm" /> },
    { key: 'operation_count', label: '분석된 작업 수', render: job => (
      <span className="text-xs text-slate-700">{job.operation_count}</span>
    ) },
    { key: 'created_at', label: '등록 시각', render: job => (
      <span className="text-xs text-slate-500">{formatTimestamp(job.created_at)}</span>
    ) },
  ];

  return (
    <div>
      <PageHeader
        title="Factory 실제 분석 작업"
        description="서버에 저장된 OpenAPI 분석 작업을 조회합니다. 시연용 프로젝트와 다른 실제 이력입니다."
        actions={
          <Button variant="outline" onClick={() => navigate('/tool-factory')}>
            시연 화면 보기
          </Button>
        }
      />
      <div className="p-6 space-y-4">
        <section aria-label="Factory 실제 작업 안내" className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
          <p className="font-semibold text-slate-800">실제 서버 작업 기록</p>
          <p className="mt-1">아래 목록은 Factory API에서 가져온 분석 이력입니다. 이 화면은 조회 전용이며 소스 업로드·후보 생성·빌드·게시를 실행하지 않습니다.</p>
          <p className="mt-1 text-xs">작업 완료는 MCP 도구 생성이나 활성화 완료를 의미하지 않습니다. 작업을 선택해 실제 분석 상세와 후보 수를 확인하세요.</p>
        </section>
        <div className="flex justify-end">
          <Button variant="outline" onClick={() => setRefreshKey(key => key + 1)} disabled={loading}>
            새로고침
          </Button>
        </div>
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {loading ? (
            <LoadingSkeleton rows={5} />
          ) : error ? (
            <ErrorState message={error.message} requestId={error.requestId} onRetry={() => setRefreshKey(key => key + 1)} />
          ) : items.length === 0 ? (
            <EmptyState
              title="등록된 Factory 분석 작업이 없습니다"
              description="실제 서버에 저장된 분석 작업이 없거나 현재 조회할 작업이 없습니다. 예시 데이터는 이 목록에 표시하지 않습니다."
            />
          ) : (
            <>
              <DataTable
                columns={columns}
                data={items}
                rowKey={job => job.id}
                onRowClick={job => navigate(`/tool-factory/jobs/${job.id}`)}
              />
              <Pagination
                page={page}
                pageSize={PAGE_SIZE}
                total={total}
                hasNext={hasNext}
                onPageChange={setPage}
                disabled={loading}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
