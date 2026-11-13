import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { Search, AlertTriangle, ExternalLink, ArrowRight, Loader2 } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import {
  EmptyState,
  ErrorState,
  InlineAlert,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import { isAbortError, isApiError } from '../../api/client';
import {
  createExternalMCPSearch,
  importExternalMCPCandidate,
  listExternalMCPSources,
  reviewExternalMCPCandidate,
} from '../../api/mcpDiscovery';
import type {
  ExternalMCPCandidateDto,
  ExternalMCPReviewDecision,
  ExternalMCPSourceDto,
} from '../../api/types';
import { labelExternalMCPReviewState } from '../../domain';

const IMPORTABLE_TRANSPORTS = new Set(['STREAMABLE_HTTP', 'LEGACY_HTTP_SSE']);

const FLOW_HINT = [
  '후보 검토',
  '→ DRAFT 가져오기',
  '→ 서버 상세',
  '→ 연결 테스트',
  '→ 도구 탐색',
  '→ 도구 검증',
  '→ 활성화',
] as const;

function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const parsed = new URL(raw);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch {
    /* untrusted / malformed */
  }
  return null;
}

function canImportCandidate(candidate: ExternalMCPCandidateDto): boolean {
  return (
    candidate.review_state === 'APPROVED'
    && candidate.imported_mcp_server_id == null
    && !!candidate.endpoint_url
    && IMPORTABLE_TRANSPORTS.has(candidate.transport_type ?? '')
  );
}

function reviewStateClass(state: string): string {
  switch (state) {
    case 'APPROVED':
      return 'bg-green-50 text-green-700';
    case 'REJECTED':
      return 'bg-red-50 text-red-700';
    default:
      return 'bg-slate-100 text-slate-600';
  }
}

function importDisabledReason(candidate: ExternalMCPCandidateDto): string | null {
  if (candidate.imported_mcp_server_id) return null;
  const hasRemote =
    !!candidate.endpoint_url
    && IMPORTABLE_TRANSPORTS.has(candidate.transport_type ?? '');
  if (!hasRemote) {
    return '원격 MCP 연결 주소가 없거나 지원하지 않는 전송 방식이므로 가져올 수 없습니다.';
  }
  if (candidate.review_state !== 'APPROVED') {
    return '가져오려면 이 후보를 먼저 승인해야 합니다.';
  }
  return null;
}

