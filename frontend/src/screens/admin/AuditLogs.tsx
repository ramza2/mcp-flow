import { Fragment, useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import {
  EmptyState,
  ErrorState,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import {
  getAuditEvent,
  listAuditEvents,
  type AuditEventDetailDto,
  type AuditEventListItemDto,
} from '../../api/audit';
import { isAbortError, isApiError } from '../../api/client';
import {
  AUDIT_ACTOR_TYPES,
  AUDIT_RESULTS,
  formatTimestamp,
  labelAuditActorType,
  labelAuditResult,
  shortenId,
} from '../../domain';

const PAGE_LIMIT = 50;

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <div>
      <p className="font-semibold text-slate-500 mb-1">{title}</p>
      <pre className="font-mono bg-white rounded p-2 border border-slate-200 text-slate-600 overflow-x-auto max-h-48">
        {value == null ? 'null' : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

export default function AuditLogs() {
  const [items, setItems] = useState<AuditEventListItemDto[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null]);
  const [cursorIndex, setCursorIndex] = useState(0);
  const [resultFilter, setResultFilter] = useState('');
  const [actorTypeFilter, setActorTypeFilter] = useState('');
  const [qFilter, setQFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditEventDetailDto | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const currentCursor = cursorStack[cursorIndex] ?? null;

  const load = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setForbidden(false);

    listAuditEvents({
      cursor: currentCursor ?? undefined,
      limit: PAGE_LIMIT,
      q: qFilter || undefined,
      result: resultFilter || undefined,
      actor_type: actorTypeFilter || undefined,
      signal: controller.signal,
    })
      .then((data) => {
        if (cancelled) return;
        setItems(data.items);
        setNextCursor(data.next_cursor);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setItems([]);
          setNextCursor(null);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Audit 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setItems([]);
        setNextCursor(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [currentCursor, resultFilter, actorTypeFilter, qFilter]);

  useEffect(() => load(), [load]);

  useEffect(() => {
    if (!expanded) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    setDetailError(null);
    getAuditEvent(expanded, controller.signal)
      .then((d) => setDetail(d))
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        if (isApiError(err) && err.status === 403) {
          setDetailError('권한이 없습니다.');
          return;
        }
        setDetailError(isApiError(err) ? err.message : '상세를 불러오지 못했습니다.');
      })
      .finally(() => setDetailLoading(false));
    return () => controller.abort();
  }, [expanded]);

  const resetPagination = () => {
    setCursorStack([null]);
    setCursorIndex(0);
    setExpanded(null);
  };

  const goNext = () => {
    if (!nextCursor) return;
    setCursorStack((stack) => {
      const trimmed = stack.slice(0, cursorIndex + 1);
      return [...trimmed, nextCursor];
    });
    setCursorIndex((i) => i + 1);
    setExpanded(null);
  };

  const goPrev = () => {
    if (cursorIndex <= 0) return;
    setCursorIndex((i) => i - 1);
    setExpanded(null);
  };

  return (
    <div>
      <PageHeader
        title="Audit Logs"
        description="시스템 전체 감사 로그를 검색하고 조회합니다."
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="Actor, Action, Resource, Request ID 검색..."
          onSearch={(q) => {
            setQFilter(q.trim());
            resetPagination();
          }}
          filters={[
            {
              key: 'result',
              label: 'Result',
              options: AUDIT_RESULTS.map((v) => ({
                value: v,
                label: labelAuditResult(v),
              })),
            },
            {
              key: 'actor_type',
              label: 'Actor type',
              options: AUDIT_ACTOR_TYPES.map((v) => ({
                value: v,
                label: labelAuditActorType(v),
              })),
            },
          ]}
          onFilter={(key, value) => {
            resetPagination();
            if (key === 'result') setResultFilter(value);
            if (key === 'actor_type') setActorTypeFilter(value);
          }}
        />

        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {forbidden ? (
            <PermissionDenied />
          ) : loading ? (
            <LoadingSkeleton rows={8} />
          ) : error ? (
            <ErrorState
              message={error.message}
              requestId={error.requestId}
              onRetry={() => load()}
            />
          ) : items.length === 0 ? (
            <EmptyState title="Audit event가 없습니다." />
          ) : (
            <>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50">
                    {['Time', 'Actor', 'Action', 'Resource', 'Result', 'Request ID'].map(
                      (h) => (
                        <th
                          key={h}
                          className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide"
                        >
                          {h}
                        </th>
                      ),
                    )}
                    <th className="px-4 py-3 w-8" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((log) => (
                    <Fragment key={log.event_id}>
                      <tr
                        className="border-b border-slate-100 hover:bg-slate-50 cursor-pointer transition-colors"
                        onClick={() =>
                          setExpanded(expanded === log.event_id ? null : log.event_id)
                        }
                      >
                        <td className="px-4 py-3 font-mono text-xs text-slate-500 whitespace-nowrap">
                          {formatTimestamp(log.occurred_at)}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs text-slate-700">
                          {log.actor_type}
                          {log.actor_id ? ` · ${shortenId(log.actor_id, 8)}` : ''}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs text-indigo-600">
                          {log.action}
                        </td>
                        <td className="px-4 py-3 text-slate-700">
                          {log.resource_type ?? '—'}
                          {log.resource_id ? (
                            <span className="font-mono text-xs text-slate-400 ml-1">
                              {shortenId(log.resource_id, 8)}
                            </span>
                          ) : null}
                        </td>
                        <td className="px-4 py-3">
                          <span
                            className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                              log.result === 'SUCCESS'
                                ? 'bg-green-50 text-green-700'
                                : log.result === 'DENIED'
                                  ? 'bg-amber-50 text-amber-700'
                                  : 'bg-red-50 text-red-700'
                            }`}
                          >
                            {log.result}
                          </span>
                        </td>
                        <td className="px-4 py-3 font-mono text-xs text-slate-400">
                          {log.request_id ?? '—'}
                        </td>
                        <td className="px-4 py-3 text-slate-400">
                          {expanded === log.event_id ? (
                            <ChevronDown size={13} />
                          ) : (
                            <ChevronRight size={13} />
                          )}
                        </td>
                      </tr>
                      {expanded === log.event_id && (
                        <tr className="border-b border-slate-100 bg-slate-50">
                          <td colSpan={7} className="px-4 py-3">
                            {detailLoading ? (
                              <LoadingSkeleton rows={2} />
                            ) : detailError ? (
                              <p className="text-sm text-red-600">{detailError}</p>
                            ) : detail ? (
                              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                                <JsonBlock title="Before" value={detail.before_data} />
                                <JsonBlock title="After" value={detail.after_data} />
                                <JsonBlock title="Change set" value={detail.change_set} />
                              </div>
                            ) : null}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
              <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-slate-200">
                <p className="text-xs text-slate-500">
                  page {cursorIndex + 1} · {items.length}건
                </p>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={cursorIndex <= 0 || loading}
                    onClick={goPrev}
                  >
                    Previous
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!nextCursor || loading}
                    onClick={goNext}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
