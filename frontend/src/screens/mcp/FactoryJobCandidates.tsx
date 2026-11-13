import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import Pagination from '../../components/ui/Pagination';
import StatusBadge from '../../components/ui/StatusBadge';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import {
  createFactoryBuild,
  FACTORY_MAX_BUILD_CANDIDATES,
  getFactoryJob,
  listFactoryCandidates,
  materializeFactoryCandidates,
  type FactoryCandidateDto,
  type FactoryJobDetailDto,
} from '../../api/factory';

const PAGE_SIZE = 20;

function newBuildKey(): string {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return 'factory-build-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

function getError(err: unknown, fallback: string): { message: string; requestId?: string } {
  const apiErr = isApiError(err) ? err : null;
  return { message: apiErr?.message ?? fallback, requestId: apiErr?.requestId ?? undefined };
}

function ErrorAlert({ error }: { error: { message: string; requestId?: string } }) {
  return (
    <div className="space-y-1">
      <InlineAlert type="error" message={error.message} />
      {error.requestId && <p className="text-xs font-mono text-slate-500">Request ID: {error.requestId}</p>}
    </div>
  );
}

export default function FactoryJobCandidates() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [job, setJob] = useState<FactoryJobDetailDto | null>(null);
  const [candidates, setCandidates] = useState<FactoryCandidateDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ message: string; requestId?: string } | null>(null);
  const [actionError, setActionError] = useState<{ message: string; requestId?: string } | null>(null);
  const [materializing, setMaterializing] = useState(false);
  const [building, setBuilding] = useState(false);
  const [materializeResult, setMaterializeResult] = useState<string | null>(null);
  const [selected, setSelected] = useState<Record<string, true>>({});
  // Retry the same selected candidate set with the same Idempotency-Key.
  const buildKeysRef = useRef<Map<string, string>>(new Map());
  const selectedIds = useMemo(() => Object.keys(selected).sort(), [selected]);
  const busy = materializing || building;

  useEffect(() => {
    setSelected({});
    buildKeysRef.current.clear();
    setPage(1);
    setMaterializeResult(null);
    setActionError(null);
  }, [jobId]);

  useEffect(() => {
    if (!jobId) {
      setLoading(false);
      setJob(null);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      getFactoryJob(jobId, controller.signal),
      listFactoryCandidates(jobId, { page, page_size: PAGE_SIZE, signal: controller.signal }),
    ])
      .then(([jobResponse, listResponse]) => {
        if (cancelled) return;
        setJob(jobResponse);
        setCandidates(listResponse.items);
        setTotal(listResponse.total);
        setHasNext(listResponse.has_next);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        setJob(null);
        setCandidates([]);
        setLoadError(getError(err, '후보 도구 목록을 불러오지 못했습니다.'));
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
  }, [jobId, page, refreshKey]);

  const handleMaterialize = async () => {
    if (!jobId || !job || job.status !== 'SUCCEEDED' || job.operation_count === 0 || busy) return;
    setMaterializing(true);
    setActionError(null);
    setMaterializeResult(null);
    try {
      const result = await materializeFactoryCandidates(jobId);
      setMaterializeResult(`후보 저장 완료: 신규 ${result.created_count}개, 총 ${result.total_count}개`);
      setPage(1);
      setRefreshKey(v => v + 1);
    } catch (err) {
      setActionError(getError(err, '후보 저장에 실패했습니다.'));
    } finally {
      setMaterializing(false);
    }
  };

  const handleBuild = async () => {
    if (!jobId || job?.status !== 'SUCCEEDED' || busy || loading ||
        selectedIds.length === 0 || selectedIds.length > FACTORY_MAX_BUILD_CANDIDATES) return;
    setBuilding(true);
    setActionError(null);
    try {
      const fingerprint = selectedIds.join(',');
      let key = buildKeysRef.current.get(fingerprint);
      if (!key) {
        key = newBuildKey();
        buildKeysRef.current.set(fingerprint, key);
      }
      const build = await createFactoryBuild(jobId, selectedIds, key);
      navigate(`/tool-factory/builds/${build.id}`);
    } catch (err) {
      setActionError(getError(err, 'Build 정의를 생성하지 못했습니다. 동일한 후보를 다시 시도할 수 있습니다.'));
    } finally {
      setBuilding(false);
    }
  };

  const toggle = (candidateId: string) => {
    if (busy) return;
    setSelected(prev => {
      const next = { ...prev };
      if (next[candidateId]) delete next[candidateId];
      else if (Object.keys(next).length < FACTORY_MAX_BUILD_CANDIDATES) next[candidateId] = true;
      return next;
    });
    setActionError(null);
  };

  const pageSelected = candidates.length > 0 && candidates.every(c => !!selected[c.id]);

  const togglePage = () => {
    if (busy || loading) return;
    setSelected(prev => {
      const next = { ...prev };
      if (candidates.every(c => !!next[c.id])) {
        candidates.forEach(c => { delete next[c.id]; });
      } else {
        for (const c of candidates) {
          if (Object.keys(next).length >= FACTORY_MAX_BUILD_CANDIDATES) break;
          next[c.id] = true;
        }
      }
      return next;
    });
    setActionError(null);
  };

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Factory 실제 작업', to: '/tool-factory/jobs' },
          { label: '분석 상세', to: jobId ? `/tool-factory/jobs/${jobId}` : undefined }]}
        title="도구 후보 조회·Build 정의"
        description="분석 작업에 연결된 실제 후보를 검토하고 선택한 후보로 Build 정의를 저장합니다."
        actions={
          <Button variant="outline" onClick={() => navigate(`/tool-factory/jobs/${jobId}`)}>
            분석 상세로
          </Button>
        }
      />
      <div className="p-6 max-w-5xl space-y-4">
        <InlineAlert
          type="info"
          message="후보 목록 조회는 읽기 전용입니다. '후보 DB 저장'과 'Build 정의 생성'은 별도 버튼을 누른 경우에만 DB 기록을 생성합니다. Build 정의 생성은 패키지 생성·보안 검증·게시·도구 활성화를 수행하지 않습니다."
        />
        {loading ? (
          <LoadingSkeleton rows={5} />
        ) : loadError ? (
          <ErrorState message={loadError.message} requestId={loadError.requestId}
            onRetry={() => setRefreshKey(v => v + 1)} />
        ) : job ? (
          <>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-2">
              <div className="flex items-center justify-between gap-3">
                <h2 className="font-semibold text-sm text-slate-800 break-all">{job.source_name}</h2>
                <StatusBadge status={job.status} size="sm" />
              </div>
              <p className="text-xs text-slate-500">
                분석 작업 ID: <span className="font-mono">{job.id}</span> · 분석된 작업: {job.operation_count}개 · 저장된 후보: {total}개
              </p>
              {job.status !== 'SUCCEEDED' && (
                <InlineAlert type="warning" message="분석이 성공적으로 완료된 작업에서만 후보를 저장하거나 Build 정의를 생성할 수 있습니다. 분석 상세에서 상태를 다시 확인하세요." />
              )}
              <div className="flex flex-wrap gap-2 pt-1">
                <Button variant="outline" onClick={() => setRefreshKey(v => v + 1)} disabled={busy}>
                  목록 새로고침
                </Button>
                <Button
                  variant="outline"
                  disabled={job.status !== 'SUCCEEDED' || job.operation_count === 0 || busy}
                  loading={materializing}
                  onClick={() => void handleMaterialize()}
                >
                  후보 DB 저장
                </Button>
              </div>
              <p className="text-xs text-slate-500">
                분석 성공 후 후보가 없다면 '후보 DB 저장'을 눌러 분석 결과를 실제 후보 기록으로 저장할 수 있습니다.
                이미 저장된 후보에 다시 실행해도 서버가 중복 생성을 제한합니다.
              </p>
              {materializeResult && <p className="text-sm text-green-700" role="status">{materializeResult}</p>}
            </section>
            {actionError && <ErrorAlert error={actionError} />}
            {candidates.length === 0 ? (
              <div className="rounded-xl border border-slate-200 bg-white">
                <EmptyState
                  title="저장된 후보 도구가 없습니다"
                  description="분석에 성공하고 작업이 발견되었다면 '후보 DB 저장'을 명시적으로 실행하세요. 조회만으로 후보가 생성되지는 않습니다."
                />
              </div>
            ) : (
              <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
                <div className="px-4 py-3 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
                  <label className="flex items-center gap-2 text-sm text-slate-700">
                    <input type="checkbox" aria-label="현재 페이지 후보 전체 선택"
                      checked={pageSelected} disabled={busy} onChange={togglePage} />
                    현재 페이지 전체 선택
                  </label>
                  <span className="text-sm font-semibold text-slate-700">선택: {selectedIds.length}개 / 총 {total}개</span>
                </div>
                <ul className="divide-y divide-slate-100">
                  {candidates.map(candidate => (
                    <li key={candidate.id} className="flex gap-3 px-4 py-3">
                      <input
                        aria-label={`후보 선택: ${candidate.operation_key}`}
                        type="checkbox"
                        checked={!!selected[candidate.id]}
                        disabled={busy || (selectedIds.length >= FACTORY_MAX_BUILD_CANDIDATES && !selected[candidate.id])}
                        onChange={() => toggle(candidate.id)}
                        className="mt-1"
                      />
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-xs font-mono font-semibold text-indigo-700">{candidate.method.toUpperCase()}</span>
                          <span className="font-mono text-sm text-slate-800 break-all">{candidate.path}</span>
                          {candidate.deprecated && <span className="text-xs text-amber-700">사용 중단 예정</span>}
                        </div>
                        <p className="text-sm text-slate-700 break-all">{candidate.summary || candidate.operation_id || candidate.operation_key}</p>
                        <p className="text-xs font-mono text-slate-400 break-all">{candidate.operation_key}</p>
                      </div>
                    </li>
                  ))}
                </ul>
                <Pagination page={page} pageSize={PAGE_SIZE} total={total} hasNext={hasNext}
                  disabled={busy} onPageChange={setPage} />
              </section>
            )}
            <section className="rounded-xl border border-slate-200 bg-white px-4 py-4 space-y-3">
              <h2 className="text-sm font-semibold text-slate-800">선택 후보로 Build 정의 저장</h2>
              <p className="text-xs text-slate-500">
                선택은 페이지 이동 후에도 유지됩니다. 선택한 후보 {selectedIds.length}개의 불변 스냅샷을 생성합니다.
                이 단계는 실행 가능한 MCP 패키지를 만들거나 게시하지 않습니다.
              </p>
              <Button onClick={() => void handleBuild()}
                disabled={busy || loading || job.status !== 'SUCCEEDED' || selectedIds.length === 0 || selectedIds.length > FACTORY_MAX_BUILD_CANDIDATES}
                loading={building}>
                Build 정의 생성 ({selectedIds.length}개)
              </Button>
            </section>
          </>
        ) : (
          <EmptyState title="분석 작업을 찾을 수 없습니다" />
        )}
      </div>
    </div>
  );
}