export default function ExternalDiscovery() {
  const navigate = useNavigate();

  const [sources, setSources] = useState<ExternalMCPSourceDto[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [sourcesError, setSourcesError] = useState<{
    message: string;
    requestId?: string;
  } | null>(null);
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null);
  const [sourcesReloadKey, setSourcesReloadKey] = useState(0);

  const [q, setQ] = useState('');
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [candidates, setCandidates] = useState<ExternalMCPCandidateDto[]>([]);
  const [candidateCount, setCandidateCount] = useState<number | null>(null);
  const [searchNetworkError, setSearchNetworkError] = useState<{
    message: string;
    requestId?: string;
  } | null>(null);
  const [searchFailed, setSearchFailed] = useState<{
    errorCode: string | null;
    errorMessage: string;
  } | null>(null);

  const [reviewTarget, setReviewTarget] = useState<{
    candidate: ExternalMCPCandidateDto;
    decision: ExternalMCPReviewDecision;
  } | null>(null);
  const [reviewComment, setReviewComment] = useState('');
  const [reviewSubmitting, setReviewSubmitting] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);

  const [importingId, setImportingId] = useState<string | null>(null);
  const [candidateActionError, setCandidateActionError] = useState<Record<string, string>>(
    {},
  );
  const [draftCreatedIds, setDraftCreatedIds] = useState<Record<string, true>>({});

  const searchSeqRef = useRef(0);
  const searchAbortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      searchAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setSourcesLoading(true);
    setForbidden(false);
    setSourcesError(null);

    listExternalMCPSources(controller.signal)
      .then((res) => {
        if (!mountedRef.current || controller.signal.aborted) return;
        const items = res.items ?? [];
        setSources(items);
        const firstEnabled = items.find((s) => s.enabled);
        setSelectedSourceId((prev) => {
          if (prev && items.some((s) => s.id === prev && s.enabled)) return prev;
          return firstEnabled?.id ?? null;
        });
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || controller.signal.aborted || !mountedRef.current) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setSources([]);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setSourcesError({
          message: apiErr?.message ?? '외부 MCP 검색 출처 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      })
      .finally(() => {
        if (!controller.signal.aborted && mountedRef.current) {
          setSourcesLoading(false);
        }
      });

    return () => controller.abort();
  }, [sourcesReloadKey]);

  const selectedSource = sources.find((s) => s.id === selectedSourceId) ?? null;
  const enabledSelected = selectedSource?.enabled === true;
  const trimmedQuery = q.trim();
  // New search supersedes an in-flight one (abort + monotonic seq). Disable only
  // when there is nothing valid to submit.
  const searchDisabled = !enabledSelected || !trimmedQuery;

  const sourceNameById = (sourceId: string): string | undefined =>
    sources.find((s) => s.id === sourceId)?.name;

  const handleSearch = async () => {
    if (searchDisabled || !selectedSourceId) return;

    searchAbortRef.current?.abort();
    const controller = new AbortController();
    searchAbortRef.current = controller;
    const seq = ++searchSeqRef.current;
    const queryForRequest = trimmedQuery;

    setSearching(true);
    setSearchNetworkError(null);
    setSearchFailed(null);
    setHasSearched(true);

    try {
      const result = await createExternalMCPSearch(
        {
          source_id: selectedSourceId,
          q: queryForRequest,
          limit: 20,
        },
        controller.signal,
      );

      if (!mountedRef.current || seq !== searchSeqRef.current) return;

      if (result.status === 'FAILED') {
        setCandidates([]);
        setCandidateCount(0);
        setSearchFailed({
          errorCode: result.error_code,
          errorMessage:
            result.error_message
            ?? '검색에 실패했습니다. 잠시 후 다시 시도하세요.',
        });
        return;
      }

      setSearchFailed(null);
      setCandidates(result.candidates ?? []);
      setCandidateCount(result.candidate_count);
    } catch (err: unknown) {
      if (isAbortError(err) || !mountedRef.current || seq !== searchSeqRef.current) {
        return;
      }
      const apiErr = isApiError(err) ? err : null;
      setCandidates([]);
      setCandidateCount(null);
      setSearchNetworkError({
        message: apiErr?.message ?? '검색 요청에 실패했습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      if (mountedRef.current && seq === searchSeqRef.current) {
        setSearching(false);
      }
    }
  };

  const openReview = (
    candidate: ExternalMCPCandidateDto,
    decision: ExternalMCPReviewDecision,
  ) => {
    setReviewTarget({ candidate, decision });
    setReviewComment('');
    setReviewError(null);
  };

  const submitReview = async () => {
    if (!reviewTarget || reviewSubmitting) return;
    setReviewSubmitting(true);
    setReviewError(null);
    try {
      const comment = reviewComment.trim();
      const res = await reviewExternalMCPCandidate(reviewTarget.candidate.id, {
        decision: reviewTarget.decision,
        ...(comment ? { comment } : {}),
      });
      if (!mountedRef.current) return;
      setCandidates((prev) =>
        prev.map((c) =>
          c.id === reviewTarget.candidate.id
            ? { ...c, review_state: res.review_state }
            : c,
        ),
      );
      setCandidateActionError((prev) => {
        const next = { ...prev };
        delete next[reviewTarget.candidate.id];
        return next;
      });
      setReviewTarget(null);
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      const apiErr = isApiError(err) ? err : null;
      setReviewError(apiErr?.message ?? '검토 요청에 실패했습니다.');
    } finally {
      if (mountedRef.current) setReviewSubmitting(false);
    }
  };

  const handleImport = async (candidate: ExternalMCPCandidateDto) => {
    if (!canImportCandidate(candidate) || importingId) return;
    setImportingId(candidate.id);
    setCandidateActionError((prev) => {
      const next = { ...prev };
      delete next[candidate.id];
      return next;
    });
    try {
      const res = await importExternalMCPCandidate(candidate.id);
      if (!mountedRef.current) return;
      setCandidates((prev) =>
        prev.map((c) =>
          c.id === candidate.id
            ? { ...c, imported_mcp_server_id: res.mcp_server_id }
            : c,
        ),
      );
      setDraftCreatedIds((prev) => ({ ...prev, [candidate.id]: true }));
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      const apiErr = isApiError(err) ? err : null;
      setCandidateActionError((prev) => ({
        ...prev,
        [candidate.id]: apiErr?.message ?? 'DRAFT 서버 가져오기에 실패했습니다.',
      }));
    } finally {
      if (mountedRef.current) setImportingId(null);
    }
  };

  if (sourcesLoading) {
    return (
      <div>
        <PageHeader
          title="외부 MCP 탐색"
          description="외부 레지스트리에서 MCP 서버 후보를 찾고 검토하여 등록합니다."
        />
        <LoadingSkeleton rows={6} />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div>
        <PageHeader
          title="외부 MCP 탐색"
          description="외부 레지스트리에서 MCP 서버 후보를 찾고 검토하여 등록합니다."
        />
        <PermissionDenied />
      </div>
    );
  }

  if (sourcesError) {
    return (
      <div>
        <PageHeader
          title="외부 MCP 탐색"
          description="외부 레지스트리에서 MCP 서버 후보를 찾고 검토하여 등록합니다."
        />
        <ErrorState
          message={sourcesError.message}
          requestId={sourcesError.requestId}
          onRetry={() => setSourcesReloadKey((k) => k + 1)}
        />
      </div>
    );
  }

  const hasEnabledSource = sources.some((s) => s.enabled);

  return (
    <div>
      <PageHeader
        title="외부 MCP 탐색"
        description="외부 레지스트리에서 MCP 서버 후보를 찾고 검토하여 등록합니다."
      />
      <div className="p-6 space-y-4 max-w-3xl">
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
          <p className="font-semibold text-slate-800">외부 MCP 서버 등록 순서</p>
          <p className="mt-1">① 검색 출처 선택 및 검색 → ② 후보 검토·승인 → ③ DRAFT 서버 가져오기 → ④ 서버 상세에서 연결 테스트·도구 탐색·검증 및 활성화</p>
          <p className="mt-1 text-xs">외부 등록 정보만으로 서버나 도구의 안전성·연결 상태를 신뢰할 수 없습니다.</p>
        </div>
        <InlineAlert
          type="warning"
          message="검색 결과는 검토가 필요한 외부 MCP 서버 후보입니다. 후보를 승인하고 가져와도 연결 테스트·도구 탐색·활성화가 자동 실행되지 않습니다. DRAFT 서버를 생성한 뒤 상세 화면에서 연결 테스트 → 도구 탐색 → 도구 검증 → 활성화를 진행하세요."
        />

        {!hasEnabledSource ? (
          <EmptyState
            title="사용 가능한 검색 출처가 없습니다"
            description="활성화된 외부 MCP 검색 출처가 없습니다. 관리자에게 출처 설정을 요청하세요."
          />
        ) : (
          <>
            <div className="space-y-2">
              <label htmlFor="discovery-source" className="block text-xs font-medium text-slate-600">
                검색 출처
              </label>
              <select
                id="discovery-source"
                value={selectedSourceId ?? ''}
                onChange={(e) => setSelectedSourceId(e.target.value || null)}
                className="w-full h-10 px-3 text-sm border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
              >
                {sources.map((source) => (
                  <option
                    key={source.id}
                    value={source.id}
                    disabled={!source.enabled}
                  >
                    {source.name}
                    {!source.enabled ? ' (사용 안 함)' : ''}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex gap-2">
              <div className="relative flex-1">
                <Search
                  size={14}
                  className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      void handleSearch();
                    }
                  }}
                  maxLength={128}
                  placeholder="Slack, Notion, GitHub..."
                  className="w-full h-10 pl-9 pr-4 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              <Button
                onClick={() => void handleSearch()}
                disabled={searchDisabled}
                icon={
                  searching
                    ? <Loader2 size={13} className="animate-spin" />
                    : <Search size={13} />
                }
              >
                검색
              </Button>
            </div>
          </>
        )}

        {searchNetworkError && (
          <div className="space-y-1">
            <InlineAlert type="error" message={searchNetworkError.message} />
            {searchNetworkError.requestId && (
              <p className="font-mono text-xs text-slate-400">
                Request ID: {searchNetworkError.requestId}
              </p>
            )}
          </div>
        )}

        {searchFailed && (
          <div className="space-y-1">
            <InlineAlert type="error" message={searchFailed.errorMessage} />
            {searchFailed.errorCode && (
              <p className="font-mono text-xs text-slate-400">
                {searchFailed.errorCode}
              </p>
            )}
          </div>
        )}

        {hasSearched && !searchNetworkError && !searchFailed && (
          <div className="space-y-3">
            <p className="text-xs text-slate-500">
              {candidateCount ?? candidates.length}개 서버 후보 검색됨
            </p>

            {candidates.length === 0 ? (
              <EmptyState
                icon={<Search size={22} />}
                title="검색 결과가 없습니다"
                description="다른 검색어로 다시 시도하세요."
              />
            ) : (
              candidates.map((c) => {
                const repoHref = safeHttpUrl(c.repository_url);
                const homeHref = safeHttpUrl(c.homepage_url);
                const imported = !!c.imported_mcp_server_id;
                const importable = canImportCandidate(c);
                const disableReason = importDisabledReason(c);
                const actionError = candidateActionError[c.id];

                return (
                  <div
                    key={c.id}
                    className="bg-white rounded-xl border border-slate-200 p-4"
                    data-testid={`candidate-${c.id}`}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 mb-1 flex-wrap">
                          <h3 className="text-sm font-semibold text-slate-800">
                            {c.name}
                          </h3>
                          <span
                            className={`text-xs px-2 py-0.5 rounded-full font-medium ${reviewStateClass(c.review_state)}`}
                          >
                            {labelExternalMCPReviewState(c.review_state)}
                          </span>
                          {imported && (
                            <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-indigo-50 text-indigo-700">
                              가져온 서버
                            </span>
                          )}
                        </div>
                        {c.description && (
                          <p className="text-sm text-slate-600 mb-2">{c.description}</p>
                        )}
                        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                          <span>
                            출처: {sourceNameById(c.source_id) ?? '—'}
                          </span>
                          <span>
                            전송 방식:{' '}
                            {c.transport_type ?? '원격 연결 주소 없음'}
                          </span>
                          {c.version && <span>버전: {c.version}</span>}
                          {c.license && <span>라이선스: {c.license}</span>}
                          {c.endpoint_url && (
                            <span className="break-all">
                              연결 주소: {c.endpoint_url}
                            </span>
                          )}
                          {repoHref ? (
                            <a
                              href={repoHref}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-0.5 text-indigo-600 hover:underline"
                            >
                              저장소 <ExternalLink size={10} />
                            </a>
                          ) : c.repository_url ? (
                            <span>저장소: 안전하지 않은 주소</span>
                          ) : null}
                          {homeHref ? (
                            <a
                              href={homeHref}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-0.5 text-indigo-600 hover:underline"
                            >
                              홈페이지 <ExternalLink size={10} />
                            </a>
                          ) : c.homepage_url ? (
                            <span>홈페이지: 안전하지 않은 주소</span>
                          ) : null}
                        </div>

                        {disableReason && !imported && (
                          <div className="mt-2 flex items-center gap-1.5 text-xs text-amber-700">
                            <AlertTriangle size={12} /> {disableReason}
                          </div>
                        )}

                        {draftCreatedIds[c.id] && (
                          <p className="mt-2 text-xs font-medium text-green-700">
                            DRAFT 생성 완료
                          </p>
                        )}

                        {actionError && (
                          <div className="mt-2">
                            <InlineAlert type="error" message={actionError} />
                          </div>
                        )}
                      </div>

                      <div className="shrink-0 flex flex-col gap-2 items-stretch">
                        {!imported && (
                          <div className="flex gap-1.5">
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => openReview(c, 'APPROVE')}
                            >
                              승인
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => openReview(c, 'REJECT')}
                            >
                              거절
                            </Button>
                          </div>
                        )}

                        {imported && c.imported_mcp_server_id ? (
                          <Button
                            size="sm"
                            variant="primary"
                            icon={<ArrowRight size={12} />}
                            onClick={() =>
                              navigate(`/mcp/servers/${c.imported_mcp_server_id}`)
                            }
                          >
                            MCP 서버 상세 보기
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            variant="outline"
                            loading={importingId === c.id}
                            disabled={!importable || importingId !== null}
                            icon={<ArrowRight size={12} />}
                            onClick={() => void handleImport(c)}
                          >
                            DRAFT 서버 가져오기
                          </Button>
                        )}
                      </div>
                    </div>

                    <div className="mt-3 flex items-center gap-1 text-xs text-slate-300 flex-wrap">
                      {FLOW_HINT.map((step, i) => (
                        <span key={step} className={i === 0 ? 'text-slate-500' : ''}>
                          {step}
                        </span>
                      ))}
                    </div>

                    {imported && c.imported_mcp_server_id && (
                      <p className="mt-2 text-xs text-slate-500">
                        등록된 서버:{' '}
                        <Link
                          to={`/mcp/servers/${c.imported_mcp_server_id}`}
                          className="text-indigo-600 hover:underline"
                        >
                          /mcp/servers/{c.imported_mcp_server_id}
                        </Link>
                      </p>
                    )}
                  </div>
                );
              })
            )}
          </div>
        )}

        {!hasSearched && hasEnabledSource && (
          <div className="flex flex-col items-center py-12 text-center">
            <Search size={32} className="text-slate-300 mb-3" />
            <p className="text-sm text-slate-500">외부 MCP 레지스트리를 검색하세요.</p>
            <p className="text-xs text-slate-400 mt-1">
              검색 결과는 검토가 필요한 후보입니다. 검토·승인 후 DRAFT 서버로 가져올 수 있으며, 자동으로 연결되거나 활성화되지 않습니다.
            </p>
          </div>
        )}
      </div>

      <Dialog
        open={!!reviewTarget}
        onClose={() => {
          if (reviewSubmitting) return;
          setReviewTarget(null);
        }}
        title={reviewTarget?.decision === 'APPROVE' ? '후보 승인' : '후보 거절'}
        description={
          reviewTarget
            ? `${reviewTarget.candidate.name} — ${reviewTarget.decision === 'APPROVE' ? '승인' : '거절'}`
            : undefined
        }
        footer={
          <>
            <Button
              variant="outline"
              onClick={() => setReviewTarget(null)}
              disabled={reviewSubmitting}
            >
              취소
            </Button>
            <Button
              variant={reviewTarget?.decision === 'REJECT' ? 'danger' : 'primary'}
              onClick={() => void submitReview()}
              loading={reviewSubmitting}
              disabled={reviewSubmitting}
            >
              {reviewTarget?.decision === 'APPROVE' ? '승인 제출' : '거절 제출'}
            </Button>
          </>
        }
      >
        {reviewTarget && (
          <div className="space-y-3">
            <InlineAlert
              type="warning"
              message="후보를 승인하거나 거절해도 서버가 설치·연결·활성화되지는 않습니다. 가져오기 이후 연결 테스트와 도구 탐색을 별도로 실행해야 합니다."
            />
            <div>
              <label
                htmlFor="review-comment"
                className="block text-xs font-medium text-slate-600 mb-1"
              >
                코멘트 (선택)
              </label>
              <textarea
                id="review-comment"
                value={reviewComment}
                onChange={(e) => setReviewComment(e.target.value.slice(0, 1000))}
                maxLength={1000}
                rows={3}
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                placeholder="검토 사유를 입력할 수 있습니다."
              />
              <p className="text-xs text-slate-400 mt-1 text-right">
                {reviewComment.length}/1000
              </p>
            </div>
            {reviewError && <InlineAlert type="error" message={reviewError} />}
          </div>
        )}
      </Dialog>
    </div>
  );
}
