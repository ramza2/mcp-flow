/**
 * Approval query / pending inbox — GET /api/v1/approvals (docs/06 §13).
 * Permission: current DB `approval.decide` (no separate approval.read).
 * Phase 1: read-only list/detail. POST decisions are out of scope.
 */

import type { ApprovalStatus } from '../domain/types';
import { apiRequest } from './client';
import type { JsonValue } from './types';

export type ApprovalDecisionMode = 'ANY' | 'ALL' | 'QUORUM';

export type ApprovalDecisionValue = 'APPROVE' | 'REJECT';

export type ApprovalSort =
  | 'requested_at'
  | '-requested_at'
  | 'expires_at'
  | '-expires_at';

export interface ApprovalListItemDto {
  id: string;
  status: ApprovalStatus | string;
  execution_id: string;
  step_execution_id: string;
  requested_by: string;
  decision_mode: ApprovalDecisionMode | string;
  required_approvals: number;
  requested_at: string;
  expires_at: string;
  resolved_at: string | null;
  approve_count: number;
  reject_count: number;
  can_decide: boolean;
}

export interface ApprovalListDto {
  items: ApprovalListItemDto[];
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export interface ApprovalDecisionHistoryDto {
  decision_id: string;
  decided_by: string;
  decision: ApprovalDecisionValue | string;
  comment: string | null;
  decided_at: string;
}

export interface ApprovalDetailDto extends ApprovalListItemDto {
  /** Masked historical projection only — never raw context_snapshot. */
  safe_context: Record<string, JsonValue>;
  decisions: ApprovalDecisionHistoryDto[];
}

export interface ApprovalListParams {
  page?: number;
  page_size?: number;
  /** Default PENDING actionable inbox on the server. */
  status?: ApprovalStatus;
  execution_id?: string;
  requested_by?: string;
  sort?: ApprovalSort;
  signal?: AbortSignal;
}

export function listApprovals(params: ApprovalListParams = {}) {
  return apiRequest<ApprovalListDto>('/approvals', {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      status: params.status,
      execution_id: params.execution_id,
      requested_by: params.requested_by,
      sort: params.sort,
    },
    signal: params.signal,
  });
}

export function getApproval(approvalId: string, signal?: AbortSignal) {
  return apiRequest<ApprovalDetailDto>(`/approvals/${approvalId}`, { signal });
}
