/** MCP Tool Verification tab — version-scoped list/create/detail (FNC-TOOL-006). */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  createToolVerification,
  getToolVerification,
  listToolVerifications,
} from '../../api/mcp';
import { isAbortError } from '../../api/client';
import type {
  JsonValue,
  MCPToolVersionDto,
  ToolVerificationCreateRequest,
  ToolVerificationDto,
} from '../../api/types';
import { formatTimestamp, shortenId } from '../../domain';
import { VerificationBadge } from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import Pagination from '../../components/ui/Pagination';
import { EmptyState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import JsonViewer from '../../components/ui/JsonViewer';
import { toFeedbackError, type FeedbackError } from './toolLifecycle';

const PAGE_SIZE = 20;

type CreateStatus = 'PENDING' | 'FAILED' | 'VERIFIED';

interface Props {
  toolId: string;
  selectedVersion: MCPToolVersionDto | null;
  currentVersionId: string | null;
  active: boolean;
}

function checklistFlag(summary: Record<string, JsonValue> | null, key: string): boolean | null {
  if (!summary || !(key in summary)) return null;
  const value = summary[key];
  return typeof value === 'boolean' ? value : null;
}

export default function ToolVerificationTab({
  toolId,
  selectedVersion,
  currentVersionId,
  active,
}: Props) {
  const navigate = useNavigate();
  const mountedRef = useRef(true);
  const listRequestRef = useRef<AbortController | null>(null);
  const detailRequestRef = useRef<AbortController | null>(null);
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<ToolVerificationDto[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<FeedbackError | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createStatus, setCreateStatus] = useState<CreateStatus>('PENDING');
  const [criteriaVersion, setCriteriaVersion] = useState('tool-verification-v1');
  const [testExecutionId, setTestExecutionId] = useState('');
  const [errorTestExecutionId, setErrorTestExecutionId] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [resultSummaryText, setResultSummaryText] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ToolVerificationDto | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<FeedbackError | null>(null);

  const versionId = selectedVersion?.id ?? null;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      listRequestRef.current?.abort();
      detailRequestRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    setPage(1);
    setItems([]);
    setSelectedId(null);
    setDetail(null);
    setError(null);
  }, [versionId]);

  const loadList = useCallback(() => {
    if (!versionId) return;
    listRequestRef.current?.abort();
    const controller = new AbortController();
    listRequestRef.current = controller;
    setLoading(true);
    setError(null);

    listToolVerifications(toolId, versionId, {
      page,
      page_size: PAGE_SIZE,
      signal: controller.signal,
    })
      .then(data => {
        if (listRequestRef.current !== controller || !mountedRef.current) return;
        setItems(data.items);
        setTotal(data.total);
        setHasNext(data.has_next);
      })
      .catch(err => {
        if (
          isAbortError(err) ||
          controller.signal.aborted ||
          listRequestRef.current !== controller ||
          !mountedRef.current
        ) {
          return;
        }
        setItems([]);
        setTotal(0);
        setHasNext(false);
        setError(toFeedbackError(err, '도구 검증 이력을 불러오지 못했습니다.'));
      })
      .finally(() => {
        if (listRequestRef.current === controller && mountedRef.current) {
          setLoading(false);
        }
      });
  }, [toolId, versionId, page]);

  useEffect(() => {
    if (!active || !versionId) return;
    loadList();
  }, [active, versionId, loadList]);

  const loadDetail = useCallback(
    (verificationId: string) => {
      if (!versionId) return;
      detailRequestRef.current?.abort();
      const controller = new AbortController();
      detailRequestRef.current = controller;
      setSelectedId(verificationId);
      setDetailLoading(true);
      setDetailError(null);

      getToolVerification(toolId, versionId, verificationId, controller.signal)
        .then(data => {
          if (detailRequestRef.current !== controller || !mountedRef.current) return;
          setDetail(data);
        })
        .catch(err => {
          if (
            isAbortError(err) ||
            controller.signal.aborted ||
            detailRequestRef.current !== controller ||
            !mountedRef.current
          ) {
            return;
          }
          setDetail(null);
          setDetailError(toFeedbackError(err, '도구 검증 상세를 불러오지 못했습니다.'));
        })
        .finally(() => {
          if (detailRequestRef.current === controller && mountedRef.current) {
            setDetailLoading(false);
          }
        });
    },
    [toolId, versionId],
  );

  const resetCreateForm = () => {
    setCreateStatus('PENDING');
    setCriteriaVersion('tool-verification-v1');
    setTestExecutionId('');
    setErrorTestExecutionId('');
    setExpiresAt('');
    setResultSummaryText('');
    setCreateError(null);
  };

  const handleCreate = async () => {
    if (!versionId) return;
    setCreateError(null);
    const criteria = criteriaVersion.trim();
    if (!criteria) {
      setCreateError('검증 기준 버전을 입력하세요.');
      return;
    }

    const body: ToolVerificationCreateRequest = {
      status: createStatus,
      criteria_version: criteria,
    };

    if (createStatus === 'VERIFIED') {
      const normalId = testExecutionId.trim();
      const errorId = errorTestExecutionId.trim();
      if (!normalId) {
        setCreateError('정상 실행 테스트의 Execution ID가 필요합니다.');
        return;
      }
      if (!errorId) {
        setCreateError('실패 또는 시간 초과 실행 테스트의 Execution ID가 필요합니다.');
        return;
      }
      body.test_execution_id = normalId;
      body.error_test_execution_id = errorId;
      const expires = expiresAt.trim();
      if (expires) {
        body.expires_at = new Date(expires).toISOString();
      }
    } else if (createStatus === 'FAILED') {
      const trimmed = resultSummaryText.trim();
      if (!trimmed) {
        setCreateError('실패 기록에는 결과 요약 JSON 객체가 필요합니다.');
        return;
      }
      try {
        const parsed: unknown = JSON.parse(trimmed);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          setCreateError('결과 요약은 JSON 객체여야 합니다.');
          return;
        }
        body.result_summary = parsed as Record<string, JsonValue>;
      } catch {
        setCreateError('결과 요약의 JSON 형식이 올바르지 않습니다.');
        return;
      }
    }

    setCreating(true);
    try {
      const created = await createToolVerification(toolId, versionId, body);
      if (!mountedRef.current) return;
      setCreateOpen(false);
      resetCreateForm();
      setPage(1);
      loadList();
      loadDetail(created.id);
    } catch (err) {
      if (!mountedRef.current) return;
      setCreateError(toFeedbackError(err, '도구 검증 기록 생성에 실패했습니다.').message);
    } finally {
      if (mountedRef.current) setCreating(false);
    }
  };

  if (!active) return null;

  if (!selectedVersion || !versionId) {
    return <EmptyState title="선택된 도구 버전이 없습니다" />;
  }

  const isCurrent = currentVersionId === versionId;
  const expiredByTimestamp =
    detail?.status === 'VERIFIED' &&
    detail.expires_at &&
    new Date(detail.expires_at).getTime() < Date.now();
  const schemaValid = checklistFlag(detail?.result_summary ?? null, 'schema_valid');
  const normalPassed = checklistFlag(detail?.result_summary ?? null, 'normal_call_passed');
  const errorChecked = checklistFlag(detail?.result_summary ?? null, 'error_handling_checked');
  const errorExecId =
    detail?.result_summary && typeof detail.result_summary.error_test_execution_id === 'string'
      ? detail.result_summary.error_test_execution_id
      : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-800">
            v{selectedVersion.version_no} · {isCurrent ? '현재 버전' : '이전 버전'}
          </p>
          <p className="text-xs text-slate-400">
            검증 기록은 선택한 도구 버전에만 적용됩니다. VERIFIED 판정은 서버가 수동 실행 테스트의
            저장된 증빙과 객체 저장소 보고서를 확인해 결정합니다.
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={() => {
            resetCreateForm();
            setCreateOpen(true);
          }}
        >
          검증 기록 생성
        </Button>
      </div>

      <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
        VERIFIED 검증을 생성하려면 동일 도구 버전에서 성공한 수동 실행 테스트(SUCCEEDED)와
        별도의 실패 또는 시간 초과 테스트(FAILED/TIMED_OUT)가 필요합니다.
        결과 요약과 증빙 파일은 서버에서 생성하며, 화면에서 임의로 입력하지 않습니다.
      </div>

      {error && (
        <div className="space-y-1">
          <InlineAlert type="error" message={error.message} />
          {error.requestId && (
            <p className="font-mono text-xs text-slate-400">Request ID: {error.requestId}</p>
          )}
          <Button variant="outline" size="sm" onClick={loadList}>
            다시 시도
          </Button>
        </div>
      )}

      {loading ? (
        <LoadingSkeleton rows={4} />
      ) : !error && items.length === 0 ? (
        <EmptyState
          title="검증 이력이 없습니다"
          description="대기·실패 기록을 남기거나, 필요한 실행 테스트 증빙을 확보한 후 검증 완료 기록을 생성할 수 있습니다."
        />
      ) : (
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {items.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => loadDetail(item.id)}
              className={`w-full text-left px-4 py-3 border-b last:border-0 hover:bg-slate-50 ${
                selectedId === item.id ? 'bg-indigo-50' : ''
              }`}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <VerificationBadge status={item.status} />
                    <span className="text-xs font-mono text-slate-500">{item.criteria_version}</span>
                  </div>
                  <p className="text-xs text-slate-400">
                    검증 일시 {formatTimestamp(item.verified_at)} · 검증자{' '}
                    {item.verified_by ? shortenId(item.verified_by) : '기록 없음'}
                  </p>
                </div>
                <div className="text-right text-xs text-slate-400 space-y-0.5">
                  <p>만료 시각 {item.expires_at ? formatTimestamp(item.expires_at) : '—'}</p>
                  <p>실행 ID {item.test_execution_id ? shortenId(item.test_execution_id) : '—'}</p>
                </div>
              </div>
            </button>
          ))}
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            hasNext={hasNext}
            onPageChange={setPage}
            disabled={loading}
          />
        </div>
      )}

      {(detailLoading || detail || detailError) && (
        <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
          <h3 className="text-sm font-semibold text-slate-800">검증 상세</h3>
          {detailLoading && <LoadingSkeleton rows={3} />}
          {detailError && (
            <div className="space-y-1">
              <InlineAlert type="error" message={detailError.message} />
              {detailError.requestId && (
                <p className="font-mono text-xs text-slate-400">
                  Request ID: {detailError.requestId}
                </p>
              )}
            </div>
          )}
          {detail && !detailLoading && (
            <div className="space-y-2 text-sm">
              <Row label="상태">
                <VerificationBadge status={detail.status} />
                {expiredByTimestamp && (
                  <span className="text-xs text-amber-700 ml-2">유효 기간 만료</span>
                )}
              </Row>
              <Row label="검증 기준">{detail.criteria_version}</Row>
              <Row label="검증 일시">{formatTimestamp(detail.verified_at)}</Row>
              <Row label="검증자">
                {detail.verified_by ? (
                  <span className="font-mono text-xs">{detail.verified_by}</span>
                ) : (
                  '기록 없음'
                )}
              </Row>
              <Row label="정상 실행 테스트">
                {detail.test_execution_id ? (
                  <button
                    type="button"
                    className="font-mono text-xs text-indigo-600 hover:underline"
                    onClick={() => navigate(`/executions/${detail.test_execution_id}`)}
                  >
                    {detail.test_execution_id}
                  </button>
                ) : (
                  '—'
                )}
              </Row>
              <Row label="오류 실행 테스트">
                {errorExecId ? (
                  <button
                    type="button"
                    className="font-mono text-xs text-indigo-600 hover:underline"
                    onClick={() => navigate(`/executions/${errorExecId}`)}
                  >
                    {errorExecId}
                  </button>
                ) : (
                  '—'
                )}
              </Row>
              <Row label="증빙 파일 ID">
                {detail.evidence_blob_id ? (
                  <span className="font-mono text-xs">{detail.evidence_blob_id}</span>
                ) : (
                  '—'
                )}
              </Row>
              <Row label="만료 시각">
                {detail.expires_at ? formatTimestamp(detail.expires_at) : '—'}
              </Row>
              {detail.status === 'VERIFIED' && (
                <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700 space-y-1">
                  <p className="font-semibold text-slate-800">서버 판정 체크리스트</p>
                  <p>schema_valid: {schemaValid == null ? '—' : String(schemaValid)}</p>
                  <p>normal_call_passed: {normalPassed == null ? '—' : String(normalPassed)}</p>
                  <p>
                    error_handling_checked:{' '}
                    {errorChecked == null ? '—' : String(errorChecked)}
                  </p>
                </div>
              )}
              {detail.result_summary && (
                <div>
                  <p className="text-xs text-slate-400 mb-1">결과 요약 (result_summary)</p>
                  <JsonViewer value={detail.result_summary} />
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <Dialog
        open={createOpen}
        onClose={() => !creating && setCreateOpen(false)}
        title="검증 기록 생성"
        description="검증 완료(VERIFIED)는 서버가 수동 실행 테스트 증빙을 검토해 판정합니다. 결과 요약과 증빙 파일은 직접 입력하지 않습니다."
        footer={
          <>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={creating}>
              취소
            </Button>
            <Button variant="primary" loading={creating} onClick={() => void handleCreate()}>
              {createStatus === 'VERIFIED' ? '검증 완료 기록 생성' : '기록 생성'}
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-sm">
          {createError && <InlineAlert type="error" message={createError} />}
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">기록 상태</span>
            <select
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={createStatus}
              onChange={e => setCreateStatus(e.target.value as CreateStatus)}
            >
              <option value="PENDING">대기 (PENDING)</option>
              <option value="FAILED">실패 (FAILED)</option>
              <option value="VERIFIED">검증 완료 (VERIFIED)</option>
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">검증 기준 버전</span>
            <input
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={criteriaVersion}
              onChange={e => setCriteriaVersion(e.target.value)}
            />
          </label>
          {createStatus === 'VERIFIED' && (
            <>
              <label className="block space-y-1">
                <span className="text-xs text-slate-500">
                  정상 실행 테스트 ID (SUCCEEDED)
                </span>
                <input
                  className="w-full border border-slate-300 rounded-md px-2 py-1.5 font-mono text-xs"
                  value={testExecutionId}
                  onChange={e => setTestExecutionId(e.target.value)}
                  placeholder="uuid of SUCCEEDED MANUAL_TOOL_TEST"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs text-slate-500">
                  실패·시간 초과 테스트 ID
                </span>
                <input
                  className="w-full border border-slate-300 rounded-md px-2 py-1.5 font-mono text-xs"
                  value={errorTestExecutionId}
                  onChange={e => setErrorTestExecutionId(e.target.value)}
                  placeholder="uuid of FAILED or TIMED_OUT MANUAL_TOOL_TEST"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs text-slate-500">만료 시각 (선택)</span>
                <input
                  type="datetime-local"
                  className="w-full border border-slate-300 rounded-md px-2 py-1.5"
                  value={expiresAt}
                  onChange={e => setExpiresAt(e.target.value)}
                />
              </label>
            </>
          )}
          {createStatus === 'FAILED' && (
            <label className="block space-y-1">
              <span className="text-xs text-slate-500">실패 사유 요약 (JSON 객체)</span>
              <textarea
                className="w-full border border-slate-300 rounded-md px-2 py-1.5 font-mono text-xs min-h-[96px]"
                value={resultSummaryText}
                onChange={e => setResultSummaryText(e.target.value)}
                placeholder='{"schema_valid": false, "reason": "Manual review failed"}'
              />
            </label>
          )}
        </div>
      </Dialog>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-4">
      <span className="text-slate-400 w-32 shrink-0 text-xs">{label}</span>
      <div className="text-slate-700">{children}</div>
    </div>
  );
}
