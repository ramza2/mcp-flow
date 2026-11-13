import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import Button from '../../components/ui/Button';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import { generateFactoryPackage, listFactoryPackages, type FactoryPackageDto } from '../../api/factory';
import { formatTimestamp } from '../../domain';

function newKey(): string {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return 'factory-package-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

export default function FactoryBuildPackages({ buildId }: { buildId: string }) {
  const navigate = useNavigate();
  const [packages, setPackages] = useState<FactoryPackageDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [generationError, setGenerationError] = useState<{ message: string; requestId?: string } | null>(null);
  const keyRef = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    listFactoryPackages(buildId, controller.signal)
      .then(data => {
        if (!cancelled) setPackages(data.items);
      })
      .catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setPackages([]);
        setError({
          message: apiErr?.message ?? '실제 패키지 목록을 조회할 수 없습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [buildId, refresh]);

  const handleGenerate = async () => {
    if (generating || loading || error) return;
    setGenerating(true);
    setGenerationError(null);
    try {
      const key = keyRef.current ?? newKey();
      keyRef.current = key;
      const result = await generateFactoryPackage(buildId, key);
      navigate(`/tool-factory/artifacts/${result.id}`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setGenerationError({
        message: apiErr?.message ?? '실제 Factory 패키지 생성에 실패했습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      setGenerating(false);
    }
  };

  return (
    <section className="rounded-xl bg-white border border-slate-200 p-4 space-y-3">
      <h2 className="font-semibold text-slate-800">실제 Factory 패키지</h2>
      <InlineAlert type="info" message="현재 Build 정의의 불변 스냅샷으로 선언형 패키지를 생성합니다. 생성한 패키지는 MCP 서버 등록이나 게시를 수행하지 않으며, 보안·계약 검증도 별도로 실행해야 합니다." />
      {loading ? (
        <LoadingSkeleton rows={3} />
      ) : error ? (
        <ErrorState message={error.message} requestId={error.requestId} onRetry={() => setRefresh(v => v + 1)} />
      ) : (
        <>
          <div className="flex flex-wrap gap-2">
            <Button loading={generating} disabled={generating}
              onClick={() => void handleGenerate()}>
              패키지 생성
            </Button>
            <Button variant="outline" disabled={generating}
              onClick={() => setRefresh(v => v + 1)}>
              패키지 목록 새로고침
            </Button>
          </div>
          {generationError && (
            <div className="space-y-1">
              <InlineAlert type="error" message={generationError.message} />
              {generationError.requestId && (
                <p className="text-xs font-mono text-slate-500">Request ID: {generationError.requestId}</p>
              )}
              <p className="text-xs text-slate-500">같은 Build로 재시도할 때 동일한 멱등성 키를 유지합니다.</p>
            </div>
          )}
          {packages.length === 0 ? (
            <EmptyState title="생성된 패키지가 없습니다" description="명시적으로 패키지 생성을 실행하기 전에는 패키지가 생성되지 않습니다." />
          ) : (
            <ul className="divide-y divide-slate-100">
              {packages.map(pkg => (
                <li key={pkg.id} className="py-3 flex flex-wrap gap-3 justify-between items-center">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-slate-800">선언형 패키지 · {pkg.tool_count}개 도구</p>
                    <p className="font-mono text-xs text-slate-500 break-all">{pkg.id}</p>
                    <p className="text-xs text-slate-500">{formatTimestamp(pkg.created_at)}</p>
                  </div>
                  <Button size="sm" variant="outline" onClick={() => navigate(`/tool-factory/artifacts/${pkg.id}`)}>
                    패키지 상세·테스트
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
