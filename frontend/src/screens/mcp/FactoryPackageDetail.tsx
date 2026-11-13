import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import StatusBadge from '../../components/ui/StatusBadge';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import {
  getFactoryPackage,
  listFactoryPackageTests,
  startFactoryPackageTest,
  type FactoryPackageDto,
  type FactoryTestRunDto,
} from '../../api/factory';
import { formatTimestamp } from '../../domain';

function makeTestKey() {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return 'factory-test-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

export default function FactoryPackageDetail() {
  const { packageId } = useParams();
  const navigate = useNavigate();
  const [pkg, setPkg] = useState<FactoryPackageDto | null>(null);
  const [testRuns, setTestRuns] = useState<FactoryTestRunDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [loadError, setLoadError] = useState<{ message: string; requestId?: string } | null>(null);
  const [submitError, setSubmitError] = useState<{ message: string; requestId?: string } | null>(null);
  const testKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!packageId) {
      setLoading(false);
      setPkg(null);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      getFactoryPackage(packageId, controller.signal),
      listFactoryPackageTests(packageId, controller.signal),
    ])
      .then(([packageData, tests]) => {
        if (cancelled) return;
        setPkg(packageData);
        setTestRuns(tests.items);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setPkg(null);
        setTestRuns([]);
        setLoadError({
          message: apiErr?.message ?? '패키지 또는 샌드박스 테스트 이력을 조회하지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [packageId, refresh]);

  const handleRunTest = async () => {
    if (!packageId || loading || submitting || loadError || !pkg) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const key = testKeyRef.current ?? makeTestKey();
      testKeyRef.current = key;
      const result = await startFactoryPackageTest(packageId, key);
      navigate(`/tool-factory/test-runs/${result.id}`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setSubmitError({
        message: apiErr?.message ?? '샌드박스 테스트 실행 요청에 실패했습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Factory 실제 작업', to: '/tool-factory/jobs' }]}
        title="Factory 패키지 상세·테스트"
        description="실제 생성된 선언형 패키지를 확인하고 명시적으로 보안·계약 테스트를 요청합니다."
        actions={pkg ? (
          <Button variant="outline" onClick={() => navigate(`/tool-factory/builds/${pkg.build_id}`)}>
            Build 정의로
          </Button>
        ) : undefined}
      />
      <div className="p-6 max-w-4xl space-y-4">
        <InlineAlert type="warning"
          message="패키지 생성은 테스트 통과나 게시 완료를 의미하지 않습니다. 테스트는 실제 백엔드 샌드박스 Worker를 통해 비동기 처리되며, 실제 결과가 돌아오기 전에는 통과로 표시하지 않습니다." />
        {loading ? (
          <LoadingSkeleton rows={6} />
        ) : loadError ? (
          <ErrorState message={loadError.message} requestId={loadError.requestId} onRetry={() => setRefresh(v => v + 1)} />
        ) : pkg ? (
          <>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">생성된 패키지</h2>
              <dl className="text-sm space-y-2">
                <div className="flex gap-3 flex-wrap"><dt className="text-slate-500">패키지 ID</dt><dd className="font-mono break-all">{pkg.id}</dd></div>
                <div className="flex gap-3 flex-wrap"><dt className="text-slate-500">도구 개수</dt><dd>{pkg.tool_count}개</dd></div>
                <div className="flex gap-3 flex-wrap"><dt className="text-slate-500">패키지 해시</dt><dd className="font-mono text-xs break-all">{pkg.package_content_sha256}</dd></div>
                <div className="flex gap-3 flex-wrap"><dt className="text-slate-500">생성 시각</dt><dd>{formatTimestamp(pkg.created_at)}</dd></div>
              </dl>
              <h3 className="font-medium text-sm text-slate-700 pt-3">포함 도구</h3>
              <ul className="divide-y divide-slate-100">
                {pkg.tools.map(tool => (
                  <li key={tool.candidate_id} className="py-2 space-y-1">
                    <p className="text-sm flex flex-wrap gap-2">
                      <span className="font-mono text-indigo-700">{tool.method.toUpperCase()}</span>
                      <span className="font-mono text-slate-800 break-all">{tool.path}</span>
                    </p>
                    <p className="text-xs text-slate-500">{tool.summary || tool.operation_id || tool.operation_key}</p>
                  </li>
                ))}
              </ul>
            </section>
            <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-3">
              <h2 className="font-semibold text-slate-800">보안·계약 샌드박스 테스트</h2>
              <p className="text-xs text-slate-500">
                명시적으로 테스트를 요청하면 실제 TestRun이 등록됩니다. 완료 여부·PASS/FAIL은 TestRun 상세의 실제 결과로 확인해야 합니다.
              </p>
              <div className="flex gap-2 flex-wrap">
                <Button loading={submitting} disabled={submitting} onClick={() => void handleRunTest()}>
                  샌드박스 테스트 시작
                </Button>
                <Button variant="outline" disabled={submitting} onClick={() => setRefresh(v => v + 1)}>
                  테스트 이력 새로고침
                </Button>
              </div>
              {submitError && (
                <div className="space-y-1">
                  <InlineAlert type="error" message={submitError.message} />
                  {submitError.requestId && <p className="font-mono text-xs text-slate-500">Request ID: {submitError.requestId}</p>}
                  <p className="text-xs text-slate-500">다시 요청할 때 동일한 멱등성 키를 사용합니다.</p>
                </div>
              )}
              {testRuns.length === 0 ? (
                <EmptyState title="등록된 테스트가 없습니다" description="테스트 버튼을 누르기 전에는 성공 상태나 검사 결과를 표시하지 않습니다." />
              ) : (
                <ul className="divide-y divide-slate-100">
                  {testRuns.map(t => (
                    <li key={t.id} className="flex flex-wrap gap-3 items-center justify-between py-3">
                      <div className="space-y-1">
                        <div className="flex gap-2 items-center">
                          <StatusBadge status={t.status} size="sm" />
                          {t.result && <span className="text-sm font-medium">{t.result}</span>}
                        </div>
                        <p className="font-mono text-xs text-slate-500 break-all">{t.id}</p>
                        <p className="text-xs text-slate-500">{formatTimestamp(t.created_at)}</p>
                      </div>
                      <Button variant="outline" size="sm" onClick={() => navigate(`/tool-factory/test-runs/${t.id}`)}>
                        실제 테스트 결과
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        ) : (
          <EmptyState title="패키지를 찾지 못했습니다" />
        )}
      </div>
    </div>
  );
}
