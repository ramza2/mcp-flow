/** MCP Tool Verification tab — version-scoped list/create/detail. */

import { useCallback, useEffect, useRef, useState } from 'react';
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

interface Props {
  toolId: string;
  selectedVersion: MCPToolVersionDto | null;
  currentVersionId: string | null;
  active: boolean;
}

export default function ToolVerificationTab({
  toolId,
  selectedVersion,
  currentVersionId,
  active,
}: Props) {
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
  const [createStatus, setCreateStatus] = useState<'PENDING' | 'FAILED'>('PENDING');
  const [criteriaVersion, setCriteriaVersion] = useState('tool-verification-v1');
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
        setError(toFeedbackError(err, 'Verification 목록을 불러오지 못했습니다.'));
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
          setDetailError(toFeedbackError(err, 'Verification 상세를 불러오지 못했습니다.'));
        })
        .finally(() => {
          if (detailRequestRef.current === controller && mountedRef.current) {
            setDetailLoading(false);
          }
        });
    },
    [toolId, versionId],
  );

  const handleCreate = async () => {
    if (!versionId) return;
    setCreateError(null);
    const criteria = criteriaVersion.trim();
    if (!criteria) {
      setCreateError('criteria_version is required.');
      return;
    }

    let resultSummary: Record<string, JsonValue> | null = null;
    if (createStatus === 'FAILED') {
      const trimmed = resultSummaryText.trim();
      if (!trimmed) {
        setCreateError('FAILED requires result_summary JSON object.');
        return;
      }
      try {
        const parsed: unknown = JSON.parse(trimmed);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          setCreateError('result_summary must be a JSON object.');
          return;
        }
        resultSummary = parsed as Record<string, JsonValue>;
      } catch {
        setCreateError('result_summary is not valid JSON.');
        return;
      }
    }

    const body: ToolVerificationCreateRequest = {
      status: createStatus,
      criteria_version: criteria,
      result_summary: resultSummary,
    };

    setCreating(true);
    try {
      await createToolVerification(toolId, versionId, body);
      if (!mountedRef.current) return;
      setCreateOpen(false);
      setResultSummaryText('');
      setCreateStatus('PENDING');
      setPage(1);
      loadList();
    } catch (err) {
      if (!mountedRef.current) return;
      setCreateError(toFeedbackError(err, 'Verification 생성에 실패했습니다.').message);
    } finally {
      if (mountedRef.current) setCreating(false);
    }
  };

  if (!active) return null;

  if (!selectedVersion || !versionId) {
    return <EmptyState title="선택된 ToolVersion이 없습니다" />;
  }

  const isCurrent = currentVersionId === versionId;
  const expiredByTimestamp =
    detail?.status === 'VERIFIED' &&
    detail.expires_at &&
    new Date(detail.expires_at).getTime() < Date.now();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-800">
            v{selectedVersion.version_no} · {isCurrent ? 'Current' : 'Historical'}
          </p>
          <p className="text-xs text-slate-400">
            Verification은 선택한 ToolVersion에만 귀속됩니다. Tool 전역 Verified badge는 표시하지
            않습니다.
          </p>
        </div>
        <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>
          Record Verification
        </Button>
      </div>

      <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
        VERIFIED evidence 생성은 Manual Tool Test와 증빙 업로드 연동 후 지원됩니다. 이번 화면에서는
        PENDING / FAILED 기록만 생성할 수 있습니다.
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
        <EmptyState title="Verification 이력이 없습니다" description="PENDING 또는 FAILED 증적을 기록할 수 있습니다." />
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
                    Verified at {formatTimestamp(item.verified_at)} · By{' '}
                    {item.verified_by ? shortenId(item.verified_by) : 'Not recorded'}
                  </p>
                </div>
                <div className="text-right text-xs text-slate-400 space-y-0.5">
                  <p>Expires {item.expires_at ? formatTimestamp(item.expires_at) : '—'}</p>
                  <p>Exec {item.test_execution_id ? shortenId(item.test_execution_id) : '—'}</p>
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
          <h3 className="text-sm font-semibold text-slate-800">Verification Detail</h3>
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
              <Row label="Status">
                <VerificationBadge status={detail.status} />
                {expiredByTimestamp && (
                  <span className="text-xs text-amber-700 ml-2">Expired by timestamp</span>
                )}
              </Row>
              <Row label="Criteria">{detail.criteria_version}</Row>
              <Row label="Verified at">{formatTimestamp(detail.verified_at)}</Row>
              <Row label="Verified by">
                {detail.verified_by ? shortenId(detail.verified_by) : 'Not recorded'}
              </Row>
              <Row label="Test execution">
                {detail.test_execution_id ? (
                  <span className="font-mono text-xs">{detail.test_execution_id}</span>
                ) : (
                  '—'
                )}
              </Row>
              <Row label="Evidence blob">
                {detail.evidence_blob_id ? (
                  <span className="font-mono text-xs">{detail.evidence_blob_id}</span>
                ) : (
                  '—'
                )}
              </Row>
              <Row label="Expires at">
                {detail.expires_at ? formatTimestamp(detail.expires_at) : '—'}
              </Row>
              {detail.result_summary && (
                <div>
                  <p className="text-xs text-slate-400 mb-1">result_summary</p>
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
        title="Record Verification"
        description="PENDING / FAILED only. VERIFIED create is deferred."
        footer={
          <>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={creating}>
              취소
            </Button>
            <Button variant="primary" loading={creating} onClick={() => void handleCreate()}>
              Create
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-sm">
          {createError && <InlineAlert type="error" message={createError} />}
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">status</span>
            <select
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={createStatus}
              onChange={e => setCreateStatus(e.target.value as 'PENDING' | 'FAILED')}
            >
              <option value="PENDING">PENDING</option>
              <option value="FAILED">FAILED</option>
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">criteria_version</span>
            <input
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={criteriaVersion}
              onChange={e => setCriteriaVersion(e.target.value)}
            />
          </label>
          {createStatus === 'FAILED' && (
            <label className="block space-y-1">
              <span className="text-xs text-slate-500">result_summary (JSON object)</span>
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
