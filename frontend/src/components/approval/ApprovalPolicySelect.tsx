/**
 * ACTIVE ApprovalPolicy picker for ToolPolicy / Workflow APPROVAL steps.
 * Reuses /approval-policies list + detail; restores inactive/missing current refs.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  getApprovalPolicy,
  listApprovalPolicies,
  type ApprovalPolicyDto,
} from '../../api/approvalPolicies';
import { isAbortError, isApiError } from '../../api/client';
import { labelApprovalPolicyStatus, shortenId } from '../../domain';

const PAGE_SIZE = 100;

export interface ApprovalPolicySelectProps {
  value: string | null | undefined;
  onChange: (policyId: string | null) => void;
  disabled?: boolean;
  allowEmpty?: boolean;
  emptyLabel?: string;
  className?: string;
  'aria-label'?: string;
}

function optionLabel(policy: ApprovalPolicyDto, current: boolean): string {
  const base = `${policy.name} (${policy.code})`;
  if (policy.status === 'ACTIVE') return base;
  const status = labelApprovalPolicyStatus(policy.status);
  return current ? `${base} — ${status} (현재 연결)` : `${base} — ${status}`;
}

export default function ApprovalPolicySelect({
  value,
  onChange,
  disabled = false,
  allowEmpty = true,
  emptyLabel = '승인 정책 선택...',
  className,
  'aria-label': ariaLabel = '승인 정책',
}: ApprovalPolicySelectProps) {
  const selectedId = value?.trim() || null;
  const [activeItems, setActiveItems] = useState<ApprovalPolicyDto[]>([]);
  const [currentExtra, setCurrentExtra] = useState<ApprovalPolicyDto | null>(null);
  const [currentMissing, setCurrentMissing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setForbidden(false);

    listApprovalPolicies({
      page: 1,
      page_size: PAGE_SIZE,
      status: 'ACTIVE',
      sort: 'name',
      signal: controller.signal,
    })
      .then((page) => {
        if (cancelled) return;
        setActiveItems(page.items);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setActiveItems([]);
          return;
        }
        setError(
          isApiError(err) ? err.message : '승인 정책 목록을 불러오지 못했습니다.',
        );
        setActiveItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    setCurrentExtra(null);
    setCurrentMissing(false);
    if (!selectedId) return;
    if (activeItems.some((item) => item.id === selectedId)) return;
    if (loading) return;

    const controller = new AbortController();
    let cancelled = false;

    getApprovalPolicy(selectedId, controller.signal)
      .then((detail) => {
        if (cancelled) return;
        setCurrentExtra(detail);
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        setCurrentMissing(true);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [selectedId, activeItems, loading]);

  const options = useMemo(() => {
    const byId = new Map<string, ApprovalPolicyDto>();
    for (const item of activeItems) byId.set(item.id, item);
    if (currentExtra) byId.set(currentExtra.id, currentExtra);
    return Array.from(byId.values());
  }, [activeItems, currentExtra]);

  const showOrphan =
    !!selectedId &&
    !options.some((item) => item.id === selectedId) &&
    (currentMissing || (forbidden && !loading));

  return (
    <div className="space-y-1">
      <select
        aria-label={ariaLabel}
        className={
          className ??
          'w-full border border-slate-300 rounded-md px-2 py-1.5 bg-white text-sm'
        }
        value={selectedId ?? ''}
        disabled={disabled || loading || forbidden}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next ? next : null);
        }}
      >
        {allowEmpty && <option value="">{emptyLabel}</option>}
        {options.map((policy) => (
          <option key={policy.id} value={policy.id}>
            {optionLabel(policy, policy.id === selectedId)}
          </option>
        ))}
        {showOrphan && selectedId && (
          <option value={selectedId}>
            {shortenId(selectedId)} — 삭제되었거나 조회할 수 없음 (현재 연결)
          </option>
        )}
      </select>

      {loading && (
        <p className="text-[11px] text-slate-400">승인 정책 목록을 불러오는 중…</p>
      )}
      {forbidden && (
        <p className="text-[11px] text-amber-700">
          승인 정책 목록을 볼 권한이 없습니다 (`approval.policy.manage`). 기존에 연결된
          정책은 유지할 수 있지만 다른 정책으로 바꿀 수 없습니다.
        </p>
      )}
      {error && <p className="text-[11px] text-red-600">{error}</p>}
      {currentExtra && currentExtra.status !== 'ACTIVE' && (
        <p className="text-[11px] text-amber-700">
          현재 연결된 승인 정책이 {labelApprovalPolicyStatus(currentExtra.status)}{' '}
          상태입니다. 가능하면 ACTIVE 정책으로 바꾸세요.
        </p>
      )}
      {showOrphan && selectedId && !forbidden && (
        <p className="text-[11px] text-amber-700">
          연결된 승인 정책을 찾을 수 없습니다. 다른 ACTIVE 정책을 선택하세요.
        </p>
      )}
    </div>
  );
}
